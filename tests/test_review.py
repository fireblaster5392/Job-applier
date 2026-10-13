"""Weekly review: counts, all-time groupings, suggestions, schedule, and the dashboard page."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from review.weekly import compute, due, format_telegram, mark_sent, suggestions, week_window
from watcher.db import JobStore, now
from watcher.models import Job
from web.app import create_app

TZ = ZoneInfo("America/Edmonton")


def seed(store: JobStore):
    """30 applied jobs: 90+ scores from greenhouse get interviews, 70-79 from indeed get nothing."""
    ts = now()
    for i in range(30):
        high = i < 15
        ats = "greenhouse" if high else "indeed"
        title = "Backend Engineer" if high else "Frontend Engineer"
        store.conn.execute(
            "INSERT INTO jobs (ats, company, job_id, title, first_seen, last_seen, matched, is_new, status, score, "
            "app_status) VALUES (?, 'acme', ?, ?, ?, ?, 1, 1, 'submitted', ?, ?)",
            (ats, str(i), title, ts, ts, 92 if high else 72, ("interview" if i % 3 == 0 else "applied") if high else "applied"))
        store.conn.execute("INSERT INTO applications (job_rowid, outcome, submitted_at, last_attempt_at, attempts) "
                           "VALUES (?, 'submitted', ?, ?, 1)", (i + 1, ts, ts))
    for i in range(10):    # 10 emails to recruiters, no replies
        store.conn.execute("INSERT INTO contacts (id, kind, company, name, rank, created_at) VALUES (?, 'job', 'acme', 'R', 1, ?)",
                           (i + 1, ts))
        store.conn.execute("INSERT INTO messages (contact_id, channel, body, status, sent_at, created_at) "
                           "VALUES (?, 'email', 'hi', 'sent', ?, ?)", (i + 1, ts, ts))
    store.conn.execute("INSERT INTO applications (job_rowid, outcome, reasons, last_attempt_at, attempts) VALUES "
                       "(99, 'needs_you', ?, ?, 1)", (json.dumps(["“Salary expectation”: no saved answer"]), ts))
    store.conn.commit()


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.store = JobStore(":memory:")
        seed(self.store)
        until = datetime.now(timezone.utc) + timedelta(minutes=1)
        self.stats = compute(self.store, until - timedelta(days=7), until)

    def test_counts_and_groups(self):
        w = self.stats["week"]
        self.assertEqual((w["submitted"], w["emails_sent"], w["needs_you"]), (30, 10, 1))
        self.assertEqual(self.stats["by_band"]["90+"], {"applied": 15, "positive": 5, "rejected": 0})
        self.assertEqual(self.stats["by_source"]["indeed"]["positive"], 0)
        self.assertEqual(self.stats["by_role"]["backend"]["positive"], 5)
        self.assertEqual(self.stats["outreach"], {"job: recruiters": {"sent": 10, "replied": 0}})
        self.assertEqual(self.stats["stuck_reasons"], {"Salary expectation": 1})

    def test_suggestions(self):
        tips = "\n".join(suggestions(self.stats, {"pipeline": {"score_threshold": 70}}))
        self.assertIn("score_threshold = 80", tips)
        self.assertIn("found on indeed", tips)
        self.assertIn("'backend' roles respond well", tips)
        self.assertIn("Emails to job: recruiters have no replies", tips)
        text = format_telegram(self.stats, suggestions(self.stats, {}))
        self.assertIn("Applied to 30", text)
        self.assertIn("5/30 (17%)", text)

    def test_no_suggestions_on_little_data(self):
        empty = compute(JobStore(":memory:"), datetime.now(timezone.utc) - timedelta(days=7), datetime.now(timezone.utc))
        self.assertEqual(suggestions(empty, {}), ["No new matching jobs this week; widen title_include, locations, "
                                                  "or add companies and searches."])

    def test_schedule_once_a_week(self):
        cfg = {"review": {"weekday": 6, "hour": 18, "timezone": "America/Edmonton"}}
        sunday_7pm = datetime(2026, 10, 11, 19, tzinfo=TZ)
        self.assertFalse(due(self.store, cfg, sunday_7pm - timedelta(hours=3)))
        self.assertTrue(due(self.store, cfg, sunday_7pm))
        mark_sent(self.store, cfg, sunday_7pm)
        self.assertFalse(due(self.store, cfg, sunday_7pm + timedelta(hours=1)))
        self.assertTrue(due(self.store, cfg, sunday_7pm + timedelta(days=7)))
        since, until = week_window(sunday_7pm, TZ)
        self.assertEqual((until - since).days, 7)

    def test_dashboard_page(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "jobs.db")
            seed(JobStore(db))
            r = TestClient(create_app(db, cfg={"pipeline": {"score_threshold": 70}})).get("/review")
            self.assertEqual(r.status_code, 200)
            self.assertIn("score_threshold = 80", r.text)
            self.assertIn("Email to job: recruiters", r.text)


if __name__ == "__main__":
    unittest.main()
