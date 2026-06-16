"""SQLite database setup. SQLite is just a file on disk — nothing to install."""
import os

from sqlmodel import SQLModel, create_engine, Session
from sqlalchemy import text

from . import config

# In production DATA_DIR points at a persistent disk (e.g. /data); locally it's
# empty, so this stays "aussieblock.db" in the working directory as before.
DB_PATH = config.data_path("aussieblock.db")
DB_URL = f"sqlite:///{DB_PATH}"
# check_same_thread=False lets the background GPS poller share the connection.
engine = create_engine(DB_URL, echo=False, connect_args={"check_same_thread": False})


def init_db() -> None:
    if config.DATA_DIR:
        os.makedirs(config.DATA_DIR, exist_ok=True)   # ensure the disk mount exists
    SQLModel.metadata.create_all(engine)   # creates any missing tables
    _run_migrations()                      # adds any missing columns


# New columns added to existing models go here. create_all() makes new tables
# but never alters existing ones, so a database created before a column was added
# needs the column backfilled. Each step is idempotent and safe to re-run.
_COLUMN_MIGRATIONS = {
    "customer": {"qbo_id": "VARCHAR", "cod": "BOOLEAN DEFAULT 0", "email": "VARCHAR"},
    "user": {"phone": "VARCHAR", "company": "VARCHAR", "project": "VARCHAR"},
    "truck": {"notes": "VARCHAR", "fluidsecure_vehicle_id": "VARCHAR"},
    "fueltransaction": {"driver": "VARCHAR"},   # added after the table first shipped
    "invoice": {"qbo_invoice_id": "VARCHAR"},   # for the "Make a payment" link
    "order": {
        "notes": "VARCHAR", "slump": "VARCHAR", "admixtures": "VARCHAR", "use_for": "VARCHAR", "project": "VARCHAR", "batch_ticket": "VARCHAR", "batch_data": "VARCHAR", "batch_ticket_print": "VARCHAR", "archived": "BOOLEAN DEFAULT 0", "driver": "VARCHAR",
        "hauler": "VARCHAR", "mileage": "FLOAT", "price_override": "FLOAT",
        "prepay_required": "BOOLEAN DEFAULT 0", "prepay_amount": "FLOAT",
        "prepay_invoice_id": "VARCHAR", "prepaid": "BOOLEAN DEFAULT 0",
        "signed_by": "VARCHAR", "signature": "VARCHAR", "signed_at": "VARCHAR", "water_added": "VARCHAR",
    },
}


def _run_migrations() -> None:
    # Identifiers are quoted because some table names ("order") are SQL keywords.
    with engine.connect() as conn:
        for table, columns in _COLUMN_MIGRATIONS.items():
            existing = {row[1] for row in conn.execute(text(f'PRAGMA table_info("{table}")'))}
            for col, coltype in columns.items():
                if col not in existing:
                    conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{col}" {coltype}'))
        conn.commit()


def get_session():
    with Session(engine) as session:
        yield session
