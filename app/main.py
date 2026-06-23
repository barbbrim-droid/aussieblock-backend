"""Aussieblock Ready Mix — backend API.

Run it:
    uvicorn app.main:app --reload

Then open the interactive docs:
    http://localhost:8000/docs

Every endpoint below returns JSON in the exact shape the customer app expects,
so wiring the front-end to it later is a drop-in.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from typing import List, Optional
import glob
import json
import os
import shutil
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, status, BackgroundTasks, UploadFile, File, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlmodel import Session, select

from datetime import date, datetime, timedelta
try:
    from zoneinfo import ZoneInfo
    _BIZ_TZ = ZoneInfo("America/Chicago")   # San Angelo is US Central
except Exception:   # noqa: BLE001 — zoneinfo/tzdata missing → fall back to slack below
    _BIZ_TZ = None

from .db import init_db, get_session
from .seed import seed_if_empty
from .models import Customer, Truck, Order, PlusLoadRequest, User, Invoice, Doc, Load, FuelTransaction, Material, MaterialReceipt, MixDesign, MixerReading, PurchaseOrder, Driver
from .auth import (
    verify_password, hash_password, create_access_token, get_current_user, require_staff, require_finance,
    require_driver,
)


class CustomerLoginIn(BaseModel):
    """Body for creating/resetting a customer's login (staff action)."""
    email: str
    password: str
    phone: str = ""               # optional — updates the customer's contact phone (used for the invite text)


class StaffLoginIn(BaseModel):
    """Body for creating/resetting a login. role: 'staff' = the dispatch operator
    (full board); 'worker' = a customer's field person, scoped to ONE company
    (their orders + tracking only, no billing, no board)."""
    email: str
    password: str = ""            # required for a NEW login; blank when editing = keep current password
    role: str = "worker"
    phone: str = ""               # worker's cell, so they can be texted their login
    customer_id: int | None = None  # REQUIRED for a worker — the company they work for (scopes what they see)
    project: str = ""             # their current project/job (label only)
    company: str = ""             # for a 'driver' login: the driver's name (matches Order.driver)


class OrderIn(BaseModel):
    """Body for scheduling a new order (staff action). `truck` is optional — an
    order starts 'scheduled' and a truck can be assigned later from the board."""
    customer_id: int
    site: str
    mix: str
    qty: str
    scheduled_for: str            # date the customer wants it (e.g. "2026-06-10")
    time: str = ""                # delivery time (e.g. "9:30 AM" or "08:00")
    truck: str | None = None      # optional truck label to assign now
    driver: str = ""              # optional driver name to assign now
    notes: str = ""               # delivery instructions (optional)
    slump: str = ""
    admixtures: list[str] = []
    use_for: str = ""
    project: str = ""             # optional project / job name


class OrderRequestIn(BaseModel):
    """Body for a customer placing an order from the app (becomes 'requested')."""
    site: str
    mix: str
    qty: str
    scheduled_for: str
    time: str = ""
    notes: str = ""
    slump: str = ""
    admixtures: list[str] = []
    use_for: str = ""
    project: str = ""             # optional project / job name


class TruckIn(BaseModel):
    """Body for adding/updating a truck (staff action). `gps_device_id` is the
    One Step GPS device id used to match live positions; `fluidsecure_vehicle_id`
    is the FluidSecure vehicle number used to attach fuel fills — both optional."""
    label: str
    gps_device_id: str | None = None
    fluidsecure_vehicle_id: str | None = None
    notes: str = ""


class TextInviteIn(BaseModel):
    """Body for sending a customer an invite text via the texting service."""
    message: str


class CodIn(BaseModel):
    cod: bool


class ChargeIn(BaseModel):
    amount: float | None = None
from .integrations.onestep_gps import (
    gps_poll_loop, arrival_pending, learn_site_location,
    pin_job_location, pin_load_job_location,
)
from .integrations.fluidsecure import fuel_poll_loop, ingest_csv as ingest_fuel_csv, veh_keys
from .integrations.fuel_email import fuel_email_loop
from .integrations.moby_mix_csv import import_orders_from_csv
from .integrations.quickbooks import (
    get_billing_for_customer, sync_ar_from_quickbooks, qbo_sync_loop,
    import_customers_from_quickbooks, backfill_customer_emails, get_invoice_pay_link,
    create_cod_invoice, cod_link_from_existing, cod_invoice_status,
)
from .integrations.sms import send_sms
from .ticketgen import convert as ticket_convert
from . import pricing
from . import config
from . import mixer


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_if_empty()
    tasks = [
        asyncio.create_task(gps_poll_loop()),   # live truck updates
        asyncio.create_task(qbo_sync_loop()),   # periodic QuickBooks A/R sync
    ]
    # FluidSecure retired 2026-06-23 — fuel now comes from the on-truck ESP32
    # meters via POST /api/fuel/fill. The poll/email loops are no longer started.
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Aussieblock Ready Mix API", version="0.1.0", lifespan=lifespan)

# Allow the front-end (running on another port) to call this during development.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# Mixer-drum telemetry (device-authenticated, separate from the dispatch flow).
app.include_router(mixer.router)


LOAD_SIZE_YD = 10.0   # one truck-load; orders over this are continuous pours, split into loads


def _split_loads(qty: str):
    """Yards per load for a pour. '80' -> [10]*8; '25' -> [10,10,5]. Returns [] for a
    single-truck order (<= 10 yd)."""
    total = pricing._num(qty)
    if total <= LOAD_SIZE_YD + 1e-9:
        return []
    loads, rem = [], total
    while rem > LOAD_SIZE_YD + 1e-9:
        loads.append(LOAD_SIZE_YD); rem -= LOAD_SIZE_YD
    loads.append(round(rem, 2))
    return loads


def _create_loads(order: Order, s: Session):
    """Create the per-load rows for a continuous pour (no-op for single-load orders)."""
    sizes = _split_loads(order.qty)
    for i, q in enumerate(sizes, start=1):
        s.add(Load(order_id=order.id, seq=i, qty=("%g" % q), status="scheduled"))
    if sizes:
        s.commit()


def _rollup_pour(o: Order, s: Session):
    """Roll a pour's progress up from its loads (progress = delivered / ordered).

    Pours are NOT auto-completed: a big pour can take more loads than the ordered
    estimate, so finishing it is a deliberate staff action (set "complete" on the
    pour). We only keep progress current and mark it "ongoing" while in flight. A
    pour staff have already marked complete stays complete through later load edits
    (e.g. correcting a load's yards)."""
    loads = s.exec(select(Load).where(Load.order_id == o.id)).all()
    if not loads:
        return   # no loads added yet — leave the order as scheduled
    if o.status == "complete":
        return   # staff completed it manually — don't reopen on a load edit
    total = pricing._num(o.qty)
    delivered = sum(pricing._num(ld.qty) for ld in loads if ld.status == "complete")
    # A pour with loads in flight is "ongoing" (umbrella status) — each truck
    # carries its own per-load status. Progress is capped at 100% even if the
    # delivered yards run past the ordered estimate.
    o.status = "ongoing"
    o.progress = min(1.0, round(delivered / total, 3)) if total else 0.0
    s.add(o); s.commit()


def _load_ticket_prefix(ref: str, seq: int) -> str:
    """Files for a pour load's own batch ticket live alongside the order's, but
    namespaced by load (e.g. AB1042_L2.pdf, AB1042_L2_original.jpg)."""
    return f"{ref}_L{seq}"


def _load_json(ld: Load, s: Session, ref: str) -> dict:
    t = s.get(Truck, ld.truck_id) if ld.truck_id else None
    prefix = _load_ticket_prefix(ref, ld.seq)
    has_orig = bool(ld.batch_ticket == f"{prefix}.pdf"
                    and glob.glob(os.path.join(_batch_ticket_dir(), f"{prefix}_original.*")))
    return {
        "seq": ld.seq, "qty": ld.qty,
        "truck": t.label if t else "—", "driver": ld.driver or "—",
        "status": ld.status, "progress": round(ld.progress, 3),
        "has_batch_ticket": bool(ld.batch_ticket),
        "has_original": has_orig,
        "signed_by": ld.signed_by, "signed_at": ld.signed_at,
        "water_added": ld.water_added, "has_signature": bool(ld.signature),
        "truck_position": ({"lat": t.lat, "lng": t.lng, "heading": t.heading}
                           if t and t.lat is not None else None),
    }


def _order_json(o: Order, s: Session) -> dict:
    truck = s.get(Truck, o.truck_id) if o.truck_id else None
    customer = s.get(Customer, o.customer_id) if o.customer_id else None
    loads = s.exec(select(Load).where(Load.order_id == o.id).order_by(Load.seq)).all()
    return {
        "ref": o.ref,
        "customer": customer.name if customer else None,
        "site": o.site,
        "mix": o.mix,
        "qty": o.qty,
        "when": o.scheduled_for,
        "time": o.time,
        "status": o.status,
        "truck": truck.label if truck else "—",
        "driver": o.driver or "—",
        "progress": round(o.progress, 3),
        "notes": o.notes,
        "slump": o.slump,
        "admixtures": o.admixtures,
        "use_for": o.use_for,
        "project": o.project,
        "site_lat": o.site_lat,
        "site_lng": o.site_lng,
        "has_batch_ticket": bool(o.batch_ticket),
        "has_print_ticket": bool(o.batch_ticket_print),
        "has_original": _has_original(o.ref, o.batch_ticket),
        "batch_data": json.loads(o.batch_data) if o.batch_data else None,
        "archived": bool(o.archived),
        "signed_by": o.signed_by,
        "signed_at": o.signed_at,
        "water_added": o.water_added,
        "mixer_water_gal": o.mixer_water_gal,
        "driver_notes": o.driver_notes,
        "has_signature": bool(o.signature),
        "prepay_required": o.prepay_required,
        "prepaid": o.prepaid,
        "prepay_amount": o.prepay_amount,
        "price_override": o.price_override,
        "fiber_rate": o.fiber_rate,
        "truck_position": (
            {"lat": truck.lat, "lng": truck.lng, "heading": truck.heading}
            if truck and truck.lat is not None else None
        ),
        # True when an en-route truck looks parked at the job — dispatch confirms On site.
        "arrival_pending": (o.status == "enroute" and arrival_pending(truck)),
        # A continuous pour is for orders bigger than one truck (>10 yd): staff add
        # one load per truck, each with its own GPS, status and batch ticket. An order
        # of 10 yd or less is a single delivery (one truck, one ticket) and skips the
        # per-load machinery. If staff have explicitly split an order into loads, it
        # stays a pour regardless of size.
        "is_pour": pricing._num(o.qty) > 10 or len(loads) > 0,
        "loads_total": len(loads),
        "loads_done": sum(1 for ld in loads if ld.status == "complete"),
        "yards_loaded": round(sum(pricing._num(ld.qty) for ld in loads), 2),
        "loads": [_load_json(ld, s, o.ref) for ld in loads],
    }


# The delivery stages an order moves through, in order. Staff drive these from
# the dispatch board. The progress snap keeps the map + progress bar coherent
# with whatever stage was just set (e.g. "onsite" => full bar, not 40%).
# "requested" = placed by a customer in the app, awaiting staff confirmation.
# "ongoing" is the umbrella status for a continuous pour while its loads are in
# flight (set by _rollup_pour, not a single-truck stage). Single orders never use it.
ORDER_STATUSES = ["requested", "scheduled", "ongoing", "batched", "enroute", "onsite", "pouring", "returning", "complete"]
_STATUS_PROGRESS = {"requested": 0.0, "scheduled": 0.0, "batched": 0.05, "onsite": 1.0, "pouring": 1.0, "returning": 1.0, "complete": 1.0}
# Stages that mean a truck is carrying the load — you can't enter them unassigned.
# "ongoing" is NOT here: it's a pour umbrella, trucks live on its loads.
_STATUSES_NEEDING_TRUCK = {"batched", "enroute", "onsite", "pouring", "returning"}


@app.get("/health")
def health():
    return {"ok": True}


# Deploy marker — bump APP_VERSION on each backend change so we can confirm from
# the outside which build is actually live (the API surface alone doesn't reveal it).
APP_VERSION = "2026-06-23.19-fuel-cleanup"


@app.get("/version")
def version():
    # `vision` reports whether ANTHROPIC_API_KEY is configured on this service —
    # batch-ticket auto-branding is skipped (original kept as-is) when it's False.
    return {"version": APP_VERSION, "vision": ticket_convert.available()}


# ── Authentication ──────────────────────────────────────────────────────────
@app.post("/auth/login")
def login(form: OAuth2PasswordRequestForm = Depends(), s: Session = Depends(get_session)):
    """Log in with email + password, get a bearer token back.

    In /docs, click the green "Authorize" button and enter the email as the
    username — every protected endpoint then sends the token for you.
    """
    user = s.exec(select(User).where(User.email == form.username)).first()
    if not user or not verify_password(form.password, user.password_hash):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # company name for company-scoped users; for a driver it's their own name.
    company = (s.get(Customer, user.customer_id).name if user.customer_id else None) or user.company
    return {
        "access_token": create_access_token(user),
        "token_type": "bearer",
        "role": user.role,
        "customer_id": user.customer_id,
        "company": company,
    }


@app.get("/auth/me")
def me(user: User = Depends(get_current_user), s: Session = Depends(get_session)):
    """Who am I? Handy for the front-end to render the right screen. `company` is
    the customer name for company-scoped users (customer/worker), else None."""
    company = (s.get(Customer, user.customer_id).name if user.customer_id else None) or user.company
    return {"email": user.email, "role": user.role, "customer_id": user.customer_id, "company": company}


def _next_order_ref(s: Session) -> str:
    """Generate the next order reference, e.g. 'AB-10042'. Continues from the
    highest existing numeric ref so refs stay unique and roughly sequential."""
    nums = []
    for o in s.exec(select(Order)).all():
        tail = o.ref.split("-")[-1]
        if tail.isdigit():
            nums.append(int(tail))
    return f"AB-{(max(nums) + 1) if nums else 10001}"


# Customers that only ever take one mix — force it on create/edit so an order for
# them can never go out on the wrong mix. Matched as a substring of the customer
# name (case-insensitive), so "Landers" covers "Landers Concrete" etc.
_FORCED_MIX = {"landers": "Precast"}


def _forced_mix(customer_name: str, mix: str) -> str:
    n = (customer_name or "").lower()
    for key, forced in _FORCED_MIX.items():
        if key in n:
            return forced
    return mix


@app.post("/orders")
def create_order(
    body: OrderIn,
    _: User = Depends(require_staff),
    s: Session = Depends(get_session),
):
    """Schedule a new order for a customer (staff only).

    Requires a customer, site, mix, quantity, and date; time and truck are
    optional. The order starts in the 'scheduled' stage — assign a truck and
    advance it from the dispatch board. Returns the new order in the same shape
    as /orders, so the board can drop it straight into the list."""
    customer = s.get(Customer, body.customer_id)
    if not customer:
        raise HTTPException(404, "Customer not found")
    site, mix, qty = body.site.strip(), body.mix.strip(), body.qty.strip()
    mix = _forced_mix(customer.name, mix)   # precast-only customers (Landers) → always Precast
    when = body.scheduled_for.strip()
    if not all([site, mix, qty, when]):
        raise HTTPException(422, "Site, mix, quantity, and date are all required")
    if _is_past_date(when):
        raise HTTPException(422, "Delivery date can't be in the past.")

    truck_id = None
    label = (body.truck or "").strip()
    if label and label not in ("—", "-"):
        t = s.exec(select(Truck).where(Truck.label == label)).first()
        if not t:
            raise HTTPException(404, f"No truck labelled '{label}'")
        truck_id = t.id

    o = Order(ref=_next_order_ref(s), customer_id=body.customer_id, site=site, mix=mix,
              qty=qty, scheduled_for=when, time=body.time.strip(), status="scheduled",
              truck_id=truck_id, driver=(body.driver or "").strip() or None, progress=0.0,
              notes=pricing.strip_self_haul_fee((body.notes or "").strip() or None, customer.name),
              slump=(body.slump or "").strip() or None, admixtures=", ".join(body.admixtures) or None,
              use_for=(body.use_for or "").strip() or None, project=(body.project or "").strip() or None,
              prepay_required=bool(customer.cod))
    s.add(o); s.commit(); s.refresh(o)
    return _order_json(o, s)


def _business_today() -> date:
    """Today in the business's timezone (Central). Falls back to UTC minus a day of
    slack so an evening order is never wrongly flagged as 'past' when the server
    clock (UTC) has already rolled to tomorrow."""
    if _BIZ_TZ is not None:
        return datetime.now(_BIZ_TZ).date()
    return date.today() - timedelta(days=1)


def _is_past_date(s: str) -> bool:
    """True if a YYYY-MM-DD string is before today (business tz). Non-ISO → False."""
    try:
        return datetime.strptime(s, "%Y-%m-%d").date() < _business_today()
    except ValueError:
        return False


