"""
read_protocol.py -- AI vision reader for a photographed dornerBatch
"Total batch protocol" (the TYPED plant printout). Returns the full data dict
that generator.render_ticket() renders into the branded Total Batch Protocol.

Typed text => reliable reads (unlike the handwritten field tickets).
"""
import os, io, re, base64, json
import anthropic
from PIL import Image, ImageOps

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_DIM = 1568


def _b64(path):
    im = Image.open(path)
    im = ImageOps.exif_transpose(im).convert("RGB")
    im.thumbnail((MAX_DIM, MAX_DIM))
    buf = io.BytesIO(); im.save(buf, "JPEG", quality=88)
    return base64.standard_b64encode(buf.getvalue()).decode()


def _num(s):
    """'17,753.10 lb' -> 17753.10 ; '+32.97' -> 32.97 ; '' -> 0.0"""
    if s is None:
        return 0.0
    m = re.search(r"-?\d[\d,]*\.?\d*", str(s).replace(",", ""))
    return float(m.group()) if m else 0.0


def _lim(s):
    """'+/-2%' -> 2.0 ; '+1%' -> 1.0"""
    m = re.search(r"\d+\.?\d*", str(s or ""))
    return float(m.group()) if m else 0.0


def _astm(name, unit, cfg):
    """ASTM C94 batching tolerance by material type (the protocol leaves this
    column blank, so derive it like the example sheet). Returns (pct, label)."""
    t = cfg.get("astm_tolerance_pct", {})
    n = (name or "").lower(); u = (unit or "").lower()
    if "water" in n:
        pct = t.get("water", 1)
    elif any(k in n for k in ["cem", "slag", "portland", "binder", "fly ash", "ash"]):
        pct = t.get("cement", 1)
    elif "fl" in u or "oz" in u or any(k in n for k in ["add", "lfa", "seed", "admix",
                                                        "plast", "retard", "reduc", "fiber", "matrix"]):
        pct = t.get("admixture", 3)
    else:
        pct = t.get("aggregate", 2)   # gravel / sand / aggregate
    return float(pct), f"+/-{pct:g}%"


TOOL = {
    "name": "emit_protocol",
    "description": "Return every field read off the Total Batch Protocol.",
    "input_schema": {
        "type": "object",
        "properties": {
            "report_date": {"type": "string"},
            "order": {"type": "object", "properties": {
                "plant": {"type": "string"}, "recipe": {"type": "string"},
                "customer": {"type": "string"}, "site_no": {"type": "string"},
                "site_addr": {"type": "string"}, "vrn": {"type": "string"},
                "qty": {"type": "string"}, "load_no": {"type": "string"},
            }},
            "batches": {"type": "array", "items": {"type": "object", "properties": {
                "no": {"type": "string"}, "time": {"type": "string"}, "qty": {"type": "string"}}}},
            "materials": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "unit": {"type": "string"},
                "density": {"type": "string"}, "recipe_sv": {"type": "string"},
                "set_value": {"type": "string"}, "actual_value": {"type": "string"},
                "allowable": {"type": "string"}}}},
            "totals": {"type": "object", "properties": {
                "water_sum": {"type": "string"}, "water_per": {"type": "string"},
                "binder_sum": {"type": "string"}, "binder_per": {"type": "string"},
                "agg_sum": {"type": "string"}, "agg_per": {"type": "string"},
                "total_sum": {"type": "string"}, "total_per": {"type": "string"}}},
            "process": {"type": "object", "properties": {
                "total_mixing_time": {"type": "string"}, "avg_mixing_time": {"type": "string"},
                "avg_mixer_load": {"type": "string"}, "slump": {"type": "string"},
                "slump_set": {"type": "string"},
                "water_correction": {"type": "string"}, "wc": {"type": "string"},
                "wc_eq": {"type": "string"}, "max_wc": {"type": "string"}}},
        },
        "required": ["order", "materials"],
    },
}

PROMPT = (
    "This is a photo of a dornerBatch 'Total batch protocol' (a machine-PRINTED "
    "concrete batch report, not handwritten). Transcribe it exactly via "
    "emit_protocol.\n"
    "- ORDER INFORMATION: plant, recipe no./name, customer no./name, construction "
    "site no./name and address, vehicle no./VRN, quantity, load/delivery number.\n"
    "- BATCHES: each row's protocol no., production time, batch quantity.\n"
    "- BATCH INFORMATION: each material row — material/silo name, unit, density, "
    "Recipe(SV), Set value(SV), Actual value(AV), and the Allowable ASTM C94 "
    "column (e.g. '+/-2%'). Copy numbers exactly including decimals.\n"
    "- The Σ totals block: Water / Binder / Aggregate / Total weight (sum and per-yd³).\n"
    "- The process block: total & average mixing time, mixer load, concrete slump, "
    "water correction, w/c, w/c eq., max w/c.\n"
    "- In the Consistency block the 'Set value' is the REQUESTED/target slump — "
    "return it as slump_set (just the number, e.g. '5'). The 'Actual value' is slump.\n"
    "Leave anything not present blank."
)


