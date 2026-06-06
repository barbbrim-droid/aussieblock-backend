"""Seed the database with demo data on first run, so every endpoint returns
something real the moment you start the server. Safe to leave in — it only
inserts if the tables are empty."""
from datetime import datetime
from sqlmodel import Session, select
from .db import engine
from .models import Customer, Truck, Order, Invoice, User
from .auth import hash_password
from . import config


def seed_if_empty() -> None:
    if not config.SEED_DEMO:
        return               # production: never (re)create demo data or logins
    _seed_demo_data()
    _seed_users()        # idempotent — safe to run every startup


def _seed_demo_data() -> None:
    with Session(engine) as s:
        if s.exec(select(Customer)).first():
            return  # already seeded

        # ── Customers ──
        tindol = Customer(name="Tindol Construction", acct_no="TIND-0142",
                          terms="Net 30", credit_limit=100000, contact="(325) 658-1424")
        reece = Customer(name="Reece Albert Inc.", acct_no="REEC-0098",
                         terms="Net 30", credit_limit=150000, contact="(325) 658-1424")
        s.add(tindol); s.add(reece); s.commit()
        s.refresh(tindol); s.refresh(reece)

        # ── Trucks (gps_device_id is what you'll map to real One Step GPS devices) ──
        t14 = Truck(label="Truck 14", gps_device_id="DEMO-DEV-14",
                    lat=config.PLANT_LAT, lng=config.PLANT_LNG, heading=0, updated_at=datetime.utcnow())
        t9 = Truck(label="Truck 9", gps_device_id="DEMO-DEV-09",
                   lat=config.PLANT_LAT, lng=config.PLANT_LNG, heading=0, updated_at=datetime.utcnow(), mock_phase=0.4)
        s.add(t14); s.add(t9); s.commit()
        s.refresh(t14); s.refresh(t9)

        # ── Orders ──
        s.add(Order(ref="AB-24817", customer_id=tindol.id, site="Concho Valley Commons — Pad B",
                    mix='4000 PSI · 3/4" Limestone', qty="32 CY", scheduled_for="today",
                    time="9:30 AM", status="enroute", truck_id=t14.id, progress=0.46))
        s.add(Order(ref="AB-24820", customer_id=tindol.id, site="Knickerbocker Rd Bridge Deck",
                    mix="TxDOT Class S · River Rock", qty="18 CY", scheduled_for="today",
                    time="1:15 PM", status="batched", truck_id=t9.id, progress=0.05))
        s.add(Order(ref="AB-24826", customer_id=tindol.id, site="Sunset Hills Foundation",
                    mix="3500 PSI · Pea Gravel", qty="24 CY", scheduled_for="tomorrow",
                    time="7:00 AM", status="scheduled", truck_id=None, progress=0.0))

        # ── Invoices (mirror the app's billing screen; later these sync from QuickBooks) ──
        s.add(Invoice(number="INV-10377", customer_id=tindol.id, date="May 14, 2026",
                      amount=16815.00, status="overdue", order_ref="AB-24761"))
        s.add(Invoice(number="INV-10428", customer_id=tindol.id, date="May 28, 2026",
                      amount=11840.00, status="due", order_ref="AB-24817"))
        s.add(Invoice(number="INV-10402", customer_id=tindol.id, date="May 21, 2026",
                      amount=19620.50, status="due", order_ref="AB-24790"))
        s.add(Invoice(number="INV-10341", customer_id=tindol.id, date="May 02, 2026",
                      amount=14200.00, status="paid", order_ref="AB-24710"))
        s.add(Invoice(number="INV-10299", customer_id=tindol.id, date="Apr 24, 2026",
                      amount=9760.00, status="paid", order_ref="AB-24655"))
        s.commit()
        print("Seeded demo data (2 customers, 2 trucks, 3 orders, 5 invoices).")


# Demo logins. Email + plain password here -> stored only as a salted hash.
# CHANGE THESE before real use. customer_acct links a customer login to its
# Customer row (by account number); staff logins leave it None.
_DEMO_USERS = [
    {"email": "ops@aussieblock.com", "password": "dispatch123", "role": "staff",    "customer_acct": None},
    {"email": "billing@tindol.com",  "password": "tindol123",   "role": "customer", "customer_acct": "TIND-0142"},
    {"email": "ap@reece.com",        "password": "reece123",    "role": "customer", "customer_acct": "REEC-0098"},
]


def _seed_users() -> None:
    """Insert any demo login that doesn't already exist. Idempotent: matches on
    email, so re-running never duplicates and backfills older databases."""
    with Session(engine) as s:
        created = 0
        for u in _DEMO_USERS:
            if s.exec(select(User).where(User.email == u["email"])).first():
                continue
            customer_id = None
            if u["customer_acct"]:
                customer = s.exec(
                    select(Customer).where(Customer.acct_no == u["customer_acct"])
                ).first()
                customer_id = customer.id if customer else None
            s.add(User(
                email=u["email"],
                password_hash=hash_password(u["password"]),
                role=u["role"],
                customer_id=customer_id,
            ))
            created += 1
        s.commit()
        if created:
            print(f"Seeded {created} login(s). Demo credentials:")
            for u in _DEMO_USERS:
                print(f"  [{u['role']:8}] {u['email']} / {u['password']}")
