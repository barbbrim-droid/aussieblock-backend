"""
generator.py  -- Aussieblock branded batch ticket renderer (100% offline)

Pure-Python PDF generation via fpdf2. No native libraries, no internet.
Bundles cleanly into a single Windows .exe with PyInstaller.

Input:  a `data` dict (produced by parser.py from a dornerBatch protocol)
Output: a branded one-page PDF identical in layout to the Aussieblock ticket.
"""
import os, sys
from fpdf import FPDF

# ---- brand palette ----
INK   = (31, 42, 55)     # dark navy header bars
ORANGE = (231, 115, 42)
GREEN = (21, 128, 61)
RED   = (220, 38, 38)
SHADE = (239, 237, 232)  # light label shading
GREY  = (107, 114, 128)
WATER_LB_PER_GAL = 8.345
MIN_MATERIAL_ROWS = 9   # reserve blank rows so an added admixture has room

def _res(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)
HERE = os.path.dirname(os.path.abspath(__file__))
LOGO = _res("ab_logo.png")


def _pct(sv, av):
    return 0.0 if not sv else (av - sv) / sv * 100.0


def _max_water(binder_lb, wc_eq, max_wc):
    lb = max(0.0, (max_wc - wc_eq) * binder_lb)
    return lb, lb / WATER_LB_PER_GAL


class Ticket(FPDF):
    def header(self):
        pass

    def footer(self):
        pass


