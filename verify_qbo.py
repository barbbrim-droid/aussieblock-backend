"""One-off QuickBooks connection check. Safe, read-only.
Refreshes the access token, then asks QBO for the company name and a customer count.
"""
import sys
from pathlib import Path
import httpx

ENV = {}
for line in Path(__file__).with_name(".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        ENV[k.strip()] = v.strip()

CID = ENV.get("QBO_CLIENT_ID", "")
SECRET = ENV.get("QBO_CLIENT_SECRET", "")
REFRESH = ENV.get("QBO_REFRESH_TOKEN", "")
REALM = ENV.get("QBO_REALM_ID", "")
ENVIRON = ENV.get("QBO_ENVIRONMENT", "production").lower()
API_BASE = "https://sandbox-quickbooks.api.intuit.com" if ENVIRON == "sandbox" else "https://quickbooks.api.intuit.com"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"

print(f"Environment : {ENVIRON}")
print(f"Realm ID    : {REALM}")
print("Refreshing access token...")
r = httpx.post(TOKEN_URL, auth=(CID, SECRET), headers={"Accept": "application/json"},
               data={"grant_type": "refresh_token", "refresh_token": REFRESH}, timeout=30)
if r.status_code != 200:
    print(f"FAILED to refresh token ({r.status_code}): {r.text}")
    sys.exit(1)
access = r.json()["access_token"]
print("Token refresh OK.")

h = {"Authorization": f"Bearer {access}", "Accept": "application/json"}
ci = httpx.get(f"{API_BASE}/v3/company/{REALM}/companyinfo/{REALM}",
               headers=h, params={"minorversion": "65"}, timeout=30)
if ci.status_code != 200:
    print(f"FAILED company info ({ci.status_code}): {ci.text}")
    sys.exit(1)
name = ci.json()["CompanyInfo"]["CompanyName"]
print(f"Connected to company: {name}")

cnt = httpx.get(f"{API_BASE}/v3/company/{REALM}/query",
                headers=h, params={"query": "SELECT COUNT(*) FROM Customer", "minorversion": "65"}, timeout=30)
if cnt.status_code == 200:
    total = cnt.json().get("QueryResponse", {}).get("totalCount")
    print(f"Customer count in QuickBooks: {total}")
print("\nSUCCESS - QuickBooks credentials are working.")
