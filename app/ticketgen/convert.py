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


_MAX_PX = 1600   # longest side sent to the vision reader (plenty for typed text)


def _downscale_to_jpeg(img_bytes: bytes, out_path: str) -> bool:
    """Open an image's bytes and save a downscaled JPEG using PIL 'draft' mode,
    which decodes large JPEGs at reduced resolution -> low memory. Returns False
    if PIL can't open it."""
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(img_bytes))
        im.draft("RGB", (_MAX_PX, _MAX_PX))   # cheap partial decode for big JPEGs
        im = im.convert("RGB")
        im.thumbnail((_MAX_PX, _MAX_PX))
        im.save(out_path, "JPEG", quality=85)
        im.close()
        return True
    except Exception:
        return False


def _to_image_file(data: bytes, filename: str) -> str:
    """Write the ticket out as a modest JPEG (page 1 if PDF) for the vision reader,
    using a low-memory path so big scans don't blow up the worker."""
    import gc
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
    tmp.close()
    if _is_pdf(data, filename):
        import fitz  # PyMuPDF
        doc = fitz.open(stream=data, filetype="pdf")
        page = doc[0]
        # A scanned protocol is usually ONE big embedded image — pull it out and
        # downscale it cheaply, instead of rendering the whole page at high res.
        done = False
        try:
            imgs = page.get_images(full=True)
            if imgs:
                xref = max(imgs, key=lambda im: (im[2] or 0) * (im[3] or 0))[0]
                ext = doc.extract_image(xref)
                if ext and ext.get("image"):
                    done = _downscale_to_jpeg(ext["image"], tmp.name)
        except Exception:
            done = False
        if not done:
            pix = page.get_pixmap(dpi=170)   # vector/text PDF (small) — readable render
            pix.save(tmp.name)
            pix = None
        doc.close()
        doc = None
    else:
        _downscale_to_jpeg(data, tmp.name)
    gc.collect()
    return tmp.name


def convert(data: bytes, filename: str, customer_name: str = None, site: str = None,
            order_mix: str = None, order_qty=None, price_sheet: dict = None,
            order_admixtures: str = "") -> bytes:
    """Read the uploaded ticket and render the branded PDF. Returns PDF bytes.
    Raises on any failure (the caller falls back to the original)."""
    cfg = _cfg()
    # context the reader uses to compute the ticket's pricing block
    cfg["_pricing"] = {"sheet": price_sheet, "mix": order_mix,
                       "customer": customer_name, "order_qty": order_qty,
                       "order_admixtures": order_admixtures}
    img = _to_image_file(data, filename)
    out = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    out.close()
    try:
        if _is_pdf(data, filename):
            # Typed dornerBatch "Total batch protocol" — full materials/batches/water.
            from . import read_protocol, generator
            d = read_protocol.read_protocol(img, cfg)
            if isinstance(d.get("order"), dict):
                if customer_name:
                    d["order"]["customer"] = customer_name   # order is authoritative for who it's for
                if site:
                    d["order"]["site_addr"] = site           # …and for the job-site address
            generator.render_ticket(d, out.name)
        else:
            # Handwritten field-ticket photo — summary delivery ticket.
            from . import read_ticket, delivery_ticket
            d = read_ticket.read_ticket(img, cfg)
            if customer_name:
                d["customer"] = customer_name
            if site:
                # The address on the order card is authoritative — override whatever
                # was read off the paper (handwriting is easy to misread).
                d["job_address"] = site
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
