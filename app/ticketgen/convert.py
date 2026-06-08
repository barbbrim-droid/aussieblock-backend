"""Turn an uploaded scan/photo of a paper batch ticket into the branded
Aussieblock delivery ticket.

Pipeline: rasterize (if PDF) -> Claude vision reads the fields (read_ticket) ->
render the branded PDF (delivery_ticket). Needs ANTHROPIC_API_KEY in the env;
if it's absent, available() is False and the caller keeps the original as-is.
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


def _to_image_file(data: bytes, filename: str) -> str:
    """Write the ticket out as a PNG the vision reader can take. Page 1 only if PDF."""
    name = (filename or "").lower()
    is_pdf = name.endswith(".pdf") or data[:5] == b"%PDF-"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    tmp.close()
    if is_pdf:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=data, filetype="pdf")
        doc[0].get_pixmap(dpi=200).save(tmp.name)
        doc.close()
    else:
        from PIL import Image
        Image.open(io.BytesIO(data)).convert("RGB").save(tmp.name)
    return tmp.name


def convert(data: bytes, filename: str, customer_name: str = None, site: str = None) -> bytes:
    """Read the uploaded ticket and render the branded delivery ticket.
    Returns the branded PDF bytes. Raises on any failure (caller falls back)."""
    from . import read_ticket, delivery_ticket
    cfg = _cfg()
    img = _to_image_file(data, filename)
    try:
        fields = read_ticket.read_ticket(img, cfg)
    finally:
        try:
            os.remove(img)
        except OSError:
            pass
    # The order is authoritative for who/where — never let a misread change them.
    if customer_name:
        fields["customer"] = customer_name
    if site and not (fields.get("job_address") or fields.get("site")):
        fields["site"] = site
    out = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    out.close()
    try:
        delivery_ticket.render_delivery_ticket(fields, out.name)
        with open(out.name, "rb") as fh:
            return fh.read()
    finally:
        try:
            os.remove(out.name)
        except OSError:
            pass
