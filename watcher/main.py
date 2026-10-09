"""Poll every board in config.toml on an interval and notify on new matching jobs.

    python -m watcher.main            # run forever (every poll_seconds)
    python -m watcher.main --once     # one pass, then exit
    python -m watcher.main --dry-run  # print matches instead of sending to Telegram
"""
import argparse
import asyncio
import logging
import random
import time
import tomllib

import httpx

from .db import JobStore
from .filters import JobFilter
from .notify import Notifier
from .sources import BoardNotFound, fetch_board

log = logging.getLogger("watcher")
RETRYABLE = {429, 500, 502, 503, 504}


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def boards_from_config(cfg: dict) -> list[tuple[str, str]]:
    boards = []
    for ats in ("greenhouse", "lever", "ashby"):
        for slug in cfg.get("companies", {}).get(ats, []):
            boards.append((ats, slug.strip().lower()))
    return boards


async def fetch_with_retry(client, ats, slug, attempts=3):
    for i in range(attempts):
        try:
            return await fetch_board(client, ats, slug)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in RETRYABLE or i == attempts - 1:
                raise
        except httpx.TransportError:
            if i == attempts - 1:
                raise
        await asyncio.sleep(2 ** i + random.random())


async def poll_once(cfg, store: JobStore, notifier: Notifier, client, is_match) -> dict:
    boards = boards_from_config(cfg)
    sem = asyncio.Semaphore(cfg.get("concurrency", 8))
    stats = {"boards": len(boards), "ok": 0, "failed": 0, "jobs": 0, "new": 0}

    async def one(ats, slug):
        async with sem:
            await asyncio.sleep(random.random() * 0.5)  # jitter so we don't burst
            try:
                jobs = await fetch_with_retry(client, ats, slug)
            except BoardNotFound:
                store.record_board_error(ats, slug, "404: board not found, check the slug")
                log.warning("%s/%s: board not found, check the slug in config.toml", ats, slug)
                stats["failed"] += 1
                return
            except (httpx.HTTPError, ValueError) as exc:
                store.record_board_error(ats, slug, repr(exc))
                log.warning("%s/%s: %s", ats, slug, exc)
                stats["failed"] += 1
                return
            seeded = store.is_seeded(ats, slug)
            new = store.sync_board(ats, slug, jobs, is_match)
            stats["ok"] += 1
            stats["jobs"] += len(jobs)
            stats["new"] += len(new)
            if not seeded:
                log.info("%s/%s: seeded with %d existing jobs", ats, slug, len(jobs))
            for job in new:
                log.info("new: %s/%s %s (%s)%s", ats, slug, job.title, job.location,
                         " MATCH" if is_match(job) else "")

    await asyncio.gather(*(one(a, s) for a, s in boards))

    pending = store.pending_notifications()
    for job in pending:
        if await notifier.send(job):
            store.mark_notified(job)
    stats["notified"] = len(pending)
    return stats


async def run(cfg_path: str, db_path: str, once: bool, dry_run: bool):
    cfg = load_config(cfg_path)
    store = JobStore(db_path)
    is_match = JobFilter(cfg.get("filters", {}))
    interval = cfg.get("poll_seconds", 60)
    headers = {"User-Agent": cfg.get("user_agent", "job-watcher/0.1")}
    async with httpx.AsyncClient(timeout=20, headers=headers, follow_redirects=True) as client:
        notifier = Notifier(client, dry_run=dry_run)
        while True:
            started = time.monotonic()
            stats = await poll_once(cfg, store, notifier, client, is_match)
            took = time.monotonic() - started
            log.info("poll done in %.1fs: %s", took, stats)
            if once:
                return stats
            await asyncio.sleep(max(1.0, interval - took))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--db", default="jobs.db")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(run(args.config, args.db, args.once, args.dry_run))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