@app.post("/orders/request")
def request_order(
    body: OrderRequestIn,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    s: Session = Depends(get_session),
):
    """A customer (or their worker) places a concrete order from the app. It lands
    as 'requested' on the dispatch board for staff to confirm. Always tied to the
    caller's own company — the customer_id can't be spoofed."""
    if user.customer_id is None:   # company-scoped users only (customer or worker); staff use /orders
        raise HTTPException(403, "Only a company account can place orders here")
    site, mix, qty = body.site.strip(), body.mix.strip(), body.qty.strip()
    when = body.scheduled_for.strip()
    if not all([site, mix, qty, when]):
        raise HTTPException(422, "Site, mix, quantity, and date are all required")
    if _is_past_date(when):
        raise HTTPException(422, "Delivery date can't be in the past.")
    cust = s.get(Customer, user.customer_id)
    mix = _forced_mix(cust.name if cust else "", mix)   # precast-only customers (Landers) → always Precast
    o = Order(ref=_next_order_ref(s), customer_id=user.customer_id, site=site, mix=mix,
              qty=qty, scheduled_for=when, time=body.time.strip(), status="requested",
              truck_id=None, progress=0.0,
              notes=pricing.strip_self_haul_fee((body.notes or "").strip() or None, cust.name if cust else ""),
              slump=(body.slump or "").strip() or None, admixtures=", ".join(body.admixtures) or None,
              use_for=(body.use_for or "").strip() or None, project=(body.project or "").strip() or None,
              prepay_required=bool(cust and cust.cod))
    s.add(o); s.commit(); s.refresh(o)
    data = _order_json(o, s)
    # Alert staff (text/email) in the background — never blocks or fails the order.
    from .integrations.notify import notify_new_order
    background.add_task(notify_new_order, data, cust.name if cust else "Customer")
    return data


_EDITABLE_STATUSES = ("requested", "scheduled")   # before the load is on a truck


@app.delete("/orders/{ref}")
def cancel_order(ref: str, user: User = Depends(get_current_user), s: Session = Depends(get_session)):
    """Cancel (delete) an order. Staff can cancel any order; a customer can cancel
    their own only while it's still requested/scheduled (not yet dispatched).
    Also clears any plus-load requests tied to it."""
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    if not o or (user.role != "staff" and o.customer_id != user.customer_id):
        raise HTTPException(404, "Order not found")
    if user.role != "staff" and o.status not in _EDITABLE_STATUSES:
        raise HTTPException(409, "This delivery is already in progress — please call the office to change it.")
    for r in s.exec(select(PlusLoadRequest).where(PlusLoadRequest.order_id == o.id)).all():
        s.delete(r)
    for ld in s.exec(select(Load).where(Load.order_id == o.id)).all():
        s.delete(ld)
    s.delete(o)
    s.commit()
    return {"ok": True, "cancelled": True, "ref": ref}


@app.patch("/orders/{ref}")
def edit_order(ref: str, body: OrderRequestIn, user: User = Depends(get_current_user),
               s: Session = Depends(get_session)):
    """Modify an order's details. Staff or the owning customer, only while the
    order is still requested/scheduled (not yet on a truck)."""
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    if not o or (user.role != "staff" and o.customer_id != user.customer_id):
        raise HTTPException(404, "Order not found")
    if user.role != "staff" and o.status not in _EDITABLE_STATUSES:
        raise HTTPException(409, "This delivery is already in progress — please call the office to change it.")
    site, mix, qty, when = body.site.strip(), body.mix.strip(), body.qty.strip(), body.scheduled_for.strip()
    if not all([site, mix, qty, when]):
        raise HTTPException(422, "Site, mix, quantity, and date are all required")
    if user.role != "staff" and _is_past_date(when):
        raise HTTPException(422, "Delivery date can't be in the past.")
    _cust = s.get(Customer, o.customer_id)
    mix = _forced_mix(_cust.name if _cust else "", mix)   # precast-only customers (Landers) → always Precast
    o.site, o.mix, o.qty, o.scheduled_for, o.time = site, mix, qty, when, body.time.strip()
    o.slump = (body.slump or "").strip() or None
    o.admixtures = ", ".join(body.admixtures) or None
    o.use_for = (body.use_for or "").strip() or None
    o.project = (body.project or "").strip() or None
    o.notes = pricing.strip_self_haul_fee((body.notes or "").strip() or None, _cust.name if _cust else "")
    s.add(o); s.commit(); s.refresh(o)
    return _order_json(o, s)


class LoadIn(BaseModel):
    truck: Optional[str] = None
    driver: Optional[str] = None
    status: Optional[str] = None
    qty: Optional[str] = None


@app.patch("/orders/{ref}/loads/{seq}")
def update_load(ref: str, seq: int, body: LoadIn, _: User = Depends(require_staff),
                s: Session = Depends(get_session)):
    """Assign a truck/driver, advance the status, or correct the yards of one
    load within a pour (the actual yards a truck poured drive what we bill)."""
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    if not o:
        raise HTTPException(404, "Order not found")
    ld = s.exec(select(Load).where(Load.order_id == o.id, Load.seq == seq)).first()
    if not ld:
        raise HTTPException(404, "Load not found")
    if body.qty is not None:
        q = body.qty.strip()
        if q and pricing._num(q) > 0:
            ld.qty = q
        else:
            raise HTTPException(422, "Load yards must be a positive number")
    if body.truck is not None:
        label = body.truck.strip()
        if label and label not in ("—", "-"):
            t = s.exec(select(Truck).where(Truck.label == label)).first()
            if not t:
                raise HTTPException(404, f"No truck labelled '{label}'")
            ld.truck_id = t.id
        else:
            ld.truck_id = None
    if body.driver is not None:
        ld.driver = body.driver.strip() or None
    if body.status is not None:
        st = body.status.strip()
        if st not in ORDER_STATUSES:
            raise HTTPException(422, "Unknown status")
        if st in _STATUSES_NEEDING_TRUCK and not ld.truck_id:
            raise HTTPException(409, "Assign a truck to this load first")
        ld.status = st
        ld.progress = _STATUS_PROGRESS.get(st, ld.progress)
        # On site: learn the truck's real parked spot as this job's pin and anchor
        # the return-trip check to it, so a wrongly-geocoded address doesn't read as
        # the load having "left the job" and flip it straight to 'returning'.
        if st == "onsite" and ld.truck_id:
            truck = s.get(Truck, ld.truck_id)
            if truck and truck.lat is not None:
                learn_site_location(o, truck)
                pin_load_job_location(ld.id, truck)
    s.add(ld); s.commit()
    _rollup_pour(o, s)
    return _order_json(o, s)


class AddLoadIn(BaseModel):
    truck: Optional[str] = None
    qty: Optional[str] = None
    driver: Optional[str] = None
    status: Optional[str] = "batched"


@app.post("/orders/{ref}/loads")
def add_load(ref: str, body: AddLoadIn, _: User = Depends(require_staff),
             s: Session = Depends(get_session)):
    """Add a load to a pour — one truck-load, recorded as it's batched/loaded.
    Defaults its yards to what's left of the order, capped at one truck."""
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    if not o:
        raise HTTPException(404, "Order not found")
    existing = s.exec(select(Load).where(Load.order_id == o.id)).all()
    seq = max((ld.seq for ld in existing), default=0) + 1
    remaining = pricing._num(o.qty) - sum(pricing._num(ld.qty) for ld in existing)
    default_q = min(LOAD_SIZE_YD, remaining) if remaining > 0 else LOAD_SIZE_YD
    qty = (body.qty or "").strip() or ("%g" % round(default_q, 2))
    truck_id = None
    label = (body.truck or "").strip()
    if label and label not in ("—", "-"):
        t = s.exec(select(Truck).where(Truck.label == label)).first()
        if not t:
            raise HTTPException(404, f"No truck labelled '{label}'")
        truck_id = t.id
    st = (body.status or "batched").strip()
    if st in _STATUSES_NEEDING_TRUCK and not truck_id:
        st = "scheduled"   # no truck yet — keep it pre-dispatch
    ld = Load(order_id=o.id, seq=seq, qty=qty, truck_id=truck_id,
              driver=(body.driver or "").strip() or None, status=st,
              progress=_STATUS_PROGRESS.get(st, 0.0))
    s.add(ld); s.commit()
    _rollup_pour(o, s)
    return _order_json(o, s)


@app.delete("/orders/{ref}/loads/{seq}")
def remove_load(ref: str, seq: int, _: User = Depends(require_staff),
                s: Session = Depends(get_session)):
    """Remove a load from a pour (mis-add / cancelled truck)."""
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    if not o:
        raise HTTPException(404, "Order not found")
    ld = s.exec(select(Load).where(Load.order_id == o.id, Load.seq == seq)).first()
    if ld:
        s.delete(ld); s.commit()
        _rollup_pour(o, s)
    return _order_json(o, s)


# ── Batch tickets (the plant's PDF, attached by staff once an order is batched) ──
_BATCHABLE_STATUSES = {"ongoing", "batched", "enroute", "onsite", "pouring", "returning", "complete"}


def _batch_ticket_dir() -> str:
    d = config.data_path("batch_tickets")
    os.makedirs(d, exist_ok=True)
    return d


