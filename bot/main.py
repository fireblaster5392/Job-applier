"""Telegram button handler.

    python -m bot.main --db data/jobs.db

Buttons on a job alert:  Tailor resume -> Claude tailors it -> Approve / Reject.
Nothing is submitted anywhere: "Approve" only marks the job approved in the database.
"""
import argparse
import asyncio
import html
import logging
import os

import httpx

from apply.status import mark_submitted_by_user
from outreach.mailer import SmtpMailer
from outreach.store import mark_linkedin_sent, send_email_message, skip_message
from profile_mcp.profile import Profile
from tailor.claude import DEFAULT_MODEL
from tailor.pipeline import tailor_job
from watcher.db import JobStore
from watcher.main import load_config

log = logging.getLogger("bot")
API = "https://api.telegram.org/bot{token}/{method}"


class Bot:
    def __init__(self, store: JobStore, client: httpx.AsyncClient, *, token: str, chat_id: str,
                 profile_loader=Profile.load, model: str = DEFAULT_MODEL, dashboard_url: str = "",
                 tailor=tailor_job, store_factory=None, mailer=None, max_emails_per_day: int = 10):
        self.store, self.client = store, client
        # Tailoring runs in a worker thread, which needs its own sqlite connection.
        self.store_factory = store_factory or (lambda: store)
        self.mailer = mailer or SmtpMailer()
        self.max_emails_per_day = max_emails_per_day
        self.token, self.chat_id = token, str(chat_id)
        self.profile_loader, self.model = profile_loader, model
        self.dashboard_url = dashboard_url.rstrip("/")
        self.tailor = tailor
        self.offset = 0
        self.tasks: set[asyncio.Task] = set()

    async def call(self, method: str, **payload) -> dict:
        resp = await self.client.post(API.format(token=self.token, method=method), json=payload, timeout=60)
        resp.raise_for_status()
        return resp.json()

    async def send(self, text: str, buttons: list | None = None) -> None:
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        await self.call("sendMessage", **payload)

    async def poll(self) -> None:
        data = await self.call("getUpdates", offset=self.offset, timeout=50, allowed_updates=["callback_query"])
        for update in data.get("result", []):
            self.offset = update["update_id"] + 1
            if "callback_query" in update:
                await self.on_callback(update["callback_query"])

    async def on_callback(self, cq: dict) -> None:
        chat = str(cq.get("message", {}).get("chat", {}).get("id", ""))
        if chat != self.chat_id:
            log.warning("ignoring button press from chat %s", chat)
            await self.call("answerCallbackQuery", callback_query_id=cq["id"])
            return
        action, _, raw_id = (cq.get("data") or "").partition(":")
        if action == "lisent" and raw_id.isdigit():
            mark_linkedin_sent(self.store, int(raw_id))
            await self.ack(cq, "✅ Marked sent")
            return
        if action in ("email", "noemail") and raw_id.isdigit():
            await self.on_email(cq, action, int(raw_id))
            return
        job = self.store.get_job(int(raw_id)) if raw_id.isdigit() else None
        if job is None:
            await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Job not found")
            return
        rowid, label = job["rowid"], f"{job['title']} at {job['company']}"

        if action == "skip":
            self.store.set_status(rowid, "skipped")
            await self.ack(cq, "Skipped")
        elif action == "reject":
            self.store.set_status(rowid, "rejected")
            await self.ack(cq, "Rejected")
        elif action == "approve":
            self.store.set_status(rowid, "approved")
            await self.ack(cq, "✅ Approved, applying")
        elif action == "submit" and job["status"] == "needs_review":
            self.store.set_status(rowid, "submit_requested")
            await self.ack(cq, "Submitting")
        elif action == "retry" and job["status"] in ("apply_failed", "needs_you"):
            self.store.set_status(rowid, "approved")
            await self.ack(cq, "Retrying")
        elif action == "done":
            mark_submitted_by_user(self.store, rowid)
            await self.ack(cq, "✅ Marked applied")
        elif action == "tailor":
            if job["status"] == "tailoring":
                await self.ack(cq, "Already tailoring")
                return
            await self.ack(cq, "Tailoring…")
            task = asyncio.create_task(self.run_tailor(rowid, label))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        else:
            await self.call("answerCallbackQuery", callback_query_id=cq["id"])

    async def on_email(self, cq: dict, action: str, msg_id: int) -> None:
        if action == "noemail":
            skip_message(self.store, msg_id)
            await self.ack(cq, "Not sent")
            return
        await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Sending…")
        result = await asyncio.to_thread(send_email_message, self.store_factory(), msg_id,
                                         self.mailer, self.max_emails_per_day)
        sent = result.startswith("Sent") or result == "Already sent."
        msg = cq.get("message") or {}
        if msg.get("message_id"):
            label = "✅ Sent" if sent else "Send email (retry)"
            data = "noop:" if sent else f"email:{msg_id}"
            await self.call("editMessageReplyMarkup", chat_id=self.chat_id, message_id=msg["message_id"],
                            reply_markup={"inline_keyboard": [[{"text": label, "callback_data": data}]]})
        if not sent:
            await self.send(html.escape(result))

    async def ack(self, cq: dict, text: str) -> None:
        await self.call("answerCallbackQuery", callback_query_id=cq["id"], text=text)
        msg = cq.get("message") or {}
        if msg.get("message_id"):
            # Replace the buttons with a status line so the same job can't be double-tapped.
            await self.call("editMessageReplyMarkup", chat_id=self.chat_id, message_id=msg["message_id"],
                            reply_markup={"inline_keyboard": [[{"text": text, "callback_data": "noop:"}]]})

    async def run_tailor(self, rowid: int, label: str) -> None:
        try:
            rec = await asyncio.to_thread(self.tailor, self.store_factory(), rowid, self.profile_loader(),
                                          model=self.model)
        except Exception as exc:  # noqa: BLE001
            await self.send(f"⚠️ Couldn't tailor <b>{html.escape(label)}</b>: {html.escape(str(exc))}")
            return
        await self.send(*format_tailored(rowid, label, rec, self.dashboard_url))


