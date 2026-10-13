"""SQLite job store. Plain SQL with portable types so it moves to Postgres later."""
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from .models import Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS boards (
    ats          TEXT NOT NULL,
    company      TEXT NOT NULL,
    seeded_at    TEXT,              -- first successful poll; jobs seen then are backlog, not "new"
    last_ok_at   TEXT,
    last_error   TEXT,
    PRIMARY KEY (ats, company)
);

CREATE TABLE IF NOT EXISTS jobs (
    ats          TEXT NOT NULL,
    company      TEXT NOT NULL,
    job_id       TEXT NOT NULL,
    title        TEXT NOT NULL,
    location     TEXT,
    department   TEXT,
    remote       INTEGER NOT NULL DEFAULT 0,
    url          TEXT,
    apply_url    TEXT,
    posted_at    TEXT,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    closed_at    TEXT,              -- set when the job disappears from the board
    matched      INTEGER NOT NULL DEFAULT 0,
    is_new       INTEGER NOT NULL DEFAULT 0,  -- 0 = backlog from the board's first poll
    notified_at  TEXT,
    status       TEXT NOT NULL DEFAULT 'discovered',
                                    -- discovered | below_threshold | tailoring | tailored | approved
                                    -- | applying | needs_review | submit_requested | needs_you | submitted
                                    -- | apply_failed | rejected | skipped | failed
    score        INTEGER,           -- quick fit score 0-100 (auto mode)
    score_reasons TEXT,             -- JSON list
    outreach_at  TEXT,              -- when contacts were found and drafts sent to you
    app_status   TEXT,              -- from the company's emails: applied | assessment | interview
                                    -- | rejected | offer
    last_error   TEXT,
    raw          TEXT,
    PRIMARY KEY (ats, company, job_id)
);

CREATE TABLE IF NOT EXISTS tailored (
    job_rowid    INTEGER PRIMARY KEY,   -- jobs.rowid
    created_at   TEXT NOT NULL,
    model        TEXT,
    description  TEXT,                  -- job description the tailoring used
    data         TEXT NOT NULL,         -- TailoredApplication JSON
    warnings     TEXT,                  -- guardrail fixes, JSON list
    usage        TEXT                   -- token usage, JSON
);

CREATE TABLE IF NOT EXISTS contacts (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL DEFAULT 'job',   -- job (after an approval) | network (daily batch)
    job_rowid    INTEGER,                       -- NULL for networking contacts
    company      TEXT,
    name         TEXT NOT NULL,
    title        TEXT,
    linkedin_url TEXT,
    email        TEXT,
    source       TEXT,
    rank         INTEGER,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY,
    contact_id   INTEGER NOT NULL,
    channel      TEXT NOT NULL,     -- linkedin (you send it yourself) | email
    subject      TEXT,
    body         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'draft',   -- draft | sending | sent | replied | skipped | failed
    is_followup  INTEGER NOT NULL DEFAULT 0,
    rfc_message_id TEXT,            -- Message-ID header of the sent email, to match replies
    error        TEXT,
    sent_at      TEXT,
    replied_at   TEXT,
    created_at   TEXT NOT NULL
);

-- Emails from your inbox that matter: replies from contacts, and the companies' application emails.
CREATE TABLE IF NOT EXISTS inbox (
    id           INTEGER PRIMARY KEY,
    rfc_message_id TEXT UNIQUE,
    kind         TEXT NOT NULL,     -- reply | application
    contact_id   INTEGER,
    job_rowid    INTEGER,
    from_addr    TEXT,
    subject      TEXT,
    snippet      TEXT,
    category     TEXT,              -- application emails: confirmation | rejection | interview_request
                                    -- | assessment | offer | other
    received_at  TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    job_rowid    INTEGER PRIMARY KEY,
    outcome      TEXT,              -- submitted | needs_review | needs_you | failed
    answers      TEXT,              -- JSON [{label, type, answer, source}] as filled
    reasons      TEXT,              -- JSON list: why it came back to you
    screenshot   TEXT,              -- path to the latest screenshot
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    submitted_at TEXT
);

CREATE TABLE IF NOT EXISTS prep_sheets (
    job_rowid    INTEGER PRIMARY KEY,   -- interview prep sheet for this job (prep/sheet.py)
    data         TEXT NOT NULL,         -- PrepSheet JSON
    model        TEXT,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
    key          TEXT PRIMARY KEY,
    value        TEXT
);

