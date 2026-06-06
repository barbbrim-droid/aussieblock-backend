"""Delete users by email."""
import sys
from sqlmodel import Session, select
from app.db import engine
from app.models import User

emails = sys.argv[1:]
with Session(engine) as s:
    for email in emails:
        user = s.exec(select(User).where(User.email == email)).first()
        if user:
            s.delete(user)
            print(f"Deleted {email} (role: {user.role})")
        else:
            print(f"No user with email {email} (already gone)")
    s.commit()
