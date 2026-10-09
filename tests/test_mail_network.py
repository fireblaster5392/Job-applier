import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from fastapi.testclient import TestClient

from mail.classify import ApplicationEmail
from mail.imap import parse_message
from mail.sync import process
from networking.daily import due, followups, pick_companies, run_daily, target_companies
from outreach.finder import Contact
from outreach.store import save_outreach, send_email_message
from profile_mcp.profile import Profile
from watcher.db import JobStore
from web.app import create_app

from tests.helpers import store_with_job
from tests.test_outreach import FakeMailer, claude_drafts

PROFILE = Profile.load()


class FakeNotifier:
    def __init__(self):
        self.texts = []

    async def send_text(self, text, buttons=None):
        self.texts.append((text, buttons))
        return True


def raw_email(frm, subject, body, in_reply_to=None, mid="<x1@mail>"):
    m = EmailMessage()
    m["From"], m["Subject"], m["Message-ID"], m["Date"] = frm, subject, mid, "Fri, 09 Oct 2026 10:00:00 -0600"
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
    m.set_content(body)
    return parse_message(m.as_bytes())


def setup_sent_email(store, rowid, email="jane@acme.com"):
    contacts = [Contact("Jane Doe", "Engineering Manager", "acme", email=email)]
    saved = save_outreach(store, rowid, contacts, {0: _draft()})
    send_email_message(store, saved[0]["email_msg_id"], FakeMailer(), 10)
    return saved[0]


def _draft():
    from outreach.draft import ContactDraft
    return ContactDraft(contact_index=0, linkedin_note="hi", email_subject="Hello", email_body="Body")


class MailSyncTests(unittest.TestCase):
    def setUp(self):
        self.store, self.rowid = store_with_job()
        self.store.set_status(self.rowid, "approved")
        self.classified = []

    def classifier(self, category, company="Acme Inc", is_app=True):
        def fn(mail, **kw):
            self.classified.append(mail["subject"])
            return ApplicationEmail(is_application_email=is_app, company=company, category=category, summary="s")
        return fn

    def test_reply_by_thread_header_marks_replied(self):
        setup_sent_email(self.store, self.rowid, email="jane@acme.com")
        mail = raw_email("Someone Else <assistant@acme.com>", "Re: Hello", "Sure, Tuesday?", in_reply_to="<1@test>")
        events = process(self.store, [mail], classifier=self.classifier("other"))
        self.assertEqual([e["kind"] for e in events], ["reply"])
        self.assertEqual(self.store.conn.execute("SELECT status FROM messages WHERE channel='email'").fetchone()[0], "replied")
        self.assertEqual(self.classified, [])   # replies never reach Claude
        self.assertEqual(process(self.store, [mail], classifier=self.classifier("other")), [])   # deduped

    def test_reply_by_sender_address(self):
        setup_sent_email(self.store, self.rowid)
        events = process(self.store, [raw_email("Jane <Jane@Acme.com>", "Hello back", "Hi!")],
                         classifier=self.classifier("other"))
        self.assertEqual(events[0]["contact"]["name"], "Jane Doe")

    def test_application_emails_update_status(self):
        mails = [raw_email("no-reply@greenhouse-mail.io", "Thanks for applying to Acme", "We got it", mid="<a@x>")]
        process(self.store, mails, classifier=self.classifier("confirmation"))
        self.assertEqual(self.store.get_job(self.rowid)["app_status"], "applied")
        mails = [raw_email("recruiting@acme.com", "Next steps", "Let's schedule", mid="<b@x>")]
        events = process(self.store, mails, classifier=self.classifier("interview_request"))
        self.assertEqual(self.store.get_job(self.rowid)["app_status"], "interview")
        self.assertEqual(events[0]["category"], "interview_request")

    def test_unrelated_mail_is_ignored_without_claude(self):
        process(self.store, [raw_email("deals@shop.com", "Sale", "50% off")], classifier=self.classifier("other"))
        self.assertEqual(self.classified, [])


