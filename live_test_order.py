"""
live_test_order.py <photo>
TEST: read one old ticket photo and file it as a completed order in the LIVE app
(via the production API). Matched customer only -- never auto-creates.

    venv\\Scripts\\python live_test_order.py "G:\\My Drive\\Batch Tickets\\<photo>.jpg"
"""
import os, sys, io, json, tempfile
import requests

TICKET_TOOL = r"C:\Users\accou\Downloads\aussieblock-ticket-tool\ab_ticket_tool"
sys.path.insert(0, TICKET_TOOL)
import read_ticket, delivery_ticket  # noqa: E402

API = "https://aussieblock-api.onrender.com"
LOGIN = {"username": "ops@aussieblock.com", "password": "Aussie1234"}
cfg = json.load(open(os.path.join(TICKET_TOOL, "config.json"), encoding="utf-8"))


def main(photo):
    s = requests.Session()
    r = s.post(f"{API}/auth/login", data=LOGIN, timeout=30); r.raise_for_status()
    tok = r.json()["access_token"]
    H = {"Authorization": f"Bearer {tok}"}

    # 1) read + match
    d = read_ticket.read_ticket(photo, cfg)
    matched = d.get("customer_match_score", 0) >= 0.78
    if not matched:
        print(f"NOT CONFIDENTLY MATCHED -> read '{d.get('customer_read')}' "
              f"(score {d.get('customer_match_score')}). Refusing to file (would not auto-create).")
        return
    # find the live customer id by exact name
    custs = s.get(f"{API}/customers", headers=H, timeout=30).json()
    cust = next((c for c in custs if c["name"] == d["customer"]), None)
    if not cust:
        print(f"'{d['customer']}' not in LIVE customer list -> flag, do not create.")
        return
    print(f"matched LIVE customer: {cust['name']} (id {cust['id']})")

    # 2) create the order
    body = {
        "customer_id": cust["id"],
        "site": d.get("job_address") or "—",
        "mix": d.get("product") or "—",
        "qty": d.get("delivered_qty") or d.get("ordered_qty") or "—",
        "scheduled_for": d.get("date") or "—",
        "time": "",
        "driver": d.get("driver") or "",
        "slump": d.get("slump") or "",
    }
    r = s.post(f"{API}/orders", headers=H, json=body, timeout=30); r.raise_for_status()
    ref = r.json()["ref"]
    print(f"created order {ref}")

    # 3) mark complete (so it lands in Past orders)
    r = s.post(f"{API}/orders/{ref}/status", headers=H, params={"status": "complete"}, timeout=30)
    r.raise_for_status()
    print("status -> complete")

    # 4) render the branded ticket PDF and attach it
    pdf_path = os.path.join(tempfile.gettempdir(), f"{ref}.pdf")
    data = dict(d); data["company"] = cfg["company"]; data["sales_tax_pct"] = cfg.get("sales_tax_pct", 8.25)
    delivery_ticket.render_delivery_ticket(data, pdf_path)
    with open(pdf_path, "rb") as fh:
        r = s.post(f"{API}/orders/{ref}/batch-ticket", headers=H,
                   files={"file": (f"{ref}.pdf", fh, "application/pdf")}, timeout=60)
    print("attach ticket PDF:", "OK" if r.ok else f"FAILED {r.status_code} {r.text[:120]}")

    # 5) batch_data (only if the new backend deploy is live)
    try:
        from scan_to_app import to_batch_data
        r = s.put(f"{API}/orders/{ref}/batch-data", headers=H, json={"data": to_batch_data(d)}, timeout=30)
        print("batch_data fields:", "OK (deploy live)" if r.ok else f"skipped ({r.status_code} - deploy not live yet)")
    except Exception as e:
        print("batch_data: skipped -", e)

    print(f"\nDONE. Open the LIVE app -> Past orders -> {cust['name']} -> {ref}")


if __name__ == "__main__":
    main(sys.argv[1])
