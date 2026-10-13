import asyncio
import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import httpx
import httpx2

from apply.fields import Field, answer_known, classify, country_for
from apply.main import ApplyQueue
from apply.runner import apply_job, get_application
from profile_mcp.profile import Profile
from tailor.pipeline import tailor_job
from watcher.db import JobStore
from watcher.notify import Notifier

from tests.helpers import fake_claude, store_with_job

FORMS = Path(__file__).parent / "fixtures" / "forms"
CHROMIUM = os.environ.get("CHROMIUM_PATH") or next(
    (str(p) for p in sorted(Path("/opt/pw-browsers").glob("chromium-*/chrome-linux*/chrome"))), "")
HAS_BROWSER = bool(CHROMIUM) and os.path.exists(CHROMIUM)

BASE = Profile.load()
ANSWERS = {"legal": {"authorized_to_work": {"united states": "No", "canada": "Yes"},
                     "requires_sponsorship": {"united states": "Yes", "canada": "No"}, "eeo": "decline"},
           "notice_period": "2 weeks", "salary_expectation": ""}
PROFILE = Profile(BASE.data, ANSWERS)


def claude_answers(calls=None):
    """Fake Claude for form questions: answers every id it's asked, first option for choices."""
    def handler(request):
        body = json.loads(request.content)
        if calls is not None:
            calls.append(body)
        text = body["messages"][0]["content"]
        answers = []
        for m in re.finditer(r"- id=(ja\d+) \([^)]*\): (.*?)(?:\n  options: (\[.*?\]))?(?=\n- id=|\Z)", text, re.S):
            opts = eval(m.group(3)) if m.group(3) else None  # noqa: S307 - test fixture
            answers.append({"field_id": m.group(1), "can_answer": True,
                            "answer": opts[0] if opts else "I build Python services and love Acme's product."})
        return httpx2.Response(200, json={
            "id": "m", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "stop_reason": "end_turn",
            "stop_sequence": None, "content": [{"type": "text", "text": json.dumps({"answers": answers})}],
            "usage": {"input_tokens": 1, "output_tokens": 1}})
    return anthropic.Anthropic(api_key="t", max_retries=0,
                               http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))


class FieldTests(unittest.TestCase):
    def f(self, label, type="text", options=(), required=True):
        return classify(Field("x", label, type, required, list(options)))

    def test_classify(self):
        cases = {"First Name *": "first_name", "Resume/CV": "resume", "LinkedIn Profile": "linkedin",
                 "Are you legally authorized to work in Canada?": "work_auth",
                 "Will you require visa sponsorship?": "sponsorship", "Veteran status": "eeo",
                 "Full name": "full_name", "Desired salary": "salary"}
        for label, kind in cases.items():
            self.assertEqual(self.f(label).kind, kind, label)
        self.assertEqual(self.f("Tell us about a project", "textarea").kind, "essay")

    def test_legal_answers_come_only_from_saved_answers(self):
        f = self.f("Are you legally authorized to work in the United States?", "select", ["Yes", "No"])
        self.assertEqual(answer_known(f, PROFILE.basics, ANSWERS, "", {})[0], "No")
        f = self.f("Are you authorized to work in this country?", "select", ["Yes", "No"])
        self.assertEqual(answer_known(f, PROFILE.basics, ANSWERS, "Toronto, Canada", {})[0], "Yes")
        value, why = answer_known(f, PROFILE.basics, ANSWERS, "Berlin", {})   # country unknown, answers differ
        self.assertIsNone(value)
        self.assertIn("legal", why)
        self.assertIsNone(answer_known(f, PROFILE.basics, {}, "Toronto, Canada", {})[0])  # nothing saved

    def test_eeo_declines(self):
        f = self.f("Gender", "select", ["Male", "Female", "I don't wish to answer"], required=False)
        self.assertEqual(answer_known(f, PROFILE.basics, ANSWERS, "", {})[0], "I don't wish to answer")

    def test_country_aliases(self):
        self.assertEqual(country_for("", "Remote - US"), "united states")


