"""FluidSecure (Graco) fuel-tracking integration.

Pulls fuel/fluid dispense transactions from FluidSecure's Export Transactions API
and stores one FuelTransaction per fill, matched to a Truck by its FluidSecure
vehicle number (Truck.fluidsecure_vehicle_id).

Two modes:
  • MOCK (no token/company) — the loop idles; the rest of the app runs normally.
  • LIVE (token + company)   — polls FluidSecure on a schedule.

FluidSecure's export field names aren't fully documented, so parsing tries the
common variants and stashes the original record in `raw`. On the first live pull
it prints one sample record to the logs — if a field comes through blank, copy a
key from that sample into the relevant _first(...) list below and redeploy.

API (per FluidSecure docs):
  POST https://www.fluidsecure.net/api/External/ExportTransactions
  Authorization: Bearer <token>      Content-Type: application/x-www-form-urlencoded
  body: TransactionFromDate, TransactionToDate ("YYYY-MM-DD hh:mm"), CompanyName
  NOTE: bad params still return 200 with a JSON failure message (no list).
"""
import asyncio
import json
from datetime import datetime, timedelta

import httpx
from sqlmodel import Session, select

from ..db import engine
from ..models import Truck, FuelTransaction
from .. import config


def _norm(k) -> str:
    """Normalize a key to letters+digits only, lowercased — so 'Vehicle Number',
    'VehicleNumber' and 'vehicle_number' all collapse to the same thing. This is
    what makes parsing robust to however FluidSecure spells its JSON keys."""
    return "".join(ch for ch in str(k).lower() if ch.isalnum())


def _first(d: dict, *keys):
    """First present, non-empty value among `keys`, matched punctuation/space- and
    case-insensitively (see _norm)."""
    norm = {_norm(k): v for k, v in d.items()}
    for k in keys:
        v = norm.get(_norm(k))
        if v not in (None, ""):
            return v
    return None


def _to_float(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _parse_dt(v):
    if not v:
        return None
    s = str(v).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
                "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _external_id(rec: dict, vehicle_no, occurred_at, gallons) -> str:
    """Stable de-dup key: prefer FluidSecure's own transaction id, else synthesize
    one from vehicle + time + gallons (enough to recognize a repeated record)."""
    tid = _first(rec, "TransactionNumber", "Transaction #", "TransactionId", "TransactionID", "Id")
    if tid:
        return f"fs:{tid}"
    stamp = occurred_at.isoformat() if occurred_at else "?"
    return f"fs:{vehicle_no or '?'}|{stamp}|{gallons if gallons is not None else '?'}"


async def _fetch(from_dt: datetime, to_dt: datetime):
    url = f"{config.FLUIDSECURE_API_BASE}/External/ExportTransactions"
    headers = {"Authorization": f"Bearer {config.FLUIDSECURE_TOKEN}",
               "Content-Type": "application/x-www-form-urlencoded"}
    body = {
        "TransactionFromDate": from_dt.strftime("%Y-%m-%d %H:%M"),
        "TransactionToDate": to_dt.strftime("%Y-%m-%d %H:%M"),
        "CompanyName": config.FLUIDSECURE_COMPANY,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, headers=headers, data=body)
        resp.raise_for_status()
        return resp.json()


def _records(payload):
    """Pull the list of transaction dicts out of whatever envelope is returned."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("Transactions", "transactions", "Data", "data",
                    "Result", "result", "Records", "records"):
            v = payload.get(key)
            if isinstance(v, list):
                return v
    return []


_printed_sample = False


def _ingest(records: list) -> int:
    """Store any transactions we haven't seen. Returns the count newly added."""
    global _printed_sample
    added = 0
    with Session(engine) as s:
        trucks = s.exec(
            select(Truck).where(Truck.fluidsecure_vehicle_id.is_not(None))
        ).all()
        by_vehicle = {str(t.fluidsecure_vehicle_id).strip().lower(): t.id for t in trucks}
        for rec in records:
            if not isinstance(rec, dict):
                continue
            if not _printed_sample:
                print("FluidSecure sample record:", json.dumps(rec)[:600])
                _printed_sample = True
            # Candidate keys cover the FluidSecure web columns (Vehicle Number,
            # Fluid Quantity, Product, Current Odometer, Drivers Name, …) plus
            # common variants; _norm makes spacing/punctuation differences moot.
            vehicle_no = _first(rec, "Vehicle Number", "VehicleNumber", "Vehicle", "VehicleNo",
                                "VehicleName", "Asset", "AssetName", "Unit")
            gallons = _to_float(_first(rec, "Fluid Quantity", "FluidQuantity", "Quantity",
                                       "Gallons", "Volume", "Amount", "DispensedQuantity"))
            occurred_at = _parse_dt(_first(rec, "Transaction Date & Time", "TransactionDateTime",
                                           "TransactionDate", "Date", "DateTime", "Timestamp"))
            odometer = _to_float(_first(rec, "Current Odometer", "CurrentOdometer", "Odometer",
                                        "Mileage", "Miles", "Hours", "Hourmeter"))
            fuel_type = _first(rec, "Product", "ProductName", "FuelType", "Fluid", "FluidName")
            driver = _first(rec, "Drivers Name", "DriversName", "DriverName", "Driver",
                            "Operator", "OperatorName", "Personnel")
            pin = _first(rec, "PIN", "Pin", "OperatorPIN")
            ext = _external_id(rec, vehicle_no, occurred_at, gallons)
            if s.exec(select(FuelTransaction).where(FuelTransaction.external_id == ext)).first():
                continue   # already stored — rolling-window overlap
            truck_id = by_vehicle.get(str(vehicle_no).strip().lower()) if vehicle_no else None
            s.add(FuelTransaction(
                external_id=ext, truck_id=truck_id,
                vehicle_no=str(vehicle_no) if vehicle_no else None,
                gallons=gallons, fuel_type=str(fuel_type) if fuel_type else None,
                odometer=odometer, driver=str(driver) if driver else None,
                pin=str(pin) if pin else None,
                occurred_at=occurred_at, raw=json.dumps(rec)[:4000],
            ))
            added += 1
        if added:
            s.commit()
    return added


async def _poll_once() -> None:
    # Pad the upper bound a day for any timezone skew; dedup makes overlap harmless.
    to_dt = datetime.utcnow() + timedelta(days=1)
    from_dt = datetime.utcnow() - timedelta(days=config.FUEL_LOOKBACK_DAYS)
    payload = await _fetch(from_dt, to_dt)
    records = _records(payload)
    if not records:
        # Bad params come back as 200 + a JSON failure message — surface it once.
        print("FluidSecure: no transactions (response:", json.dumps(payload)[:300], ")")
        return
    added = _ingest(records)
    if added:
        print(f"FluidSecure: stored {added} new fuel transaction(s).")


async def fuel_poll_loop() -> None:
    if not config.USE_FLUIDSECURE:
        print("FluidSecure poller idle (set FLUIDSECURE_TOKEN + FLUIDSECURE_COMPANY to enable).")
        return
    print(f"FluidSecure fuel poller started (every {config.FUEL_POLL_SECONDS}s).")
    while True:
        try:
            await _poll_once()
        except Exception as e:   # never let a hiccup kill the loop
            print("FluidSecure poll error:", e)
        await asyncio.sleep(config.FUEL_POLL_SECONDS)
