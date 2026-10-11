"""Job-search discovery through JobSpy: Indeed, Glassdoor and LinkedIn's public job search.

Runs as its own service, separate from the 60-second ATS watcher, because these sites are slower
and stricter: each search runs every `interval_minutes`, one site at a time, with no proxies, and
a site that blocks or errors is backed off (doubling, up to 6 hours) instead of retried hard.
Nothing here logs in anywhere: these are the logged-out public job searches.

    python -m watcher.discover --once --dry-run   # one pass, print what it found
    python -m watcher.discover                    # run forever

Found jobs go into the same jobs table as the ATS watcher, so the worker scores, tailors and sends
them the same way. A job already found on a company's own board (same company and title in the
last 30 days) isn't added twice.
"""
import argparse
import json
import logging
import math
import time

from .db import JobStore
from .filters import JobFilter
from .main import load_config
from .models import Job

log = logging.getLogger("discover")
SITES = ("indeed", "glassdoor", "linkedin")
MAX_BACKOFF = 6 * 3600


def _clean(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value).strip()


def to_job(row: dict) -> Job | None:
    """One JobSpy result row as a Job. ats is the site; company is the company's name."""
    site, title, company = _clean(row.get("site")), _clean(row.get("title")), _clean(row.get("company"))
    job_id = _clean(row.get("id")) or _clean(row.get("job_url"))
    if not (site and title and company and job_id):
        return None
    posted = row.get("date_posted")
    raw = {k: _clean(v) for k, v in row.items() if k in (
        "description", "job_type", "company_url", "min_amount", "max_amount", "currency", "interval",
        "job_url", "job_url_direct", "emails")}
    raw["source"] = "jobspy"
    return Job(ats=site, company=company, job_id=job_id, title=title, location=_clean(row.get("location")),
               url=_clean(row.get("job_url")), apply_url=_clean(row.get("job_url_direct")) or _clean(row.get("job_url")),
               remote=bool(row.get("is_remote")) if _clean(row.get("is_remote")) else False,
               posted_at=_clean(posted.isoformat() if hasattr(posted, "isoformat") else posted), raw=raw)


class _JobSpyErrors(logging.Handler):
    """JobSpy logs a blocked or failed site and returns no rows instead of raising; collect those."""
    def __init__(self):
        super().__init__(logging.ERROR)
        self.messages = []

    def emit(self, record):
        if record.name.startswith("JobSpy"):
            self.messages.append(record.getMessage())


def default_scraper(**kwargs) -> list[dict]:
    from jobspy import scrape_jobs   # imported here: pandas is heavy and tests fake this
    errors = _JobSpyErrors()
    loggers = [logging.getLogger(n) for n in list(logging.root.manager.loggerDict) if n.startswith("JobSpy")]
    for lg in loggers + [logging.getLogger()]:
        lg.addHandler(errors)
    try:
        df = scrape_jobs(**kwargs)
    finally:
        for lg in loggers + [logging.getLogger()]:
            lg.removeHandler(errors)
    rows = df.to_dict("records") if df is not None else []
    if not rows and errors.messages:
        raise RuntimeError(errors.messages[0][:300])
    return rows


class Discovery:
    def __init__(self, store: JobStore, cfg: dict, *, scraper=default_scraper, clock=time.time):
        self.store, self.scraper, self.clock = store, scraper, clock
        d = cfg.get("discovery", {})
        self.enabled = d.get("enabled", True)
        self.sites = [s for s in d.get("sites", list(SITES)) if s in SITES]
        self.searches = d.get("searches", [])
        self.interval = d.get("interval_minutes", 30) * 60
        self.hours_old = d.get("hours_old", 24)
        self.results = d.get("results_per_site", 25)
        self.country = d.get("country_indeed", "usa")
        self.is_match = JobFilter(cfg.get("filters", {}))

    def _board(self, site: str, search: dict) -> str:
        return f"{search['term']}|{search.get('location', '')}|{'remote' if search.get('remote') else ''}"

    def _due(self, site: str, board: str) -> bool:
        state = json.loads(self.store.get_state(f"discover:{site}:{board}") or "{}")
        return self.clock() >= state.get("next_at", 0)

    def _schedule(self, site: str, board: str, ok: bool) -> None:
        key = f"discover:{site}:{board}"
        state = json.loads(self.store.get_state(key) or "{}")
        backoff = 0 if ok else min(MAX_BACKOFF, state.get("backoff", 0) * 2 or self.interval * 2)
        self.store.set_state(key, json.dumps({"next_at": self.clock() + (backoff or self.interval), "backoff": backoff}))

    def run_once(self, force: bool = False) -> dict:
        stats = {"searches": 0, "found": 0, "new": 0, "duplicates": 0, "failed": 0}
        if not self.enabled:
            return stats
        for search in self.searches:
            for site in self.sites:
                board = self._board(site, search)
                if not force and not self._due(site, board):
                    continue
                stats["searches"] += 1
                try:
                    rows = self.scraper(site_name=[site], search_term=search["term"],
                                        location=search.get("location") or None,
                                        is_remote=bool(search.get("remote")), results_wanted=self.results,
                                        hours_old=self.hours_old, country_indeed=self.country,
                                        description_format="markdown", linkedin_fetch_description=False,
                                        verbose=0)
                except Exception as exc:  # noqa: BLE001 - blocks and site changes are expected; back off
                    log.warning("%s %r: %s; backing off", site, search["term"], exc)
                    self.store.record_board_error(f"search:{site}", board, repr(exc)[:500])
                    self._schedule(site, board, ok=False)
                    stats["failed"] += 1
                    continue
                jobs = [j for j in (to_job(r) for r in rows) if j]
                new, dupes = self.store.add_found_jobs(f"search:{site}", board, jobs, self.is_match)
                stats["found"] += len(jobs)
                stats["new"] += len(new)
                stats["duplicates"] += dupes
                self._schedule(site, board, ok=True)
                for job in new:
                    log.info("new from %s: %s at %s (%s)%s", site, job.title, job.company, job.location,
                             " MATCH" if self.is_match(job) else "")
                time.sleep(0 if self.scraper is not default_scraper else 5)   # one site at a time, gently
        return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--db", default="data/jobs.db")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="use a throwaway database and print matches")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    store = JobStore(":memory:" if args.dry_run else args.db)
    disc = Discovery(store, cfg)
    while True:
        stats = disc.run_once(force=args.once or args.dry_run)
        log.info("discovery pass: %s", stats)
        if args.once or args.dry_run:
            if args.dry_run:
                for r in store.list_jobs(limit=50):
                    print(f"{r['ats']:9} {r['company'][:24]:24} {r['title'][:50]:50} {r['location']}")
            return
        time.sleep(60)


if __name__ == "__main__":
    main()
