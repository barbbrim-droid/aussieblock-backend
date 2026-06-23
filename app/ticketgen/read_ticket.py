"""
read_ticket.py -- AI vision reader for handwritten Aussieblock delivery tickets.

Takes a phone photo (jpg/png) of the paper ticket and returns a field dict that
delivery_ticket.render_delivery_ticket() can render. Uses Claude vision.

Needs an Anthropic API key (config.json -> "anthropic_api_key", or env
ANTHROPIC_API_KEY). Cost is a fraction of a cent per ticket.
"""
import os, io, base64, json, re, difflib
import anthropic
from PIL import Image, ImageOps

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_customers():
    """Real customer roster (exported from the dispatch DB) for name-matching."""
    p = os.path.join(HERE, "customers.json")
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return []


def _norm(s):
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def match_customer(raw, customers):
    """Snap an AI-read name to the closest real account. Returns
    (display_name, acct_no, matched_bool, score)."""
    raw_n = _norm(raw)
    if not raw_n or not customers:
        return raw, "", False, 0.0
    names = [c["name"] for c in customers]
    norm_names = [_norm(n) for n in names]
    best_i, best = -1, 0.0
    for i, nn in enumerate(norm_names):
        # whole-string similarity (no common-word boosting -> no false matches)
        score = difflib.SequenceMatcher(None, raw_n, nn).ratio()
        if score > best:
            best, best_i = score, i
    # only accept a confident match; otherwise keep the raw read and flag it
    if best >= 0.78:
        c = customers[best_i]
        return c["name"], c.get("acct_no", "") or "", True, round(best, 2)
    return raw, "", False, round(best, 2)

MAX_DIM = 1568          # Anthropic's recommended max edge; keeps tokens/cost low
JPEG_Q = 85

FIELDS = [
    "date", "customer", "customer_phone", "job_address", "product",
    "ordered_qty", "delivered_qty", "load", "truck", "driver", "plant",
    "slump", "air", "cash_charge", "unit_price", "subtotal", "tax", "total",
    "water_reducer", "retarder", "notes",
]

TOOL = {
    "name": "emit_ticket",
    "description": "Return the fields read off the Aussieblock delivery ticket.",
    "input_schema": {
        "type": "object",
        "properties": {f: {"type": "string"} for f in FIELDS} | {
            "low_confidence": {
                "type": "array", "items": {"type": "string"},
                "description": "names of fields you are unsure about (illegible handwriting)",
            }
        },
        "required": ["customer", "product", "ordered_qty"],
    },
}

PROMPT = (
    "This is a photo of an Aussieblock Ready Mix Concrete DELIVERY ticket -- a "
    "pre-printed form filled in BY HAND with pen. The photo may be rotated or "
    "taken at an angle. Read every handwritten value and return it via the "
    "emit_ticket tool.\n\n"
    "Field guide (left/top area of the form): DATE, SOLD TO (customer), CUSTOMER "
    "PHONE, JOB ADDRESS, PRODUCT / PRODUCT NAME (the concrete mix, e.g. '3000 "
    "PSI'), QUANTITY/ORDERED and DELIVERED (in cubic yards), LOAD number, TRUCK, "
    "DRIVER, PLANT, SLUMP (inches), AIR, CASH/CHARGE, and on the right the price "
    "columns UNIT PRICE / EXTENDED / SUBTOTAL / TAX / TOTAL, plus WATER REDUCER "
    "and RETARDER admixtures.\n\n"
    "Rules:\n"
    "- Transcribe exactly what is written. Do NOT invent or guess values that "
    "aren't there -- leave a field as an empty string if it's blank.\n"
    "- For quantities include the unit (e.g. '10 yd³'); for slump like '5 in'.\n"
    "- If handwriting is unclear, give your best reading AND list that field name "
    "in low_confidence.\n"
    "- 'customer' should be the Sold-To name; if a separate job/contact name is "
    "written, use 'Name / Job' form (e.g. 'Reece / Guest')."
)


_ROT = {0: None, 90: Image.Transpose.ROTATE_90,
        180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_270}


def _b64(path, rotate=270):
    im = Image.open(path)
    im = ImageOps.exif_transpose(im)          # honor phone orientation
    im = im.convert("RGB")
    if _ROT.get(rotate):                      # bring a sideways form upright
        im = im.transpose(_ROT[rotate])
    im.thumbnail((MAX_DIM, MAX_DIM))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=JPEG_Q)
    return base64.standard_b64encode(buf.getvalue()).decode()


def read_ticket(path, cfg):
    key = cfg.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("No Anthropic API key. Set anthropic_api_key in config.json.")
    model = cfg.get("vision_model", "claude-sonnet-4-6")
    rotate = cfg.get("photo_rotate", 270)
    # Extra retries ride out transient 529 "Overloaded" responses.
    client = anthropic.Anthropic(api_key=key, max_retries=6)
    msg = client.messages.create(
        model=model,
        max_tokens=1024,
        tools=[TOOL],
        tool_choice={"type": "tool", "name": "emit_ticket"},
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64",
                 "media_type": "image/jpeg", "data": _b64(path, rotate)}},
                {"type": "text", "text": PROMPT},
            ],
        }],
    )
    out = {}
    for block in msg.content:
        if block.type == "tool_use":
            out = block.input
            break
    data = {f: (out.get(f) or "") for f in FIELDS}
    low = list(out.get("low_confidence", []))

    # --- snap the read customer to a real account from the roster ---
    customers = _load_customers()
    if customers and data.get("customer"):
        disp, acct, matched, score = match_customer(data["customer"], customers)
        data["customer_read"] = data["customer"]      # keep what the pen actually said
        data["customer"] = disp
        data["acct_no"] = acct
        data["customer_match_score"] = score
        if matched:
            low = [f for f in low if f != "customer"]  # resolved -> no longer uncertain
        elif "customer" not in low:
            low.append("customer")

    data["low_confidence"] = low
    data["company"] = cfg["company"]
    data["sales_tax_pct"] = cfg.get("sales_tax_pct", 8.25)
    return data


if __name__ == "__main__":
    import sys
    HERE = os.path.dirname(os.path.abspath(__file__))
    cfg = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
    d = read_ticket(sys.argv[1], cfg)
    print(json.dumps({k: v for k, v in d.items() if k not in ("company",)},
                     indent=2, ensure_ascii=False))
