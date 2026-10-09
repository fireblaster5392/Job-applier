"""Telegram notifications. Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID."""
import html
import logging
import os

import httpx

from .models import Job

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"


def format_job(job: Job) -> str:
    e = html.escape
    lines = [f"<b>{e(job.title)}</b>", f"🏢 {e(job.company)} · {e(job.ats)}"]
    if job.location:
        lines.append(f"📍 {e(job.location)}" + (" (remote)" if job.remote and "remote" not in job.location.lower() else ""))
    if job.department:
        lines.append(f"🗂 {e(job.department)}")
    if job.posted_at:
        lines.append(f"🕒 posted {e(job.posted_at[:16].replace('T', ' '))} UTC")
    return "\n".join(lines)


class Notifier:
    def __init__(self, client: httpx.AsyncClient, dry_run: bool = False):
        self.client = client
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        self.dry_run = dry_run or not (self.token and self.chat_id)
        if self.dry_run and not dry_run:
            log.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; printing matches instead")

    async def send(self, job: Job) -> bool:
        text = format_job(job)
        if self.dry_run:
            print(f"\n[match] {text}\n{job.url}\n")
            return True
        buttons = [[{"text": "Open posting", "url": job.url}]]
        if job.db_id is not None:
            # Handled by bot/main.py
            buttons.append([{"text": "✍️ Tailor resume", "callback_data": f"tailor:{job.db_id}"},
                            {"text": "Skip", "callback_data": f"skip:{job.db_id}"}])
        try:
            resp = await self.client.post(
                API.format(token=self.token, method="sendMessage"),
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "reply_markup": {"inline_keyboard": buttons},
                },
            )
            resp.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            log.error("telegram send failed for %s: %s", job.key, exc)
            return False
