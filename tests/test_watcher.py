"""Offline tests: sample payloads in the shape each ATS returns, served by httpx.MockTransport."""
import asyncio
import unittest

import httpx

from watcher.db import JobStore
from watcher.filters import JobFilter
from watcher.main import poll_once
from watcher.models import Job
from watcher.sources import parse_ashby, parse_greenhouse, parse_lever

GH = {"jobs": [{"id": 101, "title": "Software Engineer, Payments", "updated_at": "2026-10-09T10:00:00-04:00",
                "location": {"name": "New York, NY"}, "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/101"}]}
LEVER = [{"id": "abc-1", "text": "Backend Engineer", "createdAt": 1760000000000,
          "categories": {"location": "Remote - US", "team": "Platform"},
          "hostedUrl": "https://jobs.lever.co/acme/abc-1", "applyUrl": "https://jobs.lever.co/acme/abc-1/apply",
          "workplaceType": "remote"}]
ASHBY = {"jobs": [{"id": "u-1", "title": "ML Engineer", "location": "San Francisco", "isRemote": False,
                   "publishedAt": "2026-10-09T15:00:00Z", "jobUrl": "https://jobs.ashbyhq.com/acme/u-1",
                   "applyUrl": "https://jobs.ashbyhq.com/acme/u-1/application", "isListed": True,
                   "secondaryLocations": [{"location": "Seattle"}]},
                  {"id": "u-2", "title": "Hidden", "location": "SF", "isListed": False}]}

FILTER = JobFilter({"title_include": ["engineer"], "title_exclude": ["manager", "staff"],
                    "locations": ["new york", "san francisco"], "allow_remote": True})


class ParseTests(unittest.TestCase):
    def test_greenhouse(self):
        [j] = parse_greenhouse("acme", GH)
        self.assertEqual((j.job_id, j.title, j.location), ("101", "Software Engineer, Payments", "New York, NY"))

    def test_lever(self):
        [j] = parse_lever("acme", LEVER)
        self.assertTrue(j.remote)
        self.assertEqual(j.department, "Platform")
        self.assertTrue(j.posted_at.startswith("2025-10-09"))

    def test_ashby_skips_unlisted(self):
        jobs = parse_ashby("acme", ASHBY)
        self.assertEqual([j.job_id for j in jobs], ["u-1"])
        self.assertEqual(jobs[0].location, "San Francisco / Seattle")


class FilterTests(unittest.TestCase):
    def job(self, title, loc="New York", remote=False):
        return Job("greenhouse", "acme", "1", title, loc, "u", remote=remote)

    def test_rules(self):
        self.assertTrue(FILTER(self.job("Software Engineer")))
        self.assertFalse(FILTER(self.job("Engineering Manager")))
        self.assertFalse(FILTER(self.job("Staff Engineer")))
        self.assertFalse(FILTER(self.job("Software Engineer", loc="London")))
        self.assertTrue(FILTER(self.job("Software Engineer", loc="London", remote=True)))

    def test_whole_word(self):
        f = JobFilter({"title_include": ["intern"]})
        self.assertTrue(f(self.job("Software Engineering Intern")))
        self.assertFalse(f(self.job("Internal Tools Engineer")))


class FakeNotifier:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send(self, job):
        if self.fail:
            return False
        self.sent.append(job)
        return True


class PollTests(unittest.TestCase):
    def setUp(self):
        self.gh = {"jobs": list(GH["jobs"])}
        self.cfg = {"companies": {"greenhouse": ["acme", "nope"], "lever": ["acme"], "ashby": ["acme"]}}
        self.store = JobStore(":memory:")

    def handler(self, request: httpx.Request):
        url = str(request.url)
        if "greenhouse" in url:
            return httpx.Response(404) if "/nope/" in url else httpx.Response(200, json=self.gh)
        if "lever" in url:
            return httpx.Response(200, json=LEVER)
        return httpx.Response(200, json=ASHBY)

    def poll(self, notifier):
        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(self.handler)) as c:
                return await poll_once(self.cfg, self.store, notifier, c, FILTER)
        return asyncio.run(go())

    def test_first_poll_seeds_silently_then_notifies_new_matches_once(self):
        n = FakeNotifier()
        stats = self.poll(n)
        self.assertEqual((stats["ok"], stats["failed"], stats["new"]), (3, 1, 0))
        self.assertEqual(n.sent, [])

        self.gh["jobs"].append({"id": 102, "title": "Backend Engineer", "location": {"name": "San Francisco"},
                                "absolute_url": "https://x/102"})
        self.gh["jobs"].append({"id": 103, "title": "Engineering Manager", "location": {"name": "San Francisco"},
                                "absolute_url": "https://x/103"})
        stats = self.poll(n)
        self.assertEqual(stats["new"], 2)
        self.assertEqual([j.job_id for j in n.sent], ["102"])  # manager filtered out

        self.poll(n)  # same listing again: no duplicate notification
        self.assertEqual(len(n.sent), 1)

    def test_failed_send_is_retried_next_poll(self):
        self.poll(FakeNotifier())
        self.gh["jobs"].append({"id": 104, "title": "Software Engineer", "location": {"name": "New York"},
                                "absolute_url": "https://x/104"})
        self.poll(FakeNotifier(fail=True))
        n = FakeNotifier()
        self.poll(n)
        self.assertEqual([j.job_id for j in n.sent], ["104"])

    def test_removed_job_is_marked_closed(self):
        self.poll(FakeNotifier())
        self.gh["jobs"] = []
        self.poll(FakeNotifier())
        row = self.store.conn.execute("SELECT closed_at FROM jobs WHERE job_id='101'").fetchone()
        self.assertIsNotNone(row["closed_at"])


if __name__ == "__main__":
    unittest.main()
