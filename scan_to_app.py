"""
scan_to_app.py -- read ONE batch-ticket photo and drop it into the dispatch app,
under the matched customer, with every field saved on the order's batch_data.

    venv\\Scripts\\python scan_to_app.py "G:\\My Drive\\Batch Tickets\\photo.jpg"

It writes straight to the same database the app reads (aussieblock.db), so the
new delivery shows up under that customer the next time the app loads.
"""
import os, sys, json

# let us import the photo reader that lives with the ticket tool
TICKET_TOOL = r"C:\Users\accou\Downloads\aussieblock-ticket-tool\ab_ticket_tool"
sys.path.insert(0, TICKET_TOOL)
import read_ticket  # noqa: E402

from sqlmodel import Session, select  # noqa: E402
from app.db import init_db, engine  # noqa: E402
from app.models import Customer, Order  # noqa: E402
from app.main import _next_order_ref, _order_json  # noqa: E402


def _cfg():
    return json.load(open(os.path.join(TICKET_TOOL, "config.json"), encoding="utf-8"))


def to_batch_data(d):
    """Map the photo read into the app's batch_data shape (full paper ticket)."""
    return {
        "date": d.get("date", ""), "cash_charge": d.get("cash_charge", ""),
        "customer_phone": d.get("customer_phone", ""), "product_name": d.get("product", ""),
        "plant": d.get("plant", ""), "air": d.get("air", ""), "load": d.get("load", ""),
        "ordered": d.get("ordered_qty", ""), "delivered": d.get("delivered_qty", ""),
        "water_reducer": d.get("water_reducer", ""), "retarder": d.get("retarder", ""),
        "times": {"left_plant": "", "a_train_pr": "", "left_job": "", "return_plant": ""},
        "inspector": "", "received_by": "",
        "mix_design": {k: {"design": "", "target": "", "actual": ""}
                       for k in ["rock", "sand", "cement", "air", "water"]},
        "pricing": {"unit_price": d.get("unit_price", ""), "extended": "",
                    "subtotal": d.get("subtotal", ""), "tax1": d.get("tax", ""),
                    "tax2": "", "total": d.get("total", ""), "job_running_total": ""},
        # provenance, so the office can see what the pen actually said
        "_source_photo": os.path.basename(d.get("_photo", "")),
        "_read_as": d.get("customer_read", ""),
        "_match_score": d.get("customer_match_score", ""),
    }


def scan_to_app(path):
    cfg = _cfg()
    init_db()
    d = read_ticket.read_ticket(path, cfg)
    d["_photo"] = path
    matched = d.get("customer_match_score", 0) >= 0.78

    with Session(engine) as s:
        cust = None
        if matched:
            cust = s.exec(select(Customer).where(Customer.name == d["customer"])).first()
        if not cust:
            return {"ok": False, "reason": "no confident customer match",
                    "read_as": d.get("customer_read"), "best": d.get("customer"),
                    "score": d.get("customer_match_score")}

        o = Order(
            ref=_next_order_ref(s),
            customer_id=cust.id,
            site=d.get("job_address", "") or "—",
            mix=d.get("product", "") or "—",
            qty=d.get("delivered_qty") or d.get("ordered_qty") or "—",
            scheduled_for=d.get("date", "") or "—",
            time="",
            status="complete",
            driver=d.get("driver", "") or None,
            slump=d.get("slump", "") or None,
            batch_data=json.dumps(to_batch_data(d)),
        )
        s.add(o); s.commit(); s.refresh(o)
        return {"ok": True, "ref": o.ref, "customer": cust.name,
                "mix": o.mix, "qty": o.qty, "date": o.scheduled_for,
                "low_confidence": d.get("low_confidence", [])}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python scan_to_app.py <photo>"); raise SystemExit(1)
    res = scan_to_app(sys.argv[1])
    print(json.dumps(res, indent=2, ensure_ascii=False))
