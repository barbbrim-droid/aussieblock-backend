"""Set a customer's payment terms (display string).
Usage: python set_terms.py "<customer-name-prefix>" "<terms>"  e.g. "Landers" "Net 14" """
import sys
from sqlmodel import Session, select
from app.db import engine
from app.models import Customer

name_prefix, terms = sys.argv[1], sys.argv[2]
with Session(engine) as s:
    cust = s.exec(select(Customer).where(Customer.name.like(name_prefix + "%"))).first()
    if not cust:
        print(f"No customer matching '{name_prefix}'"); sys.exit(1)
    cust.terms = terms
    s.add(cust); s.commit()
    print(f"Set terms for {cust.name} -> {terms}")
