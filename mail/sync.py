"""Turn inbox mail into replies and application updates in the job database."""
import logging
import re

from watcher.db import JobStore, now

from .classify import classify

log = logging.getLogger(__name__)

# Senders the ATSs use for application emails. Anything else is ignored unless it's a reply
# from a contact or comes from a company you've applied to, so most mail never reaches Claude.
ATS_DOMAINS = ("greenhouse-mail.io", "greenhouse.io", "hire.lever.co", "lever.co", "ashbyhq.com",
               "myworkday.com", "workday.com", "smartrecruiters.com", "icims.com", "jobvite.com")

APP_STATUS = {"confirmation": "applied", "assessment": "assessment", "interview_request": "interview",
              "rejection": "rejected", "offer": "offer"}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _match_reply(store: JobStore, mail: dict):
    ids = [mail["in_reply_to"], *mail["references"]]
    ids = [i for i in ids if i]
    if ids:
        marks = ",".join("?" * len(ids))
        row = store.conn.execute(
            f"SELECT contact_id FROM messages WHERE rfc_message_id IN ({marks}) LIMIT 1", ids).fetchone()
        if row:
            return row["contact_id"]
    row = store.conn.execute(
        "SELECT c.id FROM contacts c JOIN messages m ON m.contact_id = c.id"
        " WHERE lower(c.email)=? AND m.status IN ('sent', 'replied') ORDER BY c.id DESC LIMIT 1",
        (mail["from_addr"],)).fetchone()
    return row["id"] if row else None


def _applied_companies(store: JobStore) -> dict[str, int]:
    """normalised company slug -> most recent job rowid you approved there."""
    rows = store.conn.execute(
        "SELECT rowid, company FROM jobs WHERE status IN ('approved', 'submitted') ORDER BY first_seen").fetchall()
    return {_norm(r["company"]): r["rowid"] for r in rows}


def process(store: JobStore, mails: list[dict], *, classifier=classify, model: str | None = None) -> list[dict]:
    """Store what's new and return events worth a Telegram message."""
    events = []
    companies = _applied_companies(store)
    for mail in mails:
        mid = mail["message_id"] or f"{mail['from_addr']}|{mail['date']}|{mail['subject']}"
        if store.conn.execute("SELECT 1 FROM inbox WHERE rfc_message_id=?", (mid,)).fetchone():
            continue
        snippet = mail["body"][:400]

        contact_id = _match_reply(store, mail)
        if contact_id:
            store.conn.execute(
                "INSERT INTO inbox (rfc_message_id, kind, contact_id, from_addr, subject, snippet, received_at, created_at)"
                " VALUES (?, 'reply', ?, ?, ?, ?, ?, ?)",
                (mid, contact_id, mail["from_addr"], mail["subject"], snippet, mail["date"], now()))
            store.conn.execute(
                "UPDATE messages SET status='replied', replied_at=? WHERE contact_id=? AND status='sent'",
                (mail["date"], contact_id))
            store.conn.commit()
            c = store.conn.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
            events.append({"kind": "reply", "contact": c, "subject": mail["subject"], "snippet": snippet})
            continue

        domain = mail["from_domain"]
        from_company = next((rowid for slug, rowid in companies.items() if slug and slug in _norm(domain)), None)
        if not (from_company or any(domain == d or domain.endswith("." + d) for d in ATS_DOMAINS)):
            continue
        try:
            result = classifier(mail, **({"model": model} if model else {}))
        except Exception as exc:  # noqa: BLE001 - try again on the next sync
            log.warning("couldn't classify %s: %s", mail["subject"], exc)
            continue
        if not result.is_application_email:
            store.conn.execute(
                "INSERT INTO inbox (rfc_message_id, kind, from_addr, subject, category, received_at, created_at)"
                " VALUES (?, 'ignored', ?, ?, 'other', ?, ?)", (mid, mail["from_addr"], mail["subject"], mail["date"], now()))
            store.conn.commit()
            continue
        job_rowid = from_company or companies.get(_norm(result.company))
        if job_rowid is None:   # e.g. "Stripe, Inc." vs slug "stripe"
            job_rowid = next((r for slug, r in companies.items() if slug and slug in _norm(result.company)), None)
        store.conn.execute(
            "INSERT INTO inbox (rfc_message_id, kind, job_rowid, from_addr, subject, snippet, category, received_at, created_at)"
            " VALUES (?, 'application', ?, ?, ?, ?, ?, ?, ?)",
            (mid, job_rowid, mail["from_addr"], mail["subject"], result.summary, result.category, mail["date"], now()))
        if job_rowid and result.category in APP_STATUS:
            store.conn.execute("UPDATE jobs SET app_status=? WHERE rowid=?", (APP_STATUS[result.category], job_rowid))
        store.conn.commit()
        if result.category in ("interview_request", "assessment", "offer", "rejection"):
            events.append({"kind": "application", "company": result.company, "category": result.category,
                           "summary": result.summary, "job_rowid": job_rowid})
    return events
