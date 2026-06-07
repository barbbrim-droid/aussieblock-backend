"""
inject_protocol_example.py <photo>
Read a typed Total Batch Protocol photo, file it in the app as a completed order
(creating the customer if needed), save the fields to batch_data, and attach the
branded Total Batch Protocol PDF so it opens from the app.
"""
import os, sys, json
TICKET_TOOL = r"C:\Users\accou\Downloads\aussieblock-ticket-tool\ab_ticket_tool"
sys.path.insert(0, TICKET_TOOL)
import read_protocol, generator  # noqa: E402
from sqlmodel import Session, select  # noqa: E402
from app.db import init_db, engine  # noqa: E402
from app.models import Customer, Order  # noqa: E402
from app.main import _next_order_ref, _batch_ticket_dir  # noqa: E402

cfg = json.load(open(os.path.join(TICKET_TOOL, "config.json"), encoding="utf-8"))


def mix_design_from(materials):
    md = {k: {"design": "", "target": "", "actual": ""} for k in ["rock", "sand", "cement", "air", "water"]}
    cem_sv = cem_av = cem_rec = 0.0
    for name, unit, dens, rec, sv, av, lim, lab in materials:
        n = name.lower()
        cell = {"design": f"{rec:g}", "target": f"{sv:,.0f}", "actual": f"{av:,.0f}"}
        if "gravel" in n or "agg1" in n:
            md["rock"] = cell
        elif "sand" in n or "agg2" in n:
            md["sand"] = cell
        elif "cem" in n:
            cem_rec += rec; cem_sv += sv; cem_av += av
        elif "water" in n:
            md["water"] = cell
    if cem_sv:
        md["cement"] = {"design": f"{cem_rec:g}", "target": f"{cem_sv:,.0f}", "actual": f"{cem_av:,.0f}"}
    return md


def batch_data_from(data, photo):
    o = data["order"]; pr = data["process"]
    return {
        "date": data.get("report_date", ""), "cash_charge": "", "customer_phone": "",
        "product_name": o.get("recipe", ""), "plant": o.get("plant", ""), "air": "",
        "load": o.get("load_no", ""), "ordered": o.get("qty", ""), "delivered": o.get("qty", ""),
        "water_reducer": "", "retarder": "",
        "times": {"left_plant": "", "a_train_pr": "", "left_job": "", "return_plant": ""},
        "inspector": "", "received_by": "",
        "mix_design": mix_design_from(data["materials"]),
        "pricing": {"unit_price": "", "extended": "", "subtotal": "", "tax1": "", "tax2": "",
                    "total": "", "job_running_total": ""},
        "_source_photo": os.path.basename(photo), "_kind": "typed protocol",
    }


def main(photo):
    init_db()
    data = read_protocol.read_protocol(photo, cfg)
    o = data["order"]
    cust_name = (o.get("customer", "") or "").split("/")[0].strip() or "Unknown"
    with Session(engine) as s:
        cust = s.exec(select(Customer).where(Customer.name == cust_name)).first()
        if not cust:
            cust = Customer(name=cust_name, acct_no="", terms="Net 10")
            s.add(cust); s.commit(); s.refresh(cust)
            made = " (new customer created)"
        else:
            made = ""
        order = Order(
            ref=_next_order_ref(s), customer_id=cust.id,
            site=o.get("site_addr") or o.get("site_no") or "—",
            mix=o.get("recipe", "—"), qty=o.get("qty", "—"),
            scheduled_for=data.get("report_date", "") or "—", time="",
            status="complete", slump=(data["process"].get("Concrete slump") or None),
            batch_data=json.dumps(batch_data_from(data, photo)),
        )
        s.add(order); s.commit(); s.refresh(order)
        # render + attach the branded Total Batch Protocol PDF
        pdf_dir = _batch_ticket_dir(); os.makedirs(pdf_dir, exist_ok=True)
        fname = f"{order.ref}.pdf"
        generator.render_ticket(data, os.path.join(pdf_dir, fname))
        order.batch_ticket = fname
        s.add(order); s.commit()
        print(json.dumps({"ref": order.ref, "customer": cust.name + made,
                          "mix": order.mix, "qty": order.qty,
                          "pdf_attached": fname, "materials": len(data["materials"])},
                         indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1])
