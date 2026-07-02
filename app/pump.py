"""
Pump relay control — add this file to app/ and wire it into main.py.

Two new DB tables (auto-created on next deploy):
  PumpPin   — 4-digit driver PINs managed by staff
  PumpState — current commanded relay state per device

Endpoints added:
  GET  /pump_state?device_id=...  — ESP32 polls (no auth)
  POST /pump_control              — driver submits PIN + desired state
  GET  /pump_pins                 — staff: list all PINs
  POST /pump_pins                 — staff: create/update a PIN
  DELETE /pump_pins/{pin}         — staff: remove a PIN
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Field, Session, SQLModel, select

from .auth import require_staff
from .db import get_session

router = APIRouter()


# ── Models (also import these in models.py or let SQLModel find them here) ──

class PumpPin(SQLModel, table=True):
    """4-digit PIN codes that authorise a driver to operate the yard pump."""
    id: Optional[int] = Field(default=None, primary_key=True)
    pin: str = Field(index=True, unique=True)
    label: str                                       # driver name for display
    created_at: datetime = Field(default_factory=datetime.utcnow)


class PumpState(SQLModel, table=True):
    """Current commanded relay state per pump device.
    The ESP32 polls GET /pump_state; drivers flip it via POST /pump_control."""
    device_id: str = Field(primary_key=True)
    relay_on: bool = False
    commanded_by: Optional[str] = None              # PumpPin.label of last actor
    commanded_at: Optional[datetime] = None


# ── Request schemas ─────────────────────────────────────────────────────────

class PumpControlIn(BaseModel):
    device_id: str
    pin: str        # 4-digit driver PIN
    relay_on: bool

class PumpPinIn(BaseModel):
    pin: str
    label: str


# ── Endpoints ───────────────────────────────────────────────────────────────

@router.get("/pump_state")
def get_pump_state(device_id: str, s: Session = Depends(get_session)):
    """ESP32 polls this every 3 s. No auth — device has no credentials."""
    state = s.get(PumpState, device_id)
    if state is None:
        return {"relay": "off"}
    return {"relay": "on" if state.relay_on else "off"}


@router.post("/pump_control")
def pump_control(body: PumpControlIn, s: Session = Depends(get_session)):
    """Driver submits their 4-digit PIN from the app to turn the pump on/off."""
    driver_pin = s.exec(select(PumpPin).where(PumpPin.pin == body.pin)).first()
    if not driver_pin:
        raise HTTPException(status_code=403, detail="Invalid PIN")

    state = s.get(PumpState, body.device_id)
    if state is None:
        state = PumpState(device_id=body.device_id)
        s.add(state)

    state.relay_on = body.relay_on
    state.commanded_by = driver_pin.label
    state.commanded_at = datetime.utcnow()
    s.commit()
    return {
        "relay": "on" if state.relay_on else "off",
        "by": driver_pin.label,
    }


@router.get("/pump_pins")
def list_pump_pins(s: Session = Depends(get_session), _=Depends(require_staff)):
    """Staff: list all driver PINs."""
    return s.exec(select(PumpPin)).all()


@router.post("/pump_pins", status_code=201)
def create_pump_pin(
    body: PumpPinIn,
    s: Session = Depends(get_session),
    _=Depends(require_staff),
):
    """Staff: create or update a driver PIN."""
    existing = s.exec(select(PumpPin).where(PumpPin.pin == body.pin)).first()
    if existing:
        existing.label = body.label
        s.add(existing)
    else:
        s.add(PumpPin(pin=body.pin, label=body.label))
    s.commit()
    return {"ok": True}


@router.delete("/pump_pins/{pin}")
def delete_pump_pin(
    pin: str,
    s: Session = Depends(get_session),
    _=Depends(require_staff),
):
    """Staff: revoke a driver PIN."""
    row = s.exec(select(PumpPin).where(PumpPin.pin == pin)).first()
    if not row:
        raise HTTPException(status_code=404, detail="PIN not found")
    s.delete(row)
    s.commit()
    return {"ok": True}
