"""QuickBooks Online billing integration.

Two modes (mirrors the One Step GPS integration):
  • MOCK  (no QBO_* creds) — billing is served from the local Invoice table
                             (seeded demo data), so the whole billing screen
                             works before QuickBooks is connected.
  • LIVE  (all creds set)  — the backend holds an always-on OAuth 2.0 connection
                             to QuickBooks Online and periodically syncs each
                             customer's invoices / A-R into that same Invoice
                             table. The rest of the app keeps reading from one
                             place (get_billing_for_customer), unchanged.

One-time setup to go live:
  1. Create an app at https://developer.intuit.com → Client ID + Secret.
  2. Run the OAuth consent flow once for your company → you receive a refresh
     token + realm (company) ID. Put all four in .env (QBO_CLIENT_ID,
     QBO_CLIENT_SECRET, QBO_REFRESH_TOKEN, QBO_REALM_ID) and set
     QBO_ENVIRONMENT=production.
  3. Restart. The background loop (app/main.py) re-pulls A/R every
     QBO_SYNC_SECONDS; staff can also hit POST /billing/sync to run it on demand.

Customer mapping: a QuickBooks invoice carries a CustomerRef with the customer's
QuickBooks Id (value) and name. We match on the **Id** — stable across renames and
punctuation — by storing it as Customer.qbo_id. Run import_customers_from_quickbooks
once (POST /import/customers) to pull the roster and populate those ids; the sync
then matches invoices to local customers by id (falling back to name if a local
customer has no qbo_id yet).
"""
import asyncio
import json
import time
from datetime import datetime, date

import httpx
from sqlmodel import Session, select

from ..db import engine
from ..models import Customer, Invoice, InvoicePaidOverride
from .. import config


# ── Local read path (unchanged — the app always reads from here) ─────────────
def get_billing_for_customer(customer_id: int) -> dict | None:
    with Session(engine) as s:
        customer = s.get(Customer, customer_id)
        if not customer:
            return None
        invoices = s.exec(select(Invoice).where(Invoice.customer_id == customer_id)).all()
        # Staff "mark paid" overrides (kept out of the synced Invoice table so the
        # mirror-sync can't wipe them). A matching number counts as paid.
        overrides = {o.number for o in s.exec(select(InvoicePaidOverride)).all()}

        def _paid(i):
            return i.status == "paid" or i.number in overrides

        outstanding = sum(i.amount for i in invoices if not _paid(i))
        past_due = sum(i.amount for i in invoices if i.status == "overdue" and not _paid(i))
        available = customer.credit_limit - outstanding

        return {
            "company": customer.name,
            "acctNo": customer.acct_no,
            # COD overrides net terms — a COD customer pays before delivery, so
            # show "COD" rather than their stored Net term (kept for if COD is off).
            "terms": "COD" if customer.cod else customer.terms,
            "contact": customer.contact,
            "creditLimit": customer.credit_limit,
            "balance": round(outstanding, 2),
            "pastDue": round(past_due, 2),
            "available": round(available, 2),
            "invoices": [
                {"id": i.number, "date": i.date, "amount": i.amount,
                 # effective status reflects the override; manually_paid lets the UI
                 # show "Paid (manual)" and offer an Undo.
                 "status": "paid" if _paid(i) else i.status,
                 "manually_paid": (i.number in overrides and i.status != "paid"),
                 "order": i.order_ref}
                for i in invoices
            ],
        }


