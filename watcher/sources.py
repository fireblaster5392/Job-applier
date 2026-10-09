"""Fetch and normalise jobs from the public Greenhouse, Lever and Ashby board APIs.

All three are official, unauthenticated, read-only endpoints meant for career pages.
"""
from datetime import datetime, timezone

import httpx

from .models import Job

URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
}


class BoardNotFound(Exception):
    """The slug doesn't exist on that ATS (usually a wrong slug in companies.toml)."""


def _ms_to_iso(ms) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def parse_greenhouse(slug: str, data: dict) -> list[Job]:
    jobs = []
    for j in data.get("jobs", []):
        location = (j.get("location") or {}).get("name", "")
        jobs.append(Job(
            ats="greenhouse",
            company=slug,
            job_id=str(j["id"]),
            title=j.get("title", "").strip(),
            location=location,
            url=j.get("absolute_url", ""),
            apply_url=j.get("absolute_url", ""),
            remote="remote" in location.lower(),
            # Greenhouse's list only has updated_at; first_published appears on newer boards.
            posted_at=j.get("first_published") or j.get("updated_at") or "",
            raw=j,
        ))
    return jobs


def parse_lever(slug: str, data: list) -> list[Job]:
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        location = cats.get("location") or ", ".join(cats.get("allLocations") or [])
        jobs.append(Job(
            ats="lever",
            company=slug,
            job_id=str(j["id"]),
            title=j.get("text", "").strip(),
            location=location,
            url=j.get("hostedUrl", ""),
            apply_url=j.get("applyUrl", ""),
            department=cats.get("team") or cats.get("department") or "",
            remote=j.get("workplaceType") == "remote" or "remote" in location.lower(),
            posted_at=_ms_to_iso(j.get("createdAt")),
            raw=j,
        ))
    return jobs


def parse_ashby(slug: str, data: dict) -> list[Job]:
    jobs = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""] + [
            s.get("location", "") for s in j.get("secondaryLocations") or []
        ]
        location = " / ".join(l for l in locs if l)
        jobs.append(Job(
            ats="ashby",
            company=slug,
            job_id=str(j["id"]),
            title=j.get("title", "").strip(),
            location=location,
            url=j.get("jobUrl", ""),
            apply_url=j.get("applyUrl", ""),
            department=j.get("department") or j.get("team") or "",
            remote=bool(j.get("isRemote")) or j.get("workplaceType") == "Remote",
            posted_at=j.get("publishedAt", ""),
            raw=j,
        ))
    return jobs


PARSERS = {"greenhouse": parse_greenhouse, "lever": parse_lever, "ashby": parse_ashby}


async def fetch_board(client: httpx.AsyncClient, ats: str, slug: str) -> list[Job]:
    resp = await client.get(URLS[ats].format(slug=slug))
    if resp.status_code == 404:
        raise BoardNotFound(f"{ats}/{slug}")
    resp.raise_for_status()
    return PARSERS[ats](slug, resp.json())
