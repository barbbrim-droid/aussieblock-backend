"""Ingest FluidSecure fuel exports delivered by EMAIL (the no-API-token path).

FluidSecure can email a scheduled transaction CSV. This poller reads that
mailbox over IMAP, pulls the CSV attachment out of each new (unseen) message,
runs it through the same parser the manual upload / API poller use
(fluidsecure.ingest_csv → _ingest), then marks the message seen so it isn't
re-processed. De-dup on the FluidSecure transaction id makes re-runs harmless.

Idle unless FUEL_IMAP_HOST + FUEL_IMAP_USER + FUEL_IMAP_PASSWORD are set
(config.USE_FUEL_EMAIL). Uses only the standard library (imaplib/email).
"""
import asyncio
import email
import imaplib
from email.header import decode_header, make_header

from .fluidsecure import ingest_csv
from .. import config


def _sender(msg) -> str:
    try:
        return str(make_header(decode_header(msg.get("From", "")))).lower()
    except Exception:
        return (msg.get("From", "") or "").lower()


def _csv_attachments(msg):
    """Yield (filename, text) for each .csv attachment in the message."""
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        name = part.get_filename()
        if name:
            try:
                name = str(make_header(decode_header(name)))
            except Exception:
                pass
        ctype = (part.get_content_type() or "").lower()
        is_csv = (name and name.lower().endswith((".csv", ".txt", ".tsv"))) or "csv" in ctype
        if not is_csv:
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = payload.decode("latin-1", errors="replace")
        yield (name or "fuel.csv", text)


def _poll_once() -> int:
    """Check the mailbox once; ingest CSVs from new matching mail. Returns the
    number of new fuel transactions stored across all processed messages."""
    added_total = 0
    M = imaplib.IMAP4_SSL(config.FUEL_IMAP_HOST)
    try:
        M.login(config.FUEL_IMAP_USER, config.FUEL_IMAP_PASSWORD)
        M.select(config.FUEL_IMAP_FOLDER)
        typ, data = M.search(None, "UNSEEN")
        if typ != "OK":
            return 0
        ids = data[0].split()
        for mid in ids:
            typ, msg_data = M.fetch(mid, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            # Sender filter (skip unrelated mail). Non-matching mail is left
            # UNSEEN so a human still sees it in the inbox.
            if config.FUEL_IMAP_FROM and config.FUEL_IMAP_FROM not in _sender(msg):
                continue
            had_csv = False
            for fname, text in _csv_attachments(msg):
                had_csv = True
                try:
                    result = ingest_csv(text)
                    added_total += result["added"]
                    print(f"Fuel email: {fname} → {result['added']} new of {result['rows']} rows.")
                except Exception as e:
                    print("Fuel email: failed to parse", fname, "-", e)
            # Mark seen only once we've handled it (a matching FluidSecure mail,
            # whether or not it carried rows we hadn't seen before).
            if had_csv:
                M.store(mid, "+FLAGS", "\\Seen")
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return added_total


async def fuel_email_loop() -> None:
    if not config.USE_FUEL_EMAIL:
        print("Fuel-email ingester idle (set FUEL_IMAP_USER + FUEL_IMAP_PASSWORD to enable).")
        return
    print(f"Fuel-email ingester started ({config.FUEL_IMAP_USER} every "
          f"{config.FUEL_EMAIL_POLL_SECONDS}s).")
    while True:
        try:
            await asyncio.to_thread(_poll_once)
        except Exception as e:   # never let a hiccup kill the loop
            print("Fuel-email poll error:", e)
        await asyncio.sleep(config.FUEL_EMAIL_POLL_SECONDS)