def _media_type(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(ext, "application/octet-stream")


def _has_original(ref: str, batch_ticket: str | None) -> bool:
    """An 'Original' upload exists alongside a branded ticket (so it's worth showing)."""
    if not batch_ticket or batch_ticket != f"{ref}.pdf":
        return False
    return bool(glob.glob(os.path.join(_batch_ticket_dir(), f"{ref}_original.*")))


def _capture_mixer_water(o, s) -> None:
    """When an order is completed, total the truck's on-site mixer water for this job
    — the auto-posted 'truck…' readings not yet claimed by an order — freeze it onto
    the order, and tag those readings with this order so the next job can't
    double-count them. Idempotent: a no-op once the order already has a total. The
    caller commits."""
    if o.mixer_water_gal is not None or not o.truck_id:
        return
    t = s.get(Truck, o.truck_id)
    if not t or not t.label:
        return
    rows = s.exec(
        select(MixerReading).where(
            MixerReading.truck_label == t.label,
            MixerReading.order_ref.is_(None),
            MixerReading.load_uid.like("truck%"),
        )
    ).all()
    if not rows:
        return
    o.mixer_water_gal = round(sum(r.gallons or 0 for r in rows), 1)
    for r in rows:
        r.order_ref = o.ref
        s.add(r)
    s.add(o)


# ── Knowledge Center ─────────────────────────────────────────────────────────
# A shared library of PDFs (spec sheets, safety, how-tos). The office uploads
# them; every logged-in user (workers, admins, customers) can list & view.
def _docs_dir() -> str:
    d = config.data_path("knowledge")
    os.makedirs(d, exist_ok=True)
    return d


@app.get("/knowledge")
def list_docs(_: User = Depends(get_current_user), s: Session = Depends(get_session)):
    """The Knowledge Center library — any logged-in user can list it.
    (Path is /knowledge, not /docs — FastAPI reserves /docs for Swagger UI.)"""
    return [{"id": d.id, "title": d.title, "uploaded_at": d.uploaded_at}
            for d in s.exec(select(Doc).order_by(Doc.title)).all()]


@app.post("/knowledge")
async def upload_doc(title: str = Query(...), file: UploadFile = File(...),
                     _: User = Depends(require_staff), s: Session = Depends(get_session)):
    """Add a Knowledge Center PDF (operator/office only)."""
    t = (title or "").strip()
    if not t:
        raise HTTPException(422, "Give the document a title.")
    name = (file.filename or "").lower()
    if not (name.endswith(".pdf") or (file.content_type or "").lower() == "application/pdf"):
        raise HTTPException(422, "The document must be a PDF.")
    data = await file.read()
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(413, "That PDF is too large (25 MB max).")
    d = Doc(title=t, filename="", uploaded_at=date.today().isoformat())
    s.add(d); s.commit(); s.refresh(d)          # need the id to name the file
    fname = f"{d.id}.pdf"
    with open(os.path.join(_docs_dir(), fname), "wb") as fh:
        fh.write(data)
    d.filename = fname
    s.add(d); s.commit(); s.refresh(d)
    return {"id": d.id, "title": d.title, "uploaded_at": d.uploaded_at}


@app.get("/knowledge/{doc_id}")
def get_doc(doc_id: int, _: User = Depends(get_current_user), s: Session = Depends(get_session)):
    """View/download a Knowledge Center PDF (any logged-in user)."""
    d = s.get(Doc, doc_id)
    if not d or not d.filename:
        raise HTTPException(404, "Document not found")
    path = os.path.join(_docs_dir(), d.filename)
    if not os.path.exists(path):
        raise HTTPException(404, "The document file is missing.")
    safe = "".join(c for c in d.title if c.isalnum() or c in " -_").strip() or "document"
    return FileResponse(path, media_type="application/pdf", filename=f"{safe}.pdf")


@app.delete("/knowledge/{doc_id}")
def delete_doc(doc_id: int, _: User = Depends(require_staff), s: Session = Depends(get_session)):
    """Remove a Knowledge Center PDF (operator/office only)."""
    d = s.get(Doc, doc_id)
    if not d:
        raise HTTPException(404, "Document not found")
    if d.filename:
        try:
            os.remove(os.path.join(_docs_dir(), d.filename))
        except OSError:
            pass
    s.delete(d); s.commit()
    return {"ok": True, "removed": doc_id}


# ── Cement & Slag tracker ────────────────────────────────────────────────────
# Two pieces: (1) a receiving log of incoming cement/slag loads, to reconcile
# against supplier invoices; (2) a silo gauge — on-hand = opening + received −
# used, where "used" is completed-order yards × the mix design's lb/yd ÷ 2000.
# Operator/office only (require_staff). Silos seed themselves on first read.
TONS_PER_LB = 1 / 2000.0
# Ballpark cementitious content per cubic yard (lb), ~25% slag replacement. These
# are EDITABLE defaults so the gauge works out of the box — the office should set
# their real numbers in the mix-design editor.
DEFAULT_MIX_DESIGNS = [
    {"mix": "3000 PSI", "cement_lb_yd": 376, "slag_lb_yd": 94},
    {"mix": "3500 PSI", "cement_lb_yd": 413, "slag_lb_yd": 104},
    {"mix": "4000 PSI", "cement_lb_yd": 451, "slag_lb_yd": 113},
    {"mix": "4500 PSI", "cement_lb_yd": 480, "slag_lb_yd": 120},
    {"mix": "5000 PSI", "cement_lb_yd": 508, "slag_lb_yd": 132},
    # TxDOT Item 421 Class A — 470 lb/yd³ binder, 50% slag replacement (per the
    # certified batch protocol: 235 lb/yd³ Portland + 235 lb/yd³ slag).
    {"mix": "TxDOT Class A", "cement_lb_yd": 235, "slag_lb_yd": 235},
]
_MATERIAL_FIELD = {"Portland": "cement_lb_yd", "Slag": "slag_lb_yd"}

# Every tracked material, keyed by name → (batch_data mix_design key it reads its
# actual batched amount from, display/cost unit, convert that actual from lb→ton?).
# Cement & slag are silos (inventory + lb/yd estimate fallback); the rest are tracked
# by ACTUAL ticket amounts + cost only. Aggregates batch in lb (shown as tons); fiber
# in lb; liquid admixtures in oz — the cost rate is per that unit.
_MATERIAL_SPEC = {
    "Portland":         ("cement",        "ton", True),
    "Slag":             ("slag",          "ton", True),
    "Gravel":           ("rock",          "ton", True),
    "Sand":             ("sand",          "ton", True),
    "Mac Matrix Fiber": ("fiber",         "lb",  False),
    "Masterset Delvo":  ("retarder",      "oz",  False),
    "Water Reducer":    ("water_reducer", "oz",  False),
    "E5 LFA":           ("e5_lfa",        "oz",  False),
}
# Silos (on-hand draw-down) vs usage-only (just used + cost). order = display order.
DEFAULT_MATERIALS = [
    {"name": "Portland",         "unit": "ton", "track_inventory": True},
    {"name": "Slag",             "unit": "ton", "track_inventory": True},
    {"name": "Gravel",           "unit": "ton", "track_inventory": False},
    {"name": "Sand",             "unit": "ton", "track_inventory": False},
    {"name": "Mac Matrix Fiber", "unit": "lb",  "track_inventory": False},
    {"name": "Masterset Delvo",  "unit": "oz",  "track_inventory": False},
    {"name": "Water Reducer",    "unit": "oz",  "track_inventory": False},
    {"name": "E5 LFA",           "unit": "oz",  "track_inventory": False},
]


def _ensure_materials(s: Session) -> None:
    """Create the tracked materials and seed default mix designs once. Backfills any
    material missing by name so new ones (gravel/sand/admixtures) reach an
    already-seeded production DB on deploy. Runs in production too; idempotent."""
    today = _business_today().isoformat()
    changed = False
    # Rename a retired material in place so it's REPLACED (keeping its id, cost rate
    # and history), not duplicated alongside the new one. Air Entrainer → Masterset
    # Delvo (the plant tracks the retarder, not air entrainer).
    _RENAMES = {"air entrainer": "Masterset Delvo"}
    existing_mats = s.exec(select(Material)).all()
    have = {(m.name or "").strip().lower() for m in existing_mats}
    for m in existing_mats:
        new = _RENAMES.get((m.name or "").strip().lower())
        if new and new.strip().lower() not in have:
            m.name = new; s.add(m); have.add(new.strip().lower()); changed = True
    for spec in DEFAULT_MATERIALS:
        if spec["name"].strip().lower() not in have:
            # Inventory silos count from today; usage-only materials count all-time
            # (counted_on stays null) so historical actuals aren't excluded.
            s.add(Material(name=spec["name"], unit=spec["unit"],
                           track_inventory=spec["track_inventory"],
                           counted_on=today if spec["track_inventory"] else None))
            changed = True
    # Backfill any default design missing by name (not just on an empty table) so
    # new defaults like TxDOT Class A reach an already-seeded production DB on
    # deploy. Office-customized lb/yd values are left untouched.
    existing = {(d.mix or "").strip().lower() for d in s.exec(select(MixDesign)).all()}
    for d in DEFAULT_MIX_DESIGNS:
        if d["mix"].strip().lower() not in existing:
            s.add(MixDesign(**d)); changed = True
    if changed:
        s.commit()


def _design_for(mix: str, designs: list):
    """Best mix-design match for an order's mix string. Exact first, then the
    longest design whose label is contained (so '4000 PSI · 3/4\" Limestone' maps
    to '4000 PSI'). None if nothing matches."""
    m = (mix or "").strip().lower()
    if not m:
        return None
    for d in designs:
        if d.mix.strip().lower() == m:
            return d
    for d in sorted(designs, key=lambda d: -len(d.mix or "")):
        dm = d.mix.strip().lower()
        if dm and dm in m:
            return d
    return None


def _actuals_from_bd(bd: str) -> dict:
    """Actual batched amounts from one batch_data JSON blob, keyed by mix-design key
    (cement/slag/rock/sand/fiber/retarder/water_reducer/e5_lfa) in the value's
    native unit as printed (lb for solids, oz for liquid admixtures). Empty when the
    blob is missing or unparseable."""
    if not bd:
        return {}
    try:
        md = (json.loads(bd) or {}).get("mix_design") or {}
    except (ValueError, TypeError):
        return {}
    out = {}
    for key in ("cement", "slag", "rock", "sand", "fiber", "retarder", "water_reducer", "e5_lfa"):
        v = pricing._num((md.get(key) or {}).get("actual"))
        if v > 0:
            out[key] = v
    return out


def _ticket_actuals(o: Order, s: Session) -> dict:
    """Actual batched amounts for an order, SUMMED across its own batch ticket and —
    for a continuous pour — every per-load batch ticket. A pour's tickets live on the
    loads (the order itself has no batch_data), so the silo tracker must add them up
    here or it sees nothing. Empty when no ticket is on file anywhere."""
    out = dict(_actuals_from_bd(o.batch_data))
    for ld in s.exec(select(Load).where(Load.order_id == o.id)).all():
        for k, v in _actuals_from_bd(ld.batch_data).items():
            out[k] = out.get(k, 0.0) + v
    return out


def _materials_summary(s: Session, frm: str = None, to: str = None) -> dict:
    """Per-material usage + cost. Cement & slag are silos (on-hand draw-down): each
    completed order draws them down from the ACTUAL batched cement/slag weight on its
    ticket, or — when it has no parsed ticket — falls back to the mix-design lb/yd ×
    billable yards estimate, so completing an order draws material down even before
    its ticket is uploaded. Aggregates and admixtures are actual-only (ticket needed).
    Cost = used × the material's cost rate. Also returns mixes of completed orders
    that can't draw down at all (no ticket weight AND no mix design to estimate).

    frm/to (yyyy-mm-dd, optional) restrict the USAGE/cost figures to orders completed
    in that window — for "material used on a given day / range". Silo on-hand and
    received tons stay computed over the full cutoff (the true current balance) so the
    window only ever narrows what's shown as used, never the live inventory level."""
    _ensure_materials(s)
    designs = s.exec(select(MixDesign)).all()
    mats = sorted(s.exec(select(Material)).all(),
                  key=lambda m: next((i for i, d in enumerate(DEFAULT_MATERIALS)
                                      if d["name"] == m.name), 99))
    cutoffs = [m.counted_on for m in mats if m.counted_on]
    earliest = min(cutoffs) if cutoffs else ""
    # Resolve each completed order once: its date, ticket actuals, and (for the
    # cement/slag estimate fallback) its billable yards + matched mix design.
    completed, unmapped = [], {}
    for o in s.exec(select(Order).where(Order.status == "complete")).all():
        done = o.completed_at or ""
        actuals = _ticket_actuals(o, s)
        yds = pricing._num(_billable_yards(o, s))
        d = _design_for(o.mix, designs) if yds > 0 else None
        completed.append({"done": done, "actuals": actuals, "yds": yds, "design": d})
        # An order with yards but no cement actual AND no mix design can't draw the
        # cement/slag silos down — surface its mix so staff can add a design for it.
        if yds > 0 and not actuals.get("cement") and not actuals.get("slag") and not d:
            if not earliest or done >= earliest:
                unmapped[o.mix] = round(unmapped.get(o.mix, 0.0) + yds, 2)

    _EST_FIELD = {"cement": "cement_lb_yd", "slag": "slag_lb_yd"}
    items, total_cost = [], 0.0
    for m in mats:
        key, unit, to_ton = _MATERIAL_SPEC.get(m.name, ("", m.unit or "ton", (m.unit or "ton") == "ton"))
        cutoff = m.counted_on or ""
        # used_native = within the requested window (what's shown as "used"/cost);
        # bal_native = cutoff..now regardless of window, used only for the silo balance.
        used_native = used_ticket_native = bal_native = 0.0
        for c in completed:
            done = c["done"]
            if cutoff and done < cutoff:
                continue
            a = c["actuals"].get(key)
            ticket = a if a else 0.0
            est = (c["yds"] * (getattr(c["design"], _EST_FIELD[key]) or 0)
                   if (not a and m.track_inventory and key in _EST_FIELD and c["design"] and c["yds"] > 0)
                   else 0.0)
            bal_native += ticket + est
            if (not frm or done >= frm) and (not to or done <= to):
                used_native += ticket + est
                used_ticket_native += ticket
        conv = TONS_PER_LB if to_ton else 1.0
        used = used_native * conv
        used_ticket = used_ticket_native * conv
        balance_used = bal_native * conv
        cost = used * (m.cost_rate or 0)
        total_cost += cost
        item = {
            "id": m.id, "name": m.name, "unit": unit,
            "cost_rate": m.cost_rate or 0, "track_inventory": bool(m.track_inventory),
            "counted_on": m.counted_on,
            "used_amount": round(used, 2), "used_ticket_amount": round(used_ticket, 2),
            "used_estimate_amount": round(used - used_ticket, 2),
            "cost": round(cost, 2),
        }
        if m.track_inventory:
            received = sum((r.tons or 0) for r in s.exec(
                select(MaterialReceipt).where(MaterialReceipt.material_id == m.id)).all()
                if (r.received_on or "") >= cutoff)
            on_hand = (m.opening_tons or 0) + received - balance_used
            item.update({
                "capacity_tons": m.capacity_tons, "reorder_tons": m.reorder_tons,
                "opening_tons": m.opening_tons, "received_tons": round(received, 2),
                "on_hand_tons": round(on_hand, 2),
                "low": bool((m.reorder_tons or 0) > 0 and on_hand <= m.reorder_tons),
                "pct": (round(max(0.0, min(1.0, on_hand / m.capacity_tons)), 3)
                        if m.capacity_tons else None),
            })
        items.append(item)
    return {"materials": items, "total_cost": round(total_cost, 2),
            "unmapped_mixes": [{"mix": k, "yards": v} for k, v in sorted(unmapped.items())]}


_PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif"}
# A receipt attachment can be a photo OR a PDF delivery/scale ticket.
_ATTACH_EXTS = _PHOTO_EXTS | {".pdf"}


def _photo_root() -> str:
    return config.data_path("material_photos")


def _receipt_photo_dir(receipt_id: int, create: bool = False) -> str:
    d = os.path.join(_photo_root(), str(receipt_id))
    if create:
        os.makedirs(d, exist_ok=True)
    return d


def _receipt_photos(receipt_id: int) -> list:
    """Filenames of the attachments (photos or PDFs) on a receipt, in upload order."""
    d = _receipt_photo_dir(receipt_id)
    if not os.path.isdir(d):
        return []
    names = [f for f in os.listdir(d) if os.path.splitext(f)[1].lower() in _ATTACH_EXTS]
    return sorted(names, key=lambda n: (int(os.path.splitext(n)[0]) if os.path.splitext(n)[0].isdigit() else 1 << 30, n))


def _receipt_json(r: MaterialReceipt, mat_name: str) -> dict:
    return {"id": r.id, "material_id": r.material_id, "material": mat_name,
            "received_on": r.received_on, "supplier": r.supplier, "tons": r.tons,
            "ticket_no": r.ticket_no, "invoice_no": r.invoice_no,
            "unit_cost": r.unit_cost, "total_cost": r.total_cost,
            "invoice_matched": r.invoice_matched, "notes": r.notes,
            "po_id": r.po_id, "photos": _receipt_photos(r.id)}


@app.get("/materials")
def get_materials(_: User = Depends(require_staff), s: Session = Depends(get_session),
                  frm: Optional[str] = Query(None, alias="from"),
                  to: Optional[str] = Query(None)):
    """Silo levels + per-material usage/cost (staff). Optional from/to (yyyy-mm-dd)
    narrow the usage figures to orders completed in that window."""
    return _materials_summary(s, frm=frm, to=to)


class MaterialIn(BaseModel):
    capacity_tons: Optional[float] = None
    reorder_tons: Optional[float] = None
    opening_tons: Optional[float] = None
    on_hand_tons: Optional[float] = None         # "set current tons" — desired on-hand NOW
    counted_on: Optional[str] = None             # ISO date
    cost_rate: Optional[float] = None            # $ per unit, for usage-based cost


@app.put("/materials/{material_id:int}")
def update_material(material_id: int, body: MaterialIn, _: User = Depends(require_staff),
                    s: Session = Depends(get_session)):
    """Set a material's cost rate, and (for silos) capacity, reorder point, and
    opening balance/date (staff). `on_hand_tons` is a physical-count override: it
    sets the silo to read EXACTLY that amount right now (today's already-completed
    orders aren't subtracted from the count again), and future deliveries draw it
    down from there."""
    m = s.get(Material, material_id)
    if not m:
        raise HTTPException(404, "Material not found")
    for f in ("capacity_tons", "reorder_tons", "opening_tons", "counted_on", "cost_rate"):
        v = getattr(body, f)
        if v is not None:
            setattr(m, f, v)
    # Physical count: store opening so on-hand == the counted amount. on-hand =
    # opening + receipts_in_window − usage_in_window, so opening = count + usage −
    # receipts (computed against the now-current count window).
    if body.on_hand_tons is not None:
        if body.counted_on is None:
            m.counted_on = _business_today().isoformat()
        s.add(m); s.commit()
        mi = next((x for x in _materials_summary(s)["materials"] if x["id"] == m.id), None)
        used = (mi or {}).get("used_amount", 0.0)
        received = (mi or {}).get("received_tons", 0.0)
        m.opening_tons = round(body.on_hand_tons + used - received, 3)
    s.add(m); s.commit()
    return _materials_summary(s)


@app.get("/materials/receipts")
def list_receipts(material_id: Optional[int] = None, _: User = Depends(require_staff),
                  s: Session = Depends(get_session)):
    """The receiving log — incoming cement/slag loads, newest first (staff)."""
    _ensure_materials(s)
    names = {m.id: m.name for m in s.exec(select(Material)).all()}
    q = select(MaterialReceipt)
    if material_id is not None:
        q = q.where(MaterialReceipt.material_id == material_id)
    rows = s.exec(q).all()
    rows.sort(key=lambda r: (r.received_on or "", r.id or 0), reverse=True)
    return [_receipt_json(r, names.get(r.material_id, "")) for r in rows]


class ReceiptIn(BaseModel):
    material_id: int
    received_on: str                             # ISO date
    tons: float
    supplier: Optional[str] = None
    ticket_no: Optional[str] = None
    invoice_no: Optional[str] = None
    unit_cost: Optional[float] = None
    total_cost: Optional[float] = None
    invoice_matched: bool = False
    notes: Optional[str] = None
    po_id: Optional[int] = None                   # the cement PO this delivery fills


@app.post("/materials/receipts")
def add_receipt(body: ReceiptIn, _: User = Depends(require_staff), s: Session = Depends(get_session)):
    """Log an incoming cement/slag load received from a supplier (staff)."""
    if not s.get(Material, body.material_id):
        raise HTTPException(404, "Material not found")
    if body.tons <= 0:
        raise HTTPException(422, "Tons must be a positive number")
    data = body.model_dump()
    # default the line total to tons × $/ton when a unit cost was given but no total
    if data.get("total_cost") is None and data.get("unit_cost") is not None:
        data["total_cost"] = round(body.tons * body.unit_cost, 2)
    r = MaterialReceipt(**data)
    s.add(r); s.commit(); s.refresh(r)
    mat = s.get(Material, r.material_id)
    return _receipt_json(r, mat.name if mat else "")


class ReceiptPatch(BaseModel):
    received_on: Optional[str] = None
    tons: Optional[float] = None
    supplier: Optional[str] = None
    ticket_no: Optional[str] = None
    invoice_no: Optional[str] = None
    unit_cost: Optional[float] = None
    total_cost: Optional[float] = None
    invoice_matched: Optional[bool] = None
    notes: Optional[str] = None
    po_id: Optional[int] = None


@app.patch("/materials/receipts/{receipt_id}")
def edit_receipt(receipt_id: int, body: ReceiptPatch, _: User = Depends(require_staff),
                 s: Session = Depends(get_session)):
    """Edit a logged receipt — correct figures or tick 'invoice matched' (staff)."""
    r = s.get(MaterialReceipt, receipt_id)
    if not r:
        raise HTTPException(404, "Receipt not found")
    for f, v in body.model_dump(exclude_unset=True).items():
        setattr(r, f, v)
    s.add(r); s.commit()
    names = {m.id: m.name for m in s.exec(select(Material)).all()}
    return _receipt_json(r, names.get(r.material_id, ""))


@app.delete("/materials/receipts/{receipt_id}")
def delete_receipt(receipt_id: int, _: User = Depends(require_staff), s: Session = Depends(get_session)):
    """Remove a logged receipt and any photos attached to it (staff)."""
    r = s.get(MaterialReceipt, receipt_id)
    if r:
        s.delete(r); s.commit()
    d = _receipt_photo_dir(receipt_id)
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": True, "removed": receipt_id}


@app.post("/materials/receipts/{receipt_id}/photos")
async def add_receipt_photo(receipt_id: int, file: UploadFile = File(...),
                            _: User = Depends(require_staff), s: Session = Depends(get_session)):
    """Attach a delivery/scale ticket to a receipt — a photo (JPG/PNG/HEIC…) or a
    PDF, 15 MB max, up to 12 per receipt (staff)."""
    r = s.get(MaterialReceipt, receipt_id)
    if not r:
        raise HTTPException(404, "Receipt not found")
    ext = os.path.splitext(file.filename or "")[1].lower()
    ctype = (file.content_type or "").lower()
    ok = (ctype.startswith("image/") or ctype == "application/pdf"
          or ext in _ATTACH_EXTS)
    if not ok:
        raise HTTPException(422, "Attach a photo (JPG, PNG, HEIC…) or a PDF.")
    existing = _receipt_photos(receipt_id)
    if len(existing) >= 12:
        raise HTTPException(409, "That receipt already has the maximum of 12 attachments.")
    data = await file.read()
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(413, "That file is too large (15 MB max).")
    nums = [int(os.path.splitext(n)[0]) for n in existing if os.path.splitext(n)[0].isdigit()]
    if ext not in _ATTACH_EXTS:
        ext = ".pdf" if ctype == "application/pdf" else ".jpg"
    fname = f"{(max(nums) + 1) if nums else 1}{ext}"
    with open(os.path.join(_receipt_photo_dir(receipt_id, create=True), fname), "wb") as fh:
        fh.write(data)
    mat = s.get(Material, r.material_id)
    return _receipt_json(r, mat.name if mat else "")


@app.get("/materials/receipts/{receipt_id}/photos/{name}")
def get_receipt_photo(receipt_id: int, name: str, _: User = Depends(require_staff)):
    """View a receipt's photo (staff). Authed, so the app fetches it as a blob."""
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "Bad photo name")
    path = os.path.join(_receipt_photo_dir(receipt_id), name)
    if not os.path.exists(path):
        raise HTTPException(404, "Photo not found")
    return FileResponse(path, media_type=_media_type(path), filename=name)


@app.delete("/materials/receipts/{receipt_id}/photos/{name}")
def delete_receipt_photo(receipt_id: int, name: str, _: User = Depends(require_staff),
                         s: Session = Depends(get_session)):
    """Remove one photo from a receipt (staff)."""
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "Bad photo name")
    path = os.path.join(_receipt_photo_dir(receipt_id), name)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass
    r = s.get(MaterialReceipt, receipt_id)
    mat = s.get(Material, r.material_id) if r else None
    return _receipt_json(r, mat.name if mat else "") if r else {"ok": True}


