"""
weight_ticket_pdf.py -- turn a driver's phone photo of a pit scale ticket into
a clean, readable, text-searchable PDF.

Page 1: an Aussieblock header, a typed block with everything known about the
load (pit, ticket #, date, truck, driver, material, gross/tare/net, tons) --
real text, so the PDF is searchable and legible even when the photo isn't --
then the photo itself, cleaned up (auto-rotated from the phone's EXIF, greyscale,
auto-contrast, sharpened) and scaled to fit. Extra photos (back of the ticket,
a retake) each get their own page. A PDF the driver uploaded instead of a photo
is pulled in page-for-page.

    render_weight_ticket_pdf(ticket_dict, [photo_path, ...], out_path)

Offline fpdf2 + Pillow; no API key needed.
"""
import io
import os
import sys
from fpdf import FPDF
from PIL import Image, ImageOps, ImageFilter

INK = (31, 42, 55)
ORANGE = (231, 115, 42)
SHADE = (239, 237, 232)
GREY = (107, 114, 128)
LINE = (201, 205, 211)

_MAX_PX = 2200          # longest side kept in the PDF -- crisp on screen/print, modest file size
_JPEG_Q = 82


def _res(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


LOGO = _res("ab_logo.png")


class _T(FPDF):
    def header(self): pass
    def footer(self): pass


def _clean_photo(path: str):
    """Open a phone photo and return (jpeg_bytes, width_px, height_px) of a
    cleaned-up copy: EXIF-rotated upright, greyscale, auto-contrast (drops the
    darkest/lightest 1% so a shadow or glare doesn't flatten the scale), a touch
    of sharpening, capped at _MAX_PX on the long side. None if it can't be read."""
    try:
        im = Image.open(path)
        im.draft("RGB", (_MAX_PX * 2, _MAX_PX * 2))   # cheap partial decode for huge JPEGs
        im = ImageOps.exif_transpose(im)
        im = im.convert("L")                          # greyscale: thermal slips scan cleanest this way
        im = ImageOps.autocontrast(im, cutoff=1)
        im.thumbnail((_MAX_PX, _MAX_PX))
        im = im.filter(ImageFilter.UnsharpMask(radius=1.4, percent=90, threshold=3))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=_JPEG_Q, optimize=True)
        w, h = im.size
        im.close()
        return buf.getvalue(), w, h
    except Exception:   # noqa: BLE001 -- an unreadable photo just gets skipped, never blocks the PDF
        return None


def _fmt_lb(v):
    try:
        return f"{float(v):,.0f} lb" if v not in (None, "") else "—"
    except (TypeError, ValueError):
        return "—"


def _fmt_tons(v):
    try:
        return f"{float(v):,.2f} tons" if v not in (None, "") else "—"
    except (TypeError, ValueError):
        return "—"


def _fmt_date(iso):
    s = str(iso or "")
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return f"{int(s[5:7])}/{int(s[8:10])}/{s[:4]}"
    return s or "—"


