"""Configuration loaded from environment variables (.env file)."""
import os
from dotenv import load_dotenv

load_dotenv()

# ── On-disk state location ──
# Where the SQLite database and the rotating QuickBooks token file live. Empty
# (the default) means the current working directory — exactly how local dev has
# always worked. In production set DATA_DIR to a PERSISTENT disk mount (e.g.
# "/data" on Render) so the database and the QuickBooks refresh token survive
# redeploys/restarts. Losing the token file would break the QuickBooks link.
DATA_DIR = os.getenv("DATA_DIR", "").strip()


def data_path(filename: str) -> str:
    """Path to an on-disk state file, placed under DATA_DIR when one is set."""
    return os.path.join(DATA_DIR, filename) if DATA_DIR else filename

ONESTEP_API_KEY = os.getenv("ONESTEP_API_KEY", "").strip()
ONESTEP_API_BASE = os.getenv("ONESTEP_API_BASE", "https://track.onestepgps.com/v3/api/public").strip()
GPS_POLL_SECONDS = int(os.getenv("GPS_POLL_SECONDS", "10"))

# When there's no API key, run in mock mode so the app still works end-to-end.
USE_MOCK_GPS = not bool(ONESTEP_API_KEY)

# ── Authentication ──
# Secret used to sign login tokens. A built-in default lets you run instantly,
# but set a long random SECRET_KEY in .env before going anywhere near production
# (anyone who knows it can forge logins). Generate one with:
#   python -c "import secrets; print(secrets.token_urlsafe(48))"
SECRET_KEY = os.getenv("SECRET_KEY", "").strip() or "dev-insecure-change-me"
# How long a login stays valid (minutes). 720 = 12 hours.
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "720"))

# ── Demo data seeding ──
# On startup the app can seed demo customers/orders/invoices and demo logins so a
# fresh checkout works instantly. Turn this OFF in production (SEED_DEMO=false in
# .env) so the public demo logins (tindol/reece) and fake data never reappear
# after a restart.
SEED_DEMO = os.getenv("SEED_DEMO", "true").strip().lower() not in ("false", "0", "no", "off")

# ── QuickBooks Online A/R sync ──
# Four OAuth values authorize the backend's always-on connection to a company's
# QuickBooks. Leave any blank to run in MOCK mode (billing serves the local
# Invoice table); fill all four to switch to LIVE sync. See integrations/quickbooks.py.
QBO_CLIENT_ID = os.getenv("QBO_CLIENT_ID", "").strip()
QBO_CLIENT_SECRET = os.getenv("QBO_CLIENT_SECRET", "").strip()
QBO_REFRESH_TOKEN = os.getenv("QBO_REFRESH_TOKEN", "").strip()
QBO_REALM_ID = os.getenv("QBO_REALM_ID", "").strip()
# "production" (real company data) or "sandbox" (Intuit's test company).
QBO_ENVIRONMENT = os.getenv("QBO_ENVIRONMENT", "production").strip().lower()
# How often (seconds) the background loop re-pulls A/R. 900 = 15 minutes.
QBO_SYNC_SECONDS = int(os.getenv("QBO_SYNC_SECONDS", "900"))
QBO_MINOR_VERSION = os.getenv("QBO_MINOR_VERSION", "65").strip()

# Live only when all four OAuth values are present; otherwise mock.
USE_MOCK_QBO = not all([QBO_CLIENT_ID, QBO_CLIENT_SECRET, QBO_REFRESH_TOKEN, QBO_REALM_ID])

# Intuit endpoints. The token endpoint is the same for both environments; the
# Accounting API base differs between sandbox and production.
QBO_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
QBO_API_BASE = (
    "https://sandbox-quickbooks.api.intuit.com" if QBO_ENVIRONMENT == "sandbox"
    else "https://quickbooks.api.intuit.com"
)

# ── Twilio SMS (automated customer texting) ──
# Set all three to enable the app to SEND texts itself (the "Send text" button).
# Leave any blank and the app falls back to opening the staff phone's messaging
# app via an sms: link. Get these from https://console.twilio.com after signing
# up, buying a number, and completing A2P 10DLC registration.
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "").strip()   # e.g. +13255551234
USE_TWILIO = all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER])

# ── New-order staff alerts ──
# When a customer places an order, alert staff by text and/or email. Set these to
# the staff phone number(s) / email(s) (comma-separated). SMS uses the Twilio
# creds above; email uses the SMTP settings below. Leave blank to disable.
# Dispatch recipients are baked in as defaults; override with env vars if needed.
NOTIFY_SMS = [s.strip() for s in os.getenv("NOTIFY_SMS", "+19405777475").split(",") if s.strip()]
NOTIFY_EMAIL = [s.strip() for s in os.getenv("NOTIFY_EMAIL", "dispatch@aussie-block.com").split(",") if s.strip()]

# ── Outbound email (SMTP) ──
# Enable order-alert emails (and any future email). Works with any SMTP provider,
# e.g. SendGrid (host smtp.sendgrid.net, user "apikey", pass = the API key) or
# Gmail (host smtp.gmail.com, user the address, pass an app password).
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASS = os.getenv("SMTP_PASS", "").strip()
SMTP_FROM = os.getenv("SMTP_FROM", "").strip() or SMTP_USER
USE_EMAIL = all([SMTP_HOST, SMTP_USER, SMTP_PASS])

# Aussieblock yard: 2951 E FM 2105, San Angelo, TX (exact pin) — origin for mock
# truck routes and the map's yard marker / geofence center.
PLANT_LAT = 31.523310
PLANT_LNG = -100.394094
# Yard geofence radius (meters) around PLANT_LAT/LNG. A "batched" order stays
# loading-at-the-yard until its truck crosses this fence, then it auto-flips to
# "enroute". Parked trucks sit ~120–165 m out, so the default leaves margin.
YARD_GEOFENCE_M = float(os.getenv("YARD_GEOFENCE_M", "500"))
# Arrival detection: an en-route truck that sits within ARRIVAL_MOVE_M of one spot
# for ARRIVAL_DWELL_SECONDS (and away from the yard) is flagged as "looks parked at
# the job" so dispatch can confirm "On site". This ignores the (imprecise) delivery
# address entirely — it keys off the truck actually stopping.
ARRIVAL_DWELL_SECONDS = int(os.getenv("ARRIVAL_DWELL_SECONDS", "300"))   # 5 minutes parked
ARRIVAL_MOVE_M = float(os.getenv("ARRIVAL_MOVE_M", "75"))                # movement under this = "stopped"
# Once an order is On site, the job location is pinned (the truck's spot). When the
# truck then moves more than this from the job, the order flips to "returning"; when
# it re-enters the yard geofence it auto-completes.
RETURN_LEAVE_SITE_M = float(os.getenv("RETURN_LEAVE_SITE_M", "250"))
# Once On site this long (and still at the job), the order auto-advances to "pouring".
POUR_DELAY_SECONDS = int(os.getenv("POUR_DELAY_SECONDS", "300"))   # 5 minutes on site