# ── Cement / slag purchase orders ────────────────────────────────────────────
# A PO is what you send a supplier; deliveries are receipts linked by po_id, so
# received tons, status, and invoice-match all roll up from the receiving log.
PO_START_NUMBER = 11   # the numbering begins at AB-CEM-0011


def _next_po_number(s: Session) -> str:
    """Next PO number, e.g. 'AB-CEM-0011', continuing from the highest existing one
    but never below PO_START_NUMBER. Derived from the max (not a stored counter)
    so it can never collide."""
    nums = []
    for po in s.exec(select(PurchaseOrder)).all():
        tail = (po.po_number or "").split("-")[-1]
        if tail.isdigit():
            nums.append(int(tail))
    nxt = max((max(nums) + 1) if nums else 1, PO_START_NUMBER)
    return f"AB-CEM-{nxt:04d}"


def _po_json(po: PurchaseOrder, s: Session, mat_names: dict) -> dict:
    receipts = s.exec(select(MaterialReceipt).where(MaterialReceipt.po_id == po.id)).all()
    received = round(sum(r.tons or 0 for r in receipts), 2)
    ordered = po.tons_ordered or 0.0
    inv_total = len(receipts)
    inv_matched = sum(1 for r in receipts if r.invoice_matched)
    if po.status in ("closed", "cancelled"):
        eff = po.status.capitalize()
    elif received <= 0:
        eff = "Open"
    elif received < ordered - 0.01:
        eff = "Partial"
    else:
        eff = "Received"
    unit_all = (po.fob_price or 0.0) + ((po.freight_cost or 0.0) if po.freight_terms == "Vendor Delivered" else 0.0)
    return {
        "id": po.id, "po_number": po.po_number, "vendor": po.vendor,
        "material_id": po.material_id, "material": mat_names.get(po.material_id, ""),
        "tons_ordered": ordered, "received_tons": received,
        "remaining": round(max(ordered - received, 0.0), 2),
        "fob_price": po.fob_price, "freight_terms": po.freight_terms, "freight_cost": po.freight_cost,
        "unit_all": round(unit_all, 2), "committed": round(ordered * unit_all, 2),
        "expected": po.expected, "dest": po.dest, "notes": po.notes,
        "status": eff, "raw_status": po.status,
        "created_at": po.created_at.isoformat() if po.created_at else None,
        "deliveries": inv_total, "invoices_matched": inv_matched,
        "fully_matched": inv_total > 0 and inv_matched == inv_total,
    }


class POIn(BaseModel):
    vendor: str
    material_id: Optional[int] = None
    tons_ordered: float
    fob_price: Optional[float] = None
    freight_terms: Optional[str] = None
    freight_cost: Optional[float] = None
    expected: Optional[str] = None
    dest: Optional[str] = None
    notes: Optional[str] = None


class POPatch(BaseModel):
    vendor: Optional[str] = None
    material_id: Optional[int] = None
    tons_ordered: Optional[float] = None
    fob_price: Optional[float] = None
    freight_terms: Optional[str] = None
    freight_cost: Optional[float] = None
    expected: Optional[str] = None
    dest: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = None   # open | closed | cancelled


@app.get("/materials/pos")
def list_pos(_: User = Depends(require_staff), s: Session = Depends(get_session)):
    """Cement/slag purchase orders, newest first, with rolled-up status (staff)."""
    _ensure_materials(s)
    names = {m.id: m.name for m in s.exec(select(Material)).all()}
    pos = s.exec(select(PurchaseOrder)).all()
    pos.sort(key=lambda p: (p.created_at or datetime.min), reverse=True)
    return [_po_json(p, s, names) for p in pos]


@app.post("/materials/pos")
def create_po(body: POIn, _: User = Depends(require_staff), s: Session = Depends(get_session)):
    """Create a cement/slag PO — auto-numbered AB-CEM-NNNN (staff)."""
    if not (body.vendor or "").strip():
        raise HTTPException(422, "Vendor is required")
    if body.tons_ordered is None or body.tons_ordered <= 0:
        raise HTTPException(422, "Tons ordered must be a positive number")
    if body.material_id is not None and not s.get(Material, body.material_id):
        raise HTTPException(404, "Material not found")
    po = PurchaseOrder(po_number=_next_po_number(s), vendor=body.vendor.strip(),
                       material_id=body.material_id, tons_ordered=body.tons_ordered,
                       fob_price=body.fob_price, freight_terms=body.freight_terms,
                       freight_cost=body.freight_cost, expected=body.expected,
                       dest=(body.dest or "").strip() or None,
                       notes=(body.notes or "").strip() or None)
    s.add(po); s.commit(); s.refresh(po)
    names = {m.id: m.name for m in s.exec(select(Material)).all()}
    return _po_json(po, s, names)


@app.patch("/materials/pos/{po_id}")
def edit_po(po_id: int, body: POPatch, _: User = Depends(require_staff), s: Session = Depends(get_session)):
    """Edit a PO or set its status (open/closed/cancelled) (staff)."""
    po = s.get(PurchaseOrder, po_id)
    if not po:
        raise HTTPException(404, "Purchase order not found")
    data = body.model_dump(exclude_unset=True)
    if "status" in data and data["status"] not in ("open", "closed", "cancelled"):
        raise HTTPException(422, "status must be open, closed, or cancelled")
    for f, v in data.items():
        setattr(po, f, v)
    s.add(po); s.commit(); s.refresh(po)
    names = {m.id: m.name for m in s.exec(select(Material)).all()}
    return _po_json(po, s, names)


@app.delete("/materials/pos/{po_id}")
def delete_po(po_id: int, _: User = Depends(require_staff), s: Session = Depends(get_session)):
    """Delete a PO. Its deliveries stay in the receiving log but are unlinked (staff)."""
    po = s.get(PurchaseOrder, po_id)
    if not po:
        raise HTTPException(404, "Purchase order not found")
    for r in s.exec(select(MaterialReceipt).where(MaterialReceipt.po_id == po_id)).all():
        r.po_id = None
        s.add(r)
    s.delete(po); s.commit()
    return {"ok": True, "removed": po_id}


@app.get("/materials/mix-designs")
def list_mix_designs(_: User = Depends(require_staff), s: Session = Depends(get_session)):
    """The cement & slag lb/yd per mix that drive silo draw-down (staff)."""
    _ensure_materials(s)
    rows = sorted(s.exec(select(MixDesign)).all(), key=lambda d: d.mix)
    return [{"id": d.id, "mix": d.mix, "cement_lb_yd": d.cement_lb_yd, "slag_lb_yd": d.slag_lb_yd}
            for d in rows]


class MixDesignIn(BaseModel):
    mix: str
    cement_lb_yd: float = 0.0
    slag_lb_yd: float = 0.0


class MixDesignsIn(BaseModel):
    designs: list[MixDesignIn] = []


@app.put("/materials/mix-designs")
def save_mix_designs(body: MixDesignsIn, _: User = Depends(require_staff),
                     s: Session = Depends(get_session)):
    """Upsert the mix-design table by mix name; rows omitted are left as-is (staff)."""
    for d in body.designs:
        name = d.mix.strip()
        if not name:
            continue
        row = s.exec(select(MixDesign).where(MixDesign.mix == name)).first()
        if row:
            row.cement_lb_yd = d.cement_lb_yd
            row.slag_lb_yd = d.slag_lb_yd
        else:
            row = MixDesign(mix=name, cement_lb_yd=d.cement_lb_yd, slag_lb_yd=d.slag_lb_yd)
        s.add(row)
    s.commit()
    return list_mix_designs(s=s)


class BatchDataIn(BaseModel):
    """The full set of paper batch-ticket fields, saved against an order.
    Free-form so the form can grow without an API change; the front-end owns
    the field layout (plant, air, load, ordered/delivered, water reducer,
    retarder, the four times, inspector, the mix-design grid, pricing,
    received-by)."""
    data: dict


@app.put("/orders/{ref}/batch-data")
def save_batch_data(ref: str, body: BatchDataIn,
                    _: User = Depends(require_staff), s: Session = Depends(get_session)):
    """Save the complete delivered batch-ticket fields for an order (staff)."""
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    if not o:
        raise HTTPException(404, "Order not found")
    o.batch_data = json.dumps(body.data)
    s.add(o); s.commit(); s.refresh(o)
    return _order_json(o, s)


@app.post("/orders/{ref}/batch-ticket")
async def upload_batch_ticket(ref: str, file: UploadFile = File(...),
                              variant: str = Query("view"),
                              _: User = Depends(require_staff), s: Session = Depends(get_session)):
    """Attach a batch ticket to an order (staff only, once it's batched).

    A scan or photo of the paper ticket is auto-converted to the branded
    Aussieblock ticket (via Claude vision), and the original upload is kept too.
    A PDF that can't be branded (or when no vision key is set) is stored as-is.
    variant='print' stores a light PDF as-is (legacy)."""
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    if not o:
        raise HTTPException(404, "Order not found")
    if o.status not in _BATCHABLE_STATUSES:
        raise HTTPException(409, "You can add a batch ticket once the order is batched.")
    raw = await file.read()
    if len(raw) > 15 * 1024 * 1024:
        raise HTTPException(413, "That file is too large (15 MB max).")
    name = (file.filename or "").lower()
    ctype = (file.content_type or "").lower()
    is_pdf = name.endswith(".pdf") or ctype == "application/pdf" or raw[:5] == b"%PDF-"
    is_img = ctype.startswith("image/") or name.endswith((".jpg", ".jpeg", ".png", ".heic", ".webp"))
    if not (is_pdf or is_img):
        raise HTTPException(422, "The batch ticket must be a PDF or a photo (JPG/PNG).")
    bdir = _batch_ticket_dir()

    # Legacy 'print' variant: store the light PDF as-is.
    if variant == "print":
        fname = f"{ref}_print.pdf"
        with open(os.path.join(bdir, fname), "wb") as fh:
            fh.write(raw)
        o.batch_ticket_print = fname
        s.add(o); s.commit(); s.refresh(o)
        return _order_json(o, s)

    # Keep the original upload so staff can verify a field against the paper.
    for old in glob.glob(os.path.join(bdir, f"{ref}_original.*")):
        try:
            os.remove(old)
        except OSError:
            pass
    ext = ".pdf" if is_pdf else (os.path.splitext(name)[1] or ".jpg")
    orig_name = f"{ref}_original{ext}"
    with open(os.path.join(bdir, orig_name), "wb") as fh:
        fh.write(raw)

    # Record the original NOW and commit, so the upload is never lost even if the
    # (memory-heavy) branding step crashes the worker.
    o.batch_ticket = orig_name
    o.batch_ticket_print = None   # branded ticket prints fine on its own
    s.add(o); s.commit(); s.refresh(o)

    # Then brand it; on success swap the branded ticket in, else the original stays.
    if ticket_convert.available():
        try:
            cust = s.get(Customer, o.customer_id).name if o.customer_id else None
            branded, parsed_bd = ticket_convert.convert(
                raw, name, customer_name=cust, site=o.site,
                order_mix=o.mix, order_qty=o.qty,
                price_sheet=pricing.load_sheet(),
                order_admixtures=o.admixtures or "", return_data=True,
                mixer_water=o.mixer_water_gal)
            if branded:
                fname = f"{ref}.pdf"
                with open(os.path.join(bdir, fname), "wb") as fh:
                    fh.write(branded)
                o.batch_ticket = fname
            # Keep the parsed cement/slag actuals so the silo tracker draws down from
            # the real batched weights (not the lb/yd estimate).
            if parsed_bd and (parsed_bd.get("mix_design") or {}):
                o.batch_data = json.dumps(parsed_bd)
            s.add(o); s.commit(); s.refresh(o)
        except Exception as e:
            print("batch-ticket branding failed:", e)
    # If this order is already signed, stamp the signature onto the new ticket.
    _stamp_signature_on_ticket(o, s)
    return _order_json(o, s)


class PriceSheetIn(BaseModel):
    tax_pct: float = 6.75
    short_load_fee: float = 200.0
    short_load_under_yd: float = 5.0
    backhaul_per_yd: float = 50.0
    backhaul_under_yd: float = 3.0
    mixes: list = []        # [{"mix","price","haul"}]
    overrides: list = []    # [{"customer","mix","price"}]
    admixtures: list = []   # [{"name","rate","per":"lb"|"yard"}]
    self_haul_customers: list = []   # pickup customers — no delivery/load fees


@app.get("/price-sheet")
def get_price_sheet(_: User = Depends(require_staff)):
    """The pricing sheet that fills the ticket's pricing block (staff)."""
    return pricing.load_sheet()


@app.put("/price-sheet")
def put_price_sheet(body: PriceSheetIn, _: User = Depends(require_staff)):
    """Save the pricing sheet (staff)."""
    return pricing.save_sheet(body.model_dump())


def _is_rts(label: str | None) -> bool:
    return bool((label or "").strip().upper().startswith("RTS"))


def _order_hauler(o: Order, s: Session):
    """Staff-set hauler, else auto: a truck labelled 'RTS…' is Ray; otherwise
    blank for staff to fill in. For a pour the trucks are on its loads, so check
    those too — any RTS truck on the order or its loads means Ray hauls it."""
    if o.hauler:
        return o.hauler
    if o.truck_id:
        t = s.get(Truck, o.truck_id)
        if t and _is_rts(t.label):
            return "RAY"
    # Continuous pour: trucks live on the loads, not the order.
    loads = s.exec(select(Load).where(Load.order_id == o.id)).all()
    for ld in loads:
        if ld.truck_id:
            t = s.get(Truck, ld.truck_id)
            if t and _is_rts(t.label):
                return "RAY"
    return None


def _billable_yards(o: Order, s: Session) -> str:
    """The yards to bill the customer and haul on — ACTUAL delivered, never the
    ordered estimate. Priority:
      1. summed loads (a pour: ordered 15, loaded 18 -> 18),
      2. the batch ticket's 'delivered' field (a single delivery's actual yards),
      3. the ordered qty as a fallback (nothing actual recorded yet).
    Returns a string quantity."""
    loads = s.exec(select(Load).where(Load.order_id == o.id)).all()
    loaded = round(sum(pricing._num(ld.qty) for ld in loads), 2)
    if loads and loaded > 0:
        return "%g" % loaded
    if o.batch_data:
        try:
            delivered = pricing._num(json.loads(o.batch_data).get("delivered"))
            if delivered > 0:
                return "%g" % delivered
        except (ValueError, TypeError):
            pass
    return o.qty


def _is_driver_of(o: Order, user: User, s: Session = None) -> bool:
    """True when this driver login is a driver on the order — the order-level driver,
    or (for a continuous pour, which is unassigned at the order level) the driver of
    any of its loads. Matched by name: User.company vs the driver field. Pass the
    session to enable the per-load check (omit it for an order-level-only check)."""
    if user.role != "driver" or not user.company:
        return False
    me = user.company.strip().lower()
    if o.driver and o.driver.strip().lower() == me:
        return True
    if s is not None:
        for ld in s.exec(select(Load).where(Load.order_id == o.id)).all():
            if ld.driver and ld.driver.strip().lower() == me:
                return True
    return False


def _pricing_for(o: Order, s: Session, sheet: dict, key_name: dict, compute_miles: bool = True) -> dict:
    """Build the pricing payload for one order (customer bill + delivery/haul cost).
    Caches a freshly-computed mileage on the order but does NOT commit — the caller
    commits (once) so a bulk run doesn't fire a write per order. compute_miles=False
    skips the (slow) road-miles lookup and uses only the stored value — the bulk path
    prefetches mileages in parallel up front, so it must not look them up again here."""
    cust = s.get(Customer, o.customer_id).name if o.customer_id else ""
    # bill the ACTUAL yards delivered (loads for a pour, batch-ticket delivered for
    # a single order), falling back to the ordered qty — see _billable_yards.
    billable = _billable_yards(o, s)
    # Admixtures actually batched on the ticket(s), by tracked-material name — so
    # plant-added ones (e.g. Masterset Delvo) bill even though they're not in the
    # order's admixtures text. _ticket_actuals reliably maps the ticket row to its
    # key (e.g. a Delvo/retarder row -> 'retarder'); map back to the material name.
    batched_adx = [key_name[k] for k in _ticket_actuals(o, s) if k in key_name]
    cp = pricing.compute_pricing(sheet, o.mix, cust, billable, billable,
                                 materials=batched_adx,
                                 order_admixtures=o.admixtures or "", unit_override=o.price_override,
                                 fiber_rate_override=o.fiber_rate)
    # mileage: use the stored value, else auto-compute once and cache it on the order
    mi = o.mileage
    if mi is None and compute_miles:
        mi = pricing.road_miles(o.site)
        if mi is not None:
            o.mileage = mi
            s.add(o)
    dl = pricing.compute_delivery(sheet, mi, billable)
    dl["hauler"] = _order_hauler(o, s)
    return {"customer": cp, "delivery": dl, "billed_qty": billable,
            "ordered_qty": o.qty, "price_override": o.price_override}


