"""Turn an uploaded batch ticket into the branded Aussieblock ticket.

Routing:
  • PDF  -> dornerBatch "Total batch protocol" -> read_protocol (AI, full data) ->
            generator.render_ticket  (the complete branded Total Batch Protocol)
  • image (photo) -> handwritten field ticket -> read_ticket -> delivery_ticket

Needs ANTHROPIC_API_KEY in the env; if it's absent, available() is False and the
caller keeps the original upload as-is.
"""
import os
import io
import json
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def available() -> bool:
    """True when we can brand a ticket (the vision key is configured)."""
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _cfg() -> dict:
    cfg = json.load(open(os.path.join(HERE, "ticket_config.json"), encoding="utf-8"))
    cfg["anthropic_api_key"] = os.environ.get("ANTHROPIC_API_KEY", "")
    return cfg


def _is_pdf(data: bytes, filename: str) -> bool:
    return (filename or "").lower().endswith(".pdf") or data[:5] == b"%PDF-"


def _to_image_file(data: bytes, filename: str) -> str:
    """Write the ticket out as a modest-resolution JPEG (page 1 if PDF) for the
    vision reader. Kept small + freed promptly to keep memory low — typed
    protocols still read reliably at this size."""
    import gc
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
    tmp.close()
    if _is_pdf(data, filename):
        import fitz  # PyMuPDF
        doc = fitz.open(stream=data, filetype="pdf")
        pix = doc[0].get_pixmap(dpi=150)   # 150 DPI ~ 1275px wide; lower memory than 200
        pix.save(tmp.name)
        pix = None
        doc.close()
        doc = None
    else:
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGB")
        im.thumbnail((1500, 1500))
        im.save(tmp.name, "JPEG", quality=85)
        im.close()
        im = None
    gc.collect()
    return tmp.name


def convert(data: bytes, filename: str, customer_name: str = None, site: str = None) -> bytes:
    """Read the uploaded ticket and render the branded PDF. Returns PDF bytes.
    Raises on any failure (the caller falls back to the original)."""
    cfg = _cfg()
    img = _to_image_file(data, filename)
    out = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    out.close()
    try:
        if _is_pdf(data, filename):
            # Typed dornerBatch "Total batch protocol" — full materials/batches/water.
            from . import read_protocol, generator
            d = read_protocol.read_protocol(img, cfg)
            if customer_name and isinstance(d.get("order"), dict):
                d["order"]["customer"] = customer_name   # order is authoritative for who it's for
            generator.render_ticket(d, out.name)
        else:
            # Handwritten field-ticket photo — summary delivery ticket.
            from . import read_ticket, delivery_ticket
            d = read_ticket.read_ticket(img, cfg)
            if customer_name:
                d["customer"] = customer_name
            if site and not (d.get("job_address") or d.get("site")):
                d["site"] = site
            delivery_ticket.render_delivery_ticket(d, out.name)
        with open(out.name, "rb") as fh:
            return fh.read()
    finally:
        for p in (img, out.name):
            try:
                os.remove(p)
            except OSError:
                pass
