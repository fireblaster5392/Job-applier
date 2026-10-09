"""Contacts and message drafts in the shared job database."""
from datetime import datetime, timezone

from watcher.db import JobStore, now

from .draft import ContactDraft
from .finder import Contact


def save_outreach(store: JobStore, job_rowid: int, contacts: list[Contact],
                  drafts: dict[int, ContactDraft]) -> list[dict]:
    """Store contacts + drafts; returns [{contact, linkedin_msg_id, email_msg_id}]."""
    saved = []
    for i, c in enumerate(contacts):
        cur = store.conn.execute(
            "INSERT INTO contacts (job_rowid, name, title, linkedin_url, email, source, rank, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (job_rowid, c.name, c.title, c.linkedin_url, c.email, c.source, c.rank, now()))
        cid = cur.lastrowid
        d = drafts.get(i)
        li = em = None
        if d and c.linkedin_url:
            li = store.conn.execute(
                "INSERT INTO messages (contact_id, channel, body, created_at) VALUES (?, 'linkedin', ?, ?)",
                (cid, d.linkedin_note, now())).lastrowid
        if d and c.email:
            em = store.conn.execute(
                "INSERT INTO messages (contact_id, channel, subject, body, created_at) VALUES (?, 'email', ?, ?, ?)",
                (cid, d.email_subject, d.email_body, now())).lastrowid
        saved.append({"contact": c, "contact_id": cid, "linkedin_msg_id": li, "email_msg_id": em})
    store.conn.execute("UPDATE jobs SET outreach_at=? WHERE rowid=?", (now(), job_rowid))
    store.conn.commit()
    return saved


def jobs_needing_outreach(store: JobStore, statuses: list[str]) -> list:
    marks = ",".join("?" * len(statuses))
    return store.conn.execute(
        f"SELECT rowid, * FROM jobs WHERE outreach_at IS NULL AND status IN ({marks}) ORDER BY first_seen",
        statuses).fetchall()


def get_message(store: JobStore, msg_id: int):
    return store.conn.execute(
        "SELECT m.*, c.name, c.email, c.title, c.job_rowid FROM messages m JOIN contacts c ON c.id = m.contact_id"
        " WHERE m.id=?", (msg_id,)).fetchone()


def outreach_for_job(store: JobStore, job_rowid: int) -> list[dict]:
    contacts = store.conn.execute("SELECT * FROM contacts WHERE job_rowid=? ORDER BY rank DESC, id",
                                  (job_rowid,)).fetchall()
    out = []
    for c in contacts:
        msgs = store.conn.execute("SELECT * FROM messages WHERE contact_id=? ORDER BY channel",
                                  (c["id"],)).fetchall()
        out.append({"contact": c, "messages": msgs})
    return out


def emails_sent_today(store: JobStore) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    return store.conn.execute(
        "SELECT COUNT(*) FROM messages WHERE channel='email' AND status='sent' AND sent_at >= ?",
        (today,)).fetchone()[0]


def send_email_message(store: JobStore, msg_id: int, mailer, daily_cap: int) -> str:
    """Send one drafted email the user approved. Returns a short result for display."""
    m = get_message(store, msg_id)
    if m is None or m["channel"] != "email":
        return "Message not found."
    if m["status"] == "sent":
        return "Already sent."
    if m["status"] not in ("draft", "failed"):
        return f"Not sent: it's {m['status']}."
    if emails_sent_today(store) >= daily_cap:
        return f"Not sent: you've hit today's cap of {daily_cap} emails. Tap again tomorrow."
    # Claim it first so a double tap can't send twice.
    claimed = store.conn.execute(
        "UPDATE messages SET status='sending' WHERE id=? AND status IN ('draft', 'failed')", (msg_id,)).rowcount
    store.conn.commit()
    if not claimed:
        return "Already being sent."
    try:
        mailer.send(m["email"], m["subject"], m["body"])
    except Exception as exc:  # noqa: BLE001
        store.conn.execute("UPDATE messages SET status='failed', error=? WHERE id=?", (str(exc), msg_id))
        store.conn.commit()
        return f"Couldn't send: {exc}"
    store.conn.execute("UPDATE messages SET status='sent', sent_at=?, error=NULL WHERE id=?", (now(), msg_id))
    store.conn.commit()
    return f"Sent to {m['name']} ({m['email']})."


def skip_message(store: JobStore, msg_id: int) -> None:
    store.conn.execute("UPDATE messages SET status='skipped' WHERE id=? AND status='draft'", (msg_id,))
    store.conn.commit()