@app.get("/orders/{ref}/pricing")
def order_pricing(ref: str, user: User = Depends(get_current_user), s: Session = Depends(get_session)):
    """Per-order pricing: what we bill the customer + the delivery (haul) cost."""
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    if not o or (user.role != "staff" and o.customer_id != user.customer_id):
        raise HTTPException(404, "Order not found")
    key_name = {spec[0]: name for name, spec in _MATERIAL_SPEC.items()}
    payload = _pricing_for(o, s, pricing.load_sheet(), key_name)
    s.commit()   # persist a freshly-cached mileage, if any
    return payload


class BulkPricingIn(BaseModel):
    refs: Optional[List[str]] = None   # limit to these orders; None/empty = all completed


@app.post("/orders/pricing-bulk")
def orders_pricing_bulk(body: BulkPricingIn, user: User = Depends(get_current_user),
                        s: Session = Depends(get_session)):
    """Pricing for many completed orders in ONE request — the Costs screen uses this
    instead of firing one request per order, which used to flood the server and lock
    the database. Returns { ref: pricingPayload } for every order the user may see."""
    q = select(Order).where(Order.status == "complete")
    if user.role != "staff":
        q = q.where(Order.customer_id == user.customer_id)
    if body.refs:
        q = q.where(Order.ref.in_(body.refs))
    orders = s.exec(q).all()
    sheet = pricing.load_sheet()
    key_name = {spec[0]: name for name, spec in _MATERIAL_SPEC.items()}
    # Pre-compute any missing haul mileages in PARALLEL (each is a ~Google lookup).
    # Doing them one-by-one inside the loop is what made the Costs screen take ages
    # on first open. Dedupe by address so repeat job sites cost one lookup, cache the
    # result on each order, and commit once — later opens then have nothing to fetch.
    need = sorted({o.site for o in orders if o.mileage is None and o.site})
    if need:
        miles = {}
        with ThreadPoolExecutor(max_workers=min(16, len(need))) as ex:
            futs = {ex.submit(pricing.road_miles, site): site for site in need}
            try:
                # Hard overall budget: whatever doesn't resolve in time is left
                # uncached and picked up on a later open, so this request can never
                # hang the Costs screen even if an address lookup is slow.
                for fut in as_completed(futs, timeout=12):
                    try:
                        miles[futs[fut]] = fut.result()
                    except Exception:
                        pass
            except TimeoutError:
                pass
        changed = False
        for o in orders:
            if o.mileage is None and miles.get(o.site) is not None:
                o.mileage = miles[o.site]; s.add(o); changed = True
        if changed:
            s.commit()
    out = {}
    for o in orders:
        try:
            out[o.ref] = _pricing_for(o, s, sheet, key_name, compute_miles=False)
        except Exception:
            out[o.ref] = {"error": True}
    s.commit()   # one commit for any mileages cached across the whole batch
    return out


class PriceOverrideIn(BaseModel):
    price_override: Optional[float] = None   # $/yd; null clears it back to the sheet price


@app.put("/orders/{ref}/price")
def set_order_price(ref: str, body: PriceOverrideIn, _: User = Depends(require_staff),
                    s: Session = Depends(get_session)):
    """Set (or clear) a custom $/yd unit price on an order — allowed at any stage,
    including completed orders, so staff can correct billing after the fact."""
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    if not o:
        raise HTTPException(404, "Order not found")
    if body.price_override is not None and body.price_override < 0:
        raise HTTPException(422, "Price must be zero or more")
    o.price_override = body.price_override
    s.add(o); s.commit(); s.refresh(o)
    return _order_json(o, s)


class FiberIn(BaseModel):
    lbs: Optional[float] = None    # Mac Matrix Fiber dosage in lbs/yd; null or 0 removes it
    rate: Optional[float] = None   # custom $/lb for this order; null = use the price-sheet rate


@app.put("/orders/{ref}/fiber")
def set_order_fiber(ref: str, body: FiberIn, _: User = Depends(require_staff),
                    s: Session = Depends(get_session)):
    """Set (or clear) the Mac Matrix Fiber dosage (lbs/yd) and the $/lb rate on an
    order from the Ticket-details panel. The dosage rewrites the fiber entry in the
    admixtures string; the rate is stored per-order and overrides the price sheet
    (blank = sheet rate). Allowed at any stage, including completed orders, so staff
    can correct billing after the fact."""
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    if not o:
        raise HTTPException(404, "Order not found")
    if body.lbs is not None and body.lbs < 0:
        raise HTTPException(422, "Fiber lbs/yd must be zero or more")
    if body.rate is not None and body.rate < 0:
        raise HTTPException(422, "Fiber $/lb must be zero or more")
    # drop any existing fiber entry, then re-add when a dosage was given. Matches
    # the "Mac Matrix Fiber: 4.5 lbs/yd" format the order form writes.
    parts = [p.strip() for p in (o.admixtures or "").split(",")
             if p.strip() and "fiber" not in p.lower()]
    if body.lbs and body.lbs > 0:
        parts.append(f"Mac Matrix Fiber: {body.lbs:g} lbs/yd")
    o.admixtures = ", ".join(parts) or None
    o.fiber_rate = body.rate if (body.rate is not None and body.rate > 0) else None
    s.add(o); s.commit(); s.refresh(o)
    return _order_json(o, s)


class DeliveryIn(BaseModel):
    hauler: Optional[str] = None
    mileage: Optional[float] = None


@app.put("/orders/{ref}/delivery")
def set_delivery(ref: str, body: DeliveryIn, _: User = Depends(require_staff), s: Session = Depends(get_session)):
    """Set the hauler and/or mileage on an order (mileage auto-computed if omitted)."""
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    if not o:
        raise HTTPException(404, "Order not found")
    o.hauler = (body.hauler or "").strip() or None
    o.mileage = body.mileage if body.mileage is not None else pricing.road_miles(o.site)
    s.add(o); s.commit(); s.refresh(o)
    # haul on the ACTUAL delivered yards (not o.qty) so saving the hauler can't
    # revert the figure to the ordered estimate — matches GET /pricing.
    dl = pricing.compute_delivery(pricing.load_sheet(), o.mileage, _billable_yards(o, s))
    dl["hauler"] = _order_hauler(o, s)
    return dl


@app.get("/orders/{ref}/batch-ticket")
def get_batch_ticket(ref: str, variant: str = Query("view"),
                     user: User = Depends(get_current_user), s: Session = Depends(get_session)):
    """Download an order's batch-ticket PDF (staff, or anyone on that company).
    variant='print' serves the light copy if one exists (else falls back)."""
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    if not o or (user.role != "staff" and o.customer_id != user.customer_id and not _is_driver_of(o, user, s)):
        raise HTTPException(404, "Order not found")
    if variant == "original":
        matches = glob.glob(os.path.join(_batch_ticket_dir(), f"{ref}_original.*"))
        path = matches[0] if matches else None
    else:
        fileref = (o.batch_ticket_print or o.batch_ticket) if variant == "print" else o.batch_ticket
        path = os.path.join(_batch_ticket_dir(), fileref) if fileref else None
    if not path:
        raise HTTPException(404, "No batch ticket for this order yet.")
    if not os.path.exists(path):
        raise HTTPException(404, "The batch ticket file is missing.")
    return FileResponse(path, media_type=_media_type(path),
                        filename=f"batch-ticket-{ref}{os.path.splitext(path)[1]}",
                        headers={"Cache-Control": "no-store, must-revalidate"})


@app.delete("/orders/{ref}/batch-ticket")
def delete_batch_ticket(ref: str, _: User = Depends(require_staff), s: Session = Depends(get_session)):
    """Remove an order's batch-ticket PDF (staff)."""
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    if not o:
        raise HTTPException(404, "Order not found")
    for fileref in (o.batch_ticket, o.batch_ticket_print):
        if fileref:
            try:
                os.remove(os.path.join(_batch_ticket_dir(), fileref))
            except OSError:
                pass   # file already gone — still clear the reference
    for orig in glob.glob(os.path.join(_batch_ticket_dir(), f"{ref}_original.*")):
        try:
            os.remove(orig)
        except OSError:
            pass
    if o.batch_ticket or o.batch_ticket_print:
        o.batch_ticket = None
        o.batch_ticket_print = None
        s.add(o); s.commit(); s.refresh(o)
    return _order_json(o, s)


# ── Driver tablet ────────────────────────────────────────────────────────────
# Aussieblock's own drivers run the app on a truck tablet: they see today's
# deliveries assigned to them, show/open the batch ticket, and capture the
# customer's signature on delivery (proof of delivery → marks the order complete).
def _signature_dir() -> str:
    d = config.data_path("signatures")
    os.makedirs(d, exist_ok=True)
    return d


def _stamp_signature_on_ticket(o: Order, s: Session) -> None:
    """Stamp the order's customer sign-off onto its batch-ticket PDF (no-op if either
    is missing). Runs on sign-off AND on each batch-ticket upload."""
    if not o.batch_ticket:
        return
    _stamp_signature_pdf(os.path.join(_batch_ticket_dir(), o.batch_ticket),
                         o.signature, o.signed_by, o.signed_at, o.water_added)


def _stamp_signature_pdf(pdf_path: str, signature: str, signed_by: str,
                         signed_at_iso: str, water_added: str) -> None:
    """Stamp a customer sign-off (signature image + water added + signed-by/date)
    onto a batch-ticket PDF — placed at the bottom of the LAST page, just under the
    existing content so it never covers the batch data. Best-effort (never raises)."""
    if not (pdf_path and pdf_path.lower().endswith(".pdf") and signature):
        return
    sig_path = os.path.join(_signature_dir(), signature)
    if not (os.path.exists(pdf_path) and os.path.exists(sig_path)):
        return
    try:
        import fitz   # PyMuPDF
        navy, grey = (0.05, 0.07, 0.09), (0.4, 0.4, 0.46)
        signed_at = signed_at_iso or ""
        try:
            signed_at = datetime.fromisoformat(signed_at_iso).strftime("%b %d, %Y %I:%M %p UTC")
        except (ValueError, TypeError):
            pass
        meta = f"Signed by {signed_by or '—'}"
        if water_added:
            meta += f"      Water added on site: {water_added} gal"
        if signed_at:
            meta += f"      {signed_at}"

        # Rebuild the PDF: shrink the LAST ticket page into the top ~84% of the
        # sheet and put the customer sign-off in the freed footer strip — so it's
        # ON the actual ticket page and covers none of the batch data.
        src = fitz.open(pdf_path)
        out = fitz.open()
        n = src.page_count
        for i in range(n - 1):                  # earlier pages copied unchanged
            out.insert_pdf(src, from_page=i, to_page=i)
        last = src[n - 1]
        W, H = last.rect.width, last.rect.height
        page = out.new_page(width=W, height=H)
        content_h = H * 0.84
        page.show_pdf_page(fitz.Rect(0, 0, W, content_h), src, n - 1, keep_proportion=False)

        m = 36
        y = content_h + 14
        page.draw_line((m, y), (W - m, y), color=(0.7, 0.72, 0.75), width=0.8); y += 16
        page.insert_text((m, y), "CUSTOMER SIGN-OFF", fontsize=10, fontname="hebo", color=navy)
        page.insert_text((m, y + 15), meta, fontsize=8.5, color=grey)
        sig_rect = fitz.Rect(W - m - 170, content_h + 18, W - m, H - 10)
        page.draw_rect(sig_rect, color=(0.8, 0.8, 0.8), width=0.6)
        page.insert_image(sig_rect, filename=sig_path, keep_proportion=True)

        tmp = pdf_path + ".tmp"
        out.save(tmp, garbage=3, deflate=True)
        out.close(); src.close()
        os.replace(tmp, pdf_path)
    except Exception as e:   # never let stamping break the request
        print("signature stamp failed:", e)


@app.get("/driver/orders")
def driver_orders(user: User = Depends(require_driver), s: Session = Depends(get_session)):
    """Today's deliveries assigned to this driver (newest scheduled first). Matched
    by the driver's name (User.company) vs Order.driver."""
    today = _business_today().isoformat()
    name = (user.company or "").strip().lower()
    rows = []
    for o in s.exec(select(Order).where(Order.scheduled_for == today)).all():
        if o.status in ("requested", "complete"):
            continue   # not yet confirmed, or already delivered
        od = (o.driver or "").strip().lower()
        if od and name and od != name:
            continue   # assigned to a different driver — hide it
        rows.append(_order_json(o, s))   # mine, or not yet assigned to anyone
    rows.sort(key=lambda r: r.get("time") or "")
    return {"driver": user.company, "date": today, "orders": rows}


@app.post("/orders/{ref}/signoff")
async def sign_off_order(ref: str, file: UploadFile = File(...),
                         signed_by: str = Query(...),
                         water_added: str = Query(""),
                         user: User = Depends(require_driver),
                         s: Session = Depends(get_session)):
    """Driver captures the customer's signature on delivery: store the signature
    image + printed name + water added on site + timestamp. Does NOT complete the
    order — completing is the batch-plant operator's call; this just records the
    sign-off (proof of delivery). Driver may only sign their own assigned order."""
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    if not o or not _is_driver_of(o, user, s):
        raise HTTPException(404, "Order not found")
    if o.status == "requested":
        raise HTTPException(409, "This delivery hasn't been confirmed yet.")
    if o.signature:
        raise HTTPException(409, "This delivery has already been signed.")
    name = (signed_by or "").strip()
    if not name:
        raise HTTPException(422, "Enter who signed for the delivery.")
    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(413, "Signature image is too large.")
    ctype = (file.content_type or "").lower()
    if not (ctype.startswith("image/") or raw[:8] == b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(422, "Signature must be an image.")
    fname = f"{ref}_signature.png"
    with open(os.path.join(_signature_dir(), fname), "wb") as fh:
        fh.write(raw)
    o.signature = fname
    o.signed_by = name
    o.water_added = (water_added or "").strip() or None
    o.signed_at = datetime.utcnow().isoformat()
    # status is left as-is — the operator completes the order when they decide.
    s.add(o); s.commit(); s.refresh(o)
    _stamp_signature_on_ticket(o, s)   # add the signature to the batch-ticket PDF if one's uploaded
    return _order_json(o, s)


@app.post("/orders/{ref}/loads/{seq}/signoff")
async def sign_off_load(ref: str, seq: int, file: UploadFile = File(...),
                        signed_by: str = Query(...),
                        water_added: str = Query(""),
                        user: User = Depends(require_driver),
                        s: Session = Depends(get_session)):
    """Capture the customer's signature for ONE load of a continuous pour, so each
    truck is signed for as it's delivered (a pour has many loads/drivers — one
    order-level signature can't cover them). Driver must have driven this load."""
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    if not o:
        raise HTTPException(404, "Order not found")
    ld = s.exec(select(Load).where(Load.order_id == o.id, Load.seq == seq)).first()
    if not ld:
        raise HTTPException(404, "Load not found")
    drives_load = (bool(user.company) and bool(ld.driver)
                   and ld.driver.strip().lower() == user.company.strip().lower())
    if not drives_load and not _is_driver_of(o, user, s):
        raise HTTPException(404, "Order not found")
    if ld.signature:
        raise HTTPException(409, "This load has already been signed.")
    name = (signed_by or "").strip()
    if not name:
        raise HTTPException(422, "Enter who signed for the delivery.")
    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(413, "Signature image is too large.")
    ctype = (file.content_type or "").lower()
    if not (ctype.startswith("image/") or raw[:8] == b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(422, "Signature must be an image.")
    fname = f"{_load_ticket_prefix(ref, seq)}_signature.png"
    with open(os.path.join(_signature_dir(), fname), "wb") as fh:
        fh.write(raw)
    ld.signature = fname
    ld.signed_by = name
    ld.water_added = (water_added or "").strip() or None
    ld.signed_at = datetime.utcnow().isoformat()
    s.add(ld); s.commit(); s.refresh(ld)
    if ld.batch_ticket:
        _stamp_signature_pdf(os.path.join(_batch_ticket_dir(), ld.batch_ticket),
                             ld.signature, ld.signed_by, ld.signed_at, ld.water_added)
    return _order_json(o, s)


class DriverNotesIn(BaseModel):
    notes: Optional[str] = None   # free text; null/blank clears it


@app.put("/orders/{ref}/driver-notes")
def set_driver_notes(ref: str, body: DriverNotesIn, user: User = Depends(get_current_user),
                     s: Session = Depends(get_session)):
    """Driver's on-site notes (site access, who received it, issues, etc.). The
    assigned driver or any staff user may set it; visible to dispatch on the board."""
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    if not o or (user.role != "staff" and not _is_driver_of(o, user, s)):
        raise HTTPException(404, "Order not found")
    o.driver_notes = (body.notes or "").strip() or None
    s.add(o); s.commit(); s.refresh(o)
    return _order_json(o, s)


@app.get("/orders/{ref}/signature")
def get_signature(ref: str, user: User = Depends(get_current_user), s: Session = Depends(get_session)):
    """View the captured signature image (staff, the owning company, or the driver)."""
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    if not o or (user.role != "staff" and o.customer_id != user.customer_id and not _is_driver_of(o, user, s)):
        raise HTTPException(404, "Order not found")
    if not o.signature:
        raise HTTPException(404, "No signature on this order yet.")
    path = os.path.join(_signature_dir(), o.signature)
    if not os.path.exists(path):
        raise HTTPException(404, "The signature file is missing.")
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "no-store, must-revalidate"})


