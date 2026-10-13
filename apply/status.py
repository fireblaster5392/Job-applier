"""Status changes the user makes by hand."""
from watcher.db import JobStore, now


def mark_submitted_by_user(store: JobStore, rowid: int) -> None:
    """The user applied themselves (CAPTCHA, odd form). Counts as submitted, so outreach starts."""
    store.conn.execute(
        """INSERT INTO applications (job_rowid, outcome, attempts, submitted_at) VALUES (?, 'submitted', 0, ?)
           ON CONFLICT (job_rowid) DO UPDATE SET outcome='submitted', submitted_at=excluded.submitted_at""",
        (rowid, now()))
    store.conn.execute("UPDATE jobs SET status='submitted', app_status=COALESCE(app_status, 'applied') WHERE rowid=?",
                       (rowid,))
    store.conn.commit()
