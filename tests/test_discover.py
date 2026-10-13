"""JobSpy discovery with a fake scraper: mapping, seeding, cross-source dedupe, backoff."""
import json
import math
import unittest
from datetime import date

from watcher.db import JobStore
from watcher.discover import Discovery, to_job
from watcher.models import Job
from watcher.sources import job_description

CFG = {"discovery": {"sites": ["indeed", "linkedin"], "interval_minutes": 30,
                     "searches": [{"term": "backend engineer", "location": "New York, NY"}]},
       "filters": {"title_include": ["engineer"]}}


def row(i, site="indeed", company="Acme", title="Backend Engineer"):
    return {"id": f"{site}-{i}", "site": site, "title": title, "company": company, "location": "New York, NY",
            "job_url": f"https://{site}.com/j/{i}", "job_url_direct": math.nan if i % 2 else f"https://boards.greenhouse.io/acme/jobs/{i}",
            "date_posted": date(2026, 10, 10), "is_remote": False, "description": "Build **APIs**."}


class FakeScraper:
    def __init__(self):
        self.calls, self.results, self.fail = [], {}, set()

    def __call__(self, **kw):
        site = kw["site_name"][0]
        self.calls.append(kw)
        if site in self.fail:
            raise RuntimeError("429 blocked")
        return self.results.get(site, [])


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.store = JobStore(":memory:")
        self.now = [1000.0]
        self.scraper = FakeScraper()
        self.disc = Discovery(self.store, CFG, scraper=self.scraper, clock=lambda: self.now[0])

    def test_to_job_maps_and_cleans_nan(self):
        j = to_job(row(1))
        self.assertEqual((j.ats, j.company, j.job_id, j.apply_url), ("indeed", "Acme", "indeed-1", "https://indeed.com/j/1"))
        self.assertEqual(j.posted_at, "2026-10-10")
        self.assertEqual(job_description(j), "Build **APIs**.")
        self.assertIsNone(to_job({"site": "indeed", "title": "", "company": "x", "id": "1"}))

    def test_first_run_seeds_then_new_jobs_and_dedupe(self):
        self.scraper.results = {"indeed": [row(1)], "linkedin": [row(9, "linkedin", "Other", "Platform Engineer")]}
        stats = self.disc.run_once()
        self.assertEqual((stats["searches"], stats["found"], stats["new"]), (2, 2, 0))   # backlog
        self.now[0] += 31 * 60
        # same job again on Indeed (seen), a new one, and the Indeed job re-posted on LinkedIn (duplicate)
        self.scraper.results = {"indeed": [row(1), row(2, title="API Engineer")],
                                "linkedin": [row(5, "linkedin", "ACME", "Backend Engineer")]}
        stats = self.disc.run_once()
        self.assertEqual((stats["new"], stats["duplicates"]), (1, 1))
        new = self.store.jobs_to_score()
        self.assertEqual([j.title for j in new], ["API Engineer"])
        self.assertEqual(new[0].apply_url, "https://boards.greenhouse.io/acme/jobs/2")

    def test_waits_for_interval_and_backs_off_on_block(self):
        self.scraper.fail = {"linkedin"}
        self.disc.run_once()
        self.now[0] += 31 * 60
        self.disc.run_once()       # indeed due again; linkedin backed off to 60 min
        sites = [c["site_name"][0] for c in self.scraper.calls]
        self.assertEqual(sites, ["indeed", "linkedin", "indeed"])
        state = json.loads(self.store.get_state("discover:linkedin:backend engineer|New York, NY|"))
        self.assertEqual(state["backoff"], 60 * 60)
        self.now[0] += 31 * 60
        self.disc.run_once()
        self.assertEqual(json.loads(self.store.get_state("discover:linkedin:backend engineer|New York, NY|"))["backoff"],
                         120 * 60)

    def test_ats_watcher_job_blocks_search_duplicate(self):
        self.store.sync_board("greenhouse", "acme", [Job("greenhouse", "acme", "1", "Backend Engineer", "NY", "u")],
                              lambda j: True)
        self.store.add_found_jobs("search:indeed", "x", [], lambda j: True)    # seed the search
        new, dupes = self.store.add_found_jobs("search:indeed", "x", [to_job(row(1))], lambda j: True)
        self.assertEqual((len(new), dupes), (0, 1))


if __name__ == "__main__":
    unittest.main()
