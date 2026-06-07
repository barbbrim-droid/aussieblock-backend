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
# MOCK MODE — a truck assigned to an active delivery drives from the yard toward
# that order's job site (so the customer's map tracks THEIR truck heading to
# THEIR site); idle trucks idle in a gentle loop near the plant. Order progress
# is nudged forward so the drive animates. When the real One Step GPS key is set
# this whole function is bypassed in favour of _poll_real().
# ──────────────────────────────────────────────────────────────────────────

# Geocoded job-site coords, cached by address so we don't hit the geocoder every
# poll. address -> (lat, lng) or None when it couldn't be resolved.
_site_geocode: dict[str, tuple[float, float] | None] = {}


async def _geocode_site(addr: str) -> tuple[float, float] | None:
    """Best-effort geocode of a job-site address via Photon (OpenStreetMap, free,
    no API key), biased toward the plant. Cached; None if it can't be resolved."""
    if not addr:
        return None
    if addr in _site_geocode:
        return _site_geocode[addr]
    coords = None
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                "https://photon.komoot.io/api/",
                params={"q": addr, "limit": 1, "lat": config.PLANT_LAT, "lon": config.PLANT_LNG},
            )
            r.raise_for_status()
            feats = r.json().get("features") or []
            if feats:
                lon, lat = feats[0]["geometry"]["coordinates"]
                coords = (float(lat), float(lon))
    except Exception:
        coords = None
    _site_geocode[addr] = coords
    return coords


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compass heading (0=N, 90=E) from point 1 to point 2."""
    d_lon = math.radians(lon2 - lon1)
    y = math.sin(d_lon) * math.cos(math.radians(lat2))
    x = (math.cos(math.radians(lat1)) * math.sin(math.radians(lat2))
         - math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(d_lon))
    return (math.degrees(math.atan2(y, x)) + 360) % 360


async def _mock_step() -> None:
    # Which trucks are mid-delivery, and where is each headed? (truck_id -> (site, progress))
    with Session(engine) as s:
        active = s.exec(
            select(Order).where(Order.status.in_(("batched", "enroute", "onsite")))
        ).all()
        deliveries = {o.truck_id: (o.site, o.progress) for o in active if o.truck_id}

    # Geocode each destination (cached) outside the DB session.
    site_coords = {tid: await _geocode_site(site) for tid, (site, _p) in deliveries.items()}

    with Session(engine) as s:
        trucks = s.exec(select(Truck)).all()
        for t in trucks:
            dest = site_coords.get(t.id)
            if t.id in deliveries and dest:
                # Interpolate yard -> job site by the order's progress; point at the site.
                _site, prog = deliveries[t.id]
                f = max(0.0, min(1.0, prog))
                t.lat = config.PLANT_LAT + (dest[0] - config.PLANT_LAT) * f
                t.lng = config.PLANT_LNG + (dest[1] - config.PLANT_LNG) * f
                t.heading = _bearing(config.PLANT_LAT, config.PLANT_LNG, dest[0], dest[1])
            else:
                # Idle truck (or un-geocodable site): gentle loop ~1.5km around the plant.
                t.mock_phase = (t.mock_phase + 0.02) % 1.0
                r = 0.015
                angle = t.mock_phase * 2 * math.pi
                t.lat = config.PLANT_LAT + r * math.sin(angle)
                t.lng = config.PLANT_LNG + r * math.cos(angle)
                t.heading = (math.degrees(angle) + 90) % 360
            t.updated_at = datetime.utcnow()
            s.add(t)
        # advance order progress for anything en route so the drive animates
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
                await _mock_step()
            else:
                await _poll_real()
        except Exception as e:  # never let a hiccup kill the loop
            print("GPS poll error:", e)
        await asyncio.sleep(config.GPS_POLL_SECONDS)
