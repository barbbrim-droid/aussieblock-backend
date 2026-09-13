"""
profit_report.py -- the one-page MARGIN REPORT: net profit on the yards poured
for a day or a date range, as a Letter PDF.

    render_profit_report(data, "out.pdf", company={...}, generated_at=datetime)

`data` is exactly what GET /profit returns (totals, days, orders, materials,
haulers, notes) so the PDF and the screen can never disagree. Everything is
budgeted to ONE page: the headline, the revenue-minus-costs ladder with
per-yard and %-of-revenue columns, materials, hauling by hauler, and a by-day
table (capped, with a "+N more days" line) — the order-level detail stays on
screen. Offline fpdf2; no API key needed.
"""
import os
import sys
from datetime import datetime
from fpdf import FPDF

INK = (31, 42, 55)
ORANGE = (231, 115, 42)
GREEN = (22, 140, 96)
RED = (193, 32, 32)
SHADE = (239, 237, 232)
GREY = (107, 114, 128)
LINE = (201, 205, 211)


def _res(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


LOGO = _res("ab_logo.png")


class _T(FPDF):
    def header(self): pass
    def footer(self): pass


def _money(v, dash_zero=False):
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        v = 0.0
    if dash_zero and abs(v) < 0.005:
        return "—"
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def _num(v, dp=1):
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        v = 0.0
    return f"{v:,.{dp}f}".rstrip("0").rstrip(".") if dp else f"{v:,.0f}"


def _pct(part, whole):
    try:
        return f"{float(part) / float(whole) * 100:.1f}%" if float(whole) else "—"
    except (TypeError, ValueError, ZeroDivisionError):
        return "—"


def _date_long(iso):
    try:
        d = datetime.strptime(iso, "%Y-%m-%d")
        return d.strftime("%A, %B %-d, %Y")
    except (TypeError, ValueError):
        return iso or ""


def _date_short(iso):
    try:
        d = datetime.strptime(iso, "%Y-%m-%d")
        return d.strftime("%a %-m/%-d")
    except (TypeError, ValueError):
        return iso or ""


def render_profit_report(data: dict, out_path: str, company: dict = None, generated_at=None) -> str:
    c = company or {}
    T = data.get("totals") or {}
    frm, to = data.get("from"), data.get("to")
    if not frm and not to:
        window = "All time"
    elif frm == to:
        window = _date_long(frm)
    else:
        window = f"{_date_long(frm) if frm else 'Start'}  –  {_date_long(to) if to else 'Today'}"

    pdf = _T(orientation="P", unit="mm", format="Letter")
    pdf.set_auto_page_break(False)
    pdf.set_margins(12, 10, 12)
    pdf.add_font("DejaVu", "", _res("DejaVuSans.ttf"))
    pdf.add_font("DejaVu", "B", _res("DejaVuSans-Bold.ttf"))
    pdf.set_title(f"Margin report — {window}")
    pdf.set_subject("Net profit on yards poured")
    pdf.add_page()
    W = pdf.w - 24
    L = 12

    # ---- header band ----
    top = pdf.get_y()
    try:
        pdf.image(LOGO, x=L, y=top, w=20)
    except Exception:   # noqa: BLE001
        pass
    pdf.set_xy(L + 24, top)
    pdf.set_text_color(*INK); pdf.set_font("DejaVu", "B", 12)
    pdf.cell(100, 5, c.get("name", "Aussieblock Ready Mix"))
    pdf.set_xy(L + 24, top + 5.5); pdf.set_font("DejaVu", "", 7); pdf.set_text_color(55, 65, 81)
    pdf.cell(120, 3.4, "  ".join(x for x in [c.get("addr", ""), c.get("city", ""), c.get("phone", "")] if x))
    pdf.set_xy(L + 24, top + 9); pdf.set_font("DejaVu", "B", 15); pdf.set_text_color(*INK)
    pdf.cell(120, 7, "Margin Report")
    # right: window + generated
    pdf.set_xy(L + W - 80, top); pdf.set_font("DejaVu", "", 6.5); pdf.set_text_color(*GREY)
    pdf.cell(80, 3.2, "PERIOD", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_xy(L + W - 80, top + 3.2); pdf.set_font("DejaVu", "B", 9.5); pdf.set_text_color(*INK)
    pdf.cell(80, 4.5, window, align="R")
    if generated_at:
        pdf.set_xy(L + W - 80, top + 8.2); pdf.set_font("DejaVu", "", 6.5); pdf.set_text_color(*GREY)
        pdf.cell(80, 3.2, f"Generated {generated_at.strftime('%-m/%-d/%Y %-I:%M %p')}", align="R")
    pdf.set_y(top + 18)
    pdf.set_draw_color(*ORANGE); pdf.set_line_width(0.6)
    y = pdf.get_y(); pdf.line(L, y, L + W, y); pdf.ln(3)

    # ---- headline tiles ----
    profit = float(T.get("profit") or 0)
    revenue = float(T.get("revenue") or 0)
    costs = float(T.get("costs") or 0)
    yards = float(T.get("yards") or 0)
    margin = T.get("margin_pct")
    per = T.get("per_yd") or {}
    tiles = [
        ("NET PROFIT", _money(profit), f"{margin:.1f}% margin" if margin is not None else "no revenue", GREEN if profit >= 0 else RED, True),
        ("REVENUE", _money(revenue), "billed, before sales tax", INK, False),
        ("TOTAL COSTS", _money(costs), _pct(costs, revenue) + " of revenue" if revenue else "", RED, False),
        ("YARDS POURED", _num(yards), f"{int(T.get('orders') or 0)} completed order{'s' if T.get('orders') != 1 else ''}", INK, False),
        ("PROFIT / CY", _money(per.get("profit")) if per.get("profit") is not None else "—",
         f"rev {_money(per.get('revenue'))} · cost {_money(per.get('costs'))}" if per.get("revenue") is not None else "", GREEN if (per.get("profit") or 0) >= 0 else RED, False),
    ]
    gap = 3
    tw = (W - gap * (len(tiles) - 1)) / len(tiles)
    ty = pdf.get_y()
    th = 19
    for i, (lbl, val, sub, col, big) in enumerate(tiles):
        x = L + i * (tw + gap)
        pdf.set_fill_color(*SHADE); pdf.set_draw_color(*(col if big else LINE)); pdf.set_line_width(0.5 if big else 0.2)
        pdf.rect(x, ty, tw, th, style="DF")
        pdf.set_xy(x + 2, ty + 1.8); pdf.set_font("DejaVu", "", 6); pdf.set_text_color(*GREY)
        pdf.cell(tw - 4, 3, lbl)
        pdf.set_xy(x + 2, ty + 5.5); pdf.set_font("DejaVu", "B", 15 if big else 13); pdf.set_text_color(*col)
        pdf.cell(tw - 4, 7.5, val)
        pdf.set_xy(x + 2, ty + 13.5); pdf.set_font("DejaVu", "", 6); pdf.set_text_color(*GREY)
        pdf.cell(tw - 4, 3, sub[:48])
    pdf.set_y(ty + th + 4)

    # ---- section helper ----
    def section(title, y=None):
        if y is not None:
            pdf.set_y(y)
        pdf.set_x(L); pdf.set_font("DejaVu", "B", 9); pdf.set_text_color(*INK)
        pdf.cell(W, 5, title, new_x="LMARGIN", new_y="NEXT")
        pdf.set_draw_color(*LINE); pdf.set_line_width(0.2)
        yy = pdf.get_y(); pdf.line(L, yy, L + W, yy); pdf.ln(0.8)

    def row(cells, widths, aligns, bold=False, color=INK, fill=False, h=5.0, size=7.6):
        pdf.set_x(L)
        pdf.set_font("DejaVu", "B" if bold else "", size); pdf.set_text_color(*color)
        if fill:
            pdf.set_fill_color(*SHADE)
        for i, (txt, w, a) in enumerate(zip(cells, widths, aligns)):
            last = i == len(cells) - 1
            pdf.cell(w, h, str(txt), align=a, fill=fill, new_x="LMARGIN" if last else "RIGHT", new_y="NEXT" if last else "TOP")

    # ---- revenue − costs = net (the true-margin ladder) ----
    section("Revenue − costs = net")
    cw = [W * 0.46, W * 0.16, W * 0.14, W * 0.12, W * 0.12]
    ca = ["L", "R", "R", "R", "R"]
    row(["", "Amount", "Per CY", "% of rev", ""], cw, ca, bold=True, color=GREY, size=6.5, h=4)
    per_cy = (lambda v: _money(float(v or 0) / yards) if yards else "—")
    row(["Revenue — yards poured × price (pre-tax)", _money(revenue), per_cy(revenue), "100%" if revenue else "—", ""], cw, ca, bold=True, color=GREEN)
    lines = [
        ("Materials batched (ticket actuals × $/unit)", T.get("materials")),
        ("Hauling paid out (delivery, short-load, back-haul)", T.get("hauling")),
        (f"Fuel ({_num(T.get('fuel_gallons'))} gal × $/gal)", T.get("fuel")),
        (f"Aggregate haul-in ({_num(T.get('aggregate_tons'))} t on weight tickets)", T.get("aggregate_haul")),
    ]
    for lbl, v in lines:
        row([f"  − {lbl}", _money(v), per_cy(v), _pct(v, revenue), ""], cw, ca)
    row(["Total costs", _money(costs), per_cy(costs), _pct(costs, revenue), ""], cw, ca, bold=True, color=RED, fill=True)
    row(["NET PROFIT", _money(profit), per_cy(profit), (f"{margin:.1f}%" if margin is not None else "—"), ""],
        cw, ca, bold=True, color=GREEN if profit >= 0 else RED, h=6, size=9)
    pdf.set_x(L); pdf.set_font("DejaVu", "", 6); pdf.set_text_color(*GREY)
    pdf.cell(W, 3.4, f"Sales tax collected {_money(T.get('tax_collected'))} is passed through and not counted. "
                     f"Aggregate purchase $ ({_money(T.get('aggregate_material'))}) is not subtracted again — gravel/sand are already costed in Materials as they batch.",
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2.5)

    # ---- two columns: materials | hauling by hauler ----
    col_y = pdf.get_y()
    half = (W - 5) / 2
    # materials (left)
    pdf.set_x(L); pdf.set_font("DejaVu", "B", 8.5); pdf.set_text_color(*INK); pdf.cell(half, 5, "Materials batched", new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(*LINE); yy = pdf.get_y(); pdf.line(L, yy, L + half, yy); pdf.ln(0.6)
    mats = data.get("materials") or []
    mw = [half * 0.42, half * 0.2, half * 0.18, half * 0.2]
    ma = ["L", "R", "R", "R"]
    if not mats:
        pdf.set_x(L); pdf.set_font("DejaVu", "", 7); pdf.set_text_color(*GREY); pdf.cell(half, 4.5, "No batch tickets with weights in this window.", new_x="LMARGIN", new_y="NEXT")
    else:
        row(["Material", "Used", "$/unit", "Cost"], mw, ma, bold=True, color=GREY, size=6.3, h=3.8)
        for m in mats[:8]:
            row([m.get("name", "") + (" *" if m.get("estimated") else ""), f"{_num(m.get('used'), 2)} {m.get('unit', '')}",
                 _money(m.get("cost_rate"), dash_zero=True), _money(m.get("cost"))], mw, ma, h=4.4, size=7)
        row(["Total", "", "", _money(T.get("materials"))], mw, ma, bold=True, h=4.6, size=7.2)
        if any(m.get("estimated") for m in mats):
            pdf.set_x(L); pdf.set_font("DejaVu", "", 5.8); pdf.set_text_color(*GREY); pdf.cell(half, 3, "* part estimated from the mix design (no ticket weights)", new_x="LMARGIN", new_y="NEXT")
    left_end = pdf.get_y()
    # hauling (right)
    rx = L + half + 5
    pdf.set_xy(rx, col_y); pdf.set_font("DejaVu", "B", 8.5); pdf.set_text_color(*INK); pdf.cell(half, 5, "Hauling paid out", new_x="LMARGIN", new_y="NEXT")
    yy = pdf.get_y(); pdf.line(rx, yy, rx + half, yy); pdf.ln(0.6)
    hs = data.get("haulers") or []
    hw = [half * 0.42, half * 0.18, half * 0.18, half * 0.22]
    ha = ["L", "L", "R", "R"]

    def rrow(cells, widths, aligns, bold=False, color=INK, h=4.4, size=7):
        pdf.set_x(rx); pdf.set_font("DejaVu", "B" if bold else "", size); pdf.set_text_color(*color)
        for i, (txt, w, a) in enumerate(zip(cells, widths, aligns)):
            last = i == len(cells) - 1
            pdf.cell(w, h, str(txt), align=a, new_x="LMARGIN" if last else "RIGHT", new_y="NEXT" if last else "TOP")

    if not hs:
        pdf.set_x(rx); pdf.set_font("DejaVu", "", 7); pdf.set_text_color(*GREY); pdf.cell(half, 4.5, "No third-party hauling in this window.", new_x="LMARGIN", new_y="NEXT")
    else:
        rrow(["Hauler", "Orders", "CY", "Paid"], hw, ha, bold=True, color=GREY, size=6.3, h=3.8)
        for h in hs[:8]:
            rrow([h.get("hauler", ""), str(h.get("orders", 0)), _num(h.get("yards")), _money(h.get("total"))], hw, ha)
        rrow(["Total", "", _num(sum(float(h.get("yards") or 0) for h in hs)), _money(T.get("hauling"))], hw, ha, bold=True, h=4.6, size=7.2)
    # fuel + aggregate notes under hauling
    pdf.set_x(rx); pdf.set_font("DejaVu", "", 6.5); pdf.set_text_color(*GREY)
    pdf.cell(half, 3.6, f"Fuel: {_num(T.get('fuel_gallons'))} gal, {_money(T.get('fuel'))}   ·   Aggregate haul-in: {_num(T.get('aggregate_tons'))} t, {_money(T.get('aggregate_haul'))}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_y(max(left_end, pdf.get_y()) + 3)

    # ---- by day (fits what's left of the page) ----
    days = data.get("days") or []
    footer_h = 12
    avail = (pdf.h - 10 - footer_h) - pdf.get_y()
    if days and avail > 22:
        section("By day")
        dw = [W * 0.15, W * 0.08, W * 0.08, W * 0.13, W * 0.12, W * 0.11, W * 0.09, W * 0.09, W * 0.15]
        da = ["L", "R", "R", "R", "R", "R", "R", "R", "R"]
        row(["Day", "Orders", "CY", "Revenue", "Materials", "Hauling", "Fuel", "Agg haul", "Net (margin)"], dw, da, bold=True, color=GREY, size=6.3, h=3.8)
        rh = 4.3
        max_rows = max(1, int((pdf.h - 10 - footer_h - pdf.get_y() - 5) / rh))
        shown = days[:max_rows] if len(days) > max_rows else days
        for d in shown:
            dp = float(d.get("profit") or 0); dr = float(d.get("revenue") or 0)
            row([_date_short(d.get("date")), str(d.get("orders", 0)), _num(d.get("yards")), _money(dr), _money(d.get("materials")),
                 _money(d.get("hauling")), _money(d.get("fuel")), _money(d.get("aggregate_haul")),
                 f"{_money(dp)} ({_pct(dp, dr)})"], dw, da, h=rh, size=6.8, color=INK)
        if len(days) > len(shown):
            pdf.set_x(L); pdf.set_font("DejaVu", "", 6.3); pdf.set_text_color(*GREY)
            pdf.cell(W, 3.6, f"+ {len(days) - len(shown)} more day{'s' if len(days) - len(shown) > 1 else ''} in this period — see the Profit screen for the full by-day and per-order detail.", new_x="LMARGIN", new_y="NEXT")

    # ---- footer: caveats + method ----
    notes = data.get("notes") or {}
    caveats = []
    if notes.get("missing_mileage"):
        n = notes["missing_mileage"]
        caveats.append(f"{n} order{'s' if n > 1 else ''} without road mileage — their haul is missing from revenue and hauling (resolve under Costs).")
    if notes.get("unmapped_mixes"):
        um = notes["unmapped_mixes"]
        caveats.append("Material cost missing for: " + ", ".join(f"{u.get('mix')} ({_num(u.get('yards'))} CY)" for u in um[:4]) + (" …" if len(um) > 4 else "") + " (add a mix design under Materials).")
    fy = pdf.h - 10 - footer_h
    pdf.set_y(fy)
    pdf.set_draw_color(*LINE); pdf.line(L, fy, L + W, fy)
    pdf.set_xy(L, fy + 1.2); pdf.set_font("DejaVu", "", 6); pdf.set_text_color(*GREY)
    pdf.multi_cell(W, 3.1, ("How this is built: revenue is what completed orders bill for the yards actually poured (pre-tax), placed on their pour date; "
                            "materials and fuel are costed on the day they were batched or filled; hauling is what's paid to third-party haulers; "
                            "aggregate haul-in comes from the drivers' weight tickets. " + (" ".join(caveats) if caveats else "")).strip())
    pdf.set_xy(L, pdf.h - 8); pdf.set_font("DejaVu", "", 6); pdf.set_text_color(*GREY)
    pdf.cell(W, 3, "Aussieblock Ready Mix · dispatch app margin report · internal", align="C")

    tmp = out_path + ".tmp"
    pdf.output(tmp)
    os.replace(tmp, out_path)
    return out_path


if __name__ == "__main__":
    import json
    render_profit_report(json.load(open(sys.argv[1])), sys.argv[2])
    print("wrote", sys.argv[2])
