"""Apply queue: submits approved jobs one at a time at a human pace.

    python -m apply.main --db data/jobs.db            # real
    python -m apply.main --db data/jobs.db --dry-run  # fill forms, never submit

Pacing: one application at a time, a random gap between applications, a daily cap and active
hours, all in [apply] in config.toml. CAPTCHAs are never solved: those come back to you.
"""
import argparse
import asyncio
import html
import json
import logging
import os
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from profile_mcp.profile import Profile
from tailor.claude import DEFAULT_MODEL
from tailor.provider import load_provider
from watcher.db import JobStore, row_to_job
from watcher.main import load_config
from watcher.notify import Notifier

from .runner import apply_job, get_application

log = logging.getLogger("apply")


class ApplyQueue:
    def __init__(self, store: JobStore, notifier: Notifier, cfg: dict, *, profile_loader=Profile.load,
                 out_root: Path = Path("data/applications"), dashboard_url: str = "", claude=None,
                 apply_fn=apply_job, dry_run: bool = False, rng=random.random):
        a = cfg.get("apply", {})
        self.max_per_day = a.get("max_per_day", 15)
        self.min_gap, self.max_gap = a.get("min_gap_minutes", 4), a.get("max_gap_minutes", 12)
        self.hours = a.get("active_hours", [8, 22])
        self.tz = ZoneInfo(a.get("timezone", cfg.get("networking", {}).get("timezone", "America/Edmonton")))
        self.dry_run = dry_run or a.get("dry_run", False)
        self.model = cfg.get("tailor", {}).get("model", DEFAULT_MODEL)
        self.store, self.notifier, self.profile_loader = store, notifier, profile_loader
        self.out_root, self.dashboard_url, self.claude = out_root, dashboard_url.rstrip("/"), claude
        self.apply_fn, self.rng = apply_fn, rng

    def submitted_today(self, at: datetime) -> int:
        start = at.astimezone(self.tz).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        return self.store.conn.execute("SELECT COUNT(*) FROM applications WHERE submitted_at >= ?",
                                       (start.isoformat(timespec="seconds"),)).fetchone()[0]

    def can_run(self, at: datetime) -> tuple[bool, str]:
        local = at.astimezone(self.tz)
        if not (self.hours[0] <= local.hour < self.hours[1]):
            return False, "outside active hours"
        if self.submitted_today(at) >= self.max_per_day:
            return False, "daily cap reached"
        nxt = self.store.get_state("apply_next_at")
        if nxt and at < datetime.fromisoformat(nxt):
            return False, "waiting for the gap between applications"
        return True, ""

    def next_job(self):
        return self.store.conn.execute(
            "SELECT rowid, * FROM jobs WHERE status IN ('submit_requested', 'approved')"
            " ORDER BY status='submit_requested' DESC, first_seen LIMIT 1").fetchone()

    async def run_once(self, at: datetime | None = None) -> dict | None:
        at = at or datetime.now(timezone.utc)
        row = self.next_job()
        if row is None:
            return None
        ok, why = self.can_run(at)
        if not ok:
            log.debug("not applying yet: %s", why)
            return None
        rowid = row["rowid"]
        approved = None
        if row["status"] == "submit_requested":
            prev = get_application(self.store, rowid)
            approved = {a["key"]: a["answer"] for a in (prev or {}).get("answers", [])}
        self.store.set_status(rowid, "applying")
        gap = self.min_gap + (self.max_gap - self.min_gap) * self.rng()
        self.store.set_state("apply_next_at", (at + timedelta(minutes=gap)).isoformat())
        result = await asyncio.to_thread(self.apply_fn, self.store, rowid, self.profile_loader(), model=self.model,
                                         out_root=self.out_root, claude=self.claude, approved_answers=approved,
                                         dry_run=self.dry_run)
        await self.report(row_to_job(self.store.get_job(rowid)), result)
        return result

    async def report(self, job, r: dict) -> None:
        e = html.escape
        label = f"<b>{e(job.title)}</b> at {e(job.company)}"
        dash = f"{self.dashboard_url}/jobs/{job.db_id}" if self.dashboard_url else ""
        apply_url = job.apply_url or job.url
        reasons = "\n".join(f"• {e(x)}" for x in r["reasons"])
        if r["screenshot"] and os.path.exists(r["screenshot"]):
            await self.notifier.send_file(r["screenshot"], photo=True)
        if r["outcome"] == "submitted":
            await self.notifier.send_text(f"✅ Applied: {label}. Outreach drafts are next.")
            return
        if r["outcome"] == "needs_review":
            wrote = [a for a in r["answers"] if a["source"] == "claude"]
            lines = "\n\n".join(f"<b>{e(a['label'][:120])}</b>\n{e(a['answer'][:600])}" for a in wrote[:5])
            buttons = [[{"text": "✅ Submit", "callback_data": f"submit:{job.db_id}"}]]
            if dash:
                buttons[0].append({"text": "Review on dashboard", "url": dash})
            buttons.append([{"text": "I'll apply myself", "url": apply_url}])
            await self.notifier.send_text(f"📝 Ready to submit {label}\n{reasons}\n\n{lines}", buttons)
            return
        for path in r.get("files", {}).values():
            await self.notifier.send_file(path)
        buttons = [[{"text": "Open application", "url": apply_url},
                    {"text": "I submitted it", "callback_data": f"done:{job.db_id}"}]]
        if r["outcome"] == "failed":
            buttons.append([{"text": "Retry", "callback_data": f"retry:{job.db_id}"}])
            await self.notifier.send_text(f"⚠️ Couldn't apply to {label}\n{reasons}", buttons)
        else:
            await self.notifier.send_text(f"🙋 {label} needs you\n{reasons}\nYour resume and cover letter are "
                                          "attached; tap “I submitted it” when you're done.", buttons)


async def run(db_path: str, cfg_path: str, dry_run: bool, interval: int) -> None:
    cfg = load_config(cfg_path)
    async with httpx.AsyncClient(timeout=60) as client:
        q = ApplyQueue(JobStore(db_path), Notifier(client), cfg, dry_run=dry_run,
                       profile_loader=load_provider(cfg).profile,
                       out_root=Path(os.path.dirname(os.path.abspath(db_path))) / "applications",
                       dashboard_url=os.environ.get("DASHBOARD_URL", ""))
        log.info("apply queue running%s: max %d/day, %d-%d min apart", " (dry run)" if q.dry_run else "",
                 q.max_per_day, q.min_gap, q.max_gap)
        while True:
            try:
                await q.run_once()
            except Exception as exc:  # noqa: BLE001
                log.exception("apply queue error: %s", exc)
            await asyncio.sleep(interval)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/jobs.db")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--dry-run", action="store_true", help="fill forms but never submit")
    ap.add_argument("--interval", type=int, default=30)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(run(args.db, args.config, args.dry_run, args.interval))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
