"""Inbox sync loop.

    python -m mail.main --db data/jobs.db

Every couple of minutes: read the last few days of inbox mail (read-only), record replies to
your outreach and the companies' application emails, and ping you on Telegram for replies,
interview requests, assessments, offers and rejections.
"""
import argparse
import asyncio
import html
import logging

import httpx

from watcher.db import JobStore
from watcher.main import load_config
from watcher.notify import Notifier

from .classify import DEFAULT_MODEL
from .imap import fetch_recent
from .sync import process

log = logging.getLogger("mail")
ICONS = {"interview_request": "📅 Interview request", "assessment": "📝 Assessment", "offer": "🎉 Offer",
         "rejection": "Rejection"}


def format_event(ev: dict, dashboard_url: str = "") -> tuple[str, list | None]:
    e = html.escape
    if ev["kind"] == "reply":
        c = ev["contact"]
        where = f" at {e(c['company'])}" if c["company"] else ""
        text = f"💬 <b>{e(c['name'])}</b>{where} replied: {e(ev['subject'])}\n\n{e(ev['snippet'][:300])}"
        link = f"{dashboard_url}/jobs/{c['job_rowid']}" if c["job_rowid"] else f"{dashboard_url}/network"
    else:
        text = f"{ICONS[ev['category']]} from <b>{e(ev['company'])}</b>\n{e(ev['summary'])}"
        link = f"{dashboard_url}/jobs/{ev['job_rowid']}" if ev.get("job_rowid") else f"{dashboard_url}/"
    return text, ([[{"text": "Open dashboard", "url": link}]] if dashboard_url else None)


async def run(db_path: str, cfg_path: str, interval: int) -> None:
    import os
    cfg = load_config(cfg_path)
    model = cfg.get("mail", {}).get("model", DEFAULT_MODEL)
    dashboard_url = os.environ.get("DASHBOARD_URL", "").rstrip("/")
    store = JobStore(db_path)
    async with httpx.AsyncClient(timeout=30) as client:
        notifier = Notifier(client)
        while True:
            try:
                mails = await asyncio.to_thread(fetch_recent)
                events = await asyncio.to_thread(process, store, mails, model=model)
                for ev in events:
                    await notifier.send_text(*format_event(ev, dashboard_url))
            except Exception as exc:  # noqa: BLE001 - keep syncing
                log.warning("inbox sync failed: %s", exc)
            await asyncio.sleep(interval)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/jobs.db")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--interval", type=int, default=120)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(run(args.db, args.config, args.interval))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
