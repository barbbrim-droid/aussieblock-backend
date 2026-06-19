"""Price sheet storage + the per-ticket pricing math.

The sheet is a JSON file on the persistent data disk so it survives deploys.
Staff edit it from the app (GET/PUT /price-sheet). Pricing for a ticket is
computed from the sheet plus the order's mix/customer and the load's yards.
"""
import json
import os
import re

DEFAULT_SHEET = {
    "tax_pct": 6.75,
    "short_load_fee": 200.0,
    "short_load_under_yd": 5.0,
    "backhaul_per_yd": 50.0,
    "backhaul_under_yd": 3.0,
    "mixes": [],        # [{"mix": "3000 PSI", "price": 0.0, "haul": 0.0}]
    "overrides": [],    # [{"customer": "...", "mix": "" (=any), "price": 0.0}]
    "admixtures": [],   # [{"name": "Fiber", "rate": 3.75, "per": "lb"|"yard"}]
    "self_haul_customers": [],   # pickup customers (concrete only, no delivery/load fees)
    # Fuel cost: FluidSecure reports gallons, not dollars, so staff set a $/gal
    # rate. `fuel_prices` holds per-product rates; `fuel_price_default` covers any
    # product without one. Used to cost the fuel view's gallons.
    "fuel_price_default": 0.0,
    "fuel_prices": [],  # [{"product": "Diesel", "price": 3.85}]
}

# Haul rate per yard by road miles from the yard (Aussieblock Delivery Pricing).
# Mileage rounds UP to the next bracket; the first bracket whose max_mi >= miles wins.
DEFAULT_BRACKETS = [
    {"max_mi": 10, "rate": 35.00}, {"max_mi": 24, "rate": 38.50},
    {"max_mi": 25, "rate": 43.50}, {"max_mi": 30, "rate": 48.50},
    {"max_mi": 35, "rate": 53.50}, {"max_mi": 40, "rate": 58.50},
    {"max_mi": 45, "rate": 63.50}, {"max_mi": 50, "rate": 68.50},
    {"max_mi": 55, "rate": 73.50}, {"max_mi": 60, "rate": 78.50},
    {"max_mi": 65, "rate": 83.50}, {"max_mi": 70, "rate": 88.50},
    {"max_mi": 75, "rate": 93.50}, {"max_mi": 80, "rate": 98.50},
    {"max_mi": 85, "rate": 103.50}, {"max_mi": 90, "rate": 108.50},
    {"max_mi": 95, "rate": 113.50}, {"max_mi": 100, "rate": 118.50},
]
DEFAULT_SHEET["delivery_brackets"] = DEFAULT_BRACKETS


def compute_delivery(sheet: dict, mileage, yards) -> dict:
    """Haul (delivery) cost for a load: the bracket rate for the road miles ×
    yards. Returns {mileage, rate, total}."""
    brackets = (sheet or {}).get("delivery_brackets") or DEFAULT_BRACKETS
    mi, yd = _num(mileage), _num(yards)
    rate = 0.0
    if mi > 0:
        ordered = sorted(brackets, key=lambda b: _num(b.get("max_mi")))
        rate = _num(ordered[-1].get("rate")) if ordered else 0.0
        for b in ordered:
            if mi <= _num(b.get("max_mi")):
                rate = _num(b.get("rate"))
                break
    return {"mileage": mi, "rate": rate, "total": round(rate * yd, 2)}


def road_miles(address: str):
    """Road miles from the Aussieblock yard to a job address (Google Distance
    Matrix). Returns float miles rounded to 0.1, or None if unavailable."""
    from . import config
    if not (config.GEOCODE_API_KEY and address):
        return None
    try:
        import httpx
        r = httpx.get(
            "https://maps.googleapis.com/maps/api/distancematrix/json",
            params={"origins": f"{config.PLANT_LAT},{config.PLANT_LNG}",
                    "destinations": address, "units": "imperial",
                    "key": config.GEOCODE_API_KEY},
            timeout=12,
        )
        el = r.json()["rows"][0]["elements"][0]
        if el.get("status") == "OK":
            return round(el["distance"]["value"] / 1609.34, 1)
    except Exception:
        pass
    return None


def _path() -> str:
    from . import config
    return os.path.join(config.data_path("."), "price_sheet.json")


