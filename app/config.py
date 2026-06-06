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

# Aussieblock plant location (San Angelo, TX) — used as the origin for mock truck routes.
PLANT_LAT = 31.4421
PLANT_LNG = -100.4503
