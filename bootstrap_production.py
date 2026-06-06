"""One-time production seed — build the real dataset on a fresh hosted database,
reproducing the go-live state WITHOUT copying any local data file.

Run once in the host's shell, passing the staff password you want:

    python bootstrap_production.py "<staff-password>"

Order matters: create tables -> import the QuickBooks roster -> sync ready-mix
A/R -> drop block-only customers (they're the ones left with no ready-mix
invoices) -> set payment terms -> create the staff login. Every step upserts, so
it's safe to re-run; re-running refreshes the data and resets the staff password
to whatever you pass. Customer logins are created separately with create_login.py.
"""
import sys

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import Customer, Invoice, Order, User
from app.auth import hash_password
from app.integrations.quickbooks import (
    import_customers_from_quickbooks, sync_ar_from_quickbooks,
)

STAFF_EMAIL = "ops@aussieblock.com"


def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print('Usage: python bootstrap_production.py "<staff-password>"')
        sys.exit(1)
    staff_pw = sys.argv[1]

    init_db()

    print("1/5  importing QuickBooks customer roster ...")
    res = import_customers_from_quickbooks()
    print("    ", res)
    if not res.get("imported"):
        print("     QuickBooks not configured — set the QBO_* env vars first. Stopping.")
        sys.exit(1)

    print("2/5  syncing ready-mix A/R ...")
    r = sync_ar_from_quickbooks()
    print("    ", {k: r.get(k) for k in
                   ("synced", "invoices", "mixed_prorated", "skipped_non_readymix")})

    print("3/5  removing block-only customers (no ready-mix invoices, no orders) ...")
    with Session(engine) as s:
        inv_cids = {i.customer_id for i in s.exec(select(Invoice)).all()}
        ord_cids = {o.customer_id for o in s.exec(select(Order)).all()}
        removed = 0
        for c in s.exec(select(Customer)).all():
            if c.qbo_id and c.id not in inv_cids and c.id not in ord_cids:
                s.delete(c)
                removed += 1
        s.commit()
        remaining = len(s.exec(select(Customer)).all())
    print(f"     removed {removed}; {remaining} customers remain")

    print("4/5  setting payment terms (Net 10 default; Landers = Net 14) ...")
    with Session(engine) as s:
        for c in s.exec(select(Customer)).all():
            if c.qbo_id and not c.terms:
                c.terms = "Net 10"
                s.add(c)
        landers = s.exec(select(Customer).where(Customer.name.like("Landers%"))).first()
        if landers:
            landers.terms = "Net 14"
            s.add(landers)
            print(f"     {landers.name} -> Net 14")
        s.commit()

    print("5/5  creating staff login ...")
    with Session(engine) as s:
        u = s.exec(select(User).where(User.email == STAFF_EMAIL)).first()
        if u:
            u.password_hash = hash_password(staff_pw)
            u.role = "staff"
            u.customer_id = None
            s.add(u)
        else:
            s.add(User(email=STAFF_EMAIL, password_hash=hash_password(staff_pw), role="staff"))
        s.commit()
    print(f"     staff login ready -> {STAFF_EMAIL}")

    print("\nDone. Backend is seeded. Create customer logins with:")
    print('    python create_login.py <email> <password> "<Customer Name>"')


if __name__ == "__main__":
    main()