def render_weight_ticket_pdf(t: dict, photos: list, out_path: str, company: dict = None) -> str:
    """Build the PDF at out_path (written atomically). `t` is the ticket as a dict
    (the API's _wt_json shape); `photos` are on-disk image/PDF paths in order."""
    c = company or {}
    pdf = _T(orientation="P", unit="mm", format="Letter")
    pdf.set_auto_page_break(False)
    pdf.set_margins(10, 9, 10)
    pdf.add_font("DejaVu", "", _res("DejaVuSans.ttf"))
    pdf.add_font("DejaVu", "B", _res("DejaVuSans-Bold.ttf"))
    pdf.set_title(f"Weight ticket {t.get('ticket_no') or ('#' + str(t.get('id', '')))} — {t.get('material') or 'aggregate'}")
    pdf.set_subject("Aggregate pit scale ticket")
    pdf.add_page()
    W = pdf.w - 20

    # ---- top band: logo + company ----
    top_y = pdf.get_y()
    logo_w = 24
    try:
        pdf.image(LOGO, x=10, y=top_y, w=logo_w)
    except Exception:   # noqa: BLE001 -- logo missing is cosmetic
        pass
    cx = 10 + logo_w + 5
    pdf.set_xy(cx, top_y)
    pdf.set_text_color(*INK); pdf.set_font("DejaVu", "B", 12)
    pdf.cell(110, 5, c.get("name", "Aussieblock Ready Mix"))
    yy = top_y + 5.5
    pdf.set_font("DejaVu", "", 7); pdf.set_text_color(55, 65, 81)
    for line in [c.get("addr", ""), c.get("city", ""), c.get("phone", "")]:
        if line:
            pdf.set_xy(cx, yy); pdf.cell(120, 3.4, line); yy += 3.4

    # right-hand boxes: date + ticket #
    bw = 38
    bx = 10 + W - bw
    pdf.set_draw_color(150, 150, 150); pdf.set_line_width(0.2)
    pdf.set_xy(bx, top_y); pdf.set_font("DejaVu", "", 5.8); pdf.set_text_color(*GREY)
    pdf.cell(bw, 2.8, "Ticket date", border="LTR", align="C")
    pdf.set_xy(bx, top_y + 2.8); pdf.set_font("DejaVu", "B", 10); pdf.set_text_color(*INK)
    pdf.cell(bw, 4.8, _fmt_date(t.get("ticket_date")), border="LRB", align="C")
    ly = top_y + 8.4
    pdf.set_xy(bx, ly); pdf.set_font("DejaVu", "", 5.8); pdf.set_text_color(*GREY)
    pdf.cell(bw, 2.8, "Scale ticket #", border="LTR", align="C")
    pdf.set_xy(bx, ly + 2.8); pdf.set_font("DejaVu", "B", 10); pdf.set_text_color(*INK)
    pdf.cell(bw, 4.8, str(t.get("ticket_no") or "—"), border="LRB", align="C")

    pdf.set_y(top_y + 19)
    pdf.set_draw_color(*ORANGE); pdf.set_line_width(0.6)
    y = pdf.get_y(); pdf.line(10, y, 10 + W, y); pdf.ln(1.5)

    pdf.set_x(10); pdf.set_text_color(*INK); pdf.set_font("DejaVu", "B", 16)
    pdf.cell(W, 8, "Aggregate Weight Ticket", align="L")
    pdf.set_font("DejaVu", "", 7); pdf.set_text_color(*GREY)
    pdf.cell(0, 8, f"Aussieblock ticket #{t.get('id', '')}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)

    # ---- typed field grid (two columns) ----
    RH = 6.0
    half = W / 2
    pdf.set_draw_color(*LINE); pdf.set_line_width(0.2)

    def pair(k1, v1, k2, v2):
        pdf.set_x(10)
        pdf.set_font("DejaVu", "B", 7.5); pdf.set_text_color(*INK); pdf.set_fill_color(*SHADE)
        pdf.cell(half * 0.38, RH, f" {k1}", border=1, fill=True)
        pdf.set_font("DejaVu", "", 9)
        pdf.cell(half * 0.62, RH, f" {v1 or '—'}", border=1)
        pdf.set_font("DejaVu", "B", 7.5); pdf.set_fill_color(*SHADE)
        pdf.cell(half * 0.38, RH, f" {k2}", border=1, fill=True)
        pdf.set_font("DejaVu", "", 9)
        pdf.cell(half * 0.62, RH, f" {v2 or '—'}", border=1, new_x="LMARGIN", new_y="NEXT")

    pair("Pit / quarry", t.get("supplier"), "Material", t.get("material"))
    pair("Truck", t.get("truck"), "Driver", t.get("driver"))
    pair("Gross", _fmt_lb(t.get("gross_lb")), "Tare", _fmt_lb(t.get("tare_lb")))
    net_lb = None
    if t.get("net_tons") not in (None, ""):
        try:
            net_lb = float(t["net_tons"]) * 2000.0
        except (TypeError, ValueError):
            net_lb = None
    pair("Net", _fmt_lb(net_lb), "NET TONS", _fmt_tons(t.get("net_tons")))
    logged = "Driver tablet" if (t.get("source") or "driver") == "driver" else "Office"
    pair("Logged from", logged, "Reviewed", "Yes" if t.get("reviewed") else "Not yet")
    if t.get("notes"):
        pdf.set_x(10); pdf.set_font("DejaVu", "B", 7.5); pdf.set_fill_color(*SHADE)
        pdf.cell(half * 0.38, RH, " Notes", border=1, fill=True)
        pdf.set_font("DejaVu", "", 8.5)
        pdf.cell(W - half * 0.38, RH, f" {t['notes']}"[:140], border=1, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_x(10); pdf.set_font("DejaVu", "", 6.5); pdf.set_text_color(*GREY)
    pdf.cell(W, 3.5, "Figures above are as logged in the Aussieblock app (typed by the driver/office or read off the photo); the pit's scale ticket below is the record.", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1.5)

    # ---- the photo(s) ----
    def place_image(jpeg, w_px, h_px, top, bottom):
        """Fit the image inside the box (10..10+W) x (top..bottom), centred."""
        box_w, box_h = W, max(20.0, bottom - top)
        scale = min(box_w / w_px, box_h / h_px)
        w_mm, h_mm = w_px * scale, h_px * scale
        x = 10 + (box_w - w_mm) / 2
        pdf.image(io.BytesIO(jpeg), x=x, y=top, w=w_mm, h=h_mm)

    first = True
    page_bottom = pdf.h - 9
    placed = 0
    for path in photos:
        ext = os.path.splitext(path)[1].lower()
        if ext == ".pdf":
            # A PDF the driver uploaded: rasterise each page and place it.
            try:
                import fitz   # PyMuPDF, already a dependency
                doc = fitz.open(path)
                for i in range(min(len(doc), 6)):
                    pix = doc[i].get_pixmap(dpi=150)
                    im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                    buf = io.BytesIO(); im.save(buf, "JPEG", quality=_JPEG_Q)
                    if not first:
                        pdf.add_page()
                    top = pdf.get_y() + 1 if first else 9
                    place_image(buf.getvalue(), pix.width, pix.height, top, page_bottom)
                    first = False; placed += 1
                doc.close()
            except Exception:   # noqa: BLE001 -- skip a PDF we can't rasterise
                pass
            continue
        got = _clean_photo(path)
        if not got:
            continue
        jpeg, w_px, h_px = got
        if not first:
            pdf.add_page()
            pdf.set_xy(10, 9); pdf.set_font("DejaVu", "", 7); pdf.set_text_color(*GREY)
            pdf.cell(W, 4, f"Weight ticket #{t.get('id', '')} — additional photo", new_x="LMARGIN", new_y="NEXT")
        top = pdf.get_y() + 1
        place_image(jpeg, w_px, h_px, top, page_bottom)
        first = False; placed += 1

    if placed == 0:
        pdf.set_x(10); pdf.set_font("DejaVu", "", 9); pdf.set_text_color(*GREY)
        pdf.cell(W, 8, "No photo of the scale ticket is on file.", new_x="LMARGIN", new_y="NEXT")

    tmp = out_path + ".tmp"
    pdf.output(tmp)
    os.replace(tmp, out_path)
    return out_path


if __name__ == "__main__":
    import json
    t = json.loads(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].startswith("{") else {"id": 0}
    render_weight_ticket_pdf(t, sys.argv[2:-1], sys.argv[-1])
    print("wrote", sys.argv[-1])
