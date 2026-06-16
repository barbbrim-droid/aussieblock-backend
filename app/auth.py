"""Authentication: password hashing + JWT tokens + FastAPI dependencies.

Two kinds of users log in:
  • customers — see only THEIR own orders and billing.
  • staff     — the office/dispatch side; can see everything and run imports.

A successful login returns a signed JWT (a tamper-proof token). The front-end
sends it back on every request as an `Authorization: Bearer <token>` header.
The dependencies at the bottom turn that token back into a User row and let
each endpoint say "I need any logged-in user" or "I need staff."

Password hashing uses Python's standard-library PBKDF2 — no extra dependency,
and far safer than storing raw passwords.
"""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlmodel import Session, select

from .db import get_session
from .models import User
from . import config


# ──────────────────────────────────────────────────────────────────────────
# Password hashing (PBKDF2-HMAC-SHA256, salted, from the standard library)
# Stored format:  pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>
# ──────────────────────────────────────────────────────────────────────────
_ALGO = "sha256"
_ITERATIONS = 240_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(_ALGO, password.encode(), salt, _ITERATIONS)
    return f"pbkdf2_{_ALGO}${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _scheme, iters, salt_hex, hash_hex = stored.split("$")
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        dk = hashlib.pbkdf2_hmac(_ALGO, password.encode(), salt, int(iters))
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(dk, expected)  # constant-time, avoids timing leaks


# ──────────────────────────────────────────────────────────────────────────
# JWT tokens
# ──────────────────────────────────────────────────────────────────────────
def create_access_token(user: User) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=config.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": str(user.id),
        "role": user.role,
        "customer_id": user.customer_id,
        "exp": expire,
    }
    return jwt.encode(payload, config.SECRET_KEY, algorithm="HS256")


# tokenUrl points at our login route so the /docs "Authorize" button works.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")


def get_current_user(
    token: str = Depends(oauth2_scheme),
    s: Session = Depends(get_session),
) -> User:
    """Decode the bearer token and load the matching user. Raises 401 if the
    token is missing, expired, tampered with, or points at a deleted user."""
    cred_exc = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, config.SECRET_KEY, algorithms=["HS256"])
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise cred_exc
    user = s.get(User, user_id)
    if not user:
        raise cred_exc
    return user


# The dispatch board is for the dispatch operator ONLY. "worker" logins are NOT
# office users — they're a customer's field people, scoped to that one company
# (orders + tracking, no billing, no board). So require_staff = staff only.
def require_staff(user: User = Depends(get_current_user)) -> User:
    """Dispatch/office access — the dispatch operator (staff) only. Workers and
    customers are company-scoped and never reach the board."""
    if user.role != "staff":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Staff access required")
    return user


def require_finance(user: User = Depends(get_current_user)) -> User:
    """Endpoints that expose money / customer account info — full staff only."""
    if user.role != "staff":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Financial access is restricted to approved staff")
    return user


# A "driver" login is one of Aussieblock's own drivers on the truck tablet: they
# see their assigned deliveries, the batch ticket, and capture the customer's
# sign-off. No board, no billing, no other companies.
def require_driver(user: User = Depends(get_current_user)) -> User:
    """Truck-tablet (driver) access only."""
    if user.role != "driver":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Driver access required")
    return user
