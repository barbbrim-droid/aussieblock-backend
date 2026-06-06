"""Remove QuickBooks-imported customers that have NO ready-mix invoices and NO
orders (i.e. block-only customers). Protects demo/sample-order customers (they
have orders) and any customer with ready-mix A/R (they have invoices)."""
from sqlmodel import Session, select
from app.db import engine
from app.models import Customer, Invoice, Order

with Session(engine) as s:
    custs = s.exec(select(Customer)).all()
    inv_cids = {i.customer_id for i in s.exec(select(Invoice)).all()}
    ord_cids = {o.customer_id for o in s.exec(select(Order)).all()}
    removed = []
    for c in custs:
        if c.qbo_id and c.id not in inv_cids and c.id not in ord_cids:
            removed.append(c.name)
            s.delete(c)
    s.commit()
    remaining = len(s.exec(select(Customer)).all())

print(f"Removed {len(removed)} block-only customers.")
print(f"Customers remaining: {remaining}")
