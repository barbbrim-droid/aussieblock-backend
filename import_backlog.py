"""
import_backlog.py -- read every batch-ticket photo and file each as a completed
delivery in the app, under the matched customer, with full batch_data.

- Skips duplicates (same customer + date + load + mix + qty).
- Confidently-matched customers only; everything else goes to a review list
  (import_review.csv) so nothing is filed under the wrong account.

    venv\\Scripts\\python import_backlog.py
"""
import os, sys, csv, json, glob, traceback

TICKET_TOOL = r"C:\Users\accou\Downloads\aussieblock-ticket-tool\ab_ticket_tool"
sys.path.insert(0, TICKET_TOOL)
import read_ticket  # noqa: E402
from scan_to_app import to_batch_data  # noqa: E402

from sqlmodel import Session, select  # noqa: E402
from app.db import init_db, engine  # noqa: E402
from app.models import Customer, Order  # noqa: E402
from app.main import _next_order_ref  # noqa: E402

cfg = json.load(open(os.path.join(TICKET_TOOL, "config.json"), encoding="utf-8"))
photos = sorted(glob.glob(os.path.join(cfg["photo_watch_dir"], "*.jpg")) +
                glob.glob(os.path.join(cfg["photo_watch_dir"], "*.jpeg")) +
                glob.glob(os.path.join(cfg["photo_watch_dir"], "*.png")))

init_db()
review_path = os.path.join(os.path.dirname(__file__), "import_review.csv")
created = dup = unmatched = errors = 0

with Session(engine) as s, open(review_path, "w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh)
    w.writerow(["photo", "result", "customer/read_as", "score", "mix", "qty", "date", "load", "ref/notes"])

    # existing dedupe set: (customer_id, date, load, mix, qty)
    seen = set()
    for o in s.exec(select(Order)).all():
        bd = json.loads(o.batch_data) if o.batch_data else {}
        seen.add((o.customer_id, bd.get("date", ""), bd.get("load", ""), o.mix, o.qty))

    for i, p in enumerate(photos, 1):
        name = os.path.basename(p)
        try:
            d = read_ticket.read_ticket(p, cfg)
            d["_photo"] = p
            matched = d.get("customer_match_score", 0) >= 0.78
            cust = s.exec(select(Customer).where(Customer.name == d["customer"])).first() if matched else None
            if not cust:
                unmatched += 1
                w.writerow([name, "UNMATCHED", d.get("customer_read", ""), d.get("customer_match_score", ""),
                            d.get("product", ""), d.get("ordered_qty", ""), d.get("date", ""), d.get("load", ""), "needs a customer"])
                print(f"[{i}/{len(photos)}] {name} UNMATCHED ({d.get('customer_read','?')})", flush=True)
                fh.flush(); continue

            qty = d.get("delivered_qty") or d.get("ordered_qty") or "—"
            key = (cust.id, d.get("date", ""), d.get("load", ""), d.get("product", "") or "—", qty)
            if key in seen:
                dup += 1
                w.writerow([name, "DUPLICATE", cust.name, d.get("customer_match_score", ""),
                            d.get("product", ""), qty, d.get("date", ""), d.get("load", ""), "skipped (already imported)"])
                print(f"[{i}/{len(photos)}] {name} dup -> {cust.name}", flush=True)
                fh.flush(); continue

            o = Order(ref=_next_order_ref(s), customer_id=cust.id,
                      site=d.get("job_address", "") or "—", mix=d.get("product", "") or "—",
                      qty=qty, scheduled_for=d.get("date", "") or "—", time="",
                      status="complete", driver=d.get("driver") or None,
                      slump=d.get("slump") or None, batch_data=json.dumps(to_batch_data(d)))
            s.add(o); s.commit(); s.refresh(o)
            seen.add(key); created += 1
            w.writerow([name, "IMPORTED", cust.name, d.get("customer_match_score", ""),
                        o.mix, o.qty, d.get("date", ""), d.get("load", ""), o.ref])
            print(f"[{i}/{len(photos)}] {name} -> {o.ref} {cust.name} | {o.mix} {o.qty}", flush=True)
            fh.flush()
        except Exception as e:
            errors += 1
            w.writerow([name, "ERROR", str(e)[:80], "", "", "", "", "", ""])
            print(f"[{i}/{len(photos)}] {name} ERROR: {e}", flush=True)
            fh.flush()

print(f"\nDONE. imported={created} duplicates={dup} unmatched={unmatched} errors={errors}")
print("review sheet ->", review_path)
