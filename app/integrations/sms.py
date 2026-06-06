"""Twilio SMS sending.

LIVE when all TWILIO_* creds are set; otherwise disabled, and the frontend falls
back to opening the staff phone's messaging app via an sms: link. Uses Twilio's
REST API directly over httpx so there's no extra dependency.
"""
import re

import httpx

from .. import config


def to_e164(contact: str) -> str | None:
    """Best-effort US phone normalization to +1XXXXXXXXXX, or None if unusable."""
    d = re.sub(r"\D", "", contact or "")
    if len(d) == 10:
        return "+1" + d
    if len(d) == 11 and d.startswith("1"):
        return "+" + d
    if len(d) >= 7:
        return "+" + d
    return None


def send_sms(to_contact: str, body: str) -> dict:
    """Send one SMS. Returns {ok: True, to, sid} or {ok: False, configured, reason}."""
    if not config.USE_TWILIO:
        return {"ok": False, "configured": False,
                "reason": "Texting service isn't set up yet."}
    e164 = to_e164(to_contact)
    if not e164:
        return {"ok": False, "configured": True,
                "reason": "No valid phone number on file for this customer."}
    url = f"https://api.twilio.com/2010-04-01/Accounts/{config.TWILIO_ACCOUNT_SID}/Messages.json"
    try:
        resp = httpx.post(
            url,
            auth=(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN),
            data={"From": config.TWILIO_FROM_NUMBER, "To": e164, "Body": body},
            timeout=20,
        )
    except httpx.HTTPError as e:
        return {"ok": False, "configured": True, "reason": f"Network error: {e}"}
    if resp.status_code >= 400:
        try:
            reason = resp.json().get("message", resp.text)
        except ValueError:
            reason = resp.text
        return {"ok": False, "configured": True, "reason": reason}
    return {"ok": True, "to": e164, "sid": resp.json().get("sid")}