@app.get("/orders/{ref}/batch-ticket-images")
def batch_ticket_images(ref: str, user: User = Depends(get_current_user), s: Session = Depends(get_session)):
    """The batch ticket rendered to PNG page images (data URLs) — for showing it
    INSIDE the app (an <iframe> PDF won't render on Android; images always do).
    Staff, the owning company, or the assigned driver."""
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    if not o or (user.role != "staff" and o.customer_id != user.customer_id and not _is_driver_of(o, user, s)):
        raise HTTPException(404, "Order not found")
    if not o.batch_ticket:
        raise HTTPException(404, "No batch ticket for this order yet.")
    path = os.path.join(_batch_ticket_dir(), o.batch_ticket)
    if not os.path.exists(path):
        raise HTTPException(404, "The batch ticket file is missing.")
    import base64
    pages = []
    if o.batch_ticket.lower().endswith(".pdf"):
        import fitz
        doc = fitz.open(path)
        for pg in doc:
            png = pg.get_pixmap(dpi=110).tobytes("png")
            pages.append("data:image/png;base64," + base64.b64encode(png).decode())
        doc.close()
    else:   # an image ticket (no branding) — serve it as a single page
        with open(path, "rb") as fh:
            mt = _media_type(path)
            pages.append(f"data:{mt};base64," + base64.b64encode(fh.read()).decode())
    return {"pages": pages}


# ── Per-load batch tickets ───────────────────────────────────────────────────
# A continuous pour delivers in ~10-yd loads, each on its own truck — so each
# load gets its own paper batch ticket. These mirror the order-level endpoints
# but key files/records to the individual Load (AB1042_L2.pdf, …).
@app.post("/orders/{ref}/loads/{seq}/batch-ticket")
async def upload_load_batch_ticket(ref: str, seq: int, file: UploadFile = File(...),
                                   _: User = Depends(require_staff), s: Session = Depends(get_session)):
    """Attach a batch ticket to one load of a pour (staff). Same auto-branding
    (scan/photo → branded Aussieblock ticket via Claude vision) as the order
    ticket; the original upload is kept too."""
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    if not o:
        raise HTTPException(404, "Order not found")
    ld = s.exec(select(Load).where(Load.order_id == o.id, Load.seq == seq)).first()
    if not ld:
        raise HTTPException(404, "Load not found")
    raw = await file.read()
    if len(raw) > 15 * 1024 * 1024:
        raise HTTPException(413, "That file is too large (15 MB max).")
    name = (file.filename or "").lower()
    ctype = (file.content_type or "").lower()
    is_pdf = name.endswith(".pdf") or ctype == "application/pdf" or raw[:5] == b"%PDF-"
    is_img = ctype.startswith("image/") or name.endswith((".jpg", ".jpeg", ".png", ".heic", ".webp"))
    if not (is_pdf or is_img):
        raise HTTPException(422, "The batch ticket must be a PDF or a photo (JPG/PNG).")
    bdir = _batch_ticket_dir()
    prefix = _load_ticket_prefix(ref, seq)

    # Keep the original upload so staff can verify a field against the paper.
    for old in glob.glob(os.path.join(bdir, f"{prefix}_original.*")):
        try:
            os.remove(old)
        except OSError:
            pass
    ext = ".pdf" if is_pdf else (os.path.splitext(name)[1] or ".jpg")
    orig_name = f"{prefix}_original{ext}"
    with open(os.path.join(bdir, orig_name), "wb") as fh:
        fh.write(raw)

    # Record the original NOW so the upload survives even if branding crashes.
    ld.batch_ticket = orig_name
    s.add(ld); s.commit(); s.refresh(ld)

    # Brand it (showing this load's yards); on success swap the branded copy in.
    if ticket_convert.available():
        try:
            cust = s.get(Customer, o.customer_id).name if o.customer_id else None
            # "Load N of M" so the customer's ticket shows which load it is.
            total_loads = len(s.exec(select(Load).where(Load.order_id == o.id)).all())
            label = f"{seq} of {total_loads}" if total_loads > 1 else str(seq)
            branded, parsed_bd = ticket_convert.convert(
                raw, name, customer_name=cust, site=o.site,
                order_mix=o.mix, order_qty=ld.qty,
                price_sheet=pricing.load_sheet(),
                order_admixtures=o.admixtures or "", return_data=True, load_label=label,
                mixer_water=o.mixer_water_gal)
            if branded:
                fname = f"{prefix}.pdf"
                with open(os.path.join(bdir, fname), "wb") as fh:
                    fh.write(branded)
                ld.batch_ticket = fname
                # Keep the parsed weights so this load draws the silos down from real
                # ticket actuals (cement/slag + admixtures), like the order ticket does.
                if parsed_bd:
                    ld.batch_data = json.dumps(parsed_bd)
                s.add(ld); s.commit(); s.refresh(ld)
        except Exception as e:
            print("load batch-ticket branding failed:", e)
    return _order_json(o, s)


@app.get("/orders/{ref}/loads/{seq}/batch-ticket")
def get_load_batch_ticket(ref: str, seq: int, variant: str = Query("view"),
                          user: User = Depends(get_current_user), s: Session = Depends(get_session)):
    """Download a load's batch-ticket PDF (staff, or anyone on that company).
    variant='original' serves the raw scan/photo that was uploaded."""
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    if not o or (user.role != "staff" and o.customer_id != user.customer_id):
        raise HTTPException(404, "Order not found")
    ld = s.exec(select(Load).where(Load.order_id == o.id, Load.seq == seq)).first()
    if not ld:
        raise HTTPException(404, "Load not found")
    prefix = _load_ticket_prefix(ref, seq)
    if variant == "original":
        matches = glob.glob(os.path.join(_batch_ticket_dir(), f"{prefix}_original.*"))
        path = matches[0] if matches else None
    else:
        path = os.path.join(_batch_ticket_dir(), ld.batch_ticket) if ld.batch_ticket else None
    if not path:
        raise HTTPException(404, "No batch ticket for this load yet.")
    if not os.path.exists(path):
        raise HTTPException(404, "The batch ticket file is missing.")
    return FileResponse(path, media_type=_media_type(path),
                        filename=f"batch-ticket-{ref}-L{seq}{os.path.splitext(path)[1]}",
                        headers={"Cache-Control": "no-store, must-revalidate"})


@app.get("/orders/{ref}/loads/{seq}/batch-ticket-images")
def load_batch_ticket_images(ref: str, seq: int, user: User = Depends(get_current_user),
                             s: Session = Depends(get_session)):
    """A load's batch ticket rendered to PNG page images (data URLs) — for showing
    it INSIDE the app, including on the driver tablet for a continuous pour (where
    the ticket lives on the load, not the order). Staff, the owning company, or the
    assigned driver."""
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    if not o:
        raise HTTPException(404, "Order not found")
    ld = s.exec(select(Load).where(Load.order_id == o.id, Load.seq == seq)).first()
    if not ld or not ld.batch_ticket:
        raise HTTPException(404, "No batch ticket for this load yet.")
    # A pour is usually unassigned at the order level (drivers live on the loads),
    # so authorize a driver who drove THIS load, not just the order's driver.
    drives_load = (user.role == "driver" and bool(user.company) and bool(ld.driver)
                   and ld.driver.strip().lower() == user.company.strip().lower())
    if user.role != "staff" and o.customer_id != user.customer_id and not _is_driver_of(o, user, s) and not drives_load:
        raise HTTPException(404, "Order not found")
    path = os.path.join(_batch_ticket_dir(), ld.batch_ticket)
    if not os.path.exists(path):
        raise HTTPException(404, "The batch ticket file is missing.")
    import base64
    pages = []
    if path.lower().endswith(".pdf"):
        import fitz
        doc = fitz.open(path)
        for pg in doc:
            pages.append("data:image/png;base64," + base64.b64encode(pg.get_pixmap(dpi=110).tobytes("png")).decode())
        doc.close()
    else:
        with open(path, "rb") as fh:
            pages.append(f"data:{_media_type(path)};base64," + base64.b64encode(fh.read()).decode())
    return {"pages": pages}


@app.delete("/orders/{ref}/loads/{seq}/batch-ticket")
def delete_load_batch_ticket(ref: str, seq: int, _: User = Depends(require_staff),
                             s: Session = Depends(get_session)):
    """Remove one load's batch-ticket PDF (staff)."""
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    if not o:
        raise HTTPException(404, "Order not found")
    ld = s.exec(select(Load).where(Load.order_id == o.id, Load.seq == seq)).first()
    if not ld:
        raise HTTPException(404, "Load not found")
    prefix = _load_ticket_prefix(ref, seq)
    if ld.batch_ticket:
        try:
            os.remove(os.path.join(_batch_ticket_dir(), ld.batch_ticket))
        except OSError:
            pass
    for orig in glob.glob(os.path.join(_batch_ticket_dir(), f"{prefix}_original.*")):
        try:
            os.remove(orig)
        except OSError:
            pass
    if ld.batch_ticket or ld.batch_data:
        ld.batch_ticket = None
        ld.batch_data = None
        s.add(ld); s.commit()
    return _order_json(o, s)


@app.post("/materials/backfill-load-tickets")
def backfill_load_tickets(force: bool = False, _: User = Depends(require_staff),
                          s: Session = Depends(get_session)):
    """Re-parse the stored ORIGINAL of every load batch ticket that has no saved
    weights yet (e.g. uploaded before per-load batch_data existed) and fill it in,
    so the silo tracker draws those pours down from real ticket actuals without
    staff re-uploading anything. Idempotent; skips loads already parsed and photos
    (only typed protocols carry per-material weights). Needs the vision key.

    `force=true` re-parses loads that ALREADY have weights too — use it after a
    parser change (e.g. a new admixture mapping) so existing tickets pick it up."""
    if not ticket_convert.available():
        raise HTTPException(503, "Ticket reader unavailable (ANTHROPIC_API_KEY not set).")
    bdir = _batch_ticket_dir()
    filled, skipped, failed = 0, 0, 0
    q = select(Load).where(Load.batch_ticket.is_not(None))
    if not force:
        q = q.where(Load.batch_data.is_(None))
    for ld in s.exec(q).all():
        o = s.get(Order, ld.order_id)
        if not o:
            skipped += 1; continue
        prefix = _load_ticket_prefix(o.ref, ld.seq)
        origs = glob.glob(os.path.join(bdir, f"{prefix}_original.*"))
        if not origs:
            skipped += 1; continue
        try:
            with open(origs[0], "rb") as fh:
                raw = fh.read()
            cust = s.get(Customer, o.customer_id).name if o.customer_id else None
            _, parsed_bd = ticket_convert.convert(
                raw, os.path.basename(origs[0]), customer_name=cust, site=o.site,
                order_mix=o.mix, order_qty=ld.qty, price_sheet=pricing.load_sheet(),
                order_admixtures=o.admixtures or "", return_data=True)
            if parsed_bd:
                ld.batch_data = json.dumps(parsed_bd)
                s.add(ld); s.commit()
                filled += 1
            else:
                skipped += 1   # handwritten photo / no per-material weights
        except Exception as e:
            print(f"backfill load {o.ref} L{ld.seq} failed:", e)
            failed += 1
    return {"filled": filled, "skipped": skipped, "failed": failed}


@app.post("/orders/{ref}/archive")
def archive_order(ref: str, archived: bool = True, _: User = Depends(require_staff),
                  s: Session = Depends(get_session)):
    """Archive (or unarchive) a completed order so it drops out of the default
    past-orders lists. Staff only; only completed orders can be archived."""
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    if not o:
        raise HTTPException(404, "Order not found")
    if archived and o.status != "complete":
        raise HTTPException(409, "Only completed orders can be archived.")
    o.archived = archived
    s.add(o); s.commit(); s.refresh(o)
    return _order_json(o, s)


@app.post("/orders/{ref}/charge")
def charge_order(
    ref: str,
    body: ChargeIn,
    _: User = Depends(require_finance),
    s: Session = Depends(get_session),
):
    """Take payment on a COD load using the invoice the office already made in
    QuickBooks — the app no longer creates invoices (that duplicated). Finds the
    customer's open QuickBooks invoice and returns its hosted pay link; the amount
    comes straight from that invoice, so staff don't enter one. Staff only."""
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    if not o:
        raise HTTPException(404, "Order not found")
    res = cod_link_from_existing(o.customer_id, body.amount)
    if not res.get("ok"):
        raise HTTPException(400, res.get("reason", "Could not find a QuickBooks invoice to charge"))
    o.prepay_required = True
    o.prepay_amount = round(float(res.get("amount") or 0), 2)
    o.prepay_invoice_id = res["invoice_id"]
    o.prepaid = False
    s.add(o); s.commit()
    return {"ok": True, "ref": o.ref, "amount": o.prepay_amount,
            "link": res["link"], "doc_number": res.get("doc_number")}


@app.get("/orders/{ref}/payment-status")
def order_payment_status(
    ref: str,
    user: User = Depends(get_current_user),
    s: Session = Depends(get_session),
):
    """COD payment status for an order. The owning customer or staff can read it;
    when the QuickBooks invoice shows paid, the order flips to prepaid (unlocks
    dispatch). Returns the hosted pay link while still unpaid."""
    if user.role == "worker":
        raise HTTPException(403, "Financial access is restricted to approved staff")
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    if not o or (user.role == "customer" and o.customer_id != user.customer_id):
        raise HTTPException(404, "Order not found")
    link = balance = None
    if o.prepay_invoice_id and not o.prepaid:
        st = cod_invoice_status(o.prepay_invoice_id)
        link, balance = st.get("link"), st.get("balance")
        if st.get("paid"):
            o.prepaid = True
            s.add(o); s.commit()
    return {"ref": o.ref, "prepay_required": o.prepay_required, "prepaid": o.prepaid,
            "amount": o.prepay_amount, "charged": o.prepay_invoice_id is not None,
            "link": link, "balance": balance}


@app.get("/orders")
def list_orders(
    customer_id: int | None = None,
    user: User = Depends(get_current_user),
    s: Session = Depends(get_session),
):
    q = select(Order)
    if user.role == "staff":
        # The dispatch operator sees everything (optionally filtered to one company).
        if customer_id is not None:
            q = q.where(Order.customer_id == customer_id)
    elif user.customer_id is not None:
        # Company-scoped (customer or worker): locked to their own company.
        q = q.where(Order.customer_id == user.customer_id)
    else:
        # Non-staff with no company → nothing (defensive; shouldn't happen).
        return []
    return [_order_json(o, s) for o in s.exec(q).all()]


@app.get("/orders/{ref}")
def get_order(
    ref: str,
    user: User = Depends(get_current_user),
    s: Session = Depends(get_session),
):
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    # Company-scoped users (customer/worker) can't even tell whether another
    # company's order exists -> 404, not 403. Only staff see any.
    if not o or (user.role != "staff" and o.customer_id != user.customer_id):
        raise HTTPException(404, "Order not found")
    return _order_json(o, s)


@app.get("/trucks")
def list_trucks(
    user: User = Depends(get_current_user),
    s: Session = Depends(get_session),
):
    """Live truck positions (updated by the GPS poller in the background)."""
    return [
        {"label": t.label, "device": t.gps_device_id, "fuel_vehicle": t.fluidsecure_vehicle_id,
         "lat": t.lat, "lng": t.lng,
         "heading": t.heading, "updated_at": t.updated_at, "notes": t.notes}
        for t in s.exec(select(Truck)).all()
    ]


@app.post("/trucks")
def add_truck(body: TruckIn, _: User = Depends(require_staff), s: Session = Depends(get_session)):
    """Add a truck, or update its GPS device id if the label already exists (staff
    only). The device id is what live One Step GPS positions match on — leave it
    blank for now and fill it in later to enable live tracking for this truck."""
    label = body.label.strip()
    if not label:
        raise HTTPException(422, "Truck name is required")
    device = (body.gps_device_id or "").strip() or None
    fuel_vehicle = (body.fluidsecure_vehicle_id or "").strip() or None
    notes = (body.notes or "").strip() or None
    truck = s.exec(select(Truck).where(Truck.label == label)).first()
    if truck:
        truck.gps_device_id = device
        truck.fluidsecure_vehicle_id = fuel_vehicle
        truck.notes = notes
        s.add(truck)
        action = "updated"
    else:
        truck = Truck(label=label, gps_device_id=device,
                      fluidsecure_vehicle_id=fuel_vehicle, notes=notes)
        s.add(truck)
        action = "added"
    s.commit(); s.refresh(truck)
    # Re-attach any fuel pulled before this truck was mapped (or remap on change),
    # tolerant of the RTS prefix/spacing (see veh_keys).
    if fuel_vehicle:
        targets = veh_keys(fuel_vehicle)
        changed = False
        for ft in s.exec(select(FuelTransaction).where(FuelTransaction.vehicle_no.is_not(None))).all():
            if (veh_keys(ft.vehicle_no) & targets) and ft.truck_id != truck.id:
                ft.truck_id = truck.id
                s.add(ft)
                changed = True
        if changed:
            s.commit()
    return {"ok": True, "action": action, "label": truck.label, "device": truck.gps_device_id}


