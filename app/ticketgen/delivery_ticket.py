"""
delivery_ticket.py -- Aussieblock DELIVERY ticket (printable PDF) that mirrors
EVERY field on the paper batch ticket. Offline fpdf2 render.

    from delivery_ticket import render_delivery_ticket
    render_delivery_ticket(data, "out.pdf")

`data` may be FLAT (read_ticket output: date, customer, job_address, product,
ordered_qty, delivered_qty, load, truck, driver, plant, slump, air, cash_charge,
customer_phone, water_reducer, retarder, unit_price, subtotal, tax, total) OR
NESTED (the app's batch_data: with times{}, mix_design{}, pricing{}, inspector,
received_by). Missing fields render blank.
"""
import os, sys
from fpdf import FPDF

INK    = (31, 42, 55)
ORANGE = (231, 115, 42)
SHADE  = (239, 237, 232)
GREY   = (107, 114, 128)
LINE   = (201, 205, 211)


def _res(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


LOGO = _res("ab_logo.png")


def _at(d, path):
    """Read a dotted path out of a (possibly nested) dict; '' if absent."""
    cur = d
    for k in path.split("."):
        if isinstance(cur, dict) and cur.get(k) not in (None, ""):
            cur = cur[k]
        else:
            return ""
    return cur if isinstance(cur, str) else str(cur)


def _first(d, *keys):
    for k in keys:
        v = _at(d, k)
        if v:
            return v
    return ""


class _T(FPDF):
    def header(self): pass
    def footer(self): pass


def render_delivery_ticket(data, out_path):
    d = data
    c = d.get("company", {})
    pdf = _T(orientation="P", unit="mm", format="Letter")
    pdf.set_auto_page_break(False)
    pdf.set_margins(10, 9, 10)
    pdf.add_font("DejaVu", "", _res("DejaVuSans.ttf"))
    pdf.add_font("DejaVu", "B", _res("DejaVuSans-Bold.ttf"))
    pdf.add_page()
    W = pdf.w - 20

    # ---- top band: logo + company ----
    top_y = pdf.get_y()
    logo_w = 24
    try:
        pdf.image(LOGO, x=10, y=top_y, w=logo_w)
    except Exception:
        pass
    cx = 10 + logo_w + 5
    pdf.set_xy(cx, top_y)
    pdf.set_text_color(*INK); pdf.set_font("DejaVu", "B", 12)
    pdf.cell(110, 5, c.get("name", "Aussieblock Ready Mix"))
    yy = top_y + 5.5
    pdf.set_font("DejaVu", "", 7); pdf.set_text_color(55, 65, 81)
    for line in [c.get("addr", ""), c.get("city", ""), c.get("phone", ""), c.get("emails", "")]:
        if line:
            pdf.set_xy(cx, yy); pdf.cell(120, 3.4, line); yy += 3.4

    pdf.set_y(top_y + 20)
    pdf.set_draw_color(*ORANGE); pdf.set_line_width(0.6)
    y = pdf.get_y(); pdf.line(10, y, 10 + W, y); pdf.ln(1.5)

    # ---- title + date / load boxes ----
    ty = pdf.get_y()
    bw = 38
    bx = 10 + W - bw
    pdf.set_draw_color(150, 150, 150); pdf.set_line_width(0.2)
    pdf.set_xy(bx, ty); pdf.set_font("DejaVu", "", 5.8); pdf.set_text_color(*GREY)
    pdf.cell(bw, 2.8, "Date", border="LTR", align="C")
    pdf.set_xy(bx, ty + 2.8); pdf.set_font("DejaVu", "B", 10); pdf.set_text_color(*INK)
    pdf.cell(bw, 4.8, _first(d, "date") or "—", border="LRB", align="C")
    ly = ty + 7.6
    pdf.set_xy(bx, ly); pdf.set_font("DejaVu", "", 5.8); pdf.set_text_color(*GREY)
    pdf.cell(bw, 2.8, "Load / Ticket #", border="LTR", align="C")
    pdf.set_xy(bx, ly + 2.8); pdf.set_font("DejaVu", "B", 10); pdf.set_text_color(*INK)
    pdf.cell(bw, 4.8, _first(d, "load") or "—", border="LRB", align="C")
    pdf.set_xy(10, ty + 1)
    pdf.set_text_color(*INK); pdf.set_font("DejaVu", "B", 20)
    pdf.cell(W - bw - 4, 8, "Delivery Ticket", align="C")
    pdf.set_y(ty + 16)

    RH = 5.6
    half = W / 2

    def section(title):
        pdf.ln(1.8)
        pdf.set_fill_color(*INK); pdf.set_text_color(255, 255, 255)
        pdf.set_font("DejaVu", "B", 9)
        pdf.cell(W, 5.0, "  " + title, ln=1, fill=True)

    def pair(k1, v1, k2, v2):
        kw = 0.17 * W
        vw = half - kw
        pdf.set_draw_color(*LINE); pdf.set_line_width(0.2)
        pdf.set_font("DejaVu", "B", 8); pdf.set_text_color(*INK); pdf.set_fill_color(*SHADE)
        pdf.cell(kw, RH, "  " + k1, border=1, fill=True)
        pdf.set_font("DejaVu", "", 8.5)
        pdf.cell(vw, RH, "  " + str(v1), border=1)
        if k2 is None:
            pdf.ln(RH); return
        pdf.set_font("DejaVu", "B", 8); pdf.set_fill_color(*SHADE)
        pdf.cell(kw, RH, "  " + k2, border=1, fill=True)
        pdf.set_font("DejaVu", "", 8.5)
        pdf.cell(vw, RH, "  " + str(v2), border=1, ln=1)

    def wide(k, v):
        kw = 0.17 * W
        pdf.set_draw_color(*LINE); pdf.set_font("DejaVu", "B", 8)
        pdf.set_text_color(*INK); pdf.set_fill_color(*SHADE)
        pdf.cell(kw, RH, "  " + k, border=1, fill=True)
        pdf.set_font("DejaVu", "", 8.5)
        pdf.cell(W - kw, RH, "  " + str(v), border=1, ln=1)

    # ---- CUSTOMER & JOB ----
    section("CUSTOMER & JOB")
    wide("Sold To", _first(d, "customer"))
    pair("Phone", _first(d, "customer_phone") or "—", "Cash / Charge", _first(d, "cash_charge") or "—")
    wide("Job Address", _first(d, "job_address", "site"))

    # ---- LOAD DETAILS ----
    section("LOAD DETAILS")
    pair("Product / Mix", _first(d, "product", "mix"), "Slump", _first(d, "slump") or "—")
    pair("Ordered", _first(d, "ordered", "ordered_qty"), "Delivered", _first(d, "delivered", "delivered_qty"))
    pair("Truck", _first(d, "truck") or "—", "Driver", _first(d, "driver") or "—")
    pair("Plant", _first(d, "plant") or "—", "Air", _first(d, "air") or "—")
    pair("Water Reducer", _first(d, "water_reducer") or "—", "Retarder", _first(d, "retarder") or "—")
    pair("Inspector", _first(d, "inspector") or "—", "Product Name", _first(d, "product_name") or "—")

    # ---- TIMES ----
    section("TIMES")
    qw = W / 4
    pdf.set_draw_color(*LINE)
    for lab in ["Left Plant", "A-Train PR", "Left Job", "Return Plant"]:
        pdf.set_font("DejaVu", "B", 7.5); pdf.set_fill_color(*SHADE); pdf.set_text_color(*INK)
        pdf.cell(qw, RH, "  " + lab, border="LTR", fill=True)
    pdf.ln(RH)
    keys = ["times.left_plant", "times.a_train_pr", "times.left_job", "times.return_plant"]
    flat = ["left_plant", "a_train_pr", "left_job", "return_plant"]
    for k, fk in zip(keys, flat):
        pdf.set_font("DejaVu", "", 8.5); pdf.set_text_color(*INK)
        pdf.cell(qw, RH, "  " + (_first(d, k, fk) or "—"), border="LRB")
    pdf.ln(RH)

    # ---- MIX DESIGN ----
    section("MIX DESIGN")
    lw = 0.22 * W
    cw = (W - lw) / 3
    pdf.set_fill_color(*INK); pdf.set_text_color(255, 255, 255); pdf.set_font("DejaVu", "B", 7.5)
    pdf.cell(lw, RH, "", fill=True)
    for h in ["Design", "Target", "Actual"]:
        pdf.cell(cw, RH, h, align="C", fill=True)
    pdf.ln(RH)
    pdf.set_text_color(*INK); pdf.set_draw_color(*LINE)
    for row in ["Rock", "Sand", "Cement", "Air", "Water"]:
        pdf.set_font("DejaVu", "B", 8); pdf.set_fill_color(*SHADE)
        pdf.cell(lw, RH, "  " + row, border=1, fill=True)
        pdf.set_font("DejaVu", "", 8.5)
        for col in ["design", "target", "actual"]:
            pdf.cell(cw, RH, _at(d, f"mix_design.{row.lower()}.{col}") or "", border=1, align="C")
        pdf.ln(RH)

    # ---- PRICING ----
    section("PRICING")
    pair("Unit Price", _first(d, "pricing.unit_price", "unit_price"), "Extended", _first(d, "pricing.extended", "extended"))
    pair("Subtotal", _first(d, "pricing.subtotal", "subtotal"), "Tax 1", _first(d, "pricing.tax1", "tax"))
    pair("Tax 2", _first(d, "pricing.tax2", "tax2"), "Total", _first(d, "pricing.total", "total"))
    wide("Job Running Total", _first(d, "pricing.job_running_total", "job_running_total"))

    # ---- signature ----
    pdf.ln(3)
    fy = pdf.get_y()
    box_h = 26
    pdf.set_draw_color(*LINE); pdf.set_line_width(0.2)
    pdf.rect(10, fy, W, box_h)
    pdf.set_xy(13, fy + 3.5); pdf.set_font("DejaVu", "B", 8.5); pdf.set_text_color(*INK)
    pdf.cell(120, 4, "Total water added at construction site:")
    pdf.line(95, fy + 7.5, 150, fy + 7.5)
    pdf.set_xy(152, fy + 4); pdf.set_font("DejaVu", "", 7.5); pdf.set_text_color(*GREY)
    pdf.cell(20, 4, "gallons")
    pdf.set_xy(13, fy + 13); pdf.set_font("DejaVu", "B", 8.5); pdf.set_text_color(*INK)
    rb = _first(d, "received_by")
    pdf.cell(46, 4, "Received by / Signature:")
    pdf.line(62, fy + 16.5, 10 + W - 5, fy + 16.5)
    if rb:
        pdf.set_xy(64, fy + 12.5); pdf.set_font("DejaVu", "", 9); pdf.cell(80, 4, rb)
    pdf.set_xy(13, fy + 20); pdf.set_font("DejaVu", "", 6.5); pdf.set_text_color(*GREY)
    pdf.cell(W - 6, 3, "Excess water added at customer's request is the customer's responsibility for strength, slump and quality of concrete.")

    pdf.set_y(fy + box_h + 2)
    pdf.set_font("DejaVu", "", 6.5); pdf.set_text_color(*GREY)
    pdf.cell(W, 3, c.get("emails", ""), align="C")

    pdf.output(out_path)
    return out_path


if __name__ == "__main__":
    import json
    HERE = os.path.dirname(os.path.abspath(__file__))
    cfg = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
    sample = {
        "company": cfg["company"], "sales_tax_pct": cfg.get("sales_tax_pct", 8.25),
        "date": "05-26-26", "customer": "Renee Guerra", "customer_phone": "",
        "job_address": "10617 E Reny Ln, San Angelo, TX", "product": "3000 PSI",
        "product_name": "3000 PSI", "ordered_qty": "10 yd³", "delivered_qty": "10 yd³",
        "load": "1", "truck": "2", "driver": "Rony", "plant": "1", "slump": "5 in",
        "air": "", "cash_charge": "Charge", "water_reducer": "12 oz", "retarder": "",
        "inspector": "", "received_by": "",
        "times": {"left_plant": "7:39 AM", "a_train_pr": "", "left_job": "8:10 AM", "return_plant": "8:35 AM"},
        "mix_design": {"cement": {"design": "564", "target": "564", "actual": "561"}},
        "pricing": {"unit_price": "$158.50", "subtotal": "$2,054.13", "tax1": "$138.65",
                    "total": "$2,192.78", "job_running_total": "$2,192.78"},
    }
    out = os.path.join(HERE, "DEMO_delivery_layout.pdf")
    render_delivery_ticket(sample, out)
    print("wrote", out)