@unittest.skipUnless(HAS_BROWSER, "Chromium not available")
class RunnerTests(unittest.TestCase):
    def setUp(self):
        os.environ["CHROMIUM_PATH"] = CHROMIUM
        self.tmp = tempfile.TemporaryDirectory()
        self.store, self.rowid = store_with_job()
        tailor_job(self.store, self.rowid, PROFILE, client=fake_claude())
        self.store.set_status(self.rowid, "approved")

    def tearDown(self):
        self.tmp.cleanup()

    def run_form(self, name, **kw):
        url = (FORMS / name).as_uri()
        self.store.conn.execute("UPDATE jobs SET apply_url=? WHERE rowid=?", (url, self.rowid))
        self.store.conn.commit()
        return apply_job(self.store, self.rowid, PROFILE, model="m", out_root=Path(self.tmp.name),
                         claude=kw.pop("claude", claude_answers()), **kw)

    def test_essay_waits_for_review_then_submits(self):
        r = self.run_form("greenhouse.html")
        self.assertEqual(r["outcome"], "needs_review", r["reasons"])
        by_label = {a["label"]: a for a in r["answers"]}
        self.assertEqual(by_label["First Name *"]["answer"], "Alex")
        self.assertEqual(by_label["Are you legally authorized to work in the United States? *"]["answer"], "No")
        self.assertEqual(by_label["Gender"]["answer"], "Decline to self-identify")
        self.assertEqual(by_label["Why do you want to work at Acme? *"]["source"], "claude")
        self.assertTrue(os.path.exists(r["screenshot"]))
        self.assertTrue(os.path.exists(r["files"]["resume"]))
        self.assertEqual(self.store.get_job(self.rowid)["status"], "needs_review")

        approved = {a["key"]: a["answer"] for a in get_application(self.store, self.rowid)["answers"]}
        r = self.run_form("greenhouse.html", approved_answers=approved)
        self.assertEqual(r["outcome"], "submitted", r["reasons"])
        job = self.store.get_job(self.rowid)
        self.assertEqual((job["status"], job["app_status"]), ("submitted", "applied"))

    def test_no_free_text_submits_on_approve(self):
        r = self.run_form("lever.html")
        self.assertEqual(r["outcome"], "submitted", r["reasons"])
        by_label = {a["label"]: a["answer"] for a in r["answers"]}
        self.assertEqual(by_label["Full name *"], "Alex Placeholder")
        self.assertEqual(by_label["Current company"], "Example Corp")

    def test_captcha_goes_to_the_user(self):
        r = self.run_form("captcha.html")
        self.assertEqual(r["outcome"], "needs_you")
        self.assertIn("CAPTCHA", r["reasons"][0])

    def test_missing_saved_answer_goes_to_the_user(self):
        calls = []
        r = self.run_form("salary.html", claude=claude_answers(calls))
        self.assertEqual(r["outcome"], "needs_you")
        self.assertIn("salary", r["reasons"][0])
        self.assertEqual(calls, [])                       # salary never goes to Claude

    def test_unconfirmed_submit_is_a_failure(self):
        r = self.run_form("broken.html")
        self.assertEqual(r["outcome"], "failed")
        self.assertEqual(self.store.get_job(self.rowid)["status"], "apply_failed")

    def test_dry_run_never_submits(self):
        r = self.run_form("lever.html", dry_run=True)
        self.assertEqual(r["outcome"], "needs_review")
        self.assertIn("Dry run", r["reasons"][0])


class QueueTests(unittest.TestCase):
    CFG = {"apply": {"max_per_day": 2, "min_gap_minutes": 5, "max_gap_minutes": 5, "active_hours": [8, 22],
                     "timezone": "UTC"}}

    def setUp(self):
        self.store, self.rowid = store_with_job()
        self.store.set_status(self.rowid, "approved")
        self.calls, self.sent = [], []

    def fake_apply(self, outcome):
        def fn(store, rowid, profile, **kw):
            self.calls.append(kw)
            from apply.runner import _save
            return _save(store, rowid, outcome, [{"label": "Why?", "type": "textarea", "key": "k", "answer": "A",
                                                  "source": "claude"}], ["reason"], None)
        return fn

    def queue(self, outcome="submitted"):
        async def handler(r):
            return httpx.Response(200, json={"ok": True})
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: self.sent.append(r) or httpx.Response(200, json={"ok": True})))
        n = Notifier(client)
        n.token, n.chat_id, n.dry_run = "T", "42", False
        return ApplyQueue(self.store, n, self.CFG, profile_loader=lambda: PROFILE, apply_fn=self.fake_apply(outcome),
                          out_root=Path("/tmp"))

    def at(self, hour, minute=0):
        return datetime(2026, 10, 9, hour, minute, tzinfo=timezone.utc)

    def test_pacing_hours_gap_and_cap(self):
        q = self.queue()
        self.assertIsNone(asyncio.run(q.run_once(self.at(6))))           # too early
        self.assertEqual(asyncio.run(q.run_once(self.at(9)))["outcome"], "submitted")
        # second job
        self.store.conn.execute("UPDATE jobs SET status='approved' WHERE rowid=?", (self.rowid,))
        self.assertIsNone(asyncio.run(q.run_once(self.at(9, 2))))        # inside the 5-minute gap
        self.assertIsNotNone(asyncio.run(q.run_once(self.at(9, 6))))
        self.store.conn.execute("INSERT INTO applications (job_rowid, outcome, submitted_at) VALUES (999, 'submitted', ?)",
                                (self.at(9, 30).isoformat(),))          # another job applied today
        self.store.conn.execute("UPDATE jobs SET status='approved' WHERE rowid=?", (self.rowid,))
        self.assertIsNone(asyncio.run(q.run_once(self.at(12))))          # daily cap of 2 reached

    def test_review_message_and_resubmit_uses_approved_answers(self):
        q = self.queue("needs_review")
        asyncio.run(q.run_once(self.at(9)))
        msg = json.loads(self.sent[-1].content)
        self.assertIn("Ready to submit", msg["text"])
        self.assertEqual(msg["reply_markup"]["inline_keyboard"][0][0]["callback_data"], f"submit:{self.rowid}")
        self.store.set_status(self.rowid, "submit_requested")
        q.apply_fn = self.fake_apply("submitted")
        asyncio.run(q.run_once(self.at(10)))
        self.assertEqual(self.calls[-1]["approved_answers"], {"k": "A"})

    def test_needs_you_message_has_done_button(self):
        q = self.queue("needs_you")
        asyncio.run(q.run_once(self.at(9)))
        msg = json.loads(self.sent[-1].content)
        datas = [b.get("callback_data") for row in msg["reply_markup"]["inline_keyboard"] for b in row]
        self.assertIn(f"done:{self.rowid}", datas)


if __name__ == "__main__":
    unittest.main()
