"""Render a TxDOT Class A sample batch ticket (Mac Matrix Fiber + liquid
admixtures in fl oz + Approved Materials MPL block) through the real generator.
Standalone — no Anthropic call, no protocol scan needed.

Numbers are Aussie Block's ACTUAL Class A mix design (hccmxdes24, SSD design
weights per CY). Change the ORDER block to spin up another sample; set/actual
values scale from the per-CY recipe automatically."""
import os, sys, json

HERE = os.path.dirname(os.path.abspath(__file__))
TG = os.path.join(HERE, "app", "ticketgen")
sys.path.insert(0, TG)
import generator  # self-contained (fpdf only)

cfg = json.load(open(os.path.join(TG, "ticket_config.json"), encoding="utf-8"))

# ── ORDER (change these for another sample) ─────────────────────────────────
CUSTOMER  = "Jordan Foster Construction (TxDOT)"
SITE      = "Loop 306 @ Sherwood Way, San Angelo, TX"
QTY       = 8                       # cubic yards (load size)
LOAD_NO   = "1"
DATE      = "06/11/2026"
VRN       = "TX-3061"
DRIVER    = "Armando"
BATCH     = [("1", "07:46:20", "1.6 yd³"), ("2", "07:52:55", "1.6 yd³"),
             ("3", "07:59:10", "1.6 yd³"), ("4", "08:05:33", "1.6 yd³"),
             ("5", "08:11:48", "1.6 yd³")]

# ── Class A mix design, per CY (SSD design weights) ─────────────────────────
# (name, unit, density lb/ft3, recipe/CY, tolerance %, ASTM label, actual var)
# actual var = a small, fixed in-tolerance offset so AV differs from SV like a
# real batch; fiber is hand-added to spec so 0.
RECIPE = [
    ("Cement Type I/II",          "lb",    200.9,   235,     1, "+/-1%", +0.0024),
    ("Slag Cement (50%)",         "lb",    177.2,   235,     1, "+/-1%", -0.0021),
    ("Water",                     "lb",     62.4,   248,     1, "+/-1%", -0.0018),
    ("Coarse Aggregate (Gr. 4)",  "lb",    165.4,  1957.88,  2, "+/-2%", +0.0046),
    ("Fine Aggregate (Sand)",     "lb",    164.1,  1421.28,  2, "+/-2%", -0.0040),
    ("Water Reducer (Type A)",    "fl oz",  71.7,  23.5,     3, "+/-3%", -0.0064),
    ("Set Retarder (Type D)",     "fl oz",  67.1,  2.35,     3, "+/-3%", +0.0165),
    ("Mac Matrix Fiber",          "lb",     56.8,  3,        3, "+/-3%",  0.0),
]

materials = []
for name, unit, dens, rec, tol, lab, var in RECIPE:
    sv = rec * QTY
    av = round(sv * (1 + var), 2)
    materials.append((name, unit, dens, rec, round(sv, 2), av, tol, lab))

# Totals (weights only; liquid admixtures carry negligible weight)
water_yd, binder_yd = 248.0, 470.0
agg_yd = 1957.88 + 1421.28
total_yd = water_yd + binder_yd + agg_yd + 3.0   # + fiber
TICKET = {
    "report_date": DATE,
    "sales_tax_pct": cfg.get("sales_tax_pct", 8.25),
    "company": cfg["company"],
    "weather": {"time": "09:25", "cond": "Sunny", "temp": "81°F",
                "humidity": "52%", "wind": "10 mph", "pressure": "30.0 inHg", "vis": "10 mi"},
    "order": {
        "plant": "2105",
        "recipe": "Class A — 3000 psi (TxDOT Item 421)",
        "customer": CUSTOMER, "cust_phone": "-",
        "site_no": "-", "site_addr": SITE,
        "mileage": "7 miles", "drive_time": "14 min",
        "vrn": VRN, "driver": DRIVER,
        "qty": f"{QTY} CY", "ordered": f"{QTY} CY",
        "load_no": LOAD_NO, "user": "Dispatch", "sales_rep": "Roy Acosta",
    },
    "batches": BATCH,
    "materials": materials,
    "mpl": cfg.get("material_mpl", []),
    "totals": {
        "Water": (water_yd * QTY, water_yd),
        "Binder": (binder_yd * QTY, binder_yd),
        "Aggregate (Dry weight)": (round(agg_yd * QTY, 2), round(agg_yd, 2)),
        "Total weight": (round(total_yd * QTY, 2), round(total_yd, 2)),
    },
    "process": {
        "Total mixing time": "1 min 30 s", "Ø Mixing time": "1 min 30 s",
        "Ø Mixer load": f"{QTY:.1f} yd³",
        "Concrete slump": "4 in (+/- 1.5 in)",
        "Water correction": "0 lb (0.0 gal)",
        "w/c": "0.53", "w/c eq.": "0.53", "max w/c": "0.60",
    },
    "binder_lb": binder_yd * QTY,
    "wc_eq": 0.53, "max_wc": 0.60, "yards": QTY, "req_slump": "4 in",
}

safe = CUSTOMER.split()[0]
out = os.path.join(os.path.expanduser("~"), "Downloads",
                   f"Aussieblock_TxDOT_ClassA_Sample_{safe}_{QTY}CY.pdf")
generator.render_ticket(TICKET, out)
print("wrote", out)
