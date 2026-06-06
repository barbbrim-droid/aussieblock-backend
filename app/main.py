"""Aussieblock Ready Mix — backend API.

Run it:
    uvicorn app.main:app --reload

Then open the interactive docs:
    http://localhost:8000/docs

Every endpoint below returns JSON in the exact shape the customer app expects,
so wiring the front-end to it later is a drop-in.
"""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlmodel import Session, select

from .db import init_db, get_session
from .seed import seed_if_empty
from .models import Customer, Truck, Order, PlusLoadRequest, User
from .auth import (
    verify_password, hash_password, create_access_token, get_current_user, require_staff,
)


class CustomerLoginIn(BaseModel):
    """Body for creating/resetting a customer's login (staff action)."""
    email: str
    password: str


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


class TruckIn(BaseModel):
    """Body for adding/updating a truck (staff action). `gps_device_id` is the
    One Step GPS device id used to match live positions — optional for now."""
    label: str
    gps_device_id: str | None = None


class TextInviteIn(BaseModel):
    """Body for sending a customer an invite text via the texting service."""
    message: str
from .integrations.onestep_gps import gps_poll_loop
from .integrations.moby_mix_csv import import_orders_from_csv
from .integrations.quickbooks import (
    get_billing_for_customer, sync_ar_from_quickbooks, qbo_sync_loop,
    import_customers_from_quickbooks, get_invoice_pay_link,
)
from .integrations.sms import send_sms
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
        "progress": round(o.progress, 3),
        "truck_position": (
            {"lat": truck.lat, "lng": truck.lng, "heading": truck.heading}
            if truck and truck.lat is not None else None
        ),
    }


# The delivery stages an order moves through, in order. Staff drive these from
# the dispatch board. The progress snap keeps the map + progress bar coherent
# with whatever stage was just set (e.g. "onsite" => full bar, not 40%).
ORDER_STATUSES = ["scheduled", "batched", "enroute", "onsite", "complete"]
_STATUS_PROGRESS = {"scheduled": 0.0, "batched": 0.05, "onsite": 1.0, "complete": 1.0}
# Stages that mean a truck is carrying the load — you can't enter them unassigned.
_STATUSES_NEEDING_TRUCK = {"batched", "enroute", "onsite"}


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
    return {
        "access_token": create_access_token(user),
        "token_type": "bearer",
        "role": user.role,
        "customer_id": user.customer_id,
    }


@app.get("/auth/me")
def me(user: User = Depends(get_current_user)):
    """Who am I? Handy for the front-end to render the right screen."""
    return {"email": user.email, "role": user.role, "customer_id": user.customer_id}


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

    truck_id = None
    label = (body.truck or "").strip()
    if label and label not in ("—", "-"):
        t = s.exec(select(Truck).where(Truck.label == label)).first()
        if not t:
            raise HTTPException(404, f"No truck labelled '{label}'")
        truck_id = t.id

    o = Order(ref=_next_order_ref(s), customer_id=body.customer_id, site=site, mix=mix,
              qty=qty, scheduled_for=when, time=body.time.strip(), status="scheduled",
              truck_id=truck_id, progress=0.0)
    s.add(o); s.commit(); s.refresh(o)
    return _order_json(o, s)


@app.delete("/orders/{ref}")
def cancel_order(ref: str, _: User = Depends(require_staff), s: Session = Depends(get_session)):
    """Cancel (delete) an order (staff only). Also clears any plus-load requests
    tied to it so nothing is left pointing at a missing order."""
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    if not o:
        raise HTTPException(404, "Order not found")
    for r in s.exec(select(PlusLoadRequest).where(PlusLoadRequest.order_id == o.id)).all():
        s.delete(r)
    s.delete(o)
    s.commit()
    return {"ok": True, "cancelled": True, "ref": ref}


@app.get("/orders")
def list_orders(
    customer_id: int | None = None,
    user: User = Depends(get_current_user),
    s: Session = Depends(get_session),
):
    q = select(Order)
    if user.role == "customer":
        # customers are locked to their own orders, ignoring any customer_id arg
        q = q.where(Order.customer_id == user.customer_id)
    elif customer_id is not None:
        q = q.where(Order.customer_id == customer_id)
    return [_order_json(o, s) for o in s.exec(q).all()]


@app.get("/orders/{ref}")
def get_order(
    ref: str,
    user: User = Depends(get_current_user),
    s: Session = Depends(get_session),
):
    o = s.exec(select(Order).where(Order.ref == ref)).first()
    # Customers can't even tell whether someone else's order exists -> 404, not 403.
    if not o or (user.role == "customer" and o.customer_id != user.customer_id):
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
         "heading": t.heading, "updated_at": t.updated_at}
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
    truck = s.exec(select(Truck).where(Truck.label == label)).first()
    if truck:
        truck.gps_device_id = device
        s.add(truck)
        action = "updated"
    else:
        truck = Truck(label=label, gps_device_id=device)
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


@app.get("/billing/{customer_id}")
def billing(customer_id: int, user: User = Depends(get_current_user)):
    """Customer balance + invoices — the data behind the app's Account screen.
    A customer may only view their own account; staff may view anyone's."""
    if user.role == "customer" and customer_id != user.customer_id:
        raise HTTPException(403, "Not your account")
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
def sync_billing(_: User = Depends(require_staff)):
    """Pull the latest A/R from QuickBooks into the local invoice table (staff only).

    Runs the same job as the background loop, on demand. No-ops with a reason if
    QuickBooks isn't configured yet (mock mode), so it's always safe to call.
    """
    return sync_ar_from_quickbooks()


@app.post("/import/customers")
def import_customers(_: User = Depends(require_staff)):
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
def list_customers(_: User = Depends(require_staff), s: Session = Depends(get_session)):
    """All customers with whether they already have a login (staff only).

    Powers the dispatch board's "Customer logins" picker. `login_email` is the
    email a customer signs in with, or null if no login exists yet."""
    customers = s.exec(select(Customer).order_by(Customer.name)).all()
    logins = {
        u.customer_id: u.email
        for u in s.exec(select(User).where(User.role == "customer")).all()
        if u.customer_id is not None
    }
    return [
        {"id": c.id, "name": c.name, "acct_no": c.acct_no, "terms": c.terms,
         "contact": c.contact, "login_email": logins.get(c.id)}
        for c in customers
    ]


@app.post("/customers/{customer_id}/login")
def set_customer_login(
    customer_id: int,
    body: CustomerLoginIn,
    _: User = Depends(require_staff),
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
    _: User = Depends(require_staff),
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
    _: User = Depends(require_staff),
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