# ── Customer-payable invoice link ("Make a payment") ─────────────────────────
def get_invoice_pay_link(customer_id: int, invoice_number: str) -> dict:
    """Return a fresh, customer-facing QuickBooks payment link for one invoice.

    QuickBooks hosts the pay page (connect.intuit.com/...), so no card/bank data
    ever touches this server, and a payment posts straight into QuickBooks — the
    next A/R sync then flips the invoice to "paid" here. The link is fetched live
    (Accounting API, `include=invoiceLink`) because it carries a short-lived token.

    Returns one of:
      {"ok": True,  "link": "https://...", "number": ..., "amount": ..., "status": ...}
      {"ok": False, "reason": "..."}   (mock mode, not found, or no link available)
    """
    with Session(engine) as s:
        inv = s.exec(
            select(Invoice)
            .where(Invoice.customer_id == customer_id)
            .where(Invoice.number == invoice_number)
        ).first()
        if not inv:
            return {"ok": False, "reason": "Invoice not found."}
        if inv.status == "paid":
            return {"ok": False, "reason": "This invoice is already paid."}
        qbo_id = inv.qbo_invoice_id
        amount, number, inv_status = inv.amount, inv.number, inv.status

    if config.USE_MOCK_QBO:
        return {"ok": False,
                "reason": "Online payment isn't available in demo mode "
                          "(QuickBooks not connected)."}
    if not qbo_id:
        return {"ok": False,
                "reason": "No QuickBooks link for this invoice yet — run a billing "
                          "sync, then try again."}

    with httpx.Client(timeout=30) as client:
        token = _access_token(client)
        url = f"{config.QBO_API_BASE}/v3/company/{config.QBO_REALM_ID}/invoice/{qbo_id}"
        resp = client.get(
            url,
            params={"include": "invoiceLink", "minorversion": config.QBO_MINOR_VERSION},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        resp.raise_for_status()
        full = resp.json().get("Invoice", {})

    link = full.get("InvoiceLink")
    if not link:
        # Online payment must be enabled on the invoice in QuickBooks for a link
        # to exist (Allow online ACH / credit card).
        return {"ok": False,
                "reason": "QuickBooks hasn't enabled online payment on this invoice. "
                          "Turn on online payments for it in QuickBooks, then retry."}
    return {"ok": True, "link": link, "number": number,
            "amount": amount, "status": inv_status}


def cod_link_from_existing(customer_id: int, amount: float | None = None) -> dict:
    """COD payment WITHOUT creating an invoice. The office makes the invoice in
    QuickBooks; this finds that customer's open invoice (closest amount if one is
    given, else most recent) and returns its hosted pay link. Never creates an
    invoice, so it can't duplicate."""
    with Session(engine) as s:
        opens = s.exec(
            select(Invoice)
            .where(Invoice.customer_id == customer_id)
            .where(Invoice.status != "paid")
        ).all()
        if not opens:
            return {"ok": False, "reason": "No open QuickBooks invoice for this "
                    "customer. Create the invoice in QuickBooks, run a billing sync, "
                    "then take the payment."}
        if amount:
            inv = min(opens, key=lambda i: abs((i.amount or 0) - amount))
        else:
            inv = sorted(opens, key=lambda i: str(i.date), reverse=True)[0]
        number, qbo_id, amt = inv.number, inv.qbo_invoice_id, inv.amount
    res = get_invoice_pay_link(customer_id, number)
    if not res.get("ok"):
        return res
    return {"ok": True, "invoice_id": qbo_id, "link": res["link"],
            "amount": amt, "doc_number": number}


# ── COD / prepay: create an invoice + read its pay link & balance ────────────
_COD_ITEM_NAME = "Ready Mix"   # QuickBooks item COD loads are billed under


def _find_item_id(client: httpx.Client, token: str, name: str) -> str | None:
    safe = name.replace("'", "''")
    rows = _query(client, token, f"select Id from Item where Name = '{safe}'", "Item")
    return str(rows[0]["Id"]) if rows else None


def _get_invoice(client: httpx.Client, token: str, qbo_invoice_id: str, with_link: bool = False) -> dict:
    url = f"{config.QBO_API_BASE}/v3/company/{config.QBO_REALM_ID}/invoice/{qbo_invoice_id}"
    params = {"minorversion": config.QBO_MINOR_VERSION}
    if with_link:
        params["include"] = "invoiceLink"
    resp = client.get(url, params=params,
                      headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    resp.raise_for_status()
    return resp.json().get("Invoice", {})


def create_cod_invoice(customer_id: int, amount: float, order_ref: str) -> dict:
    """Create a QuickBooks invoice for a COD load and return its hosted pay link.

    Returns {ok, invoice_id, doc_number, link} or {ok: False, reason}.
    """
    if config.USE_MOCK_QBO:
        return {"ok": False, "reason": "QuickBooks isn't connected."}
    with Session(engine) as s:
        customer = s.get(Customer, customer_id)
        if not customer or not customer.qbo_id:
            return {"ok": False, "reason": "Customer isn't linked to QuickBooks."}
        qbo_id = customer.qbo_id

    with httpx.Client(timeout=30) as client:
        token = _access_token(client)
        item_id = _find_item_id(client, token, _COD_ITEM_NAME)
        if not item_id:
            return {"ok": False, "reason": f"QuickBooks item '{_COD_ITEM_NAME}' not found."}
        # QuickBooks generates the sharable pay link only for invoices that have a
        # billing email — pull the customer's from QuickBooks.
        crows = _query(client, token, f"select PrimaryEmailAddr from Customer where Id = '{qbo_id}'", "Customer")
        bill_email = (crows[0].get("PrimaryEmailAddr") or {}).get("Address") if crows else None
        payload = {
            "CustomerRef": {"value": qbo_id},
            # ACH only — this company doesn't have credit-card payments enabled, and
            # setting an unsupported option stops QuickBooks generating the pay link.
            "AllowOnlineACHPayment": True,
            "AllowOnlineCreditCardPayment": False,
            "CustomerMemo": {"value": f"Prepayment for order {order_ref}"},
            "Line": [{
                "DetailType": "SalesItemLineDetail",
                "Amount": round(float(amount), 2),
                "Description": f"COD ready-mix — order {order_ref}",
                # Non-taxable so the invoice total equals the amount staff entered.
                "SalesItemLineDetail": {"ItemRef": {"value": item_id}, "TaxCodeRef": {"value": "NON"}},
            }],
        }
        if bill_email:
            payload["BillEmail"] = {"Address": bill_email}
        url = f"{config.QBO_API_BASE}/v3/company/{config.QBO_REALM_ID}/invoice"
        resp = client.post(url, params={"minorversion": config.QBO_MINOR_VERSION},
                           headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                           json=payload)
        if resp.status_code >= 400:
            try:
                reason = resp.json().get("Fault", {}).get("Error", [{}])[0].get("Detail", resp.text)
            except ValueError:
                reason = resp.text
            return {"ok": False, "reason": reason}
        inv = resp.json().get("Invoice", {})
        inv_id = inv.get("Id")
        # A freshly created invoice may not expose a pay link until online ACH is
        # explicitly enabled; force it with a sparse update, then read the link.
        try:
            up = client.post(url, params={"minorversion": config.QBO_MINOR_VERSION},
                             headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                             json={"Id": inv_id, "SyncToken": inv.get("SyncToken", "0"), "sparse": True,
                                   "AllowOnlineACHPayment": True, "AllowOnlineCreditCardPayment": False})
            up.raise_for_status()
        except httpx.HTTPError:
            pass
        full = _get_invoice(client, token, inv_id, with_link=True)

    # The invoice is created/tracked either way — the link is best-effort. COD
    # gating + paid-detection work off the invoice balance regardless.
    return {"ok": True, "invoice_id": str(inv_id), "doc_number": inv.get("DocNumber"),
            "link": full.get("InvoiceLink")}


def cod_invoice_status(qbo_invoice_id: str) -> dict:
    """Return {paid, balance, link} for a COD invoice. Paid when balance <= 0."""
    if config.USE_MOCK_QBO:
        return {"paid": False, "balance": None, "link": None}
    with httpx.Client(timeout=30) as client:
        token = _access_token(client)
        full = _get_invoice(client, token, qbo_invoice_id, with_link=True)
    balance = float(full.get("Balance", 0) or 0)
    return {"paid": balance <= 0, "balance": balance, "link": full.get("InvoiceLink")}


# ── OAuth 2.0 token handling ─────────────────────────────────────────────────
# Intuit rotates the refresh token on (most) refreshes and invalidates the old
# one, so we persist the latest to a small file and prefer it over .env — that's
# what keeps a long-running sync alive past the first day.
_TOKEN_FILE = config.data_path("qbo_tokens.json")
_access = {"token": None, "expires_at": 0.0}   # in-memory access-token cache


def _stored_refresh_token() -> str:
    try:
        with open(_TOKEN_FILE) as f:
            return json.load(f).get("refresh_token") or config.QBO_REFRESH_TOKEN
    except (OSError, ValueError):
        return config.QBO_REFRESH_TOKEN


def _store_refresh_token(token: str) -> None:
    try:
        with open(_TOKEN_FILE, "w") as f:
            json.dump({"refresh_token": token, "saved_at": datetime.utcnow().isoformat()}, f)
    except OSError:
        pass  # non-fatal: we still hold the access token in memory for this run


def _access_token(client: httpx.Client) -> str:
    """Return a valid access token, exchanging the refresh token when needed."""
    if _access["token"] and time.time() < _access["expires_at"] - 60:
        return _access["token"]
    resp = client.post(
        config.QBO_TOKEN_URL,
        auth=(config.QBO_CLIENT_ID, config.QBO_CLIENT_SECRET),   # HTTP Basic
        headers={"Accept": "application/json"},
        data={"grant_type": "refresh_token", "refresh_token": _stored_refresh_token()},
    )
    resp.raise_for_status()
    tok = resp.json()
    _access["token"] = tok["access_token"]
    _access["expires_at"] = time.time() + int(tok.get("expires_in", 3600))
    if tok.get("refresh_token"):           # Intuit usually returns a rotated one
        _store_refresh_token(tok["refresh_token"])
    return _access["token"]


# ── QuickBooks Accounting API ────────────────────────────────────────────────
def _query(client: httpx.Client, token: str, statement: str, entity: str) -> list[dict]:
    url = f"{config.QBO_API_BASE}/v3/company/{config.QBO_REALM_ID}/query"
    resp = client.get(
        url,
        params={"query": statement, "minorversion": config.QBO_MINOR_VERSION},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    resp.raise_for_status()
    return resp.json().get("QueryResponse", {}).get(entity, [])


def _query_invoices(client: httpx.Client, token: str) -> list[dict]:
    return _query(client, token, "select * from Invoice order by TxnDate desc maxresults 1000", "Invoice")


def _query_customers(client: httpx.Client, token: str) -> list[dict]:
    return _query(client, token, "select * from Customer maxresults 1000", "Customer")


def _fmt_date(qbo_date: str) -> str:
    """'2026-05-14' → 'May 14, 2026' (matches the app's billing screen)."""
    try:
        return datetime.strptime(qbo_date, "%Y-%m-%d").strftime("%b %d, %Y")
    except (ValueError, TypeError):
        return qbo_date or ""


def _status(balance: float, due_date: str) -> str:
    if balance <= 0:
        return "paid"
    try:
        if due_date and datetime.strptime(due_date, "%Y-%m-%d").date() < date.today():
            return "overdue"
    except ValueError:
        pass
    return "due"


def _order_ref(inv: dict) -> str | None:
    """Pull an order reference from a QuickBooks custom field named like 'Order'."""
    for cf in inv.get("CustomField", []) or []:
        if "order" in cf.get("Name", "").lower() and cf.get("StringValue"):
            return cf["StringValue"]
    return None


# ── Ready-mix product filter ─────────────────────────────────────────────────
# This app bills ONLY for ready-mix concrete. The same QuickBooks company also
# sells block/masonry, aggregates, turf, etc. — those must NOT appear here. We
# classify each invoice by its line items and keep only the ready-mix portion.
# Scope confirmed with the business: core concrete mixes + concrete delivery/haul
# fees + concrete admixtures. Aggregates and cement are intentionally excluded.
READY_MIX_ITEMS = frozenset({
    # core concrete mixes
    "ready mix", "concrete mix", "liquid limestone ready mix", "liquid limestone",
    # concrete delivery & haul fees
    "trucking/hauling", "short load", "back haul", "short load fee",
    # concrete admixtures
    "fiber", "retarder", "admixtures", "nycon xl-200",
})


def _is_ready_mix_item(name: str) -> bool:
    n = (name or "").strip().lower()
    # QuickBooks sub-items come through as "Parent:Child" — check the leaf too.
    return n in READY_MIX_ITEMS or n.split(":")[-1].strip() in READY_MIX_ITEMS


def _ready_mix_split(inv: dict) -> tuple[float, float]:
    """Return (ready_mix_line_total, all_item_line_total) for an invoice.

    Looks only at product/service lines (SalesItemLineDetail), ignoring discount
    and subtotal lines. The ratio of the two tells us whether an invoice is fully
    ready-mix (equal), partly (mixed), or not at all (ready total = 0).
    """
    ready = total = 0.0
    for ln in inv.get("Line", []) or []:
        if ln.get("DetailType") != "SalesItemLineDetail":
            continue
        amt = float(ln.get("Amount", 0) or 0)
        total += amt
        item = ((ln.get("SalesItemLineDetail") or {}).get("ItemRef") or {}).get("name", "")
        if _is_ready_mix_item(item):
            ready += amt
    return ready, total


def _map_invoice(inv: dict, customer_id: int, ready_fraction: float = 1.0) -> Invoice:
    """Mirror a QuickBooks invoice into a local Invoice row.

    ready_fraction (0..1] scales amount/balance to the ready-mix share of a mixed
    invoice; it is 1.0 for invoices that are entirely ready-mix products.
    """
    total_amt = float(inv.get("TotalAmt", 0) or 0)
    balance = float(inv.get("Balance", 0) or 0)
    return Invoice(
        number=inv.get("DocNumber") or f"QB-{inv.get('Id')}",
        customer_id=customer_id,
        date=_fmt_date(inv.get("TxnDate", "")),
        amount=round(total_amt * ready_fraction, 2),
        status=_status(balance * ready_fraction, inv.get("DueDate", "")),
        order_ref=_order_ref(inv),
        qbo_invoice_id=str(inv.get("Id")) if inv.get("Id") is not None else None,
    )


# ── Customer roster import ───────────────────────────────────────────────────
def _customer_contact(c: dict) -> str:
    return ((c.get("PrimaryPhone") or {}).get("FreeFormNumber")
            or (c.get("PrimaryEmailAddr") or {}).get("Address") or "")


def backfill_customer_emails() -> dict:
    """Fill each EXISTING local customer's `email` from QuickBooks (matched by
    qbo_id). Only updates rows already in the table — never adds customers — so
    it's safe to run without re-introducing ones you removed. Mock-mode no-ops."""
    if config.USE_MOCK_QBO:
        return {"ok": False, "mode": "mock",
                "reason": "QuickBooks not configured — set QBO_* in .env."}
    with httpx.Client(timeout=30) as client:
        raw = _query_customers(client, _access_token(client))
    emails = {
        str(c.get("Id")): (c.get("PrimaryEmailAddr") or {}).get("Address")
        for c in raw
    }
    filled = 0
    with Session(engine) as s:
        for cust in s.exec(select(Customer)).all():
            addr = emails.get(cust.qbo_id) if cust.qbo_id else None
            if addr and cust.email != addr:
                cust.email = addr
                s.add(cust)
                filled += 1
        s.commit()
    return {"ok": True, "mode": "live", "filled": filled}


def import_customers_from_quickbooks() -> dict:
    """Pull the QuickBooks customer roster into the local Customer table.

    Upserts by qbo_id (so it's safe to re-run): existing rows are refreshed,
    new ones created, inactive QuickBooks customers skipped. Existing local
    customers without a qbo_id (e.g. demo seed) are left untouched. Mock mode
    no-ops with a reason.
    """
    if config.USE_MOCK_QBO:
        return {"imported": False, "mode": "mock",
                "reason": "QuickBooks not configured — set QBO_* in .env to import customers."}

    with httpx.Client(timeout=30) as client:
        raw = _query_customers(client, _access_token(client))

    created = updated = skipped = 0
    with Session(engine) as s:
        for c in raw:
            if c.get("Active") is False:
                skipped += 1
                continue
            qbo_id = str(c.get("Id"))
            name = c.get("DisplayName") or c.get("CompanyName") or f"Customer {qbo_id}"
            contact = _customer_contact(c)
            existing = s.exec(select(Customer).where(Customer.qbo_id == qbo_id)).first()
            if existing:
                existing.name = name
                existing.contact = contact
                s.add(existing)
                updated += 1
            else:
                s.add(Customer(name=name, acct_no=f"QB-{qbo_id}", terms="Net 10",
                               credit_limit=0.0, contact=contact, qbo_id=qbo_id))
                created += 1
        s.commit()

    return {"imported": True, "mode": "live", "created": created, "updated": updated,
            "skipped_inactive": skipped, "at": datetime.utcnow().isoformat() + "Z"}


# ── The A/R sync itself ──────────────────────────────────────────────────────
def sync_ar_from_quickbooks() -> dict:
    """Pull invoices from QuickBooks and mirror them into the local Invoice table.

    Returns a summary dict. In MOCK mode (no creds) it no-ops with a clear reason,
    leaving the seeded local data in place so the billing screen still works.
    """
    if config.USE_MOCK_QBO:
        return {
            "synced": False, "mode": "mock",
            "reason": "QuickBooks not configured — serving local invoice data. "
                      "Set QBO_* in .env to enable live sync.",
        }

    # Refresh the customer roster FIRST, so a brand-new QuickBooks customer (and any
    # invoices already on it) is picked up this run instead of waiting for a manual
    # import. A hiccup here shouldn't block the A/R sync, so it's best-effort.
    try:
        import_customers_from_quickbooks()
    except Exception as e:
        print("customer import during A/R sync failed:", e)

    with httpx.Client(timeout=30) as client:
        token = _access_token(client)
        raw = _query_invoices(client, token)

    with Session(engine) as s:
        customers = s.exec(select(Customer)).all()
        by_qbo_id = {c.qbo_id: c.id for c in customers if c.qbo_id}
        by_name = {c.name.strip().lower(): c.id for c in customers}   # fallback only

        matched: dict[int, list[Invoice]] = {}
        unmatched: set[str] = set()
        skipped_non_readymix = mixed = 0
        for inv in raw:
            ref = inv.get("CustomerRef") or {}
            ref_id, ref_name = str(ref.get("value") or ""), ref.get("name", "")
            cid = by_qbo_id.get(ref_id) or by_name.get(ref_name.strip().lower())
            if cid is None:
                if ref_name or ref_id:
                    unmatched.add(ref_name or f"id:{ref_id}")
                continue
            ready, item_total = _ready_mix_split(inv)
            if ready <= 0:                       # no ready-mix content → not ours
                skipped_non_readymix += 1
                continue
            frac = ready / item_total if item_total > 0 else 1.0
            if frac < 0.999:                     # mixed invoice — prorated to ready-mix
                mixed += 1
            matched.setdefault(cid, []).append(_map_invoice(inv, cid, frac))

        # Mirror semantics: clear existing QuickBooks-sourced invoices, then re-add
        # the ready-mix ones we pulled this run. A customer with no ready-mix
        # invoices this run correctly ends up with none (this app bills ready-mix
        # only). Demo customers without a qbo_id are left untouched.
        qbo_cids = {c.id for c in customers if c.qbo_id}
        if qbo_cids:
            for old in s.exec(select(Invoice).where(Invoice.customer_id.in_(qbo_cids))).all():
                s.delete(old)
        total = 0
        for cid, invs in matched.items():
            for inv in invs:
                s.add(inv)
                total += 1
        s.commit()

        matched_names = sorted(
            c.name for c in s.exec(select(Customer)).all() if c.id in matched
        )

    return {
        "synced": True, "mode": "live",
        "invoices": total,
        "ready_mix_only": True,
        "skipped_non_readymix": skipped_non_readymix,
        "mixed_prorated": mixed,
        "customers_matched": matched_names,
        "customers_unmatched": sorted(unmatched),
        "at": datetime.utcnow().isoformat() + "Z",
    }


async def qbo_sync_loop() -> None:
    """Background loop: re-pull A/R on a schedule when live. Started in app/main.py."""
    mode = "MOCK" if config.USE_MOCK_QBO else "LIVE"
    if config.USE_MOCK_QBO:
        print("QuickBooks A/R sync in MOCK mode — billing serves local data "
              "(set QBO_* in .env to go live).")
        return  # nothing to poll
    print(f"QuickBooks A/R sync started in {mode} mode (every {config.QBO_SYNC_SECONDS}s).")
    while True:
        try:
            # Run the blocking httpx + DB work off the event loop.
            result = await asyncio.to_thread(sync_ar_from_quickbooks)
            if result.get("synced"):
                print(f"QuickBooks sync: {result['invoices']} invoice(s) across "
                      f"{len(result['customers_matched'])} customer(s).")
                if result["customers_unmatched"]:
                    print("  unmatched QBO customers:", ", ".join(result["customers_unmatched"]))
        except Exception as e:   # never let a hiccup kill the loop
            print("QuickBooks sync error:", e)
        await asyncio.sleep(config.QBO_SYNC_SECONDS)
