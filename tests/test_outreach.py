import asyncio
import json
import os
import unittest
from unittest import mock

import anthropic
import httpx
import httpx2

from bot.main import Bot
from outreach.draft import draft_messages
from outreach.finder import Contact, find_contacts, parse_brave
from outreach.store import emails_sent_today, get_message, save_outreach, send_email_message
from profile_mcp.profile import Profile
from watcher.notify import Notifier
from worker.main import Worker

from tests.helpers import store_with_job

PROFILE = Profile.load()

BRAVE = {"web": {"results": [
    {"title": "Jane Doe - Engineering Manager - Acme | LinkedIn", "url": "https://ca.linkedin.com/in/janedoe?trk=x",
     "description": "Engineering Manager at Acme."},
    {"title": "Raj Patel - Technical Recruiter - Acme | LinkedIn", "url": "https://www.linkedin.com/in/rajp",
     "description": "Recruiting at Acme"},
    {"title": "Old Person - Engineer - OtherCo | LinkedIn", "url": "https://www.linkedin.com/in/old",
     "description": "Previously somewhere"},
    {"title": "Acme | LinkedIn", "url": "https://www.linkedin.com/company/acme", "description": ""},
]}}


def apollo_brave_handler(request):
    url = str(request.url)
    if "brave.com" in url:
        return httpx.Response(200, json=BRAVE)
    if url.endswith("/mixed_companies/search"):
        return httpx.Response(200, json={"organizations": [{"id": "org1"}]})
    if url.endswith("/mixed_people/search"):
        return httpx.Response(200, json={"people": [
            {"id": "p1", "name": "Jane Doe", "title": "Engineering Manager",
             "linkedin_url": "http://www.linkedin.com/in/janedoe", "email": "email_not_unlocked@domain.com"},
            {"id": "p2", "name": "Sam Lee", "title": "Senior Software Engineer", "linkedin_url": "", "email": None},
        ]})
    if url.endswith("/people/match"):
        pid = json.loads(request.content)["id"]
        return httpx.Response(200, json={"person": {"email": f"{pid}@acme.com"}})
    return httpx.Response(404)


def claude_drafts(n, long_note=False):
    def handler(request):
        drafts = [{"contact_index": i, "linkedin_note": ("x " * 200) if long_note else f"Hi {i}",
                   "email_subject": "Backend role", "email_body": f"Body {i}"} for i in range(n)]
        return httpx2.Response(200, json={
            "id": "m", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "stop_reason": "end_turn",
            "stop_sequence": None, "content": [{"type": "text", "text": json.dumps({"drafts": drafts})}],
            "usage": {"input_tokens": 1, "output_tokens": 1}})
    return anthropic.Anthropic(api_key="t", max_retries=0,
                               http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))


class FakeMailer:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, to, subject, body, in_reply_to=None):
        if self.fail:
            raise OSError("smtp down")
        self.sent.append((to, subject, body))
        return f"<{len(self.sent)}@test>"


