import asyncio
import json
import os
import tempfile
import unittest

import httpx
from fastapi.testclient import TestClient

from bot.main import Bot
from profile_mcp.profile import Profile
from tailor.pipeline import tailor_job
from watcher.db import JobStore
from web.app import create_app

from tests.helpers import fake_claude, store_with_job

PROFILE = Profile.load()


def fake_tailor(store, rowid, profile, model=None):
    return tailor_job(store, rowid, profile, model=model, client=fake_claude())


class BotTests(unittest.TestCase):
    def setUp(self):
        self.store, self.rowid = store_with_job()
        self.calls = []

    def run_bot(self, *callbacks):
        def handler(request):
            method = request.url.path.rsplit("/", 1)[-1]
            self.calls.append((method, json.loads(request.content)))
            return httpx.Response(200, json={"ok": True, "result": True})

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                bot = Bot(self.store, c, token="T", chat_id="42", profile_loader=lambda: PROFILE,
                          dashboard_url="http://dash", tailor=fake_tailor)
                for data, chat in callbacks:
                    await bot.on_callback({"id": "cb", "data": data,
                                           "message": {"message_id": 7, "chat": {"id": chat}}})
                await asyncio.gather(*bot.tasks)
        asyncio.run(go())

    def test_tailor_then_approve(self):
        self.run_bot((f"tailor:{self.rowid}", 42))
        self.assertEqual(self.store.get_job(self.rowid)["status"], "tailored")
        sent = [p for m, p in self.calls if m == "sendMessage"][-1]
        self.assertIn("82/100", sent["text"])
        buttons = [b for row in sent["reply_markup"]["inline_keyboard"] for b in row]
        self.assertIn(f"approve:{self.rowid}", [b.get("callback_data") for b in buttons])
        self.assertIn(f"http://dash/jobs/{self.rowid}", [b.get("url") for b in buttons])

        self.run_bot((f"approve:{self.rowid}", 42))
        self.assertEqual(self.store.get_job(self.rowid)["status"], "approved")

    def test_skip_and_ignores_other_chats(self):
        self.run_bot((f"skip:{self.rowid}", 999))
        self.assertEqual(self.store.get_job(self.rowid)["status"], "discovered")
        self.run_bot((f"skip:{self.rowid}", 42))
        self.assertEqual(self.store.get_job(self.rowid)["status"], "skipped")


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "jobs.db")
        _, self.rowid = store_with_job(self.db)
        self.client = TestClient(create_app(self.db, profile_loader=lambda: PROFILE, tailor=fake_tailor))

    def tearDown(self):
        self.tmp.cleanup()

    def test_pages_and_flow(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Backend Engineer", r.text)

        self.assertEqual(self.client.get(f"/jobs/{self.rowid}/resume").status_code, 404)
        r = self.client.post(f"/jobs/{self.rowid}/tailor")   # background task runs before TestClient returns
        self.assertEqual(r.status_code, 200)
        self.assertIn("82/100", r.text)
        self.assertIn("Guardrail fixes", r.text)

        r = self.client.get(f"/jobs/{self.rowid}/resume")
        self.assertIn("Alex Placeholder", r.text)
        self.assertIn("Dear acme hiring team", self.client.get(f"/jobs/{self.rowid}/cover-letter").text)

        self.client.post(f"/jobs/{self.rowid}/status", data={"status": "approved"})
        self.assertEqual(JobStore(self.db).get_job(self.rowid)["status"], "approved")
        self.assertIn("approved", self.client.get("/?status=approved").text)

    def test_edit(self):
        self.assertEqual(self.client.get(f"/jobs/{self.rowid}/edit").status_code, 200)  # redirects to job page
        self.client.post(f"/jobs/{self.rowid}/tailor")
        page = self.client.get(f"/jobs/{self.rowid}/edit").text
        self.assertIn("bullet_exco-api", page)
        self.client.post(f"/jobs/{self.rowid}/edit", data={
            "summary": "My own summary.", "skills": "Python, Go", "cover_letter": "Para one.\r\n\r\nPara two.",
            "bullet_exco-api": "Built a billing API (my wording).", "bullet_exco-pipeline": "", "bullet_proj-watcher": "Kept."})
        rec = JobStore(self.db).get_tailored(self.rowid)
        self.assertEqual(rec["data"]["summary"], "My own summary.")
        self.assertEqual([b["id"] for b in rec["data"]["bullets"]], ["exco-api", "proj-watcher"])
        self.assertEqual(rec["data"]["skills"], ["Python", "Go"])
        self.assertIn("You edited this draft.", rec["warnings"])
        self.assertIn("my wording", self.client.get(f"/jobs/{self.rowid}/resume").text)
        self.assertEqual(self.client.get(f"/jobs/{self.rowid}/cover-letter").text.count("Para "), 2)

    def test_rejects_unknown_status(self):
        r = self.client.post(f"/jobs/{self.rowid}/status", data={"status": "submitted"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