@app.delete("/trucks/{label}")
def delete_truck(label: str, _: User = Depends(require_staff), s: Session = Depends(get_session)):
    """Remove a truck (staff only). It's first taken off any orders it's on, so
    those orders simply become unassigned rather than pointing at a missing truck."""
    truck = s.exec(select(Truck).where(Truck.label == label)).first()
    if not truck:
        raise HTTPException(404, f"No truck named '{label}'")
    for o in s.exec(select(Order).where(Order.truck_id == truck.id)).all():
        o.truck_id = None
        s.add(o)
    s.delete(truck)
    s.commit()
    return {"ok": True, "removed": True}


def _yards_by_truck(s: Session) -> dict:
    """Delivered cubic yards per truck_id, summed from completed work: each
    completed pour LOAD on its truck, plus each completed single order on its truck
    (orders that are split into loads are counted via their loads, not twice)."""
    loads = s.exec(select(Load)).all()
    order_has_loads = {ld.order_id for ld in loads}
    yards: dict = {}
    for ld in loads:
        if ld.truck_id and ld.status == "complete":
            yards[ld.truck_id] = yards.get(ld.truck_id, 0.0) + pricing._num(ld.qty)
    for o in s.exec(select(Order).where(Order.status == "complete")).all():
        if o.truck_id and o.id not in order_has_loads:
            yards[o.truck_id] = yards.get(o.truck_id, 0.0) + pricing._num(o.qty)
    return yards


class FuelFillIn(BaseModel):
    """One fuel fill as the on-truck ESP32 meter reports it. Everything but
    truck_id is optional so a partial read still posts."""
    truck_id: Optional[str] = None      # truck label/number the device knows (e.g. "4554")
    gallons: Optional[float] = None
    pulses: Optional[int] = None
    uptime_ms: Optional[int] = None
    occurred_at: Optional[float] = None  # epoch seconds; defaults to receipt time


@app.post("/api/fuel/fill")
def post_fuel_fill(body: FuelFillIn, _: None = Depends(mixer.require_device_key),
                   s: Session = Depends(get_session)):
    """Record one fuel fill from an on-truck ESP32 meter (device only — needs the
    X-Device-Key header, same secret as the mixer). Replaces FluidSecure as the
    fuel source. Idempotent on external_id so a resend can't double-count."""
    veh = (str(body.truck_id).strip() if body.truck_id is not None else "")
    # external_id uniquely identifies a fill: device + boot-uptime + pulse total.
    ext = f"esp:{veh or '?'}|{body.uptime_ms if body.uptime_ms is not None else '?'}|{body.pulses if body.pulses is not None else '?'}"
    existing = s.exec(select(FuelTransaction).where(FuelTransaction.external_id == ext)).first()
    if existing:
        return {"ok": True, "duplicate": True, "id": existing.id, "truck_id": existing.truck_id}
    # Map to a truck by label (or the legacy fluidsecure id), tolerant of an RTS
    # prefix/spacing — '4554', 'RTS4554' and 'RTS 4554' all match.
    truck_id = None
    targets = veh_keys(veh)
    if targets:
        for t in s.exec(select(Truck)).all():
            if (veh_keys(t.label) | veh_keys(t.fluidsecure_vehicle_id)) & targets:
                truck_id = t.id
                break
    try:
        when = datetime.utcfromtimestamp(float(body.occurred_at)) if body.occurred_at else datetime.utcnow()
    except (ValueError, OSError, OverflowError):
        when = datetime.utcnow()
    ft = FuelTransaction(
        external_id=ext, truck_id=truck_id, vehicle_no=veh or None,
        gallons=body.gallons, fuel_type="Diesel", occurred_at=when,
        raw=json.dumps(body.model_dump()))
    s.add(ft); s.commit(); s.refresh(ft)
    print(f"POST /api/fuel/fill  truck={veh or '?'} gal={body.gallons} -> id={ft.id} truck_id={truck_id}")
    return {"ok": True, "duplicate": False, "id": ft.id, "truck_id": truck_id}


@app.delete("/fuel/unmatched/{vehicle_no}")
def delete_unmatched_fuel(vehicle_no: str, k: str = Query(""),
                          s: Session = Depends(get_session)):
    """Delete UNMATCHED fuel fills for a vehicle number (e.g. a test or mistyped
    entry). Secret-code gated. Only removes fills not linked to a truck, so real
    per-truck history is never touched."""
    if k != "ab-vision-7f3a9c2e":
        raise HTTPException(404, "Not found")
    targets = veh_keys(vehicle_no)
    n = 0
    for ft in s.exec(select(FuelTransaction).where(FuelTransaction.truck_id.is_(None))).all():
        if veh_keys(ft.vehicle_no) & targets:
            s.delete(ft)
            n += 1
    s.commit()
    return {"ok": True, "deleted": n}


@app.get("/fuel")
def fuel_summary(_: User = Depends(require_staff), s: Session = Depends(get_session)):
    """Per-truck fuel usage rolled up from on-truck fuel-meter fills (staff only),
    with cost (gallons × the $/gal in the price sheet) and per-yard efficiency
    (gallons and fuel-$ per delivered yard). `unmatched` collects fills for vehicle
    numbers no truck is mapped to yet — map that number on the truck to pull them in."""
    sheet = pricing.load_sheet()
    trucks = s.exec(select(Truck)).all()
    txns = s.exec(select(FuelTransaction)).all()
    yards_by_truck = _yards_by_truck(s)
    by_truck: dict = {}
    products: dict = {}   # product name -> its current $/gal, for the price editor
    unmatched = {"gallons": 0.0, "cost": 0.0, "fills": 0, "vehicles": set()}
    for t in txns:
        gal = t.gallons or 0.0
        cost = gal * pricing.fuel_price_for(sheet, t.fuel_type)
        if t.fuel_type:
            products.setdefault(t.fuel_type, pricing.fuel_price_for(sheet, t.fuel_type))
        if t.truck_id is None:
            unmatched["gallons"] += gal
            unmatched["cost"] += cost
            unmatched["fills"] += 1
            if t.vehicle_no:
                unmatched["vehicles"].add(t.vehicle_no)
            continue
        agg = by_truck.setdefault(t.truck_id, {"gallons": 0.0, "cost": 0.0, "fills": 0,
                                               "last_fill": None, "last_odometer": None})
        agg["gallons"] += gal
        agg["cost"] += cost
        agg["fills"] += 1
        if t.occurred_at and (agg["last_fill"] is None or t.occurred_at > agg["last_fill"]):
            agg["last_fill"] = t.occurred_at
            agg["last_odometer"] = t.odometer
    rows = []
    fleet = {"gallons": 0.0, "cost": 0.0, "yards": 0.0}
    for t in trucks:
        a = by_truck.get(t.id, {"gallons": 0.0, "cost": 0.0, "fills": 0,
                                "last_fill": None, "last_odometer": None})
        yd = yards_by_truck.get(t.id, 0.0)
        fleet["gallons"] += a["gallons"]
        fleet["cost"] += a["cost"]
        fleet["yards"] += yd
        rows.append({
            "label": t.label, "fuel_vehicle": t.fluidsecure_vehicle_id,
            "gallons": round(a["gallons"], 1), "cost": round(a["cost"], 2),
            "fills": a["fills"], "last_fill": a["last_fill"], "last_odometer": a["last_odometer"],
            "yards": round(yd, 1),
            "gal_per_yd": round(a["gallons"] / yd, 2) if yd else None,
            "cost_per_yd": round(a["cost"] / yd, 2) if yd else None,
        })
    rows.sort(key=lambda r: r["gallons"], reverse=True)
    return {
        "trucks": rows,
        "fleet": {
            "gallons": round(fleet["gallons"], 1), "cost": round(fleet["cost"], 2),
            "yards": round(fleet["yards"], 1),
            "gal_per_yd": round(fleet["gallons"] / fleet["yards"], 2) if fleet["yards"] else None,
            "cost_per_yd": round(fleet["cost"] / fleet["yards"], 2) if fleet["yards"] else None,
        },
        "unmatched": {"gallons": round(unmatched["gallons"], 1), "cost": round(unmatched["cost"], 2),
                      "fills": unmatched["fills"], "vehicles": sorted(unmatched["vehicles"])},
        "products": [{"product": p, "price": pr} for p, pr in sorted(products.items())],
        "fuel_price_default": pricing._num(sheet.get("fuel_price_default")),
        "live": config.USE_FLUIDSECURE,
    }


class FuelPricesIn(BaseModel):
    """Body for saving fuel $/gal — a default plus optional per-product rates."""
    fuel_price_default: float = 0.0
    fuel_prices: list = []   # [{"product": "Diesel", "price": 3.85}]


@app.put("/fuel/prices")
def put_fuel_prices(body: FuelPricesIn, _: User = Depends(require_staff)):
    """Save fuel $/gal rates into the price sheet (staff). Other sheet fields are
    preserved (see pricing.save_sheet)."""
    sheet = pricing.load_sheet()
    sheet["fuel_price_default"] = body.fuel_price_default
    sheet["fuel_prices"] = body.fuel_prices
    pricing.save_sheet(sheet)
    return {"ok": True}


@app.post("/fuel/import")
async def import_fuel(file: UploadFile = File(...), _: User = Depends(require_staff)):
    """Import a FluidSecure transaction export (CSV) — the no-API-token path.

    Staff export the transactions from the FluidSecure portal and upload the CSV
    here; rows are de-duped on the FluidSecure transaction id and attached to the
    truck whose `fluidsecure_vehicle_id` matches the Vehicle Number, exactly like
    the live poller. Re-uploading the same file is safe (already-seen rows skip).
    Returns how many rows were read and how many were newly stored."""
    raw = await file.read()
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, "That file is too large (10 MB max).")
    name = (file.filename or "").lower()
    ctype = (file.content_type or "").lower()
    looks_csv = (name.endswith((".csv", ".txt", ".tsv"))
                 or ctype.startswith("text/")
                 or "csv" in ctype or "excel" in ctype)
    if not looks_csv:
        raise HTTPException(422, "Upload the FluidSecure export as a CSV file "
                                 "(in the portal choose File Type = CSV).")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="replace")
    result = ingest_fuel_csv(text)
    if result["rows"] == 0:
        raise HTTPException(422, "No transaction rows found in that file — make sure "
                                 "it's the FluidSecure transaction export with a header row.")
    return {"ok": True, **result}


@app.get("/trucks/{label}/fuel")
def truck_fuel(label: str, _: User = Depends(require_staff), s: Session = Depends(get_session)):
    """The fuel fills for one truck, newest first (staff only)."""
    truck = s.exec(select(Truck).where(Truck.label == label)).first()
    if not truck:
        raise HTTPException(404, f"No truck named '{label}'")
    txns = s.exec(select(FuelTransaction).where(FuelTransaction.truck_id == truck.id)).all()
    txns.sort(key=lambda t: t.occurred_at or datetime.min, reverse=True)
    return {
        "label": truck.label,
        "fuel_vehicle": truck.fluidsecure_vehicle_id,
        "fills": [
            {"when": t.occurred_at, "gallons": t.gallons, "fuel_type": t.fuel_type,
             "odometer": t.odometer, "driver": t.driver, "pin": t.pin}
            for t in txns
        ],
    }


# Keep billing fresh without depending on the background loop: opening billing
# kicks off a QuickBooks sync in the background, at most once every few minutes.
_LAST_AUTO_SYNC = {"t": 0.0}
_AUTO_SYNC_EVERY = 180   # seconds


def _maybe_auto_sync(background: BackgroundTasks):
    if config.USE_MOCK_QBO:
        return
    now = time.monotonic()
    if now - _LAST_AUTO_SYNC["t"] >= _AUTO_SYNC_EVERY:
        _LAST_AUTO_SYNC["t"] = now
        background.add_task(sync_ar_from_quickbooks)


@app.get("/billing/{customer_id}")
def billing(customer_id: int, background: BackgroundTasks,
            user: User = Depends(get_current_user)):
    """Customer balance + invoices — the data behind the app's Account screen.
    A customer may only view their own account; staff may view anyone's."""
    if user.role == "worker":
        raise HTTPException(403, "Financial access is restricted to approved staff")
    if user.role == "customer" and customer_id != user.customer_id:
        raise HTTPException(403, "Not your account")
    _maybe_auto_sync(background)
    data = get_billing_for_customer(customer_id)
    if not data:
        raise HTTPException(404, "Customer not found")
    return data


@app.get("/billing/{customer_id}/invoices/{invoice_number}/pay-link")
def invoice_pay_link(
    customer_id: int,
    invoice_number: str,
    user: User = Depends(get_current_user),
):
    """Get a customer-facing QuickBooks payment link for one of this customer's
    invoices — what the app's "Make a payment" button opens.

    Same ownership rule as billing: a customer may only pay their own invoices;
    staff may pull a link for anyone. Returns 409 (with a plain-language reason)
    when no link is available — already paid, demo mode, or online payment not
    enabled on the invoice in QuickBooks."""
    if user.role == "worker":
        raise HTTPException(403, "Financial access is restricted to approved staff")
    if user.role == "customer" and customer_id != user.customer_id:
        raise HTTPException(403, "Not your account")
    result = get_invoice_pay_link(customer_id, invoice_number)
    if not result.get("ok"):
        reason = result.get("reason", "No payment link available.")
        # "not found" → 404; everything else is a state/config issue → 409.
        code = 404 if "not found" in reason.lower() else 409
        raise HTTPException(code, reason)
    return result


@app.post("/billing/sync")
def sync_billing(_: User = Depends(require_finance)):
    """Pull the latest A/R from QuickBooks into the local invoice table (staff only).

    Runs the same job as the background loop, on demand. No-ops with a reason if
    QuickBooks isn't configured yet (mock mode), so it's always safe to call.
    """
    return sync_ar_from_quickbooks()


@app.post("/import/customers")
def import_customers(_: User = Depends(require_finance)):
    """Import the QuickBooks customer roster into the local Customer table (staff only).

    Run this once (then as needed) so the A/R sync can match invoices to customers
    by their QuickBooks Id. No-ops with a reason in mock mode. Upserts by qbo_id,
    so it's safe to re-run.
    """
    return import_customers_from_quickbooks()


# ── Customer logins (staff only) ─────────────────────────────────────────────
# Lets the office create/reset the login a customer uses to see their own
# orders & billing — without any server shell access.
@app.get("/customers")
def list_customers(background: BackgroundTasks, user: User = Depends(require_staff),
                   s: Session = Depends(get_session)):
    """All customers. Full staff get account info (login, contact, terms, COD) for
    the Customers panel. Workers get only id + name — enough for the New Order
    picker, without exposing account/financial details."""
    if user.role == "staff":
        _maybe_auto_sync(background)
    customers = s.exec(select(Customer).order_by(Customer.name)).all()
    if user.role == "worker":
        return [{"id": c.id, "name": c.name} for c in customers]
    logins = {
        u.customer_id: u.email
        for u in s.exec(select(User).where(User.role == "customer")).all()
        if u.customer_id is not None
    }
    return [
        {"id": c.id, "name": c.name, "acct_no": c.acct_no, "terms": c.terms,
         "contact": c.contact, "email": c.email, "login_email": logins.get(c.id), "cod": c.cod}
        for c in customers
    ]


@app.post("/customers/backfill-emails")
def backfill_emails(_: User = Depends(require_finance)):
    """Fill existing customers' email from QuickBooks (staff only). Safe to re-run;
    only updates rows already present, never re-adds removed customers."""
    return backfill_customer_emails()


@app.post("/staff")
def create_staff_login(body: StaffLoginIn, _: User = Depends(require_finance),
                       s: Session = Depends(get_session)):
    """Create or update a login (full staff only).

    Roles (all created here):
      • 'staff'    = the dispatch operator — full board + all companies + billing.
      • 'customer' = a company ADMIN — tied to ONE company (customer_id, REQUIRED),
                     sees that company's orders + tracking + billing.
      • 'worker'   = a company field person — ONE company, orders + tracking, NO billing.

    For an EXISTING login, a blank password leaves the current one in place — so
    company/phone/project/role can be updated without resetting the password. A
    new login always requires a 6+ character password."""
    role = body.role if body.role in ("staff", "worker", "customer", "driver") else "worker"
    email = (body.email or "").strip().lower()
    if not email:
        raise HTTPException(422, "Email is required")
    pw = body.password or ""
    phone = (body.phone or "").strip() or None
    project = (body.project or "").strip() or None
    # Workers AND company admins (customer) MUST belong to a real company — that's
    # what scopes their view. Only the full operator (staff) has no company.
    # A 'driver' has no company; `company` instead holds their NAME (matches Order.driver).
    cust_id = None
    company = None
    if role in ("worker", "customer"):
        cust = s.get(Customer, body.customer_id) if body.customer_id else None
        if not cust:
            raise HTTPException(422, "Pick the company this person belongs to")
        cust_id = cust.id
        company = cust.name            # stored for easy display in the list
    elif role == "driver":
        company = (body.company or "").strip()    # the driver's name, used to match deliveries
        if not company:
            raise HTTPException(422, "Enter the driver's name (must match the name on their orders)")
    u = s.exec(select(User).where(User.email == email)).first()
    if u:
        if pw:                                   # only reset the password when one is supplied
            if len(pw) < 6:
                raise HTTPException(422, "Password must be at least 6 characters")
            u.password_hash = hash_password(pw)
        u.role = role
        u.customer_id = cust_id
        u.phone = phone
        u.company = company
        u.project = project
        s.add(u); s.commit()
        return {"ok": True, "action": "updated", "email": email, "role": role,
                "phone": phone, "customer_id": cust_id, "company": company,
                "project": project, "password_changed": bool(pw)}
    if len(pw) < 6:
        raise HTTPException(422, "A 6+ character password is required for a new login")
    u = User(email=email, password_hash=hash_password(pw), role=role,
             customer_id=cust_id, phone=phone, company=company, project=project)
    s.add(u); s.commit()
    return {"ok": True, "action": "created", "email": email, "role": role,
            "phone": phone, "customer_id": cust_id, "company": company,
            "project": project, "password_changed": True}


