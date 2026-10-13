"""Read recent inbox mail over IMAP (Gmail: imap.gmail.com with the same app password as SMTP).

Uses BODY.PEEK so nothing is marked as read.
"""
import email
import imaplib
import os
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime


def _decode(value: str | None) -> str:
    return str(make_header(decode_header(value))) if value else ""


def _text_body(msg: email.message.Message) -> str:
    parts = msg.walk() if msg.is_multipart() else [msg]
    html = ""
    for part in parts:
        ctype = part.get_content_type()
        if part.get_content_disposition() == "attachment":
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if ctype == "text/plain":
            return text
        if ctype == "text/html" and not html:
            html = re.sub(r"<[^>]+>", " ", text)
    return html


def parse_message(raw: bytes) -> dict:
    msg = email.message_from_bytes(raw)
    from_addr = parseaddr(_decode(msg.get("From")))[1].lower()
    try:
        date = parsedate_to_datetime(msg.get("Date")).astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        date = datetime.now(timezone.utc).isoformat(timespec="seconds")
    body = re.sub(r"\s+", " ", _text_body(msg)).strip()
    return {
        "message_id": (msg.get("Message-ID") or "").strip(),
        "from_addr": from_addr,
        "from_domain": from_addr.rsplit("@", 1)[-1] if "@" in from_addr else "",
        "subject": _decode(msg.get("Subject")),
        "in_reply_to": (msg.get("In-Reply-To") or "").strip(),
        "references": (msg.get("References") or "").split(),
        "body": body[:6000],
        "date": date,
    }


def fetch_recent(days: int = 3, limit: int = 300) -> list[dict]:
    user, password = os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASSWORD", "")
    if not (user and password):
        return []
    host = os.environ.get("IMAP_HOST", "imap.gmail.com")
    since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    with imaplib.IMAP4_SSL(host) as imap:
        imap.login(user, password)
        imap.select("INBOX", readonly=True)
        _, data = imap.search(None, "SINCE", since)
        ids = data[0].split()[-limit:]
        out = []
        for i in ids:
            _, msg_data = imap.fetch(i, "(BODY.PEEK[])")
            raw = next((part[1] for part in msg_data if isinstance(part, tuple)), None)
            if raw:
                out.append(parse_message(raw))
        return out