CREATE INDEX IF NOT EXISTS jobs_first_seen ON jobs (first_seen);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status);
"""


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobStore:
    def __init__(self, path: str):
        # check_same_thread=False: the dashboard and bot run tailoring in worker threads.
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)   # e.g. a fresh data/ volume
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._add_missing_columns()
        self.conn.executescript(SCHEMA)

    def _add_missing_columns(self) -> None:
        """Bring a database made by an earlier version up to SCHEMA: new tables come from
        CREATE TABLE IF NOT EXISTS, new columns on existing tables are added here."""
        ref = sqlite3.connect(":memory:")
        ref.executescript(SCHEMA)
        tables = [r[0] for r in ref.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if not have:
                continue
            for _, name, type_, notnull, default, _ in ref.execute(f"PRAGMA table_info({table})"):
                if name in have:
                    continue
                col = f"{name} {type_}"
                if default is not None:
                    col += f" {'NOT NULL ' if notnull else ''}DEFAULT {default}"
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
        ref.close()

    def is_seeded(self, ats: str, company: str) -> bool:
        row = self.conn.execute(
            "SELECT seeded_at FROM boards WHERE ats=? AND company=?", (ats, company)
        ).fetchone()
        return bool(row and row["seeded_at"])

    def record_board_error(self, ats: str, company: str, error: str) -> None:
        self.conn.execute(
            """INSERT INTO boards (ats, company, last_error) VALUES (?, ?, ?)
               ON CONFLICT (ats, company) DO UPDATE SET last_error=excluded.last_error""",
            (ats, company, error[:500]),
        )
        self.conn.commit()

    def sync_board(self, ats: str, company: str, jobs: list[Job], is_match) -> list[Job]:
        """Upsert one board's current listing. Returns jobs never seen before.

        On a board's first successful poll every job is stored but none is returned,
        so starting the watcher doesn't flood you with the existing backlog.
        """
        ts = now()
        seeded = self.is_seeded(ats, company)
        known = {
            r["job_id"]
            for r in self.conn.execute(
                "SELECT job_id FROM jobs WHERE ats=? AND company=?", (ats, company)
            )
        }
        new = []
        for job in jobs:
            if job.job_id in known:
                self.conn.execute(
                    "UPDATE jobs SET last_seen=?, closed_at=NULL WHERE ats=? AND company=? AND job_id=?",
                    (ts, ats, company, job.job_id),
                )
                continue
            self.conn.execute(
                """INSERT INTO jobs (ats, company, job_id, title, location, department, remote,
                                     url, apply_url, posted_at, first_seen, last_seen, matched, is_new, raw)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ats, company, job.job_id, job.title, job.location, job.department,
                 int(job.remote), job.url, job.apply_url, job.posted_at, ts, ts,
                 int(is_match(job)), int(seeded), json.dumps(job.raw)),
            )
            if seeded:
                new.append(job)

        current = {j.job_id for j in jobs}
        gone = known - current
        if gone:
            self.conn.executemany(
                "UPDATE jobs SET closed_at=? WHERE ats=? AND company=? AND job_id=? AND closed_at IS NULL",
                [(ts, ats, company, jid) for jid in gone],
            )
        self.conn.execute(
            """INSERT INTO boards (ats, company, seeded_at, last_ok_at) VALUES (?, ?, ?, ?)
               ON CONFLICT (ats, company) DO UPDATE SET
                 seeded_at=COALESCE(boards.seeded_at, excluded.seeded_at),
                 last_ok_at=excluded.last_ok_at, last_error=NULL""",
            (ats, company, ts, ts),
        )
        self.conn.commit()
        return new

    def mark_notified(self, job: Job) -> None:
        """Also used by the worker for jobs it handled without a message (below threshold)."""
        self.conn.execute(
            "UPDATE jobs SET notified_at=? WHERE ats=? AND company=? AND job_id=?",
            (now(), *job.key),
        )
        self.conn.commit()

    def pending_notifications(self) -> list[Job]:
        """New matching jobs not yet notified, including ones whose send failed earlier."""
        rows = self.conn.execute(
            "SELECT rowid, * FROM jobs WHERE matched=1 AND is_new=1 AND notified_at IS NULL"
            " AND closed_at IS NULL ORDER BY first_seen"
        ).fetchall()
        return [row_to_job(r) for r in rows]

    # --- used by the worker, the bot, the dashboard and tailoring -------------------

    def add_found_jobs(self, source: str, search: str, jobs: list[Job], is_match,
                       dedupe_days: int = 30) -> tuple[list[Job], int]:
        """Store jobs from a search (JobSpy). Returns (new jobs, duplicates skipped).

        Like sync_board, a search's first run is stored silently as backlog. Unlike a board, a search
        result disappearing doesn't mean the job closed, so nothing is marked closed. A job whose
        company and title were already seen from any source in the last dedupe_days is skipped.
        """
        ts = now()
        seeded = self.is_seeded(source, search)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=dedupe_days)).isoformat(timespec="seconds")
        seen = {(_norm(r["company"]), _norm(r["title"])) for r in self.conn.execute(
            "SELECT company, title FROM jobs WHERE first_seen >= ?", (cutoff,))}
        new, dupes = [], 0
        for job in jobs:
            if self.conn.execute("SELECT 1 FROM jobs WHERE ats=? AND company=? AND job_id=?", job.key).fetchone():
                self.conn.execute("UPDATE jobs SET last_seen=? WHERE ats=? AND company=? AND job_id=?", (ts, *job.key))
                continue
            sig = (_norm(job.company), _norm(job.title))
            if sig in seen:
                dupes += 1
                continue
            seen.add(sig)
            self.conn.execute(
                """INSERT INTO jobs (ats, company, job_id, title, location, department, remote,
                                     url, apply_url, posted_at, first_seen, last_seen, matched, is_new, raw)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job.ats, job.company, job.job_id, job.title, job.location, job.department, int(job.remote),
                 job.url, job.apply_url, job.posted_at, ts, ts, int(is_match(job)), int(seeded), json.dumps(job.raw)))
            if seeded:
                new.append(job)
        self.conn.execute(
            """INSERT INTO boards (ats, company, seeded_at, last_ok_at) VALUES (?, ?, ?, ?)
               ON CONFLICT (ats, company) DO UPDATE SET seeded_at=COALESCE(boards.seeded_at, excluded.seeded_at),
                 last_ok_at=excluded.last_ok_at, last_error=NULL""", (source, search, ts, ts))
        self.conn.commit()
        return new, dupes

    def get_state(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value=excluded.value",
                          (key, value))
        self.conn.commit()

    def jobs_to_score(self, limit: int = 20) -> list[Job]:
        """New matching jobs the worker hasn't handled yet."""
        rows = self.conn.execute(
            "SELECT rowid, * FROM jobs WHERE matched=1 AND is_new=1 AND notified_at IS NULL"
            " AND closed_at IS NULL AND score IS NULL AND status='discovered' ORDER BY first_seen LIMIT ?",
            (limit,),
        ).fetchall()
        return [row_to_job(r) for r in rows]

    def set_score(self, rowid: int, score: int, reasons: list[str]) -> None:
        self.conn.execute("UPDATE jobs SET score=?, score_reasons=? WHERE rowid=?",
                          (score, json.dumps(reasons), rowid))
        self.conn.commit()

    def update_tailored_data(self, rowid: int, data: dict, note: str) -> None:
        rec = self.get_tailored(rowid)
        warnings = [w for w in rec["warnings"] if w != note] + [note]
        self.conn.execute("UPDATE tailored SET data=?, warnings=? WHERE job_rowid=?",
                          (json.dumps(data), json.dumps(warnings), rowid))
        self.conn.commit()

    def get_job(self, rowid: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT rowid, * FROM jobs WHERE rowid=?", (rowid,)).fetchone()

    def set_status(self, rowid: int, status: str, error: str | None = None) -> None:
        self.conn.execute("UPDATE jobs SET status=?, last_error=? WHERE rowid=?", (status, error, rowid))
        self.conn.commit()

    def list_jobs(self, status: str | None = None, matched_only: bool = True,
                  limit: int = 200) -> list[sqlite3.Row]:
        where, args = ["1=1"], []
        if status:
            where.append("status=?")
            args.append(status)
        if matched_only:
            where.append("matched=1")
        return self.conn.execute(
            f"SELECT rowid, * FROM jobs WHERE {' AND '.join(where)} ORDER BY first_seen DESC LIMIT ?",
            (*args, limit),
        ).fetchall()

    def status_counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM jobs WHERE matched=1 GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def save_tailored(self, rowid: int, *, model: str, description: str, data: dict,
                      warnings: list[str], usage: dict) -> None:
        self.conn.execute(
            """INSERT INTO tailored (job_rowid, created_at, model, description, data, warnings, usage)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (job_rowid) DO UPDATE SET created_at=excluded.created_at, model=excluded.model,
                 description=excluded.description, data=excluded.data, warnings=excluded.warnings,
                 usage=excluded.usage""",
            (rowid, now(), model, description, json.dumps(data), json.dumps(warnings), json.dumps(usage)),
        )
        self.conn.commit()

    def get_tailored(self, rowid: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM tailored WHERE job_rowid=?", (rowid,)).fetchone()
        if not r:
            return None
        return {
            "created_at": r["created_at"], "model": r["model"], "description": r["description"],
            "data": json.loads(r["data"]), "warnings": json.loads(r["warnings"] or "[]"),
            "usage": json.loads(r["usage"] or "{}"),
        }


def row_to_job(r: sqlite3.Row) -> Job:
    return Job(ats=r["ats"], company=r["company"], job_id=r["job_id"], title=r["title"],
               location=r["location"] or "", url=r["url"] or "", apply_url=r["apply_url"] or "",
               department=r["department"] or "", remote=bool(r["remote"]),
               posted_at=r["posted_at"] or "", raw=json.loads(r["raw"] or "{}"), db_id=r["rowid"])