@app.get("/staff")
def list_staff(_: User = Depends(require_finance), s: Session = Depends(get_session)):
    """All logins — operators (staff), company admins (customer), and workers
    (full staff only). For company-scoped logins, `company` is their customer name."""
    out = []
    for u in s.exec(select(User).where(User.role.in_(("staff", "worker", "customer", "driver")))).all():
        company = u.company
        if u.customer_id and not company:   # customer logins made via the Customers tab have no stored company name
            c = s.get(Customer, u.customer_id)
            company = c.name if c else None
        out.append({"email": u.email, "role": u.role, "phone": u.phone,
                    "customer_id": u.customer_id, "company": company, "project": u.project})
    return out


def _driver_names(s: Session) -> list[str]:
    """All assignable driver names, distinct (case-insensitive, first casing wins):
    the names of 'driver' logins (User.company) PLUS the name-only Driver roster."""
    seen, out = set(), []
    def add(nm):
        nm = (nm or "").strip()
        if nm and nm.lower() not in seen:
            seen.add(nm.lower()); out.append(nm)
    for u in s.exec(select(User).where(User.role == "driver")).all():
        add(u.company)
    for d in s.exec(select(Driver)).all():
        add(d.name)
    return sorted(out, key=str.lower)


@app.get("/drivers")
def list_drivers(_: User = Depends(require_staff), s: Session = Depends(get_session)):
    """Driver names for the dispatch assignment dropdowns. Staff-accessible (dispatch
    needs it, not just finance). A driver appears here either by having a Driver login
    (Manage Staff) or as a name-only roster entry added via POST /drivers."""
    return _driver_names(s)


class DriverIn(BaseModel):
    name: str


@app.post("/drivers")
def add_driver(body: DriverIn, _: User = Depends(require_staff), s: Session = Depends(get_session)):
    """Add a name-only driver (no login/email) to the roster — assignable on orders.
    Idempotent on name (case-insensitive); a name that already exists (roster OR a
    driver login) is a no-op. Returns the full merged driver-name list."""
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(422, "name is required")
    if name.lower() not in {n.lower() for n in _driver_names(s)}:
        s.add(Driver(name=name)); s.commit()
    return _driver_names(s)


@app.delete("/drivers/{name}")
def delete_driver(name: str, _: User = Depends(require_staff), s: Session = Depends(get_session)):
    """Remove a name-only roster driver. Drivers WITH a login are removed via
    Manage Staff (DELETE /staff) instead — this only clears roster entries.
    Idempotent: ok even if the name wasn't on the roster."""
    target = (name or "").strip().lower()
    removed = 0
    for d in s.exec(select(Driver)).all():
        if (d.name or "").strip().lower() == target:
            s.delete(d); removed += 1
    if removed:
        s.commit()
    return {"ok": True, "removed": name, "count": removed}


@app.delete("/staff/{email}")
def delete_staff(email: str, user: User = Depends(require_finance), s: Session = Depends(get_session)):
    """Remove a login — operator, company admin, or worker (full staff only).
    Can't delete your own account."""
    target = (email or "").strip().lower()
    u = s.exec(select(User).where(User.email == target)).first()
    if not u or u.role not in ("staff", "worker", "customer", "driver"):
        raise HTTPException(404, "Login not found")
    if u.id == user.id:
        raise HTTPException(409, "You can't remove your own login")
    s.delete(u); s.commit()
    return {"ok": True, "removed": target}


@app.post("/customers/{customer_id}/cod")
def set_customer_cod(customer_id: int, body: CodIn,
                     _: User = Depends(require_finance), s: Session = Depends(get_session)):
    """Mark a customer COD (pay before delivery) on/off (staff only). New orders
    for a COD customer require prepayment before they can be dispatched."""
    c = s.get(Customer, customer_id)
    if not c:
        raise HTTPException(404, "Customer not found")
    c.cod = bool(body.cod)
    s.add(c); s.commit()
    return {"ok": True, "customer": c.name, "cod": c.cod}


def _invoice_age_days(date_str: str) -> int | None:
    """Age in days of an invoice from its stored date string, or None if unparseable."""
    for fmt in ("%b %d, %Y", "%Y-%m-%d"):
        try:
            return (date.today() - datetime.strptime(date_str, fmt).date()).days
        except (ValueError, TypeError):
            continue
    return None


@app.post("/customers/cod-from-aging")
def cod_from_aging(days: int = 30, _: User = Depends(require_finance), s: Session = Depends(get_session)):
    """Flag every customer who has an unpaid invoice at least `days` days old as COD
    (staff only). Only sets COD on (never clears it), so manual flags are preserved
    and a customer doesn't flip off COD just because they paid — re-run any time."""
    aged: set[int] = set()
    for inv in s.exec(select(Invoice)).all():
        if inv.status == "paid":
            continue
        age = _invoice_age_days(inv.date)
        if age is not None and age >= days:
            aged.add(inv.customer_id)
    newly_flagged = []
    for cid in aged:
        c = s.get(Customer, cid)
        if c and not c.cod:
            c.cod = True
            s.add(c)
            newly_flagged.append(c.name)
    s.commit()
    return {"ok": True, "days": days, "aged_customers": len(aged),
            "newly_flagged": sorted(newly_flagged)}


@app.post("/customers/{customer_id}/login")
def set_customer_login(
    customer_id: int,
    body: CustomerLoginIn,
    _: User = Depends(require_finance),
    s: Session = Depends(get_session),
):
    """Create or reset a customer's login (staff only).

    Give it the email + password the customer will sign in with. If the customer
    already has a login, this updates that one (email + password); otherwise it
    creates one. The email must not already belong to a different login."""
    customer = s.get(Customer, customer_id)
    if not customer:
        raise HTTPException(404, "Customer not found")
    email = body.email.strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(422, "Enter a valid email address")
    if len(body.password) < 6:
        raise HTTPException(422, "Password must be at least 6 characters")
    # Persist an edited phone to the customer's contact (used for the invite text).
    phone = (body.phone or "").strip()
    if phone and phone != (customer.contact or ""):
        customer.contact = phone
        s.add(customer)

    # The email can't collide with a different user's login.
    clash = s.exec(select(User).where(User.email == email)).first()
    if clash and clash.customer_id != customer_id:
        raise HTTPException(409, "That email is already used by another login")

    user = s.exec(
        select(User).where(User.customer_id == customer_id).where(User.role == "customer")
    ).first()
    if user:
        user.email = email
        user.password_hash = hash_password(body.password)
        s.add(user)
        action = "reset"
    else:
        s.add(User(email=email, password_hash=hash_password(body.password),
                   role="customer", customer_id=customer_id))
        action = "created"
    s.commit()
    return {"ok": True, "action": action, "customer": customer.name, "email": email}


@app.delete("/customers/{customer_id}/login")
def remove_customer_login(
    customer_id: int,
    _: User = Depends(require_finance),
    s: Session = Depends(get_session),
):
    """Revoke a customer's login (staff only). They can no longer sign in; their
    orders/billing are untouched and a new login can be created later."""
    user = s.exec(
        select(User).where(User.customer_id == customer_id).where(User.role == "customer")
    ).first()
    if not user:
        raise HTTPException(404, "This customer has no login")
    s.delete(user)
    s.commit()
    return {"ok": True, "removed": True}


@app.get("/sms/enabled")
def sms_enabled(_: User = Depends(get_current_user)):
    """Whether the app can send texts itself (Twilio configured). The board uses
    this to choose between auto-send and opening the staff phone's messaging app."""
    return {"enabled": config.USE_TWILIO}


@app.post("/customers/{customer_id}/text-invite")
def text_invite(
    customer_id: int,
    body: TextInviteIn,
    _: User = Depends(require_finance),
    s: Session = Depends(get_session),
):
    """Send a customer their invite text via the texting service (staff only).

    Texts the customer's phone on file. Returns 503 if texting isn't set up yet
    (the board then falls back to the phone's messaging app), or 400 with the
    provider's reason if the send fails (e.g. no valid number, not yet
    registered)."""
    cust = s.get(Customer, customer_id)
    if not cust:
        raise HTTPException(404, "Customer not found")
    if not (body.message or "").strip():
        raise HTTPException(422, "Message is empty")
    result = send_sms(cust.contact, body.message)
    if not result.get("ok"):
        code = 503 if result.get("configured") is False else 400
        raise HTTPException(code, result.get("reason", "Could not send text"))
    return {"ok": True, "to": result["to"], "customer": cust.name}


@app.post("/staff/{email}/text-invite")
def staff_text_invite(
    email: str,
    body: TextInviteIn,
    _: User = Depends(require_finance),
    s: Session = Depends(get_session),
):
    """Text a worker/staffer their login invite (full staff only).

    Texts the phone on file for that office login. Returns 404 if there's no such
    login, 422 if they have no phone number on file, 503 if texting isn't set up
    yet (the board then falls back to the phone's messaging app), or 400 with the
    provider's reason if the send fails."""
    target = (email or "").strip().lower()
    u = s.exec(select(User).where(User.email == target)).first()
    if not u or u.role not in ("staff", "worker"):
        raise HTTPException(404, "Office login not found")
    if not (u.phone or "").strip():
        raise HTTPException(422, "No phone number on file for this login")
    if not (body.message or "").strip():
        raise HTTPException(422, "Message is empty")
    result = send_sms(u.phone, body.message)
    if not result.get("ok"):
        code = 503 if result.get("configured") is False else 400
        raise HTTPException(code, result.get("reason", "Could not send text"))
    return {"ok": True, "to": result["to"], "email": target}


# ── Dispatch — order control (staff only) ────────────────────────────────────
# These are the two things staff do from the dispatch board: move an order along
# its delivery stages, and put a truck on a job.
def _staff_order_or_404(ref: str, s: Session) -> Order:
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    if not o:
        raise HTTPException(404, "Order not found")
    return o


@app.post("/orders/{ref}/status")
def set_order_status(
    ref: str,
    status: str,
    _: User = Depends(require_staff),
    s: Session = Depends(get_session),
):
    """Advance (or correct) an order's delivery stage from the dispatch board.

    Guards that keep the board honest:
      • `status` must be one of ORDER_STATUSES.
      • A load-carrying stage (batched/enroute/onsite) needs a truck assigned
        first — otherwise the map + progress bar would have nothing to follow.
    Setting a stage snaps `progress` to match (e.g. onsite => full bar); enroute
    is left alone so the live GPS feed keeps driving it.
    """
    if status not in ORDER_STATUSES:
        raise HTTPException(
            422, f"Unknown status '{status}'. Expected one of: {', '.join(ORDER_STATUSES)}"
        )
    o = _staff_order_or_404(ref, s)
    if status in _STATUSES_NEEDING_TRUCK and o.truck_id is None:
        raise HTTPException(409, f"Assign a truck before setting status to '{status}'")
    # COD: can't dispatch until paid. Re-check the invoice live in case they just paid.
    if status in _STATUSES_NEEDING_TRUCK and o.prepay_required and not o.prepaid:
        if o.prepay_invoice_id and cod_invoice_status(o.prepay_invoice_id).get("paid"):
            o.prepaid = True
        else:
            raise HTTPException(409, "Payment required before dispatch — this COD order is awaiting payment.")
    o.status = status
    if status in _STATUS_PROGRESS:
        o.progress = _STATUS_PROGRESS[status]
    # Stamp the completion date the first time it's marked complete — drives the
    # cement/slag silo draw-down (usage counts orders completed since the count).
    if status == "complete" and not o.completed_at:
        o.completed_at = _business_today().isoformat()
    # Freeze the truck's on-site mixer water onto the order at completion (for the ticket).
    if status == "complete":
        _capture_mixer_water(o, s)
    # When dispatch confirms On site, LEARN where the truck is parked as this job's
    # location — replaces the inaccurate address geocode and is reused next time.
    if status == "onsite" and o.truck_id:
        truck = s.get(Truck, o.truck_id)
        if truck and truck.lat is not None:
            learn_site_location(o, truck)
            # Pin the return-trip anchor to where the truck ACTUALLY is right now —
            # not the address geocode — so the next GPS poll doesn't read a wrongly
            # geocoded site as "left the job" and flip the order to 'returning'.
            pin_job_location(o.id, truck)
    s.add(o); s.commit(); s.refresh(o)
    return _order_json(o, s)


@app.post("/orders/{ref}/assign")
def assign_truck(
    ref: str,
    truck: str,
    _: User = Depends(require_staff),
    s: Session = Depends(get_session),
):
    """Put a truck on an order (or take it off).

    Pass the truck's label, e.g. `?truck=Truck 14`. Pass an empty value (or "—")
    to unassign — but you can't unassign while the order is in a stage that needs
    a truck; move it back to 'scheduled' first.
    """
    o = _staff_order_or_404(ref, s)
    label = truck.strip()
    if label in ("", "—", "-"):
        if o.status in _STATUSES_NEEDING_TRUCK:
            raise HTTPException(
                409, f"Can't unassign while status is '{o.status}'; set it to 'scheduled' first"
            )
        o.truck_id = None
    else:
        t = s.exec(select(Truck).where(Truck.label == label)).first()
        if not t:
            raise HTTPException(404, f"No truck labelled '{label}'")
        o.truck_id = t.id
    s.add(o); s.commit(); s.refresh(o)
    return _order_json(o, s)


@app.post("/orders/{ref}/driver")
def assign_driver(ref: str, driver: str = "", _: User = Depends(require_staff),
                  s: Session = Depends(get_session)):
    """Set (or clear) the driver on an order. Pass `?driver=Rodney`, or empty/"—" to clear."""
    o = _staff_order_or_404(ref, s)
    name = driver.strip()
    o.driver = None if name in ("", "—", "-") else name
    s.add(o); s.commit(); s.refresh(o)
    return _order_json(o, s)


@app.post("/orders/{ref}/plus-load")
def request_plus_load(
    ref: str,
    note: str = "",
    user: User = Depends(get_current_user),
    s: Session = Depends(get_session),
):
    """Customer tapped 'Request plus load' — store it for the office to action."""
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    if not o or (user.role == "customer" and o.customer_id != user.customer_id):
        raise HTTPException(404, "Order not found")
    req = PlusLoadRequest(order_id=o.id, note=note)
    s.add(req); s.commit(); s.refresh(req)
    return {"ok": True, "request_id": req.id, "message": "Sent to Aussieblock dispatch"}


@app.get("/dispatch/plus-loads")
def list_plus_loads(
    _: User = Depends(require_staff),
    s: Session = Depends(get_session),
):
    """Office/dispatch view: incoming customer requests (the feed-back-to-office).
    Each row is enriched with the order + customer details staff need to act."""
    rows = s.exec(
        select(PlusLoadRequest)
        .where(PlusLoadRequest.handled == False)  # noqa: E712
        .order_by(PlusLoadRequest.created_at)
    ).all()
    out = []
    for r in rows:
        o = s.get(Order, r.order_id)
        customer = s.get(Customer, o.customer_id) if o and o.customer_id else None
        out.append({
            "id": r.id,
            "order_id": r.order_id,
            "order_ref": o.ref if o else None,
            "customer": customer.name if customer else None,
            "site": o.site if o else None,
            "time": o.time if o else None,
            "note": r.note,
            "at": r.created_at,
        })
    return out


@app.post("/dispatch/plus-loads/{request_id}/handle")
def handle_plus_load(
    request_id: int,
    _: User = Depends(require_staff),
    s: Session = Depends(get_session),
):
    """Staff marks a plus-load request as handled — it drops off the queue."""
    req = s.get(PlusLoadRequest, request_id)
    if not req:
        raise HTTPException(404, "Request not found")
    req.handled = True
    s.add(req)
    s.commit()
    return {"ok": True, "id": request_id, "handled": True}


@app.post("/import/moby-mix")
def import_moby_mix(
    path: str = "sample_data/moby_mix_sample.csv",
    _: User = Depends(require_staff),
):
    """Import a Moby Mix CSV export. Defaults to the bundled sample file. Staff only."""
    return import_orders_from_csv(path)
