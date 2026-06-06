"""Database models. SQLModel gives us tables + validation in one place."""
from typing import Optional
from datetime import datetime
from sqlmodel import SQLModel, Field


class User(SQLModel, table=True):
    """A login. `role` decides what they can see:
      • "customer" — scoped to their own Customer (via customer_id).
      • "staff"    — office/dispatch; sees everything.
    Passwords are never stored raw — only the PBKDF2 hash (see app/auth.py)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    password_hash: str
    role: str = "customer"                       # "customer" | "staff"
    customer_id: Optional[int] = Field(default=None, foreign_key="customer.id")


class Customer(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    acct_no: str
    terms: str = "Net 10"
    credit_limit: float = 0.0
    contact: str = ""
    # QuickBooks Customer Id — the stable join key the A/R sync matches on.
    # Stable across renames/punctuation; set by the customer importer.
    qbo_id: Optional[str] = Field(default=None, index=True)


class Truck(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    label: str                                  # e.g. "Truck 14"
    gps_device_id: Optional[str] = None         # maps to a One Step GPS device
    lat: Optional[float] = None
    lng: Optional[float] = None
    heading: Optional[float] = None             # degrees, 0 = north
    updated_at: Optional[datetime] = None
    # internal: phase used only by the mock simulator
    mock_phase: float = 0.0


class Order(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    ref: str                                    # e.g. "AB-24817"
    customer_id: int = Field(foreign_key="customer.id")
    site: str
    mix: str
    qty: str
    scheduled_for: str                          # "today" / "tomorrow" / a date
    time: str
    status: str = "scheduled"                   # requested|scheduled|batched|enroute|onsite|complete
    truck_id: Optional[int] = Field(default=None, foreign_key="truck.id")
    progress: float = 0.0                       # 0..1 along the route
    notes: Optional[str] = None                 # customer's delivery instructions (optional)


class Invoice(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    number: str                                 # e.g. "INV-10428"
    customer_id: int = Field(foreign_key="customer.id")
    date: str
    amount: float
    status: str                                 # paid|due|overdue
    order_ref: Optional[str] = None
    # QuickBooks Invoice Id — lets us fetch a fresh customer-payable hosted link
    # (InvoiceLink) on demand for the "Make a payment" flow. Set by the A/R sync.
    qbo_invoice_id: Optional[str] = None


class PlusLoadRequest(SQLModel, table=True):
    """A customer tapping 'Request plus load' in the app writes one of these.
    Your office/dispatch dashboard reads them — this is the feed-back-to-office link."""
    id: Optional[int] = Field(default=None, primary_key=True)
    order_id: int = Field(foreign_key="order.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    note: str = ""
    handled: bool = False
