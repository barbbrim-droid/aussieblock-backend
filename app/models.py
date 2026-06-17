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
    role: str = "customer"                       # "customer" | "staff" | "worker" | "driver"
    customer_id: Optional[int] = Field(default=None, foreign_key="customer.id")
    phone: Optional[str] = None                  # for worker logins, so they can be texted their login
    # For a "driver" login, `company` holds the driver's NAME as it appears in
    # Order.driver (e.g. "Rodney") — that's how their deliveries are matched.
    company: Optional[str] = None                # worker's employer / who they work for (label only, not access)
    project: Optional[str] = None                # worker's current project/job (label only)


class Customer(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    acct_no: str
    terms: str = "Net 10"
    credit_limit: float = 0.0
    contact: str = ""
    email: Optional[str] = None                  # billing email (from QuickBooks); pre-fills the login form
    # QuickBooks Customer Id — the stable join key the A/R sync matches on.
    # Stable across renames/punctuation; set by the customer importer.
    qbo_id: Optional[str] = Field(default=None, index=True)
    # COD: this customer must pay before delivery; their orders require prepayment.
    cod: bool = False


class Truck(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    label: str                                  # e.g. "Truck 14"
    gps_device_id: Optional[str] = None         # maps to a One Step GPS device
    # FluidSecure (Graco) vehicle number — attaches fuel fills pulled from
    # FluidSecure to this truck. Optional; fill it in to start tracking fuel.
    fluidsecure_vehicle_id: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    heading: Optional[float] = None             # degrees, 0 = north
    updated_at: Optional[datetime] = None
    notes: Optional[str] = None                 # free-form (driver, capacity, maintenance…)
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
    driver: Optional[str] = None                 # assigned driver name (Rodney/Brandon/Henry)
    progress: float = 0.0                       # 0..1 along the route
    notes: Optional[str] = None                 # customer's delivery instructions (optional)
    slump: Optional[str] = None                 # e.g. '5"'
    admixtures: Optional[str] = None            # comma-joined, e.g. "Fiber, Color"
    hauler: Optional[str] = None                # trucking co. hauling the load (LGTZ/P&L/RAY)
    mileage: Optional[float] = None             # road miles yard→job (auto, staff-overridable)
    # Exact job-site coordinates, LEARNED from where the truck actually parked when
    # the order went On site (overrides the often-inaccurate address geocode for
    # arrival detection; reused for the same customer+site on future orders).
    site_lat: Optional[float] = None
    site_lng: Optional[float] = None
    price_override: Optional[float] = None      # staff-set custom $/yd unit price (any status, incl. complete)
    fiber_rate: Optional[float] = None          # staff-set custom fiber $/lb for this order (blank = price-sheet rate)
    use_for: Optional[str] = None               # what the concrete is for (slab, curbs, …)
    project: Optional[str] = None               # optional project / job name or reference
    batch_ticket: Optional[str] = None          # stored PDF filename once a batch ticket is uploaded
    # Full delivered batch-ticket, as a JSON string: every field on the paper
    # ticket (plant, air, load, ordered/delivered, water reducer, retarder, the
    # four times, inspector, the Rock/Sand/Cement/Air/Water mix-design grid,
    # pricing, received-by). Lets the app hold a complete digital copy.
    batch_data: Optional[str] = None
    # Light/print-friendly version of the batch ticket (the on-screen one is dark
    # to match the app; this is what the "Print" button serves).
    batch_ticket_print: Optional[str] = None
    archived: bool = False                       # staff hid this completed order from the default lists
    # COD / prepay: when required, the order can't be dispatched until paid.
    prepay_required: bool = False
    prepay_amount: Optional[float] = None       # load total staff set
    prepay_invoice_id: Optional[str] = None     # QuickBooks invoice created for the prepayment
    prepaid: bool = False
    # Proof of delivery — the customer's on-site sign-off captured by the driver.
    signed_by: Optional[str] = None             # printed name of who signed for it
    signature: Optional[str] = None             # stored signature image filename
    signed_at: Optional[str] = None             # ISO timestamp of the sign-off
    water_added: Optional[str] = None           # gallons of water added on site (driver records at sign-off)
    completed_at: Optional[str] = None           # ISO date the order was marked complete (drives material draw-down)


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


class Load(SQLModel, table=True):
    """One truck-load of a continuous pour (orders over 10 yd³ are split into ~10-yd
    loads). Each load tracks its own truck, driver, status and batch ticket so a big
    pour is one card on the board with the loads tucked inside."""
    id: Optional[int] = Field(default=None, primary_key=True)
    order_id: int = Field(foreign_key="order.id")
    seq: int                                     # load number within the pour (1,2,3…)
    qty: str                                     # yards on this load, e.g. "10" or "5"
    truck_id: Optional[int] = Field(default=None, foreign_key="truck.id")
    driver: Optional[str] = None
    status: str = "scheduled"                    # same stages as an order
    progress: float = 0.0
    batch_ticket: Optional[str] = None           # this load's branded ticket filename


class FuelTransaction(SQLModel, table=True):
    """One fuel/fluid dispense pulled from FluidSecure (Graco). De-duplicated on
    `external_id` so re-pulling the rolling window never double-counts a fill.
    Matched to a Truck by FluidSecure vehicle number (Truck.fluidsecure_vehicle_id);
    `truck_id` stays None for a vehicle no truck is mapped to yet."""
    id: Optional[int] = Field(default=None, primary_key=True)
    external_id: str = Field(index=True, unique=True)   # FluidSecure transaction id, or a synthesized key
    truck_id: Optional[int] = Field(default=None, foreign_key="truck.id")
    vehicle_no: Optional[str] = None                    # FluidSecure vehicle number as reported
    gallons: Optional[float] = None                     # quantity dispensed
    fuel_type: Optional[str] = None                     # e.g. "Diesel", "DEF"
    odometer: Optional[float] = None                    # odometer/hours entered at the pump
    driver: Optional[str] = None                        # driver/operator name on the transaction
    pin: Optional[str] = None                           # operator PIN on the transaction (if reported)
    occurred_at: Optional[datetime] = None              # when the fill happened (FluidSecure time)
    raw: Optional[str] = None                           # original record as JSON, for audit/unknown fields
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Doc(SQLModel, table=True):
    """A Knowledge Center document — a shared-library PDF the office uploads.
    Every logged-in user (workers, admins, customers) can list and view them."""
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    filename: str = ""                 # stored file on the persistent disk (knowledge/{id}.pdf)
    uploaded_at: str = ""              # ISO date for display/sort


class Material(SQLModel, table=True):
    """A tracked raw material. Cementitious ones (Portland, Slag) are kept in silos
    with on-hand = opening balance + tons received − tons used, counted from
    `counted_on`; the fill gauge + reorder alert read off that. Aggregates and
    admixtures (Gravel, Sand, Mac Matrix Fiber, Air Entrainer, Water Reducer) set
    `track_inventory=False` and are tracked by actual batched usage + cost only.
    Usage cost = actual amount used × `cost_rate` ($ per `unit`)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)   # "Portland" | "Slag" | "Gravel" | ...
    unit: str = "ton"                            # display/cost unit: "ton" | "lb" | "oz"
    cost_rate: float = 0.0                       # $ per unit, for usage-based cost (editable)
    track_inventory: bool = True                 # True = silo with on-hand draw-down; False = usage + cost only
    capacity_tons: float = 0.0                   # silo capacity (tons) — for the fill gauge
    reorder_tons: float = 0.0                    # alert when on-hand falls to/below this
    opening_tons: float = 0.0                    # silo content when counting started
    counted_on: Optional[str] = None             # ISO date the opening balance was taken (usage/receipts count from here)


class MaterialReceipt(SQLModel, table=True):
    """One incoming load of cement/slag received from a supplier — the record you
    reconcile against the supplier's invoice."""
    id: Optional[int] = Field(default=None, primary_key=True)
    material_id: int = Field(foreign_key="material.id")
    received_on: str                             # ISO date received
    supplier: Optional[str] = None
    tons: float = 0.0
    ticket_no: Optional[str] = None              # scale/delivery ticket number
    invoice_no: Optional[str] = None             # supplier invoice number
    unit_cost: Optional[float] = None            # $/ton (optional)
    total_cost: Optional[float] = None           # invoice line total (optional)
    invoice_matched: bool = False                # reconciled against the supplier invoice
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class MixDesign(SQLModel, table=True):
    """Cementitious content per cubic yard for a mix — drives the silo draw-down
    (tons used = order yards × lb/yd ÷ 2000). Editable by the office; a mix with no
    row here simply contributes 0 to usage (surfaced as 'unmapped' in the UI)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    mix: str = Field(index=True, unique=True)    # e.g. "3500 PSI"
    cement_lb_yd: float = 0.0                    # Portland cement, lb per cubic yard
    slag_lb_yd: float = 0.0                      # slag, lb per cubic yard