def load_sheet() -> dict:
    try:
        with open(_path(), encoding="utf-8") as fh:
            s = json.load(fh)
        return {**DEFAULT_SHEET, **s}
    except (OSError, ValueError):
        return dict(DEFAULT_SHEET)


def save_sheet(sheet: dict) -> dict:
    sheet = sheet or {}
    current = load_sheet()   # so a partial save never drops fields it didn't send
    merged = {**DEFAULT_SHEET, **sheet}
    merged["mixes"] = sheet.get("mixes", [])
    merged["overrides"] = sheet.get("overrides", [])
    merged["admixtures"] = sheet.get("admixtures", [])
    merged["self_haul_customers"] = sheet.get("self_haul_customers", [])
    merged["delivery_brackets"] = sheet.get("delivery_brackets") or DEFAULT_BRACKETS
    # Fuel prices live in the sheet but are edited from the fuel view, not the
    # price-sheet editor — carry the saved values forward when not in this payload.
    merged["fuel_price_default"] = sheet.get("fuel_price_default", current.get("fuel_price_default", 0.0))
    merged["fuel_prices"] = sheet.get("fuel_prices", current.get("fuel_prices", []))
    with open(_path(), "w", encoding="utf-8") as fh:
        json.dump(merged, fh, indent=2)
    return merged


def fuel_price_for(sheet: dict, product) -> float:
    """$/gal for a fuel product: a matching per-product rate, else the default."""
    pn = _norm(product)
    for p in (sheet or {}).get("fuel_prices") or []:
        if pn and _norm(p.get("product")) == pn:
            return _num(p.get("price"))
    return _num((sheet or {}).get("fuel_price_default"))


def _num(v) -> float:
    """First number in a value: '10 yd³' -> 10.0, '$1,500.00' -> 1500.0."""
    m = re.search(r"-?[\d,]*\.?\d+", str(v or ""))
    return float(m.group().replace(",", "")) if m else 0.0


def _norm(s) -> str:
    return "".join(c for c in str(s or "").lower() if c.isalnum() or c == " ").strip()


def _mix_matches(sheet_mix, order_mix) -> bool:
    a, b = _norm(sheet_mix), _norm(order_mix)
    if not a or not b:
        return False
    return a in b or b in a


def _adx_lbs(name: str, order_admixtures: str, materials) -> float:
    """Total lbs of an admixture: prefer 'Name: X lbs/yd' on the order; else the
    batched actual from the protocol materials."""
    m = re.search(re.escape(name) + r"[^\d]*([\d.]+)\s*lb", order_admixtures or "", re.I)
    if m:
        return float(m.group(1))   # lbs/yd dosage (caller multiplies by yards)
    return 0.0


def _adx_present(name: str, order_admixtures: str, materials) -> bool:
    """True when this admixture was used — named in the order's admixtures text OR
    batched on the ticket (materials). Matching ignores spaces so a sheet entry
    'Master Set Delvo' matches the tracked/printed 'Masterset Delvo' / 'MasterSet
    DELVO', and a short 'Delvo' still matches either."""
    n = _norm(name).replace(" ", "")
    if n and n in _norm(order_admixtures).replace(" ", ""):
        return True
    for mat in (materials or []):
        mat_name = mat[0] if isinstance(mat, (list, tuple)) else str(mat)
        if n and n in _norm(mat_name).replace(" ", ""):
            return True
    return False


def is_self_haul(customer: str, sheet: dict = None) -> bool:
    sheet = sheet or load_sheet()
    return _norm(customer) in {_norm(c) for c in sheet.get("self_haul_customers", []) if c}


def strip_self_haul_fee(notes: str, customer: str, sheet: dict = None):
    """Self-haul customers buy concrete only — drop any '$200 short load fee' note."""
    if not notes or not is_self_haul(customer, sheet):
        return notes
    cleaned = re.sub(r"\s*[—-]?\s*Short load fee \$200 \(accepted\)\s*", " ", notes, flags=re.I).strip()
    return cleaned or None


