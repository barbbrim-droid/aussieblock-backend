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
# YARD GEOFENCE — keep "en route" honest. The plant operator sets an order to
# "batched" while it loads at the yard; it only becomes "enroute" once the truck
# physically crosses the yard geofence (so customers see "en route" when the
# truck is actually on the road, not the moment it's batched).
# ──────────────────────────────────────────────────────────────────────────
def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two lat/lng points, in meters."""
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _advance_on_yard_exit(s: Session, truck: Truck) -> None:
    """If `truck` has left the yard geofence, promote any of its 'batched' orders
    to 'enroute'. Only moves batched→enroute; never flips an order back."""
    if truck.lat is None or truck.lng is None or truck.id is None:
        return
    dist = _haversine_m(truck.lat, truck.lng, config.PLANT_LAT, config.PLANT_LNG)
    if dist <= config.YARD_GEOFENCE_M:
        return  # still inside the yard — keep it "batched" (loading)
    orders = s.exec(
        select(Order).where(Order.truck_id == truck.id, Order.status == "batched")
    ).all()
    for o in orders:
        o.status = "enroute"
        o.progress = max(o.progress, 0.05)   # nudge off 0 so the route bar shows movement
        s.add(o)
        print(f"Yard exit: {truck.label} left the yard ({dist:.0f}m) -> order {o.ref} now en route.")


# ──────────────────────────────────────────────────────────────────────────
# ARRIVAL (STOP) DETECTION — a concrete truck parks at the pour, so instead of
# trusting an (imprecise) geocoded job address, we watch for the assigned truck
# sitting still. After it's been parked long enough away from the yard, the order
# is FLAGGED as "looks arrived" (arrival_pending) for dispatch to confirm "On
# site" — we do NOT auto-change status. State is in-memory (single uvicorn worker).
# ──────────────────────────────────────────────────────────────────────────
_stop_state: dict = {}   # truck_id -> {"lat", "lng", "since": datetime}


def _update_stop_state(truck: Truck) -> None:
    """Track the spot a truck has been parked at, and since when. Resets the anchor
    (and the clock) whenever the truck moves more than ARRIVAL_MOVE_M."""
    if truck.id is None or truck.lat is None or truck.lng is None:
        return
    st = _stop_state.get(truck.id)
    if st is None or _haversine_m(truck.lat, truck.lng, st["lat"], st["lng"]) > config.ARRIVAL_MOVE_M:
        _stop_state[truck.id] = {"lat": truck.lat, "lng": truck.lng, "since": datetime.utcnow()}


# ──────────────────────────────────────────────────────────────────────────
# RETURN TRIP — once an order is On site, we pin the job location (the truck's
# GPS spot at that moment — no address geocoding needed). When the truck then
# pulls away from the job it flips to "returning"; back inside the yard fence it
# auto-completes. State is in-memory (single uvicorn worker).
# ──────────────────────────────────────────────────────────────────────────
_job_loc: dict = {}   # order_id -> {"lat","lng","since"} pinned when it went On site


def _advance_return(s: Session, truck: Truck) -> None:
    if truck.id is None or truck.lat is None or truck.lng is None:
        return
    orders = s.exec(
        select(Order).where(Order.truck_id == truck.id,
                            Order.status.in_(["onsite", "pouring", "returning"]))
    ).all()
    for o in orders:
        if o.status in ("onsite", "pouring"):
            st = _job_loc.get(o.id)
            if st is None:
                _job_loc[o.id] = {"lat": truck.lat, "lng": truck.lng, "since": datetime.utcnow()}
                continue
            if _haversine_m(truck.lat, truck.lng, st["lat"], st["lng"]) > config.RETURN_LEAVE_SITE_M:
                o.status = "returning"             # pulled away from the job
                s.add(o)
                print(f"Left job: {truck.label} -> order {o.ref} returning to yard.")
            elif o.status == "onsite" and (datetime.utcnow() - st["since"]).total_seconds() >= config.POUR_DELAY_SECONDS:
                o.status = "pouring"               # on site long enough -> pouring
                s.add(o)
                print(f"On site {config.POUR_DELAY_SECONDS // 60}m: {truck.label} -> order {o.ref} now pouring.")
        elif o.status == "returning":
            if _haversine_m(truck.lat, truck.lng, config.PLANT_LAT, config.PLANT_LNG) <= config.YARD_GEOFENCE_M:
                o.status = "complete"
                o.progress = 1.0
                _job_loc.pop(o.id, None)
                s.add(o)
                print(f"Back at yard: {truck.label} -> order {o.ref} complete.")


def arrival_pending(truck: Truck) -> bool:
    """True when `truck` looks parked at a job: stopped >= the dwell time and away
    from the yard. Read by the API to flag en-route orders for an On-site confirm."""
    if truck is None or truck.id is None or truck.lat is None or truck.lng is None:
        return False
    if _haversine_m(truck.lat, truck.lng, config.PLANT_LAT, config.PLANT_LNG) <= config.YARD_GEOFENCE_M:
        return False  # sitting at the yard isn't "arrived at a job"
    st = _stop_state.get(truck.id)
    if not st:
        return False
    return (datetime.utcnow() - st["since"]).total_seconds() >= config.ARRIVAL_DWELL_SECONDS


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
                _update_stop_state(truck)         # track how long it's been parked (arrival detection)
                _advance_on_yard_exit(s, truck)   # batched -> enroute when it leaves the yard
                _advance_return(s, truck)         # onsite -> returning -> complete on the way back
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
