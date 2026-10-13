import asyncio
import json
import unittest

import httpx

from profile_mcp.profile import Profile
from watcher.notify import Notifier
from worker.main import Worker

from tests.helpers import fake_claude, store_with_job
import anthropic
import httpx2

PROFILE = Profile.load()
CFG = {"pipeline": {"score_threshold": 70}, "networking": {"enabled": False}}


def claude_scoring(score):
    """Fake Claude: answers the scoring call with `score`, the tailoring call with the standard draft."""
    from tests.helpers import GOOD_OUTPUT

    def handler(request):
        body = json.loads(request.content)
        is_score = "score" in body["output_config"]["format"]["schema"]["properties"]
        out = {"score": score, "reasons": ["Python match"]} if is_score else GOOD_OUTPUT
        return httpx2.Response(200, json={
            "id": "m", "type": "message", "role": "assistant", "model": body["model"], "stop_reason": "end_turn",
            "stop_sequence": None, "content": [{"type": "text", "text": json.dumps(out)}],
            "usage": {"input_tokens": 10, "output_tokens": 5}})
    return anthropic.Anthropic(api_key="t", max_retries=0,
                               http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))


class WorkerTests(unittest.TestCase):
    def run_worker(self, claude, cfg=CFG, telegram_ok=True):
        self.store, self.rowid = store_with_job()
        self.calls = []

        def tg(request):
            method = request.url.path.rsplit("/", 1)[-1]
            self.calls.append((method, request))
            return httpx.Response(200 if telegram_ok else 502, json={"ok": telegram_ok})

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(tg)) as c:
                n = Notifier(c)
                n.token, n.chat_id, n.dry_run = "T", "42", False
                w = Worker(self.store, n, cfg, profile_loader=lambda: PROFILE,
                           dashboard_url="http://dash", claude=claude)
                return await w.run_once(), w
        return asyncio.run(go())

    def methods(self):
        return [m for m, _ in self.calls]

    def test_good_fit_is_tailored_and_sent_once(self):
        stats, w = self.run_worker(claude_scoring(85))
        self.assertEqual(stats["tailored"], 1)
        job = self.store.get_job(self.rowid)
        self.assertEqual((job["status"], job["score"]), ("tailored", 85))
        self.assertIsNotNone(job["notified_at"])
        self.assertEqual(self.methods(), ["sendDocument", "sendDocument", "sendMessage"])
        msg = json.loads(self.calls[-1][1].content)
        buttons = [b for row in msg["reply_markup"]["inline_keyboard"] for b in row]
        self.assertEqual([b["text"] for b in buttons][:3], ["✅ Approve", "✏️ Edit", "Skip"])
        self.assertIn(f"http://dash/jobs/{self.rowid}/edit", [b.get("url") for b in buttons])
        self.assertIn(b"Alex Placeholder", self.calls[0][1].content)  # resume file attached

        # Next pass: nothing new, nothing re-sent
        self.calls.clear()
        stats = asyncio.run(w.run_once())
        self.assertEqual(sum(stats.values()), 0)

    def test_weak_fit_goes_to_dashboard_only(self):
        stats, _ = self.run_worker(claude_scoring(40))
        self.assertEqual(stats["below"], 1)
        job = self.store.get_job(self.rowid)
        self.assertEqual((job["status"], job["score"]), ("below_threshold", 40))
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.store.get_tailored(self.rowid))

    def test_weak_fit_alert_when_enabled(self):
        self.run_worker(claude_scoring(40), cfg={"pipeline": {"score_threshold": 70, "notify_below_threshold": True}, "networking": {"enabled": False}})
        self.assertEqual(self.methods(), ["sendMessage"])

    def test_claude_failure_falls_back_to_plain_alert(self):
        stats, _ = self.run_worker(fake_claude(status=500))
        self.assertEqual(stats["fallback"], 1)
        self.assertEqual(self.methods(), ["sendMessage"])
        msg = json.loads(self.calls[0][1].content)
        datas = [b.get("callback_data") for row in msg["reply_markup"]["inline_keyboard"] for b in row]
        self.assertIn(f"tailor:{self.rowid}", datas)
        self.assertIsNotNone(self.store.get_job(self.rowid)["notified_at"])

    def test_failed_send_is_retried(self):
        _, w = self.run_worker(claude_scoring(85), telegram_ok=False)
        self.assertIsNone(self.store.get_job(self.rowid)["notified_at"])
        # Telegram is back
        async def again():
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda r: httpx.Response(200, json={"ok": True}))) as c:
                w.notifier.client = c
                return await w.run_once()
        self.assertEqual(asyncio.run(again())["resent"], 1)
        self.assertIsNotNone(self.store.get_job(self.rowid)["notified_at"])


if __name__ == "__main__":
    unittest.main()
