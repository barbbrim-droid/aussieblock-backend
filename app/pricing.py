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
}


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
    merged = {**DEFAULT_SHEET, **(sheet or {})}
    merged["mixes"] = sheet.get("mixes", []) if sheet else []
    merged["overrides"] = sheet.get("overrides", []) if sheet else []
    merged["admixtures"] = sheet.get("admixtures", []) if sheet else []
    with open(_path(), "w", encoding="utf-8") as fh:
        json.dump(merged, fh, indent=2)
    return merged


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
    n = _norm(name)
    if n and n in _norm(order_admixtures):
        return True
    for mat in (materials or []):
        mat_name = mat[0] if isinstance(mat, (list, tuple)) else str(mat)
        if n and n in _norm(mat_name):
            return True
    return False


def compute_pricing(sheet: dict, mix: str, customer: str, order_qty, load_qty,
                    materials=None, order_admixtures: str = "") -> dict:
    """Compute the ticket pricing block. Quantities are yards; load_qty is this
    load (the ticket), order_qty is the whole order (for the short-load rule)."""
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

    extended = round(lq * unit, 2)

    # admixture add-ons (Fiber $/lb, Master Set Delvo $/yd, etc.)
    adx_lines = []
    for adx in sheet.get("admixtures", []):
        nm, rate = (adx.get("name") or "").strip(), _num(adx.get("rate"))
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
