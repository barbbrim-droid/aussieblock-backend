# Turning on automated texting (Twilio)

The app can text customers and workers their app invite automatically. The code is
already built and deployed — it just needs a Twilio account wired up. Until then,
the dispatch board falls back to opening *your* phone's messaging app with the
message pre-filled (the "Text invite" button), so nothing is broken in the meantime.

Texting auto-activates the moment all three `TWILIO_*` env vars are set on the
backend (the app checks `USE_TWILIO = all three present`). No code change needed.

## Steps

### a. Create a Twilio account
Sign up at https://www.twilio.com. The free trial includes credit so you can test
before paying.

### b. Buy a phone number with SMS
Console → **Phone Numbers → Buy a number** → filter for a **local US number with
SMS** capability (try the **325** San Angelo area code). Roughly **$1.15/month**.

### c. Register for A2P 10DLC  ← the slow part, start this first
US carriers block business texting from unregistered numbers. In the Console:
**Messaging → Regulatory Compliance → A2P 10DLC**.
1. Register a **Brand** — your business legal name, address, and **EIN**.
2. Register a **Campaign** — use case **"Customer / account notifications"**;
   sample message: *"Hi {name}, Aussieblock now has an app to track your concrete
   deliveries and pay invoices online. Open https://aussieblock-app.onrender.com …"*

Carrier approval typically takes a **few days**. **Texts will error until the
campaign is approved**, even with the number bought and env vars set.

### d. Collect three values
- **Account SID** — Console dashboard (starts `AC…`)
- **Auth Token** — Console dashboard (click to reveal)
- **From number** — the number you bought, in **E.164** format, e.g. `+13255551234`

### e. Set the env vars on Render
Backend service `srv-d8hpf6mrnols73avl71g` → **Environment** → add:

| Key | Value |
|-----|-------|
| `TWILIO_ACCOUNT_SID` | `AC…` |
| `TWILIO_AUTH_TOKEN`  | (the auth token) |
| `TWILIO_FROM_NUMBER` | `+1325…` |

Then redeploy (or it redeploys on save). `USE_TWILIO` flips to true automatically.

## Verifying it's on
- `GET /sms/enabled` returns `{"enabled": true}`.
- On the dispatch board, the customer-invite and the **Workers → invite** buttons
  change from the grey "Text invite" (opens your phone) to a green **"Send text"**
  (sends automatically from your business number).

## Optional: new-order alerts to staff phones
Once Twilio works you can also have the app text the office when a customer places
an order: set `NOTIFY_SMS` (comma-separated cell numbers) on the backend. (Email
alerts use the separate `SMTP_*` / `NOTIFY_EMAIL` vars.)
