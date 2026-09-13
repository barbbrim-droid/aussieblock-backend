"""
read_weight_ticket.py -- AI vision reader for quarry / pit SCALE (weight) tickets.

An aggregate driver snaps the scale ticket they're handed at the pit; this pulls
the figures off it (net weight, gross/tare, ticket #, pit, product, date, truck)
so the office doesn't have to key them in. Uses Claude vision, same as the
delivery-ticket reader. Needs ANTHROPIC_API_KEY; the caller checks available().

Returns a dict of strings/numbers; anything not on the ticket is None. The
reader is deliberately conservative -- it never invents a weight.
"""
import os, io, base64, json, re
import anthropic
from PIL import Image, ImageOps

MAX_DIM = 1568
JPEG_Q = 85

TOOL = {
    "name": "emit_weight_ticket",
    "description": "Return the fields read off an aggregate scale (weight) ticket.",
    "input_schema": {
        "type": "object",
        "properties": {
            "ticket_no":  {"type": "string", "description": "scale ticket number as printed"},
            "date":       {"type": "string", "description": "ticket date, YYYY-MM-DD if legible"},
            "supplier":   {"type": "string", "description": "quarry / pit / company that issued the ticket"},
            "product":    {"type": "string", "description": "material as printed, e.g. '3/4 inch Gravel', 'Concrete Sand', 'Base'"},
            "truck":      {"type": "string", "description": "truck number / unit as printed"},
            "driver":     {"type": "string", "description": "driver name if printed"},
            "gross_lb":   {"type": "number", "description": "gross weight in POUNDS (convert if printed in tons)"},
            "tare_lb":    {"type": "number", "description": "tare weight in POUNDS"},
            "net_lb":     {"type": "number", "description": "net weight in POUNDS"},
            "net_tons":   {"type": "number", "description": "net weight in TONS (short tons, 2000 lb) as printed or computed"},
            "unit_price": {"type": "number", "description": "price per ton if printed"},
            "total":      {"type": "number", "description": "ticket total $ if printed"},
            "low_confidence": {"type": "array", "items": {"type": "string"},
                               "description": "field names you are unsure about"},
        },
        "required": [],
    },
}

PROMPT = (
    "This is a photo of a SCALE TICKET (weight ticket) from a rock quarry, sand pit "
    "or aggregate yard, handed to a dump-truck driver when a load is weighed. The "
    "photo may be rotated, at an angle, or a thermal-printer slip. Read the printed "
    "values and return them via the emit_weight_ticket tool.\n\n"
    "Typical fields: ticket/ticket #, date & time, customer (often 'Aussieblock' -- "
    "NOT the supplier), the pit/quarry/company name in the header (that IS the "
    "supplier), product/material, truck # / unit, driver, and the weights: GROSS, "
    "TARE, NET -- usually in lb, sometimes in tons (TN / T). There may be a price "
    "per ton and a total.\n\n"
    "Rules:\n"
    "- Transcribe exactly what is printed. Never invent a value -- omit any field "
    "that is not on the ticket.\n"
    "- Weights: give gross/tare/net in POUNDS. If the ticket prints tons, multiply "
    "by 2000 for the lb fields and also give net_tons as printed. If it prints lb, "
    "compute net_tons = net_lb / 2000 (2 decimals).\n"
    "- If net is missing but gross and tare are printed, net = gross - tare.\n"
    "- date as YYYY-MM-DD when you can read a full date.\n"
    "- List any field you are unsure of in low_confidence."
)


def _b64(path: str) -> str:
    im = Image.open(path)
    im = ImageOps.exif_transpose(im)          # honor phone orientation
    im = im.convert("RGB")
    im.thumbnail((MAX_DIM, MAX_DIM))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=JPEG_Q)
    return base64.standard_b64encode(buf.getvalue()).decode()


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d[\d,]*\.?\d*", str(v))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def read_weight_ticket(path: str, model: str = None) -> dict:
    """Read one ticket image (jpg/png) at `path`. Raises on API/key errors."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    model = model or os.environ.get("VISION_MODEL", "claude-sonnet-4-6")
    client = anthropic.Anthropic(api_key=key, max_retries=6)
    msg = client.messages.create(
        model=model,
        max_tokens=800,
        tools=[TOOL],
        tool_choice={"type": "tool", "name": "emit_weight_ticket"},
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64",
                 "media_type": "image/jpeg", "data": _b64(path)}},
                {"type": "text", "text": PROMPT},
            ],
        }],
    )
    out = {}
    for block in msg.content:
        if block.type == "tool_use":
            out = dict(block.input or {})
            break
    gross, tare, net = _num(out.get("gross_lb")), _num(out.get("tare_lb")), _num(out.get("net_lb"))
    net_tons = _num(out.get("net_tons"))
    if net is None and gross is not None and tare is not None and gross > tare:
        net = gross - tare
    if net_tons is None and net is not None:
        net_tons = round(net / 2000.0, 2)
    # A "net" under 200 with tons present is almost certainly tons mis-labelled as lb.
    if net is not None and net < 200 and net_tons is not None and abs(net - net_tons) < 0.01:
        net = net_tons * 2000.0
    return {
        "ticket_no": (out.get("ticket_no") or "").strip() or None,
        "date": (out.get("date") or "").strip() or None,
        "supplier": (out.get("supplier") or "").strip() or None,
        "product": (out.get("product") or "").strip() or None,
        "truck": (out.get("truck") or "").strip() or None,
        "driver": (out.get("driver") or "").strip() or None,
        "gross_lb": gross, "tare_lb": tare, "net_lb": net, "net_tons": net_tons,
        "unit_price": _num(out.get("unit_price")), "total": _num(out.get("total")),
        "low_confidence": list(out.get("low_confidence") or []),
    }


if __name__ == "__main__":
    import sys
    print(json.dumps(read_weight_ticket(sys.argv[1]), indent=2))
