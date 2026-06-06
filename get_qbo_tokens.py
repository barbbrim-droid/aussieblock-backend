"""One-shot QuickBooks OAuth helper — get the REFRESH_TOKEN + REALM_ID.

You only run this once (and again if the refresh token ever expires). It performs
the Intuit "Authorize" consent flow for you and writes the two values the backend
needs into .env, so the app can switch from MOCK to LIVE QuickBooks billing.

PREREQUISITES (do these first, at https://developer.intuit.com):
  1. Create an app → open its "Keys & credentials" for the **Production** keys.
     Paste the Client ID + Secret into .env as QBO_CLIENT_ID / QBO_CLIENT_SECRET.
  2. In the app's settings, add this EXACT Redirect URI (must match to the char):
         http://localhost:8200/callback
     (Override the port/URL with QBO_REDIRECT_URI in .env if 8200 is taken.)

THEN, from this folder with the venv active:
     python get_qbo_tokens.py
  …a browser opens, you pick the company and click Authorize, and this script
  captures the code, exchanges it, and writes QBO_REFRESH_TOKEN + QBO_REALM_ID
  into .env. Restart the backend afterward and the sync goes live.
"""
import secrets
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

ENV_PATH = Path(__file__).with_name(".env")
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
AUTHORIZE_URL = "https://appcenter.intuit.com/connect/oauth2"
SCOPE = "com.intuit.quickbooks.accounting"


def read_env() -> dict:
    """Minimal .env reader (KEY=VALUE, ignores blanks/comments)."""
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def upsert_env(updates: dict) -> None:
    """Set/replace KEY=VALUE lines in .env, preserving everything else."""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    remaining = dict(updates)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else None
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for k, v in remaining.items():       # keys not already present
        out.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


class _Catch(BaseHTTPRequestHandler):
    """Single-request handler that captures the ?code=…&realmId=… redirect."""
    result = {}

    def do_GET(self):
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        _Catch.result = {k: v[0] for k, v in params.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        ok = "code" in _Catch.result and "realmId" in _Catch.result
        msg = ("✅ QuickBooks connected. You can close this tab and return to the terminal."
               if ok else
               "⚠️ Something was missing in the redirect — check the terminal.")
        self.wfile.write(f"<html><body style='font:16px sans-serif;padding:3em'>{msg}</body></html>"
                         .encode("utf-8"))

    def log_message(self, *_):           # silence the default stderr logging
        pass


def main() -> int:
    env = read_env()
    client_id = env.get("QBO_CLIENT_ID", "")
    client_secret = env.get("QBO_CLIENT_SECRET", "")
    redirect_uri = env.get("QBO_REDIRECT_URI", "http://localhost:8200/callback")

    if not client_id or not client_secret:
        print("✗ QBO_CLIENT_ID / QBO_CLIENT_SECRET are not set in .env.\n"
              "  Paste your Production keys from developer.intuit.com first, then re-run.")
        return 1

    parsed = urllib.parse.urlparse(redirect_uri)
    host, port = parsed.hostname or "localhost", parsed.port or 80
    state = secrets.token_urlsafe(16)

    auth_url = AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "scope": SCOPE,
        "redirect_uri": redirect_uri,
        "state": state,
    })

    server = HTTPServer((host, port), _Catch)
    print(f"→ Listening on {redirect_uri} for the Intuit redirect…")
    print("→ Opening your browser. Pick the QuickBooks company and click Authorize.")
    print(f"  (If the browser doesn't open, paste this URL into it:)\n  {auth_url}\n")
    webbrowser.open(auth_url)
    server.handle_request()              # blocks until the one redirect arrives
    server.server_close()

    result = _Catch.result
    if result.get("state") != state:
        print("✗ State mismatch — aborting for safety. Re-run the script.")
        return 1
    if "error" in result:
        print(f"✗ Intuit returned an error: {result.get('error')} "
              f"{result.get('error_description', '')}")
        return 1
    code, realm_id = result.get("code"), result.get("realmId")
    if not code or not realm_id:
        print(f"✗ Missing code/realmId in the redirect. Got: {result}")
        return 1

    print("→ Exchanging the authorization code for tokens…")
    resp = httpx.post(
        TOKEN_URL,
        auth=(client_id, client_secret),
        headers={"Accept": "application/json"},
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"✗ Token exchange failed ({resp.status_code}): {resp.text}")
        return 1
    refresh_token = resp.json().get("refresh_token")
    if not refresh_token:
        print(f"✗ No refresh_token in the response: {resp.json()}")
        return 1

    upsert_env({"QBO_REFRESH_TOKEN": refresh_token, "QBO_REALM_ID": realm_id})
    print("\n✅ Done. Wrote QBO_REFRESH_TOKEN and QBO_REALM_ID into .env:")
    print(f"     QBO_REALM_ID={realm_id}")
    print(f"     QBO_REFRESH_TOKEN={refresh_token[:8]}…{refresh_token[-4:]} (hidden)")
    print("\nNext: restart the backend, then POST /import/customers and POST /billing/sync.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