class FinderTests(unittest.TestCase):
    def test_parse_brave_filters_and_ranks(self):
        got = parse_brave(BRAVE, "Acme")
        self.assertEqual([(c.name, c.rank) for c in got], [("Jane Doe", 3), ("Raj Patel", 1)])
        self.assertEqual(got[0].linkedin_url, "https://www.linkedin.com/in/janedoe")

    def test_find_contacts_merges_sources_and_reveals_emails(self):
        env = {"BRAVE_API_KEY": "b", "APOLLO_API_KEY": "a"}
        with mock.patch.dict(os.environ, env):
            client = httpx.Client(transport=httpx.MockTransport(apollo_brave_handler))
            got = find_contacts("Acme", limit=3, client=client)
        self.assertEqual([c.name for c in got], ["Jane Doe", "Sam Lee", "Raj Patel"])
        self.assertEqual(got[0].email, "p1@acme.com")          # Apollo email joined Brave's profile
        self.assertEqual(got[0].source, "apollo,brave")

    def test_no_keys_no_contacts(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(find_contacts("Acme"), [])


class DraftAndSendTests(unittest.TestCase):
    def test_linkedin_note_is_trimmed_to_limit(self):
        contacts = [Contact("Jane", "EM", "Acme", linkedin_url="u")]
        d = draft_messages({"title": "SWE", "company": "Acme"}, contacts, PROFILE, model="claude-opus-5-5",
                           client=claude_drafts(1, long_note=True))
        self.assertLessEqual(len(d[0].linkedin_note), 300)

    def test_send_respects_cap_and_never_double_sends(self):
        store, rowid = store_with_job()
        contacts = [Contact(f"P{i}", "EM", "Acme", email=f"p{i}@acme.com") for i in range(2)]
        drafts = draft_messages({"title": "SWE", "company": "Acme"}, contacts, PROFILE, model="m",
                                client=claude_drafts(2))
        saved = save_outreach(store, rowid, contacts, drafts)
        mailer = FakeMailer()
        first, second = saved[0]["email_msg_id"], saved[1]["email_msg_id"]
        self.assertTrue(send_email_message(store, first, mailer, daily_cap=1).startswith("Sent"))
        self.assertEqual(send_email_message(store, first, mailer, daily_cap=5), "Already sent.")
        self.assertIn("cap", send_email_message(store, second, mailer, daily_cap=1))
        self.assertEqual(len(mailer.sent), 1)
        self.assertEqual(emails_sent_today(store), 1)

    def test_smtp_failure_is_recorded_and_retryable(self):
        store, rowid = store_with_job()
        contacts = [Contact("P", "EM", "Acme", email="p@acme.com")]
        saved = save_outreach(store, rowid, contacts, draft_messages(
            {"title": "SWE", "company": "Acme"}, contacts, PROFILE, model="m", client=claude_drafts(1)))
        mid = saved[0]["email_msg_id"]
        self.assertIn("smtp down", send_email_message(store, mid, FakeMailer(fail=True), 10))
        self.assertEqual(get_message(store, mid)["status"], "failed")
        self.assertTrue(send_email_message(store, mid, FakeMailer(), 10).startswith("Sent"))


class WorkerOutreachTests(unittest.TestCase):
    def test_approved_job_gets_contacts_and_drafts_but_sends_nothing(self):
        store, rowid = store_with_job()
        store.set_score(rowid, 85, [])
        store.mark_notified(store.pending_notifications()[0])
        store.set_status(rowid, "approved")
        sent = []

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda r: sent.append(json.loads(r.content)) or httpx.Response(200, json={"ok": True}))) as c:
                n = Notifier(c)
                n.token, n.chat_id, n.dry_run = "T", "42", False
                w = Worker(store, n, {"networking": {"enabled": False}}, profile_loader=lambda: PROFILE, claude=claude_drafts(2))
                w.find_contacts = lambda company, hint, limit: [
                    Contact("Jane Doe", "Engineering Manager", company, linkedin_url="https://www.linkedin.com/in/j",
                            email="jane@acme.com", rank=3),
                    Contact("Raj", "Recruiter", company, linkedin_url="https://www.linkedin.com/in/r", rank=1)]
                return await w.run_once(), w
        stats, w = asyncio.run(go())
        self.assertEqual(stats["outreach"], 1)
        texts = [m["text"] for m in sent]
        self.assertEqual(len(texts), 4)   # header, Jane LinkedIn, Jane email, Raj LinkedIn
        self.assertIn("<code>Hi 0</code>", texts[1])
        email_buttons = sent[2]["reply_markup"]["inline_keyboard"][0]
        self.assertTrue(email_buttons[0]["callback_data"].startswith("email:"))
        # Nothing emailed, and the job isn't handled twice
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM messages WHERE status='sent'").fetchone()[0], 0)
        self.assertEqual(asyncio.run(w.run_once())["outreach"], 0)


class BotEmailTests(unittest.TestCase):
    def test_send_button_sends_once(self):
        store, rowid = store_with_job()
        contacts = [Contact("P", "EM", "Acme", email="p@acme.com")]
        saved = save_outreach(store, rowid, contacts, draft_messages(
            {"title": "SWE", "company": "Acme"}, contacts, PROFILE, model="m", client=claude_drafts(1)))
        mid = saved[0]["email_msg_id"]
        mailer, calls = FakeMailer(), []

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda r: calls.append(r) or httpx.Response(200, json={"ok": True}))) as c:
                bot = Bot(store, c, token="T", chat_id="42", mailer=mailer)
                for _ in range(2):
                    await bot.on_callback({"id": "cb", "data": f"email:{mid}",
                                           "message": {"message_id": 3, "chat": {"id": 42}}})
        asyncio.run(go())
        self.assertEqual(mailer.sent, [("p@acme.com", "Backend role", "Body 0")])


if __name__ == "__main__":
    unittest.main()
