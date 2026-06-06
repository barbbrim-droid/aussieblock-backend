"""Create (or reset) a login tied to a customer matched by name.
Usage: python create_login.py <email> <password> <customer-name-prefix> [role]"""
import sys
from sqlmodel import Session, select
from app.db import engine
from app.models import User, Customer
from app.auth import hash_password

email, pw, name_prefix = sys.argv[1], sys.argv[2], sys.argv[3]
role = sys.argv[4] if len(sys.argv) > 4 else "customer"

with Session(engine) as s:
    cid = None
    if role == "customer":
        cust = s.exec(select(Customer).where(Customer.name.like(name_prefix + "%"))).first()
        if not cust:
            print(f"No customer matching '{name_prefix}'"); sys.exit(1)
        cid = cust.id
        cust_name = cust.name
    existing = s.exec(select(User).where(User.email == email)).first()
    if existing:
        existing.password_hash = hash_password(pw); existing.role = role; existing.customer_id = cid
        s.add(existing)
    else:
        s.add(User(email=email, password_hash=hash_password(pw), role=role, customer_id=cid))
    s.commit()
print(f"Login ready -> {email} / {pw}  (role={role}" + (f", customer={cust_name})" if cid else ")"))
