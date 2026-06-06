# Aussieblock Ready Mix — Backend

The engine behind the customer app and the office/dispatch dashboard. It serves
orders, live truck positions, and customer billing over a simple JSON API.

It runs **today** with zero setup: SQLite for storage (just a file) and a
**mock GPS mode** so trucks simulate movement until your One Step GPS key arrives.

---

## Run it (about 5 minutes)

You need **Python 3.11+** installed.

```bash
# 1. from inside the aussieblock-backend folder, create a virtual environment
python -m venv venv

# 2. activate it
#    macOS / Linux:
source venv/bin/activate
#    Windows (PowerShell):
venv\Scripts\Activate.ps1

# 3. install dependencies
pip install -r requirements.txt

# 4. create your config file (works as-is in mock mode)
cp .env.example .env        # Windows: copy .env.example .env

# 5. start the server
uvicorn app.main:app --reload
```

Now open **http://localhost:8000/docs** in your browser. That's an interactive
page where you can click any endpoint, hit "Try it out," and see live results.

**Log in first.** Most endpoints now require a login. In `/docs`, click the green
**Authorize** button (top right) and use one of the demo accounts the server
prints on startup:

| Role     | Email                  | Password      | Sees                        |
|----------|------------------------|---------------|-----------------------------|
| staff    | `ops@aussieblock.com`  | `dispatch123` | everything + dispatch/import |
| customer | `billing@tindol.com`   | `tindol123`   | only Tindol's orders/billing |
| customer | `ap@reece.com`         | `reece123`    | only Reece's orders/billing  |

(Leave the client id/secret fields blank — just enter the email as the username.)
Then "Try it out" on any endpoint and it sends your token automatically.

Things to try:
- `GET /orders` — your orders (a customer sees only their own; staff sees all).
- `GET /trucks` — refresh it a few times and watch the lat/lng change (mock movement).
- `GET /billing/1` — Tindol Construction's balance + invoices.
- `POST /import/moby-mix` — pulls in the sample CSV (3 new orders).
- `POST /orders/AB-24817/plus-load` — simulates the app's "Request plus load."
- `GET /dispatch/plus-loads` — the office side seeing that request.

---

## Going live (later, one piece at a time)

**One Step GPS:** when your key arrives, paste it into `.env` as `ONESTEP_API_KEY`
and restart. The poller switches from mock to live automatically. You'll also map
each truck's `gps_device_id` to its real One Step GPS device, and confirm the
endpoint/response shape in `app/integrations/onestep_gps.py` matches their docs.

**QuickBooks:** billing currently comes from the local invoice table. To sync real
A/R, follow the steps in `app/integrations/quickbooks.py` (create an Intuit app,
authorize once, store the tokens in `.env`, implement `sync_ar_from_quickbooks`).

**Moby Mix:** update `COLUMN_MAP` in `app/integrations/moby_mix_csv.py` to match
the column headers in a real export.

---

## Not in this starter (and why)

- **Hosting.** Runs on your computer for now. Putting it online is a later step.
- **Payments.** "Make a payment" needs a processor (QuickBooks Payments / Stripe).

## Project layout

```
app/
  main.py                  API routes + startup
  config.py                settings from .env
  auth.py                  login tokens (JWT) + password hashing + access rules
  db.py                    SQLite setup
  models.py                tables (User, Customer, Truck, Order, Invoice, PlusLoadRequest)
  seed.py                  demo data on first run
  integrations/
    onestep_gps.py         GPS poller (mock + live)
    moby_mix_csv.py        CSV importer
    quickbooks.py          billing source + sync stub
sample_data/
  moby_mix_sample.csv      example export to test the importer
```
