"""Import orders/tickets from a SIMEM Moby Mix CSV export.

Your Moby Mix exports CSV/reports (not a live feed), so orders land in the app
whenever a CSV is imported. Map your real column names in COLUMN_MAP below once
you see an actual export.
"""
import csv
from sqlmodel import Session, select
from ..db import engine
from ..models import Order, Customer

# Map: our field  ->  the column header in YOUR Moby Mix CSV.
# Update the right-hand side to match a real export.
COLUMN_MAP = {
    "ref": "TicketNumber",
    "customer_acct": "CustomerAccount",
    "site": "JobSite",
    "mix": "MixDesign",
    "qty": "Quantity",
    "date": "Date",
    "time": "Time",
}


def import_orders_from_csv(path: str) -> dict:
    created, updated, skipped = 0, 0, 0
    with Session(engine) as s, open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ref = (row.get(COLUMN_MAP["ref"]) or "").strip()
            if not ref:
                skipped += 1
                continue
            acct = (row.get(COLUMN_MAP["customer_acct"]) or "").strip()
            customer = s.exec(select(Customer).where(Customer.acct_no == acct)).first()
            cust_id = customer.id if customer else None

            existing = s.exec(select(Order).where(Order.ref == ref)).first()
            if existing:
                existing.site = row.get(COLUMN_MAP["site"], existing.site)
                existing.mix = row.get(COLUMN_MAP["mix"], existing.mix)
                existing.qty = row.get(COLUMN_MAP["qty"], existing.qty)
                s.add(existing)
                updated += 1
            else:
                s.add(Order(
                    ref=ref,
                    customer_id=cust_id or 0,
                    site=row.get(COLUMN_MAP["site"], ""),
                    mix=row.get(COLUMN_MAP["mix"], ""),
                    qty=row.get(COLUMN_MAP["qty"], ""),
                    scheduled_for=row.get(COLUMN_MAP["date"], "today"),
                    time=row.get(COLUMN_MAP["time"], ""),
                    status="batched",
                ))
                created += 1
        s.commit()
    return {"created": created, "updated": updated, "skipped": skipped}