def compute_pricing(sheet: dict, mix: str, customer: str, order_qty, load_qty,
                    materials=None, order_admixtures: str = "", unit_override=None,
                    fiber_rate_override=None) -> dict:
    """Compute the ticket pricing block. Quantities are yards; load_qty is this
    load (the ticket), order_qty is the whole order (for the short-load rule).
    unit_override, when set, forces the $/yd unit price (a staff per-order price)."""
    sheet = sheet or {}
    lq = _num(load_qty) or _num(order_qty)   # fall back to order qty if the load read is blank
    oq = _num(order_qty) or lq

    unit, haul = 0.0, 0.0
    for m in sheet.get("mixes", []):
        if _mix_matches(m.get("mix"), mix):
            unit, haul = _num(m.get("price")), _num(m.get("haul"))
            break
    # customer override (blank mix = applies to any mix for that customer)
    cust_n = _norm(customer)
    for ov in sheet.get("overrides", []):
        if _norm(ov.get("customer")) == cust_n and cust_n and (not ov.get("mix") or _mix_matches(ov.get("mix"), mix)):
            unit = _num(ov.get("price"))
            break
    # staff per-order price override wins over the sheet/customer price
    if unit_override is not None and _num(unit_override) > 0:
        unit = _num(unit_override)

    extended = round(lq * unit, 2)

    # admixture add-ons (Fiber $/lb, Master Set Delvo $/yd, etc.)
    adx_lines = []
    # Fiber is handled on its own so a per-order rate (fiber_rate_override) can win
    # over the price-sheet $/lb. The lbs/yd dosage comes off the order itself.
    sheet_fiber_rate, fiber_name = 0.0, "Fiber"
    for adx in sheet.get("admixtures", []):
        if "fiber" in (adx.get("name") or "").lower():
            sheet_fiber_rate = _num(adx.get("rate"))
            fiber_name = (adx.get("name") or "Fiber").strip() or "Fiber"
            break
    fiber_rate = (_num(fiber_rate_override) if (fiber_rate_override is not None and _num(fiber_rate_override) > 0)
                  else sheet_fiber_rate)
    fiber_lbs_per_yd = _adx_lbs("fiber", order_admixtures, materials)
    if fiber_lbs_per_yd > 0 and fiber_rate > 0:
        total_lbs = round(fiber_lbs_per_yd * lq, 2)
        charge = round(fiber_rate * total_lbs, 2)
        if charge:
            adx_lines.append({"label": f"{fiber_name} ({total_lbs:g} lb @ ${fiber_rate:.2f}/lb)", "amount": charge})
    # all other (non-fiber) admixtures, priced from the sheet
    for adx in sheet.get("admixtures", []):
        nm, rate = (adx.get("name") or "").strip(), _num(adx.get("rate"))
        if "fiber" in nm.lower():
            continue   # priced above
        per = (adx.get("per") or "yard").lower()
        if not nm or rate <= 0 or not _adx_present(nm, order_admixtures, materials):
            continue
        if per == "lb":
            lbs_per_yd = _adx_lbs(nm, order_admixtures, materials)
            total_lbs = round(lbs_per_yd * lq, 2)
            charge = round(rate * total_lbs, 2)
            if charge:
                adx_lines.append({"label": f"{nm} ({total_lbs:g} lb @ ${rate:.2f}/lb)", "amount": charge})
        else:
            charge = round(rate * lq, 2)
            if charge:
                adx_lines.append({"label": f"{nm} ({lq:g} yd @ ${rate:.2f}/yd)", "amount": charge})
    adx_total = round(sum(a["amount"] for a in adx_lines), 2)

    # self-haul / pickup customers buy concrete only — no delivery or load fees
    self_haul = cust_n in {_norm(c) for c in sheet.get("self_haul_customers", []) if c}
    if self_haul:
        short = backhaul = 0.0
    else:
        short = _num(sheet.get("short_load_fee")) if (oq and oq < _num(sheet.get("short_load_under_yd"))) else 0.0
        backhaul = (round(_num(sheet.get("backhaul_per_yd")) * lq, 2)
                    if (lq and lq < _num(sheet.get("backhaul_under_yd")) and oq and oq > lq) else 0.0)
    subtotal = round(extended + adx_total + short + backhaul, 2)
    tax_pct = _num(sheet.get("tax_pct"))
    tax = round(subtotal * tax_pct / 100.0, 2)
    total = round(subtotal + tax, 2)
    return {
        "unit_price": unit,
        "extended": extended,
        "admixtures": adx_lines,
        "short_load": short,
        "backhaul": backhaul,
        "subtotal": subtotal,
        "tax_pct": tax_pct,
        "tax": tax,
        "total": total,
        "job_running_total": total,    # this load only (for now)
        "haul_internal": round(lq * haul, 2),   # tracked, not printed
    }
