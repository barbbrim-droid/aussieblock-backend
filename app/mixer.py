"""Mixer-drum telemetry endpoint.

The on-truck sensor box posts one summary per load to /api/mixer/load and
authenticates with a shared secret in the X-Device-Key header (no per-user
login — it's a headless device). Readings are stored standalone; they're
best-effort linked to a Truck by label so the board can show them, but the
endpoint never touches the order/load dispatch flow.

Set MIXER_DEVICE_KEY in the environment to the secret the devices send;
defaults to "ab-mixer-change-me" for local dev.
"""
import os
import secrets
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from .db import get_session
from .models import MixerReading, Truck

router = APIRouter(prefix="/api/mixer", tags=["mixer"])

# Shared secret the on-truck devices send in the X-Device-Key header. Override
# with MIXER_DEVICE_KEY in production — the default is for local dev only.
DEVICE_KEY = os.getenv("MIXER_DEVICE_KEY", "").strip() or "ab-mixer-change-me"


def require_device_key(x_device_key: str = Header(default="")):
    """Reject any post that doesn't carry the shared device secret. Uses a
    constant-time compare so the key can't be guessed by timing."""
    if not secrets.compare_digest((x_device_key or "").strip(), DEVICE_KEY):
        raise HTTPException(401, "Invalid or missing device key")


class MixerLoadIn(BaseModel):
    """One load's telemetry as the device reports it. `started_at`/`ended_at`
    are epoch seconds; everything but `load_uid` is optional so a sensor that
    can't read a field still posts the rest."""
    load_uid: str
    truck_id: Optional[str] = None        # the truck label/id the device knows itself by
    started_at: Optional[float] = None    # epoch seconds
    ended_at: Optional[float] = None      # epoch seconds
    gallons: Optional[float] = None
    total_revs: Optional[int] = None
    charge_revs: Optional[int] = None
    discharge_revs: Optional[int] = None
    max_rpm: Optional[float] = None
    avg_rpm: Optional[float] = None
    pressure_idx_avg: Optional[float] = None
    pressure_idx_max: Optional[float] = None
    mix_temp_c: Optional[float] = None
    mix_temp_f: Optional[float] = None
    fw: Optional[str] = None


def _from_epoch(secs: Optional[float]) -> Optional[datetime]:
    """Epoch seconds -> naive UTC datetime (matches the rest of the app, which
    stores naive UTC). Bad/blank values just become None."""
    if secs is None:
        return None
    try:
        return datetime.utcfromtimestamp(float(secs))
    except (ValueError, OSError, OverflowError):
        return None


def _reading_json(r: MixerReading) -> dict:
    return {
        "load_uid": r.load_uid,
        "truck": r.truck_label or "—",
        "truck_id": r.truck_id,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "ended_at": r.ended_at.isoformat() if r.ended_at else None,
        "gallons": r.gallons,
        "total_revs": r.total_revs,
        "charge_revs": r.charge_revs,
        "discharge_revs": r.discharge_revs,
        "max_rpm": r.max_rpm,
        "avg_rpm": r.avg_rpm,
        "pressure_idx_avg": r.pressure_idx_avg,
        "pressure_idx_max": r.pressure_idx_max,
        "mix_temp_c": r.mix_temp_c,
        "mix_temp_f": r.mix_temp_f,
        "fw": r.fw,
        "received_at": r.received_at.isoformat() if r.received_at else None,
    }


@router.post("/load")
def post_load(body: MixerLoadIn, _: None = Depends(require_device_key),
              s: Session = Depends(get_session)):
    """Record one load's mixer telemetry (device only — needs X-Device-Key).

    Idempotent on `load_uid`: a resend of the same load returns the stored row
    instead of creating a duplicate (devices retry on flaky cell coverage)."""
    uid = (body.load_uid or "").strip()
    if not uid:
        raise HTTPException(422, "load_uid is required")

    existing = s.exec(select(MixerReading).where(MixerReading.load_uid == uid)).first()
    if existing:
        return {"ok": True, "duplicate": True, "reading": _reading_json(existing)}

    # Best-effort link to a Truck by matching the device's truck_id to Truck.label.
    truck_label = (body.truck_id or "").strip() or None
    truck_id = None
    if truck_label:
        t = s.exec(select(Truck).where(Truck.label == truck_label)).first()
        if t:
            truck_id = t.id

    r = MixerReading(
        load_uid=uid,
        truck_label=truck_label,
        truck_id=truck_id,
        started_at=_from_epoch(body.started_at),
        ended_at=_from_epoch(body.ended_at),
        gallons=body.gallons,
        total_revs=body.total_revs,
        charge_revs=body.charge_revs,
        discharge_revs=body.discharge_revs,
        max_rpm=body.max_rpm,
        avg_rpm=body.avg_rpm,
        pressure_idx_avg=body.pressure_idx_avg,
        pressure_idx_max=body.pressure_idx_max,
        mix_temp_c=body.mix_temp_c,
        mix_temp_f=body.mix_temp_f,
        fw=(body.fw or "").strip() or None,
    )
    s.add(r); s.commit(); s.refresh(r)
    return {"ok": True, "duplicate": False, "reading": _reading_json(r)}


@router.get("/readings")
def list_readings(limit: int = Query(100, ge=1, le=1000),
                  truck: Optional[str] = None,
                  s: Session = Depends(get_session)):
    """Newest-first mixer readings, optionally filtered to one truck label."""
    q = select(MixerReading)
    if truck and truck.strip():
        q = q.where(MixerReading.truck_label == truck.strip())
    q = q.order_by(MixerReading.received_at.desc(), MixerReading.id.desc()).limit(limit)
    return [_reading_json(r) for r in s.exec(q).all()]


@router.delete("/readings/{load_uid}")
def delete_reading(load_uid: str, _: None = Depends(require_device_key),
                   s: Session = Depends(get_session)):
    """Delete one reading by load_uid (needs X-Device-Key) — for clearing bench/test
    rows. Idempotent: returns ok even if it was already gone."""
    r = s.exec(select(MixerReading).where(MixerReading.load_uid == load_uid)).first()
    if r:
        s.delete(r); s.commit()
    return {"ok": True, "removed": load_uid, "existed": r is not None}
