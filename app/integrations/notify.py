"""Staff alerts for new customer order requests — best-effort, never raises.

Sends a text (Twilio) and/or email (SMTP) to the staff recipients configured in
config.NOTIFY_SMS / NOTIFY_EMAIL. Both channels are dormant until their service
is set up, so this is safe to call always.
"""
from .. import config
from .sms import send_sms
from .email import send_email


def notify_new_order(order: dict, customer_name: str) -> dict:
    ref = order.get("ref", "")
    qty = order.get("qty", "")
    mix = order.get("mix", "")
    site = order.get("site", "")
    when = order.get("when") or order.get("scheduled_for") or ""
    body = (f"New Aussieblock order — {customer_name}: {qty} {mix} for {site} on {when}. "
            f"Ref {ref}. Confirm it on the dispatch board.")
    results = {"sms": [], "email": None}
    for num in config.NOTIFY_SMS:
        try:
            results["sms"].append(send_sms(num, body))
        except Exception:   # noqa: BLE001
            pass
    if config.NOTIFY_EMAIL:
        try:
            results["email"] = send_email(f"New order request — {customer_name} ({ref})", body, config.NOTIFY_EMAIL)
        except Exception:   # noqa: BLE001
            pass
    return results
