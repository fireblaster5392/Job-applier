"""Auto pipeline worker (used when [pipeline] mode = "auto").

    python -m worker.main --db data/jobs.db

For each new matching job the watcher stored:
  1. quick fit score (cheap model)
  2. score >= threshold: tailor resume + cover letter, send one Telegram message with both files
     and Approve / Edit / Skip
  3. below threshold: listed on the dashboard as below_threshold (optionally also sent as an alert)
If scoring or tailoring fails, the job is still sent as a plain alert with a Tailor button,
so nothing is missed.
"""
import argparse
import asyncio
import html
import logging
import os
import re
from pathlib import Path

import httpx

from networking.daily import due as networking_due, followups, run_daily
from outreach.draft import draft_messages
from outreach.finder import find_contacts
from outreach.store import jobs_needing_outreach, save_outreach
from outreach.telegram import send_contact_drafts
from prep.sheet import DEFAULT_PREP_MODEL, build_prep, jobs_needing_prep, prep_html
from profile_mcp.profile import Profile
from tailor.claude import DEFAULT_MODEL
from tailor.pipeline import tailor_job
from tailor.provider import load_provider
from tailor.render import cover_letter_html, resume_html
from tailor.schema import TailoredApplication
from tailor.score import DEFAULT_SCORE_MODEL, score_job
from watcher.db import JobStore, row_to_job
from watcher.main import load_config
from watcher.models import Job
from watcher.notify import Notifier
from watcher.sources import job_description

log = logging.getLogger("worker")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


