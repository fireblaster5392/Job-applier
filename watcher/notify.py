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

    async def send_tailored(self, job: Job, rec: dict, files: dict[str, str], dashboard_url: str = "") -> bool:
        """One message per tailored job: the resume and cover letter as files, then the score with
        Approve / Edit / Skip buttons. files maps filename -> HTML content."""
        d, e = rec["data"], html.escape
        lines = [format_job(job), "", f"🎯 Fit <b>{d['fit_score']}/100</b>"]
        lines += [f"• {e(r)}" for r in d["fit_reasons"][:3]]
        if d["gaps"]:
            lines.append("Gaps: " + e("; ".join(d["gaps"][:3])))
        if rec["warnings"]:
            lines.append(f"Guardrails fixed {len(rec['warnings'])} thing(s) in the draft.")
        text = "\n".join(lines)
        buttons = [[{"text": "✅ Approve", "callback_data": f"approve:{job.db_id}"}]]
        if dashboard_url:
            buttons[0].append({"text": "✏️ Edit", "url": f"{dashboard_url.rstrip('/')}/jobs/{job.db_id}/edit"})
        buttons[0].append({"text": "Skip", "callback_data": f"skip:{job.db_id}"})
        buttons.append([{"text": "Open posting", "url": job.url}])
        if self.dry_run:
            print(f"\n[tailored] {text}\nfiles: {list(files)}\n{job.url}\n")
            return True
        try:
            for name, content in files.items():
                resp = await self.client.post(
                    API.format(token=self.token, method="sendDocument"),
                    data={"chat_id": self.chat_id},
                    files={"document": (name, content.encode(), "text/html")},
                )
                resp.raise_for_status()
            resp = await self.client.post(
                API.format(token=self.token, method="sendMessage"),
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                      "disable_web_page_preview": True, "reply_markup": {"inline_keyboard": buttons}},
            )
            resp.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            log.error("telegram send failed for %s: %s", job.key, exc)
            return False

    async def send_text(self, text: str, buttons: list | None = None) -> bool:
        if self.dry_run:
            print(f"\n{text}\n")
            return True
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        try:
            resp = await self.client.post(API.format(token=self.token, method="sendMessage"), json=payload)
            resp.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            log.error("telegram send failed: %s", exc)
            return False
