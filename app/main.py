"""Aussieblock Ready Mix — backend API.

Run it:
    uvicorn app.main:app --reload

Then open the interactive docs:
    http://localhost:8000/docs

Every endpoint below returns JSON in the exact shape the customer app expects,
so wiring the front-end to it later is a drop-in.
"""
import asyncio
import glob
import json
import os
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
from .models import Customer, Truck, Order, PlusLoadRequest, User, Invoice, Doc
from .auth import (
    verify_password, hash_password, create_access_token, get_current_user, require_staff, require_finance,
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
    One Step GPS device id used to match live positions — optional for now."""
    label: str
    gps_device_id: str | None = None
    notes: str = ""


class TextInviteIn(BaseModel):
    """Body for sending a customer an invite text via the texting service."""
    message: str


class CodIn(BaseModel):
    cod: bool


class ChargeIn(BaseModel):
    amount: float | None = None
from .integrations.onestep_gps import gps_poll_loop, arrival_pending
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_if_empty()
    tasks = [
        asyncio.create_task(gps_poll_loop()),   # live truck updates
        asyncio.create_task(qbo_sync_loop()),   # periodic QuickBooks A/R sync
    ]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Aussieblock Ready Mix API", version="0.1.0", lifespan=lifespan)

# Allow the front-end (running on another port) to call this during development.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


def _order_json(o: Order, s: Session) -> dict:
    truck = s.get(Truck, o.truck_id) if o.truck_id else None
    customer = s.get(Customer, o.customer_id) if o.customer_id else None
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
        "has_batch_ticket": bool(o.batch_ticket),
        "has_print_ticket": bool(o.batch_ticket_print),
        "has_original": _has_original(o.ref, o.batch_ticket),
        "batch_data": json.loads(o.batch_data) if o.batch_data else None,
        "archived": bool(o.archived),
        "prepay_required": o.prepay_required,
        "prepaid": o.prepaid,
        "prepay_amount": o.prepay_amount,
        "truck_position": (
            {"lat": truck.lat, "lng": truck.lng, "heading": truck.heading}
            if truck and truck.lat is not None else None
        ),
        # True when an en-route truck looks parked at the job — dispatch confirms On site.
        "arrival_pending": (o.status == "enroute" and arrival_pending(truck)),
    }


# The delivery stages an order moves through, in order. Staff drive these from
# the dispatch board. The progress snap keeps the map + progress bar coherent
# with whatever stage was just set (e.g. "onsite" => full bar, not 40%).
# "requested" = placed by a customer in the app, awaiting staff confirmation.
ORDER_STATUSES = ["requested", "scheduled", "batched", "enroute", "onsite", "pouring", "returning", "complete"]
_STATUS_PROGRESS = {"requested": 0.0, "scheduled": 0.0, "batched": 0.05, "onsite": 1.0, "pouring": 1.0, "returning": 1.0, "complete": 1.0}
# Stages that mean a truck is carrying the load — you can't enter them unassigned.
_STATUSES_NEEDING_TRUCK = {"batched", "enroute", "onsite", "pouring", "returning"}


@app.get("/health")
def health():
    return {"ok": True}


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
    company = s.get(Customer, user.customer_id).name if user.customer_id else None
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
    company = s.get(Customer, user.customer_id).name if user.customer_id else None
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
              notes=(body.notes or "").strip() or None,
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
    o = Order(ref=_next_order_ref(s), customer_id=user.customer_id, site=site, mix=mix,
              qty=qty, scheduled_for=when, time=body.time.strip(), status="requested",
              truck_id=None, progress=0.0, notes=(body.notes or "").strip() or None,
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
    o.site, o.mix, o.qty, o.scheduled_for, o.time = site, mix, qty, when, body.time.strip()
    o.slump = (body.slump or "").strip() or None
    o.admixtures = ", ".join(body.admixtures) or None
    o.use_for = (body.use_for or "").strip() or None
    o.project = (body.project or "").strip() or None
    o.notes = (body.notes or "").strip() or None
    s.add(o); s.commit(); s.refresh(o)
    return _order_json(o, s)


# ── Batch tickets (the plant's PDF, attached by staff once an order is batched) ──
_BATCHABLE_STATUSES = {"batched", "enroute", "onsite", "pouring", "returning", "complete"}


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
            branded = ticket_convert.convert(raw, name, customer_name=cust, site=o.site,
                                             order_mix=o.mix, order_qty=o.qty,
                                             price_sheet=pricing.load_sheet(),
                                             order_admixtures=o.admixtures or "")
            if branded:
                fname = f"{ref}.pdf"
                with open(os.path.join(bdir, fname), "wb") as fh:
                    fh.write(branded)
                o.batch_ticket = fname
                s.add(o); s.commit(); s.refresh(o)
        except Exception as e:
            print("batch-ticket branding failed:", e)
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


@app.get("/orders/{ref}/pricing")
def order_pricing(ref: str, user: User = Depends(get_current_user), s: Session = Depends(get_session)):
    """Per-order pricing: what we bill the customer + the delivery (haul) cost."""
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    if not o or (user.role != "staff" and o.customer_id != user.customer_id):
        raise HTTPException(404, "Order not found")
    sheet = pricing.load_sheet()
    cust = s.get(Customer, o.customer_id).name if o.customer_id else ""
    cp = pricing.compute_pricing(sheet, o.mix, cust, o.qty, o.qty, order_admixtures=o.admixtures or "")
    # mileage: use the stored value, else auto-compute once and cache it on the order
    mi = o.mileage
    if mi is None:
        mi = pricing.road_miles(o.site)
        if mi is not None:
            o.mileage = mi
            s.add(o); s.commit()
    dl = pricing.compute_delivery(sheet, mi, o.qty)
    dl["hauler"] = o.hauler
    return {"customer": cp, "delivery": dl}


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
    dl = pricing.compute_delivery(pricing.load_sheet(), o.mileage, o.qty)
    dl["hauler"] = o.hauler
    return dl


@app.get("/orders/{ref}/batch-ticket")
def get_batch_ticket(ref: str, variant: str = Query("view"),
                     user: User = Depends(get_current_user), s: Session = Depends(get_session)):
    """Download an order's batch-ticket PDF (staff, or anyone on that company).
    variant='print' serves the light copy if one exists (else falls back)."""
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    if not o or (user.role != "staff" and o.customer_id != user.customer_id):
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
        {"label": t.label, "device": t.gps_device_id, "lat": t.lat, "lng": t.lng,
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
    notes = (body.notes or "").strip() or None
    truck = s.exec(select(Truck).where(Truck.label == label)).first()
    if truck:
        truck.gps_device_id = device
        truck.notes = notes
        s.add(truck)
        action = "updated"
    else:
        truck = Truck(label=label, gps_device_id=device, notes=notes)
        s.add(truck)
        action = "added"
    s.commit(); s.refresh(truck)
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
    role = body.role if body.role in ("staff", "worker", "customer") else "worker"
    email = (body.email or "").strip().lower()
    if not email:
        raise HTTPException(422, "Email is required")
    pw = body.password or ""
    phone = (body.phone or "").strip() or None
    project = (body.project or "").strip() or None
    # Workers AND company admins (customer) MUST belong to a real company — that's
    # what scopes their view. Only the full operator (staff) has no company.
    cust_id = None
    company = None
    if role in ("worker", "customer"):
        cust = s.get(Customer, body.customer_id) if body.customer_id else None
        if not cust:
            raise HTTPException(422, "Pick the company this person belongs to")
        cust_id = cust.id
        company = cust.name            # stored for easy display in the list
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
    for u in s.exec(select(User).where(User.role.in_(("staff", "worker", "customer")))).all():
        company = u.company
        if u.customer_id and not company:   # customer logins made via the Customers tab have no stored company name
            c = s.get(Customer, u.customer_id)
            company = c.name if c else None
        out.append({"email": u.email, "role": u.role, "phone": u.phone,
                    "customer_id": u.customer_id, "company": company, "project": u.project})
    return out


@app.delete("/staff/{email}")
def delete_staff(email: str, user: User = Depends(require_finance), s: Session = Depends(get_session)):
    """Remove a login — operator, company admin, or worker (full staff only).
    Can't delete your own account."""
    target = (email or "").strip().lower()
    u = s.exec(select(User).where(User.email == target)).first()
    if not u or u.role not in ("staff", "worker", "customer"):
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
