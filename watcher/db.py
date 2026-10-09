"""SQLite job store. Plain SQL with portable types so it moves to Postgres later."""
import json
import sqlite3
from datetime import datetime, timezone

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
    raw          TEXT,
    PRIMARY KEY (ats, company, job_id)
);

CREATE INDEX IF NOT EXISTS jobs_first_seen ON jobs (first_seen);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobStore:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

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
        self.conn.execute(
            "UPDATE jobs SET notified_at=? WHERE ats=? AND company=? AND job_id=?",
            (now(), *job.key),
        )
        self.conn.commit()

    def pending_notifications(self) -> list[Job]:
        """New matching jobs not yet notified, including ones whose send failed earlier."""
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE matched=1 AND is_new=1 AND notified_at IS NULL"
            " AND closed_at IS NULL ORDER BY first_seen"
        ).fetchall()
        return [
            Job(ats=r["ats"], company=r["company"], job_id=r["job_id"], title=r["title"],
                location=r["location"] or "", url=r["url"] or "", apply_url=r["apply_url"] or "",
                department=r["department"] or "", remote=bool(r["remote"]),
                posted_at=r["posted_at"] or "")
            for r in rows
        ]