class NetworkingTests(unittest.TestCase):
    CFG = {"companies": {"greenhouse": ["stripe"], "ashby": ["linear", "ramp"]},
           "networking": {"per_day": 2, "timezone": "UTC", "daily_hour": 9}}

    def test_targets_put_startup_boards_first(self):
        self.assertEqual(target_companies(self.CFG), ["linear", "ramp", "stripe"])

    def test_due_once_a_day_after_the_hour(self):
        store = JobStore(":memory:")
        morning = datetime(2026, 10, 9, 8, tzinfo=timezone.utc)
        self.assertFalse(due(store, self.CFG, morning))
        self.assertTrue(due(store, self.CFG, morning + timedelta(hours=2)))
        store.set_state("network_last_day", "2026-10-09")
        self.assertFalse(due(store, self.CFG, morning + timedelta(hours=2)))

    def test_daily_batch_drafts_and_respects_cooldown(self):
        store = JobStore(":memory:")
        n = FakeNotifier()

        def finder(company, hint, limit, mode):
            self.assertEqual(mode, "network")
            return [Contact(f"Founder of {company}", "Co-founder & CEO", company,
                            linkedin_url=f"https://www.linkedin.com/in/{company}", email=f"ceo@{company}.com", rank=3)]

        count = asyncio.run(run_daily(store, n, self.CFG, PROFILE, claude=claude_drafts(2), finder=finder))
        self.assertEqual(count, 2)
        self.assertEqual(pick_companies(store, ["linear", "ramp", "stripe"], 90), ["stripe"])
        texts = [t for t, _ in n.texts]
        self.assertIn("Today's networking: 2 people", texts[0])
        self.assertEqual(sum("📧 Email to" in t for t in texts), 2)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM messages WHERE status='sent'").fetchone()[0], 0)

    def test_followup_once_after_no_reply(self):
        store, rowid = store_with_job()
        setup_sent_email(store, rowid)
        old = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()
        store.conn.execute("UPDATE messages SET sent_at=?", (old,))
        n = FakeNotifier()
        self.assertEqual(asyncio.run(followups(store, n, {}, PROFILE)), 1)
        self.assertIn("Just bumping this", n.texts[0][0])
        self.assertEqual(asyncio.run(followups(store, n, {}, PROFILE)), 0)   # only one follow-up
        # The follow-up goes in the same thread
        fid = store.conn.execute("SELECT id FROM messages WHERE is_followup=1").fetchone()[0]
        mailer = FakeMailer()
        calls = []
        mailer.send = lambda to, subject, body, in_reply_to=None: calls.append(in_reply_to) or "<2@test>"
        send_email_message(store, fid, mailer, 10)
        self.assertEqual(calls, ["<1@test>"])


class DashboardTests(unittest.TestCase):
    def test_job_page_shows_replies_and_status_and_network_page(self):
        tmp = tempfile.TemporaryDirectory()
        db = os.path.join(tmp.name, "jobs.db")
        store, rowid = store_with_job(db)
        store.set_status(rowid, "approved")
        setup_sent_email(store, rowid)
        process(store, [raw_email("jane@acme.com", "Re: Hello", "Happy to chat Tuesday")],
                classifier=lambda m, **k: None)
        process(store, [raw_email("x@greenhouse-mail.io", "Interview", "Schedule", mid="<i@x>")],
                classifier=lambda m, **k: ApplicationEmail(is_application_email=True, company="acme",
                                                           category="interview_request", summary="Pick a time"))
        save_outreach(store, None, [Contact("Ana", "Founder", "linear", linkedin_url="https://www.linkedin.com/in/ana")],
                      {0: _draft()}, kind="network")
        client = TestClient(create_app(db, profile_loader=lambda: PROFILE))
        page = client.get(f"/jobs/{rowid}").text
        self.assertIn("Happy to chat Tuesday", page)
        self.assertIn("Pick a time", page)
        self.assertIn("interview", page)
        net = client.get("/network").text
        self.assertIn("Ana", net)
        lid = store.conn.execute("SELECT m.id FROM messages m JOIN contacts c ON c.id=m.contact_id WHERE c.name='Ana' AND channel='linkedin'").fetchone()[0]
        client.post(f"/messages/{lid}/linkedin-sent")
        client.post(f"/messages/{lid}/replied")
        self.assertIn("replied<b>1</b>", client.get("/network").text.replace(" ", ""))
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