def read_protocol(path, cfg):
    key = cfg.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("No Anthropic API key in config.json.")
    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model=cfg.get("vision_model", "claude-opus-4-8"),
        max_tokens=2048, tools=[TOOL],
        tool_choice={"type": "tool", "name": "emit_protocol"},
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _b64(path)}},
            {"type": "text", "text": PROMPT}]}],
    )
    out = {}
    for b in msg.content:
        if b.type == "tool_use":
            out = b.input
            break
    return _to_generator_data(out, cfg)


def _uk_to_us_date(s: str) -> str:
    """dornerBatch prints dates DD/MM/YYYY; rewrite them US-style MM/DD/YYYY.
    Only swaps full DD/MM/YYYY patterns (times like 12:14:19 are untouched)."""
    return re.sub(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", lambda m: f"{m.group(2)}/{m.group(1)}/{m.group(3)}", s or "")


def _with_gallons(s: str) -> str:
    """Append the gallon equivalent to a water value in pounds (water: 8.345 lb/gal).
    '392.5 lb' -> '392.5 lb (47.0 gal)'. Left as-is if there's no number."""
    m = re.search(r"-?[\d,]*\.?\d+", s or "")
    if not m:
        return (s or "-")
    try:
        lb = float(m.group().replace(",", ""))
    except ValueError:
        return (s or "-")
    return f"{(s or '').strip()} ({lb / 8.345:.1f} gal)"


def _rule_max_wc(recipe: str) -> float:
    """Locked maximum water/cement ratio by mix design:
    TxDOT Class A & B = 0.60, Class C = 0.45, every other (non-TxDOT) mix = 0.70.
    Enforced here regardless of what the protocol prints."""
    n = (recipe or "").lower()
    if re.search(r"class\s*c\b", n):
        return 0.45
    if re.search(r"class\s*[ab]\b", n):
        return 0.60
    return 0.70


def _to_generator_data(p, cfg):
    o = p.get("order", {}) or {}
    rule_wc = _rule_max_wc(o.get("recipe", "") or "")
    # dornerBatch prints dates DD/MM/YYYY; show them US-style MM/DD/YYYY.
    batches = [(b.get("no", ""), _uk_to_us_date(b.get("time", "")), b.get("qty", "")) for b in (p.get("batches") or [])]
    # Report date should match the PRODUCTION date (the batch prod time), not the
    # header text the read sometimes garbles.
    prod_date = batches[0][1].split()[0] if (batches and batches[0][1]) else ""
    dens_over = cfg.get("density_override") or {}
    mats = []
    for m in (p.get("materials") or []):
        nm = m.get("name", ""); un = m.get("unit", "lb")
        lim, lab = _astm(nm, un, cfg)        # ASTM tolerance by material type
        # dornerBatch prints aggregate density in kg/m³; use the correct lb/ft³
        # value from density_override (matched by material name) when configured.
        dens = _num(m.get("density"))
        for key, val in dens_over.items():
            if key.lower() in nm.lower():
                dens = float(val)
                break
        mats.append((
            nm, un, dens, _num(m.get("recipe_sv")),
            _num(m.get("set_value")), _num(m.get("actual_value")), lim, lab,
        ))
    t = p.get("totals", {}) or {}
    pr = p.get("process", {}) or {}
    binder_lb = _num(t.get("binder_sum"))
    # Concrete slump shown on the ticket = REQUESTED (set) slump +/- 1.5", not the
    # measured actual value.
    _ss = re.search(r"[\d.]+", pr.get("slump_set") or "")
    req = _ss.group() if _ss else ""
    req_slump = f"{req} in" if req else (pr.get("slump", "") or "")
    concrete_slump = f"{req} in (+/- 1.5 in)" if req else (pr.get("slump", "") or "-")
    qty = o.get("qty", "") or ""
    yards = _num(qty) or _num((cfg.get("_pricing") or {}).get("order_qty")) or 0
    # TxDOT: MasterFiber MAC 330 is dosed per the ORDER, not the dornerBatch
    # protocol, so the read materials won't include it. When the order carries it
    # (and the protocol didn't already list a fiber), add it as a materials row —
    # dosage (lbs/yd) × yards — so the certified ticket reflects it. Hand-added to
    # the batch at spec, so set == actual (0% variance).
    order_adx = (cfg.get("_pricing") or {}).get("order_admixtures", "") or ""
    has_fiber_row = any("fiber" in (mm[0] or "").lower() or "matrix" in (mm[0] or "").lower() for mm in mats)
    chosen_fiber = None
    if not has_fiber_row and yards:
        for part in order_adx.split(","):
            if not re.search(r"fiber|matrix|mac\s*3\d0|masterfiber", part, re.I):
                continue
            # Which fiber product the order selected -> ticket name + standard dose.
            # Default to MAC 330 (the TxDOT standard); use Mac Matrix 360 only when
            # the order explicitly names "360" / "Matrix".
            is360 = bool(re.search(r"360|matrix", part, re.I))
            chosen_fiber = "Mac Matrix 360" if is360 else "MasterFiber MAC 330"
            std = 4.5 if is360 else 4.0            # MacMatrix 360 = 4.5 lb/yd; MAC 330 = 4 lb/yd
            # Dose from the order segment if given (digit-safe: skips the 330/360 in
            # the name), else the product's standard.
            dm = re.search(r"([\d.]+)\s*lb", part, re.I)
            dose = float(dm.group(1)) if dm else std
            lim, lab = _astm(chosen_fiber, "lb", cfg)
            total = dose * yards
            # density = SG 0.91 x 62.4 = 56.8 lb/ft³ (macro-synthetic fiber)
            mats.append((chosen_fiber, "lb", 56.8, dose, total, total, lim, lab))
            break
    # MPL materials table reflects the fiber product actually used on this ticket.
    mpl = [dict(r) for r in (cfg.get("material_mpl", []) or [])]
    if chosen_fiber:
        for r in mpl:
            if "fiber" in (str(r.get("material", "")) + str(r.get("source", ""))).lower():
                r["source"] = chosen_fiber
                break
    data = {
        "report_date": prod_date or _uk_to_us_date(p.get("report_date", "")) or "",
        "sales_tax_pct": cfg.get("sales_tax_pct", 8.25),
        "company": cfg["company"],
        "weather": {"time": "-", "cond": "-", "temp": "-", "humidity": "-", "wind": "-", "pressure": "-", "vis": "-"},
        "order": {
            "plant": o.get("plant", "") or "-",
            "recipe": o.get("recipe", "") or "-",
            "customer": o.get("customer", "") or "-",
            "cust_phone": "-",
            "site_no": o.get("site_no", "") or "-",
            "site_addr": o.get("site_addr", "") or "-",
            "mileage": "-", "drive_time": "-",
            "vrn": o.get("vrn", "") or "-",
            "driver": "-",
            "qty": qty or "-",
            "ordered": qty or "-",
            "load_no": o.get("load_no", "") or "-",
            "user": "-", "sales_rep": "-",
        },
        "batches": batches,
        "materials": mats,
        "mpl": mpl,
        "totals": {
            "Water": (_num(t.get("water_sum")), _num(t.get("water_per"))),
            "Binder": (binder_lb, _num(t.get("binder_per"))),
            "Aggregate (Dry weight)": (_num(t.get("agg_sum")), _num(t.get("agg_per"))),
            "Total weight": (_num(t.get("total_sum")), _num(t.get("total_per"))),
        },
        "process": {
            "Total mixing time": pr.get("total_mixing_time", "") or "-",
            "Ø Mixing time": pr.get("avg_mixing_time", "") or "-",
            "Ø Mixer load": pr.get("avg_mixer_load", "") or "-",
            "Concrete slump": concrete_slump,
            "Water correction": _with_gallons(pr.get("water_correction", "")),
            "w/c": pr.get("wc", "") or "-",
            "w/c eq.": pr.get("wc_eq", "") or "-",
            "max w/c": f"{rule_wc:.2f}",
        },
        "binder_lb": binder_lb,
        "wc_eq": _num(pr.get("wc_eq")) or _num(pr.get("wc")),
        "max_wc": rule_wc,
        "yards": yards,
        "req_slump": req_slump,
    }
    # Pricing block, from the price sheet + order context (set by convert()).
    px = cfg.get("_pricing") or {}
    if px.get("sheet"):
        try:
            from ..pricing import compute_pricing
            data["pricing"] = compute_pricing(
                px.get("sheet"), px.get("mix") or o.get("recipe", ""),
                px.get("customer") or o.get("customer", ""),
                px.get("order_qty"), qty,
                materials=mats, order_admixtures=px.get("order_admixtures", ""))
        except Exception as e:
            print("pricing compute failed:", e)
    return data


if __name__ == "__main__":
    import sys, generator
    cfg = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
    data = read_protocol(sys.argv[1], cfg)
    out = os.path.join(HERE, "PROTOCOL_out.pdf")
    generator.render_ticket(data, out)
    print("customer:", data["order"]["customer"])
    print("recipe  :", data["order"]["recipe"])
    print("load    :", data["order"]["load_no"], "| qty:", data["order"]["qty"])
    print("materials:", len(data["materials"]), "| batches:", len(data["batches"]))
    print("wrote", out)