def format_tailored(rowid: int, label: str, rec: dict, dashboard_url: str) -> tuple[str, list]:
    d, e = rec["data"], html.escape
    lines = [f"✍️ <b>{e(label)}</b>", f"Fit: <b>{d['fit_score']}/100</b>"]
    lines += [f"• {e(r)}" for r in d["fit_reasons"][:4]]
    if d["gaps"]:
        lines.append("Gaps: " + e("; ".join(d["gaps"][:3])))
    if rec["warnings"]:
        lines.append(f"Guardrails fixed {len(rec['warnings'])} thing(s); see the dashboard.")
    buttons = [[{"text": "✅ Approve", "callback_data": f"approve:{rowid}"},
                {"text": "Reject", "callback_data": f"reject:{rowid}"}]]
    if dashboard_url:
        buttons.insert(0, [{"text": "Review resume + cover letter", "url": f"{dashboard_url}/jobs/{rowid}"}])
    return "\n".join(lines), buttons


async def run(db_path: str, cfg_path: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or exit("set TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or exit("set TELEGRAM_CHAT_ID")
    cfg = load_config(cfg_path) if os.path.exists(cfg_path) else {}
    async with httpx.AsyncClient() as client:
        bot = Bot(JobStore(db_path), client, token=token, chat_id=chat_id,
                  model=cfg.get("tailor", {}).get("model", DEFAULT_MODEL),
                  dashboard_url=os.environ.get("DASHBOARD_URL", ""),
                  store_factory=lambda: JobStore(db_path),
                  max_emails_per_day=cfg.get("outreach", {}).get("max_emails_per_day", 10))
        log.info("bot running")
        while True:
            try:
                await bot.poll()
            except httpx.HTTPError as exc:
                log.warning("telegram poll failed: %s", exc)
                await asyncio.sleep(5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/jobs.db")
    ap.add_argument("--config", default="config.toml")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(run(args.db, args.config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
