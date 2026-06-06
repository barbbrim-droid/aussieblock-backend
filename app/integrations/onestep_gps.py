"""One Step GPS integration.

Two modes:
  • MOCK  (no API key)  — trucks simulate movement so you can build the whole
                          app before your key arrives.
  • LIVE  (key present) — polls One Step GPS for real device positions.

The poll loop runs in the background and updates the `Truck` rows in the DB.
"""
import asyncio
import math
from datetime import datetime

import httpx
from sqlmodel import Session, select

from ..db import engine
from ..models import Truck, Order
from .. import config


# ──────────────────────────────────────────────────────────────────────────
# MOCK MODE — moves each truck along a gentle loop near the plant, and nudges
# any assigned order's progress forward so the app shows live movement.
# ──────────────────────────────────────────────────────────────────────────
def _mock_step() -> None:
    with Session(engine) as s:
        trucks = s.exec(select(Truck)).all()
        for t in trucks:
            t.mock_phase = (t.mock_phase + 0.02) % 1.0
            # small circular path ~1.5km around the plant
            r = 0.015
            angle = t.mock_phase * 2 * math.pi
            t.lat = config.PLANT_LAT + r * math.sin(angle)
            t.lng = config.PLANT_LNG + r * math.cos(angle)
            t.heading = (math.degrees(angle) + 90) % 360
            t.updated_at = datetime.utcnow()
            s.add(t)
        # advance order progress for anything en route
        orders = s.exec(select(Order)).all()
        for o in orders:
            if o.status in ("enroute", "batched") and o.truck_id:
                o.progress = min(1.0, o.progress + 0.01)
                if o.progress >= 1.0:
                    o.status = "onsite"
                elif o.progress > 0.05:
                    o.status = "enroute"
                s.add(o)
        s.commit()


# ──────────────────────────────────────────────────────────────────────────
# LIVE MODE — calls the One Step GPS API. The exact endpoint/response shape
# may differ slightly from what's below; adjust to match the docs they send.
# ──────────────────────────────────────────────────────────────────────────
async def _poll_real() -> None:
    url = f"{config.ONESTEP_API_BASE}/device"
    params = {"latest_point": "true", "api-key": config.ONESTEP_API_KEY}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

    # Expecting a list of devices; each with an id and a latest point.
    devices = data if isinstance(data, list) else data.get("result_list", data.get("devices", []))
    with Session(engine) as s:
        for dev in devices:
            dev_id = str(dev.get("device_id") or dev.get("id") or "")
            point = dev.get("latest_device_point") or dev.get("latest_point") or dev
            lat = point.get("lat") or point.get("latitude")
            lng = point.get("lng") or point.get("longitude")
            heading = point.get("heading") or point.get("angle") or 0
            if not dev_id or lat is None or lng is None:
                continue
            truck = s.exec(select(Truck).where(Truck.gps_device_id == dev_id)).first()
            if truck:
                truck.lat = float(lat)
                truck.lng = float(lng)
                truck.heading = float(heading)
                truck.updated_at = datetime.utcnow()
                s.add(truck)
        s.commit()


async def gps_poll_loop() -> None:
    mode = "MOCK" if config.USE_MOCK_GPS else "LIVE"
    print(f"GPS poller started in {mode} mode (every {config.GPS_POLL_SECONDS}s).")
    while True:
        try:
            if config.USE_MOCK_GPS:
                _mock_step()
            else:
                await _poll_real()
        except Exception as e:  # never let a hiccup kill the loop
            print("GPS poll error:", e)
        await asyncio.sleep(config.GPS_POLL_SECONDS)