def render_ticket(data, out_path):
    d = data
    pdf = Ticket(orientation="P", unit="mm", format="Letter")
    pdf.set_auto_page_break(False)
    pdf.set_margins(10, 9, 10)
    pdf.add_font("DejaVu","",_res("DejaVuSans.ttf"))
    pdf.add_font("DejaVu","B",_res("DejaVuSans-Bold.ttf"))
    pdf.add_page()
    W = pdf.w - 20  # usable width ~ 195.9mm
    RH = 4.6        # data row height (roomier)
    BAR = 5.6       # section header bar height
    GAP = 2.2       # gap before each section

    # ---------- top band: logo (left) / company / weather ----------
    top_y = pdf.get_y()
    c = d["company"]
    # logo top-left (not centered)
    logo_w = 26
    try:
        pdf.image(LOGO, x=10, y=top_y, w=logo_w)
    except Exception:
        pass
    # company block to the right of the logo
    cx = 10 + logo_w + 5
    pdf.set_xy(cx, top_y)
    pdf.set_text_color(*INK); pdf.set_font("DejaVu", "B", 12)
    pdf.cell(95, 5, c["name"])
    yy = top_y + 6
    pdf.set_font("DejaVu", "", 7.5); pdf.set_text_color(55, 65, 81)
    for line in [c["addr"], c["city"], c["phone"], c["emails"]]:
        pdf.set_xy(cx, yy); pdf.cell(95, 3.6, line); yy += 3.6

    pdf.set_y(top_y + 22)
    pdf.ln(1)
    # orange rule
    pdf.set_draw_color(*ORANGE); pdf.set_line_width(0.6)
    y = pdf.get_y(); pdf.line(10, y, 10 + W, y); pdf.ln(2)

    # ---------- title bar ----------
    ty = pdf.get_y()
    # report date box pinned at far right
    bx = 10 + W - 40
    pdf.set_draw_color(150, 150, 150); pdf.set_line_width(0.2)
    pdf.set_xy(bx, ty)
    pdf.set_font("DejaVu", "", 6); pdf.set_text_color(*GREY)
    pdf.cell(40, 3.2, "Report Date", border="LTR", align="C")
    pdf.set_xy(bx, ty + 3.2); pdf.set_font("DejaVu", "B", 12); pdf.set_text_color(*INK)
    pdf.cell(40, 5, d["report_date"], border="LRB", align="C")
    # title centered across the full page width
    pdf.set_xy(10, ty)
    pdf.set_text_color(*INK); pdf.set_font("DejaVu", "B", 19)
    pdf.cell(W, 8, "Total Batch Protocol", align="C")
    pdf.set_y(ty + 9)

    def section(title):
        pdf.ln(GAP)
        pdf.set_fill_color(*INK); pdf.set_text_color(255, 255, 255)
        pdf.set_font("DejaVu", "B", 10)
        pdf.cell(W, BAR, "  " + title, ln=1, fill=True)

    def kv_row(k, v, kw=0.30):
        pdf.set_draw_color(201, 205, 211); pdf.set_line_width(0.2)
        pdf.set_font("DejaVu", "B", 8); pdf.set_text_color(*INK)
        pdf.set_fill_color(*SHADE)
        pdf.cell(W * kw, RH, "  " + k, border=1, fill=True)
        pdf.set_font("DejaVu", "", 8)
        pdf.cell(W * (1 - kw), RH, "  " + str(v), border=1, ln=1)

    # ---------- ORDER INFORMATION ----------
    section("ORDER INFORMATION")
    o = d["order"]
    for k, v in [
        ("Plant", o["plant"]), ("Recipe no. / Name", o["recipe"]),
        ("Customer no. / Name", o["customer"]),
        ("Construction site no. / Name", o["site_no"]),
        ("Construction Site Address", o["site_addr"]),
        ("Vehicle no. / VRN", o["vrn"]),
        ("Quantity", o["qty"]),
        ("Load Number", o["load_no"]),
    ]:
        kv_row(k, v)

    # ---------- BATCHES ----------
    section("BATCHES")
    pdf.set_draw_color(201, 205, 211); pdf.set_line_width(0.2)
    pdf.set_fill_color(*INK); pdf.set_text_color(255, 255, 255); pdf.set_font("DejaVu", "B", 8)
    pdf.cell(W * 0.30, RH, "Batch protocol no.", border=1, align="C", fill=True)
    pdf.cell(W * 0.45, RH, "Prod. time", border=1, align="C", fill=True)
    pdf.cell(W * 0.25, RH, "Batch quantity", border=1, align="C", fill=True, ln=1)
    pdf.set_text_color(*INK); pdf.set_font("DejaVu", "", 8)
    for n, ts, q in d["batches"]:
        pdf.cell(W * 0.30, RH, n, border=1, align="C")
        pdf.cell(W * 0.45, RH, ts, border=1, align="C")
        pdf.cell(W * 0.25, RH, q, border=1, align="C", ln=1)
    # always leave room for up to 8 batches (blank rows if fewer were run)
    for _ in range(max(0, 8 - len(d["batches"]))):
        pdf.cell(W * 0.30, RH, "", border=1)
        pdf.cell(W * 0.45, RH, "", border=1)
        pdf.cell(W * 0.25, RH, "", border=1, ln=1)

    # ---------- BATCH INFORMATION ----------
    section("BATCH INFORMATION")
    # widths sum to W
    cw = [w * W / 193.0 for w in [46, 12, 16, 20, 22, 22, 20, 18, 17]]
    heads = [("Material name / Silo name", "L"), ("Unit", "C"), ("Density\n(lb/ft³)", "C"),
             ("Recipe\n(SV)", "C"), ("Set value\n(SV)", "C"), ("Actual value\n(AV)", "C"),
             ("Diff.\nAV - SV", "C"), ("% Diff.", "C"), ("Allowable\nASTM C94", "C")]
    pdf.set_font("DejaVu", "B", 6.8)
    hy = pdf.get_y()
    HH, lh = 8.0, 2.9
    pdf.set_fill_color(*INK)
    pdf.rect(10, hy, W, HH, style="F")          # one even dark band
    pdf.set_text_color(255, 255, 255)
    x = 10
    for (txt, al), wd in zip(heads, cw):
        nlines = txt.count("\n") + 1
        pdf.set_xy(x, hy + (HH - nlines * lh) / 2)   # vertically centered
        pdf.multi_cell(wd, lh, txt, border=0, align=al,
                       new_x="RIGHT", new_y="TOP", max_line_height=lh)
        x += wd
    pdf.set_xy(10, hy + HH)
    pdf.set_text_color(*INK); pdf.set_draw_color(201, 205, 211); pdf.set_font("DejaVu", "", 6.9)
    for name, unit, dens, recipe, sv, av, lim, lab in d["materials"]:
        diff = av - sv
        p = _pct(sv, av)
        ok = abs(p) <= lim
        cells = [
            (" " + name, "L", INK), (unit, "C", INK), (f"{dens:g}", "C", INK),
            (f"{recipe:g} lb", "C", INK), (f"{sv:,.2f} lb", "C", INK),
            (f"{av:,.2f} lb", "C", INK), (f"{diff:+,.2f} lb", "C", INK),
            (f"{p:+.2f}%", "C", GREEN if ok else RED), (lab, "C", INK),
        ]
        bold = [False, False, False, False, False, True, False, True, False]
        for (val, al, col), wd, b in zip(cells, cw, bold):
            pdf.set_text_color(*col); pdf.set_font("DejaVu", "B" if b else "", 6.9)
            pdf.cell(wd, RH, val, border=1, align=al)
        pdf.ln(RH)
    # always leave a few blank rows under the last material for extra admixtures
    pdf.set_text_color(*INK); pdf.set_font("DejaVu", "", 6.9)
    for _ in range(max(3, MIN_MATERIAL_ROWS - len(d["materials"]))):
        for wd in cw:
            pdf.cell(wd, RH, "", border=1)
        pdf.ln(RH)

    # ---------- two-column: totals + process ----------
    pdf.ln(GAP)
    twy = pdf.get_y()
    half = (W - 4) / 2
    # left: totals
    pdf.set_xy(10, twy)
    pdf.set_fill_color(*INK); pdf.set_text_color(255, 255, 255); pdf.set_font("DejaVu", "B", 7.8)
    pdf.cell(half * 0.42, RH, "", fill=True)
    pdf.cell(half * 0.25, RH, "Σ", align="C", fill=True)
    pdf.cell(half * 0.33, RH, "Σ / yd³", align="C", fill=True, ln=2)
    pdf.set_x(10); pdf.set_text_color(*INK); pdf.set_draw_color(201, 205, 211)
    for k, (a, b) in d["totals"].items():
        is_total = k == "Total weight"
        pdf.set_font("DejaVu", "B", 7.8)
        pdf.cell(half * 0.42, RH, "  " + k, border=1)
        pdf.set_font("DejaVu", "B" if is_total else "", 7.8)
        pdf.cell(half * 0.25, RH, f"{a:,.2f} lb", border=1, align="C")
        pdf.cell(half * 0.33, RH, f"{b:,.2f} lb/yd³", border=1, align="C", ln=2)
        pdf.set_x(10)
    left_end = pdf.get_y()

    # right: process key-values
    pdf.set_xy(10 + half + 4, twy)
    rx = 10 + half + 4
    pdf.set_draw_color(201, 205, 211)
    for k, v in d["process"].items():
        pdf.set_x(rx)
        pdf.set_font("DejaVu", "B", 7.8); pdf.set_fill_color(*SHADE); pdf.set_text_color(*INK)
        pdf.cell(half * 0.62, RH, "  " + k, border=1, fill=True)
        pdf.set_font("DejaVu", "B", 7.8)
        pdf.cell(half * 0.38, RH, str(v) + "  ", border=1, align="R", ln=2)
    right_end = pdf.get_y()
    content_end = max(left_end, right_end)

    # ---------- pricing ----------
    px = d.get("pricing")
    if px:
        pdf.set_y(content_end + 3)
        pdf.set_fill_color(*INK); pdf.set_text_color(255, 255, 255)
        pdf.set_font("DejaVu", "B", 8); pdf.set_x(10)
        pdf.cell(W, 5, "  PRICING", fill=True, ln=1)
        pdf.set_text_color(*INK)
        money = lambda v: ("-$%0.2f" % abs(v)) if v < 0 else ("$%0.2f" % v)

        def _prow(label, val, bold=False):
            pdf.set_font("DejaVu", "B" if bold else "", 7.6)
            pdf.set_x(10); pdf.cell(W - 38, 4.4, "  " + label, border="LRB")
            pdf.cell(38, 4.4, val + "  ", border="LRB", align="R", ln=1)

        yd = d.get("yards", 0) or 0
        _prow("Concrete  (%g yd x %s/yd)" % (yd, money(px["unit_price"])), money(px["extended"]))
        if px.get("short_load"):
            _prow("Short-load fee (order under min)", money(px["short_load"]))
        if px.get("backhaul"):
            _prow("Back-haul fee", money(px["backhaul"]))
        _prow("Subtotal", money(px["subtotal"]))
        _prow("Sales tax (%g%%)" % px.get("tax_pct", 0), money(px["tax"]))
        _prow("Total", money(px["total"]), bold=True)
        content_end = pdf.get_y()

    # ---------- footer: signature + max water ----------
    pdf.set_y(content_end + 4)
    fy = pdf.get_y()
    mw_lb, mw_gal = _max_water(d["binder_lb"], d["wc_eq"], d["max_wc"])
    box_h = 31
    # signature box
    pdf.set_draw_color(201, 205, 211); pdf.set_line_width(0.2)
    pdf.rect(10, fy, half, box_h)
    pdf.set_xy(12, fy + 4)
    pdf.set_font("DejaVu", "B", 8); pdf.set_text_color(*INK)
    pdf.cell(half - 4, 4, "Total water added at construction site:")
    pdf.line(12, fy + 12, 10 + half - 24, fy + 12)             # fill-in line
    pdf.set_xy(10 + half - 22, fy + 8.5); pdf.set_font("DejaVu", "", 7.5); pdf.set_text_color(*GREY)
    pdf.cell(20, 4, "gallons")
    pdf.set_xy(12, fy + 16); pdf.set_font("DejaVu", "B", 8); pdf.set_text_color(*INK)
    pdf.cell(half - 4, 4, "Customer / Contractor Signature:")
    pdf.line(12, fy + 26, 10 + half - 3, fy + 26)              # signature line (room to sign above)
    # max water box (red)
    pdf.set_draw_color(*RED); pdf.set_line_width(0.4)
    pdf.rect(10 + half + 4, fy, half, box_h)
    pdf.set_xy(10 + half + 6, fy + 7)
    pdf.set_text_color(*RED); pdf.set_font("DejaVu", "B", 11)
    pdf.cell(half - 4, 5, "Max Water Allowed to Add:")
    pdf.set_xy(10 + half + 4, fy + 16)
    pdf.set_font("DejaVu", "B", 18)
    pdf.cell(half - 3, 8, f"{mw_gal:.1f} gal", align="R")
    pdf.set_xy(10 + half + 4, fy + 25)
    pdf.set_font("DejaVu", "", 6.5); pdf.set_text_color(*GREY)
    pdf.cell(half - 3, 3, f"({mw_lb:.1f} lb  -  at max w/c {d['max_wc']})", align="R")

    pdf.output(out_path)
    return out_path


if __name__ == "__main__":
    from sample_data import TICKET
    out = os.path.join(HERE, "sample_out.pdf")
    render_ticket(TICKET, out)
    print("wrote", out)
