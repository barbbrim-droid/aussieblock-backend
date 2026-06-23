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
import re
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


def _mix_design_from(materials) -> dict:
    """Collapse the protocol's material rows into the app's mix-design grid, keeping
    Portland cement and slag as SEPARATE rows so the materials tracker can read each
    one's actual batched weight. 'Slag Cement' counts as slag (checked before cement).
    Aggregates (rock/sand) and admixtures (fiber/retarder/water reducer) carry
    their actual too — the tracker draws usage + cost off these. Admixture cells keep
    a 'unit' (lb for fiber, oz for liquids) since their cost rate is per that unit."""
    md = {k: {"design": "", "target": "", "actual": ""}
          for k in ["rock", "sand", "cement", "slag", "air", "water",
                    "fiber", "retarder", "water_reducer", "e5_lfa"]}
    cem = [0.0, 0.0, 0.0]   # recipe / target / actual lb
    slag = [0.0, 0.0, 0.0]
    for row in materials or []:
        try:
            name, unit, dens, rec, sv, av, lim, lab = row
        except (ValueError, TypeError):
            continue
        n = str(name).lower()
        cell = {"design": f"{rec:g}", "target": f"{sv:,.0f}", "actual": f"{av:,.0f}"}
        adx = {**cell, "unit": str(unit or "").strip() or "lb"}
        if "gravel" in n or "agg1" in n:
            md["rock"] = cell
        elif "sand" in n or "agg2" in n:
            md["sand"] = cell
        elif "slag" in n:
            slag[0] += rec; slag[1] += sv; slag[2] += av
        elif "cem" in n or "portland" in n:
            cem[0] += rec; cem[1] += sv; cem[2] += av
        elif "fiber" in n or "matrix" in n:
            md["fiber"] = adx
        # E5 Liquid Fly Ash — match before plain "water" (some sheets print "liquid
        # fly ash"; "fly ash" alone also counts).
        elif re.search(r"\be5\b|liquid\s*fly\s*ash|\blfa\b|fly\s*ash", n):
            md["e5_lfa"] = adx
        # Water reducer must be checked BEFORE plain water (the name contains "water").
        # "Master/Macier X-Seed 66" is this plant's water reducer (read off the ticket
        # either way), so match the distinctive "x-seed" token regardless of prefix.
        elif re.search(r"reduc|glenium|polyheed|pozzolith|wrda|daracem|\bwr\b|\badva\b|plastol|mira|x[\s-]*seed", n):
            md["water_reducer"] = adx
        # Set retarder — this plant uses "MasterSet DELVO" (a Type D retarder); also
        # catch generic retarder/stabilizer names.
        elif re.search(r"delvo|retard|stabiliz|recover|\bdtd\b", n):
            md["retarder"] = adx
        elif "water" in n:
            md["water"] = cell
    if cem[1] or cem[2]:
        md["cement"] = {"design": f"{cem[0]:g}", "target": f"{cem[1]:,.0f}", "actual": f"{cem[2]:,.0f}"}
    if slag[1] or slag[2]:
        md["slag"] = {"design": f"{slag[0]:g}", "target": f"{slag[1]:,.0f}", "actual": f"{slag[2]:,.0f}"}
    return md


def _batch_data_from(d: dict) -> dict:
    """The app's nested batch_data from a parsed typed protocol (powers the silo
    tracker's actual-usage draw-down and the in-app ticket view)."""
    o = d.get("order") or {}
    return {
        "date": d.get("report_date", ""), "cash_charge": "", "customer_phone": "",
        "product_name": o.get("recipe", ""), "plant": o.get("plant", ""), "air": "",
        "load": o.get("load_no", ""), "ordered": o.get("qty", ""), "delivered": o.get("qty", ""),
        "water_reducer": "", "retarder": "",
        "times": {"left_plant": "", "a_train_pr": "", "left_job": "", "return_plant": ""},
        "inspector": "", "received_by": "",
        "mix_design": _mix_design_from(d.get("materials") or []),
        "pricing": {"unit_price": "", "extended": "", "subtotal": "", "tax1": "", "tax2": "",
                    "total": "", "job_running_total": ""},
        "_kind": "typed protocol",
    }


def convert(data: bytes, filename: str, customer_name: str = None, site: str = None,
            order_mix: str = None, order_qty=None, price_sheet: dict = None,
            order_admixtures: str = "", return_data: bool = False, load_label: str = None,
            mixer_water=None):
    """Read the uploaded ticket and render the branded PDF. Returns PDF bytes, or
    (pdf_bytes, batch_data) when return_data=True — batch_data is the parsed nested
    record for a typed protocol (with cement & slag actuals), else None.
    Raises on any failure (the caller falls back to the original)."""
    cfg = _cfg()
    # context the reader uses to compute the ticket's pricing block
    cfg["_pricing"] = {"sheet": price_sheet, "mix": order_mix,
                       "customer": customer_name, "order_qty": order_qty,
                       "order_admixtures": order_admixtures}
    cfg["_mixer_water"] = mixer_water   # gal of on-site water from the mixer sensor (or None)
    img = _to_image_file(data, filename)
    out = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    out.close()
    batch_data = None
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
                if load_label:
                    d["order"]["load_no"] = load_label       # "3 of 6" — which load of the pour
            generator.render_ticket(d, out.name)
            try:
                batch_data = _batch_data_from(d)   # cement & slag actuals for the silo tracker
            except Exception as e:
                print("batch_data extract failed:", e)
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
            if load_label:
                d["load"] = load_label        # "3 of 6" — which load of the pour
            delivery_ticket.render_delivery_ticket(d, out.name)
        with open(out.name, "rb") as fh:
            pdf = fh.read()
        return (pdf, batch_data) if return_data else pdf
    finally:
        for p in (img, out.name):
            try:
                os.remove(p)
            except OSError:
                pass
