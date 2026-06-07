"""Outbound email over SMTP.

LIVE when SMTP_HOST/USER/PASS are set; otherwise a no-op. Works with any SMTP
provider (SendGrid, Gmail app password, etc.). Standard library only.
"""
import smtplib
from email.message import EmailMessage

from .. import config


def send_email(subject: str, body: str, to_list: list[str]) -> dict:
    """Send one plain-text email. Returns {ok: True} or {ok: False, reason}."""
    if not config.USE_EMAIL:
        return {"ok": False, "configured": False, "reason": "Email isn't set up yet."}
    if not to_list:
        return {"ok": False, "configured": True, "reason": "No recipients."}
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.SMTP_FROM
    msg["To"] = ", ".join(to_list)
    msg.set_content(body)
    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as server:
            server.starttls()
            server.login(config.SMTP_USER, config.SMTP_PASS)
            server.send_message(msg)
    except Exception as e:   # noqa: BLE001 — alerts must never break the request
        return {"ok": False, "configured": True, "reason": str(e)}
    return {"ok": True, "to": to_list}
