"""Fetch and normalise jobs from the public Greenhouse, Lever and Ashby board APIs.

All three are official, unauthenticated, read-only endpoints meant for career pages.
"""
import html
import re
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


def _strip_html(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<\s*(br|/p|/li|/h\d)\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def job_description(job: Job, client: httpx.Client | None = None) -> str:
    """Full plain-text job description. Lever and Ashby include it in the listing;
    Greenhouse needs one extra request for the job's detail."""
    raw = job.raw or {}
    if job.ats == "lever":
        parts = [raw.get("descriptionPlain") or _strip_html(raw.get("description", ""))]
        for section in raw.get("lists") or []:
            parts.append(f"{section.get('text', '')}\n{_strip_html(section.get('content', ''))}")
        parts.append(raw.get("additionalPlain") or _strip_html(raw.get("additional", "")))
        return "\n\n".join(p for p in parts if p).strip()
    if job.ats == "ashby":
        return raw.get("descriptionPlain") or _strip_html(raw.get("descriptionHtml", ""))
    if job.ats == "greenhouse":
        if raw.get("content"):
            return _strip_html(raw["content"])
        own = client is None
        client = client or httpx.Client(timeout=20)
        try:
            resp = client.get(f"https://boards-api.greenhouse.io/v1/boards/{job.company}/jobs/{job.job_id}")
            resp.raise_for_status()
            return _strip_html(resp.json().get("content", ""))
        finally:
            if own:
                client.close()
    if raw.get("source") == "jobspy":   # Indeed, Glassdoor, LinkedIn: JobSpy returns it as markdown
        return raw.get("description", "")
    return ""