class Worker:
    def __init__(self, store: JobStore, notifier: Notifier, cfg: dict, *, profile_loader=Profile.load,
                 dashboard_url: str = "", claude=None, http_client=None, provider=None):
        p = cfg.get("pipeline", {})
        self.threshold = p.get("score_threshold", 70)
        self.score_model = p.get("score_model", DEFAULT_SCORE_MODEL)
        self.notify_below = p.get("notify_below_threshold", False)
        self.tailor_model = cfg.get("tailor", {}).get("model", DEFAULT_MODEL)
        self.store, self.notifier = store, notifier
        self.provider = provider   # tailor.provider; None = Claude with the models above
        if provider is not None:
            profile_loader = provider.profile
        self.profile_loader, self.dashboard_url = profile_loader, dashboard_url
        o = cfg.get("outreach", {})
        self.outreach_on = o.get("enabled", True)
        self.outreach_statuses = o.get("trigger_statuses", ["submitted"])
        self.max_contacts = o.get("max_contacts", 3)
        self.outreach_model = o.get("model", DEFAULT_MODEL)
        self.claude, self.http_client = claude, http_client   # injectable for tests
        self.find_contacts = find_contacts
        self.cfg = cfg
        self.networking_on = cfg.get("networking", {}).get("enabled", True)
        pr = cfg.get("prep", {})
        self.prep_on, self.prep_model = pr.get("enabled", True), pr.get("model", DEFAULT_PREP_MODEL)
        self.prep_dir = Path(pr.get("output_dir", "data/prep"))

    async def run_once(self) -> dict:
        stats = {"tailored": 0, "below": 0, "fallback": 0, "resent": 0, "outreach": 0}
        for job in self.store.jobs_to_score():
            stats[await self.handle(job)] += 1
        stats["resent"] = await self.resend_unsent()
        if self.outreach_on:
            for row in jobs_needing_outreach(self.store, self.outreach_statuses):
                await self.outreach(row)
                stats["outreach"] += 1
        if self.networking_on and networking_due(self.store, self.cfg):
            await self.networking()
        stats["followups"] = await followups(self.store, self.notifier, self.cfg, self.profile_loader())
        stats["prep"] = 0
        if self.prep_on:
            for row in jobs_needing_prep(self.store):
                stats["prep"] += await self.prep(row)
        return stats

    async def prep(self, row) -> int:
        """Draft an interview prep sheet when a job's emails say interview. Gives up after 3 failures."""
        rowid, key = row["rowid"], f"prep_failures:{row['rowid']}"
        failures = int(self.store.get_state(key) or 0)
        if failures >= 3:
            return 0
        try:
            await asyncio.to_thread(build_prep, self.store, rowid, self.profile_loader(), model=self.prep_model,
                                    client=self.claude, http_client=self.http_client)
        except Exception as exc:  # noqa: BLE001 - retried next pass, then left for the dashboard button
            log.warning("prep sheet failed for job %s: %s", rowid, exc)
            self.store.set_state(key, str(failures + 1))
            return 0
        path = self.prep_dir / f"prep-{_slug(row['company'])}-{rowid}.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(prep_html(self.store, rowid, self.profile_loader()))
        e = html.escape
        buttons = [[{"text": "Open prep sheet", "url": f"{self.dashboard_url}/jobs/{rowid}/prep"}]] if self.dashboard_url else None
        await self.notifier.send_text(f"🎯 Interview prep for <b>{e(row['title'])}</b> at {e(row['company'])} is ready.",
                                      buttons)
        await self.notifier.send_file(str(path))
        return 1

    async def networking(self) -> None:
        from zoneinfo import ZoneInfo
        from datetime import datetime
        tz = ZoneInfo(self.cfg.get("networking", {}).get("timezone", "America/Edmonton"))
        self.store.set_state("network_last_day", datetime.now(tz).date().isoformat())  # once a day, even on failure
        try:
            await run_daily(self.store, self.notifier, self.cfg, self.profile_loader(), claude=self.claude,
                            finder=self.find_contacts)
        except Exception as exc:  # noqa: BLE001
            log.warning("daily networking failed: %s", exc)
            await self.notifier.send_text(f"⚠️ Today's networking batch failed: {html.escape(str(exc))}")

    async def outreach(self, row) -> None:
        """Find contacts for an approved job, draft messages, and send them to you to review.
        Emails go out only when you tap Send; LinkedIn notes are for you to paste yourself."""
        e = html.escape
        label = f"{row['title']} at {row['company']}"
        try:
            contacts = await asyncio.to_thread(self.find_contacts, row["company"], "", limit=self.max_contacts)
            drafts = {}
            if contacts:
                rec = self.store.get_tailored(row["rowid"])
                fit = "; ".join(rec["data"]["fit_reasons"]) if rec else ""
                drafts = await asyncio.to_thread(
                    draft_messages, {"title": row["title"], "company": row["company"],
                                     "location": row["location"], "fit_notes": fit},
                    contacts, self.profile_loader(), model=self.outreach_model, client=self.claude)
        except Exception as exc:  # noqa: BLE001
            log.warning("outreach failed for %s: %s", label, exc)
            self.store.conn.execute("UPDATE jobs SET outreach_at='failed' WHERE rowid=?", (row["rowid"],))
            self.store.conn.commit()
            await self.notifier.send_text(f"⚠️ Couldn't prepare outreach for <b>{e(label)}</b>: {e(str(exc))}")
            return

        saved = save_outreach(self.store, row["rowid"], contacts, drafts)
        if not saved:
            await self.notifier.send_text(f"🔎 No contacts found for <b>{e(label)}</b>.")
            return
        await self.notifier.send_text(f"🤝 Outreach for <b>{e(label)}</b>: {len(saved)} contact(s) below.")
        await send_contact_drafts(self.notifier, saved, drafts)

    async def resend_unsent(self) -> int:
        """Retry messages that failed to send (e.g. Telegram was down)."""
        sent = 0
        for row in self.store.conn.execute(
            "SELECT rowid FROM jobs WHERE matched=1 AND is_new=1 AND notified_at IS NULL AND closed_at IS NULL"
            " AND score IS NOT NULL AND status IN ('tailored', 'discovered')"
        ).fetchall():
            job = row_to_job(self.store.get_job(row["rowid"]))
            rec = self.store.get_tailored(job.db_id)
            ok = (await self.send_tailored(job, rec)) if rec else (await self.notifier.send(job))
            if ok:
                self.store.mark_notified(job)
                sent += 1
        return sent

    async def send_tailored(self, job: Job, rec: dict) -> bool:
        profile = self.profile_loader()
        app = TailoredApplication.model_validate(rec["data"])
        name = _slug(profile.basics.get("name", "resume"))
        files = {
            f"{name}-{_slug(job.company)}-resume.html": resume_html(profile, app),
            f"{name}-{_slug(job.company)}-cover-letter.html": cover_letter_html(profile, app, job.company),
        }
        return await self.notifier.send_tailored(job, rec, files, self.dashboard_url)

    async def handle(self, job: Job) -> str:
        profile = self.profile_loader()
        info = {"title": job.title, "company": job.company, "location": job.location,
                "ats": job.ats, "url": job.url}
        try:
            description = await asyncio.to_thread(job_description, job, self.http_client)
            if self.provider is not None:
                fit = await asyncio.to_thread(self.provider.score, info, description or job.title)
            else:
                fit = await asyncio.to_thread(score_job, info, description or job.title, profile,
                                              model=self.score_model, client=self.claude)
            self.store.set_score(job.db_id, fit.score, fit.reasons)
            log.info("%s at %s: score %d", job.title, job.company, fit.score)

            if fit.score < self.threshold:
                self.store.set_status(job.db_id, "below_threshold")
                if not self.notify_below or await self.notifier.send(job):
                    self.store.mark_notified(job)
                return "below"

            rec = await asyncio.to_thread(tailor_job, self.store, job.db_id, profile, model=self.tailor_model,
                                          client=self.claude, description=description, provider=self.provider)
        except Exception as exc:  # noqa: BLE001 - fall back to a plain alert so the job isn't lost
            log.warning("auto pipeline failed for %s: %s; sending a plain alert", job.key, exc)
            self.store.set_status(job.db_id, "discovered", str(exc))
            if self.store.get_job(job.db_id)["score"] is None:
                self.store.set_score(job.db_id, -1, [f"Scoring failed: {exc}"])  # don't retry forever
            if await self.notifier.send(job):
                self.store.mark_notified(job)
            return "fallback"

        if await self.send_tailored(job, rec):
            self.store.mark_notified(job)
        return "tailored"


async def run(db_path: str, cfg_path: str, interval: int, dry_run: bool) -> None:
    cfg = load_config(cfg_path)
    async with httpx.AsyncClient(timeout=60) as client:
        worker = Worker(JobStore(db_path), Notifier(client, dry_run=dry_run), cfg,
                        dashboard_url=os.environ.get("DASHBOARD_URL", ""), provider=load_provider(cfg))
        log.info("worker running: threshold %d, tailoring provider %s", worker.threshold,
                 cfg.get("tailor", {}).get("provider", "claude"))
        while True:
            stats = await worker.run_once()
            if any(stats.values()):
                log.info("handled: %s", stats)
            await asyncio.sleep(interval)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/jobs.db")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--interval", type=int, default=10, help="seconds between checks for new jobs")
    ap.add_argument("--dry-run", action="store_true", help="print instead of sending to Telegram")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(run(args.db, args.config, args.interval, args.dry_run))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
