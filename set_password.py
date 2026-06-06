"""Set a user's password by email."""
import sys
from sqlmodel import Session, select
from app.db import engine
from app.models import User
from app.auth import hash_password

email = sys.argv[1]
new_password = sys.argv[2]

with Session(engine) as s:
    user = s.exec(select(User).where(User.email == email)).first()
    if not user:
        print(f"No user with email {email}")
        sys.exit(1)
    user.password_hash = hash_password(new_password)
    s.add(user)
    s.commit()
    print(f"Password updated for {email} (role: {user.role})")
