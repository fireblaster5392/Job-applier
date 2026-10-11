"""Interview prep sheets: drafting, the worker trigger, and the dashboard page."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from prep.sheet import build_prep, get_prep, jobs_needing_prep, prep_html
from profile_mcp.profile import Profile
from tailor.pipeline import tailor_job
from watcher.notify import Notifier
from web.app import create_app
from worker.main import Worker
from tests.helpers import fake_claude, store_with_job

PROFILE = Profile.load()


def notifier(client):
    n = Notifier(client, dry_run=True)
    n.token, n.chat_id, n.dry_run = "t", "1", False
    return n
SHEET = {
    "company_notes": ["Acme sells billing software to SaaS companies."],
    "role_summary": "Build and run Python billing services.",
    "interview_details": ["45-minute call with the hiring manager"],
    "why_you": ["Python APIs at scale -> exco-api: 2M requests a day"],
    "likely_questions": [{"question": "Tell me about a system you scaled.", "why_they_ask": "Ownership at scale",
                          "answer_outline": "- billing API\n- 2M requests a day", "use": ["exco-api"]}],
    "gaps": ["No Kubernetes in production: mention the homelab cluster"],
    "questions_to_ask": ["What does on-call look like?"],
    "sources": ["https://acme.example/news"],
}


def interview(store, rowid):
    store.conn.execute("UPDATE jobs SET app_status='interview' WHERE rowid=?", (rowid,))
    store.conn.execute("INSERT INTO inbox (rfc_message_id, kind, job_rowid, from_addr, subject, snippet, category, "
                       "received_at, created_at) VALUES ('m1', 'application', ?, 'jo@acme.com', 'Interview', "
                       "'Can you do Tuesday 2pm with Jo?', 'interview_request', 't', 't')", (rowid,))
    store.conn.commit()


class PrepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "jobs.db")
        self.store, self.rowid = store_with_job(self.db)
        tailor_job(self.store, self.rowid, PROFILE, client=fake_claude())

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_uses_sources_and_saves(self):
        interview(self.store, self.rowid)
        calls = []
        stories = Path(self.tmp.name, "stories")
        stories.mkdir()
        (stories / "billing-outage.md").write_text("S: billing outage. A: led the fix. R: 30 min.")
        rec = build_prep(self.store, self.rowid, PROFILE, client=fake_claude(SHEET, calls=calls), stories_dir=stories)
        self.assertEqual(rec["data"].role_summary, SHEET["role_summary"])
        prompt = calls[0]["messages"][0]["content"]
        self.assertIn("Tuesday 2pm", prompt)                 # the interview email
        self.assertIn("[exco-api]", prompt)                  # what was sent
        self.assertIn("billing-outage", calls[0]["system"][0]["text"])   # stories
        page = prep_html(self.store, self.rowid, PROFILE)
        self.assertIn("exco-api (Example Corp)", page)
        self.assertEqual(jobs_needing_prep(self.store), [])

    def test_worker_drafts_on_interview_and_sends(self):
        interview(self.store, self.rowid)
        sent = []

        def tg(request):
            sent.append(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(tg)) as client:
                w = Worker(self.store, notifier(client),
                           {"networking": {"enabled": False}, "outreach": {"enabled": False},
                            "prep": {"output_dir": os.path.join(self.tmp.name, "prep")}},
                           profile_loader=lambda: PROFILE, claude=fake_claude(SHEET), dashboard_url="http://d")
                return await w.run_once()
        stats = asyncio.run(go())
        self.assertEqual(stats["prep"], 1)
        self.assertIn("sendDocument", sent)
        self.assertIsNotNone(get_prep(self.store, self.rowid))
        self.assertEqual(asyncio.run(go())["prep"], 0)        # not drafted twice

    def test_worker_gives_up_after_three_failures(self):
        interview(self.store, self.rowid)

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda r: httpx.Response(200, json={"ok": True, "result": {}}))) as client:
                w = Worker(self.store, notifier(client),
                           {"networking": {"enabled": False}, "outreach": {"enabled": False}},
                           profile_loader=lambda: PROFILE, claude=fake_claude(status=500))
                return await w.run_once()
        for _ in range(4):
            asyncio.run(go())
        self.assertEqual(self.store.get_state(f"prep_failures:{self.rowid}"), "3")

    def test_dashboard_button_and_page(self):
        import web.app as webapp
        original = webapp.build_prep
        webapp.build_prep = lambda store, rowid, profile, model: build_prep(store, rowid, profile, model=model,
                                                                            client=fake_claude(SHEET))
        try:
            client = TestClient(create_app(self.db, profile_loader=lambda: PROFILE))
            self.assertEqual(client.get(f"/jobs/{self.rowid}/prep").status_code, 404)
            client.post(f"/jobs/{self.rowid}/prep")
            r = client.get(f"/jobs/{self.rowid}/prep")
            self.assertEqual(r.status_code, 200)
            self.assertIn("What does on-call look like?", r.text)
            self.assertIn("Interview prep", client.get(f"/jobs/{self.rowid}").text)
        finally:
            webapp.build_prep = original


if __name__ == "__main__":
    unittest.main()
