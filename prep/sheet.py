"""Interview prep sheets: drafted when an interview email lands, or on demand from the dashboard.

A sheet pulls together the job description, what you sent (the tailored resume and cover letter),
your profile and STAR stories (profile/stories/*.md), the interview email, and recent public
information about the company (Brave web search, if BRAVE_API_KEY is set). Claude turns that into
company notes, the role in plain words, why you fit (tied to your real bullets), likely questions
with the story to use for each, gaps to prepare for, and questions to ask.

Like tailoring, it never invents your experience: suggested answers point at your bullets and
stories by id/title, and a question your profile can't answer is listed as a gap.
"""
import json
import logging
import os
from pathlib import Path

import anthropic
import httpx
from jinja2 import Environment
from pydantic import BaseModel, Field

from profile_mcp.profile import PROFILE_DIR, Profile
from tailor.claude import TailorError
from watcher.db import JobStore, now, row_to_job
from watcher.sources import job_description

log = logging.getLogger(__name__)
DEFAULT_PREP_MODEL = "claude-opus-5-5"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"


class Question(BaseModel):
    question: str
    why_they_ask: str = Field(description="what the interviewer is checking, one line")
    answer_outline: str = Field(description="2-4 short bullets in plain text, using only the candidate's real experience")
    use: list[str] = Field(description="bullet ids or story titles from the profile to draw on; empty if none fits")


class PrepSheet(BaseModel):
    company_notes: list[str] = Field(description="4-8 facts about the company: product, customers, stage, recent news; "
                                     "only from the provided sources, each short")
    role_summary: str = Field(description="what the job really is, 2-3 sentences")
    interview_details: list[str] = Field(description="format, date/time, interviewers, anything to prepare, from the "
                                         "interview email; empty if it says nothing")
    why_you: list[str] = Field(description="3-5 requirement -> matching real experience pairs, e.g. "
                               "'Python APIs at scale -> exco-api: 2M requests a day'")
    likely_questions: list[Question] = Field(description="8-12: behavioural, technical for this stack, and about the company")
    gaps: list[str] = Field(description="requirements the profile doesn't show, each with a one-line honest way to address it")
    questions_to_ask: list[str] = Field(description="4-6 specific questions for the interviewers")
    sources: list[str] = Field(description="URLs of company sources used")


SYSTEM = """You prepare a candidate for a job interview. Use only the sources below: the job
description, what the candidate sent, their profile and stories, the interview email, and the
company search results. Never invent the candidate's experience, numbers or skills. Point answer
outlines at real bullet ids or story titles. If nothing in the profile fits a likely question,
leave `use` empty and say what they could talk about honestly. Company notes come only from the
job description and search results; say less rather than guess.

Candidate profile:
{profile}

Candidate's stories:
{stories}"""

env = Environment(autoescape=True, trim_blocks=True, lstrip_blocks=True)
PAGE = env.from_string("""<!doctype html>
<html><head><meta charset="utf-8"><title>Prep: {{ title }} at {{ company }}</title>
<style>
  body { font: 15px/1.5 -apple-system, Segoe UI, Helvetica, Arial, sans-serif; max-width: 760px; margin: 24px auto; padding: 0 16px; color: #111; }
  h1 { font-size: 22px; margin-bottom: 0; } h2 { font-size: 16px; margin-top: 24px; border-bottom: 1px solid #ddd; }
  .muted { color: #666; } .q { margin: 12px 0; } .q b { display: block; } .use { color: #555; font-size: 13px; }
  pre { white-space: pre-wrap; font: inherit; margin: 4px 0; }
  @media (prefers-color-scheme: dark) { body { background: #111; color: #eee; } .muted, .use { color: #aaa; } h2 { border-color: #333; } }
</style></head><body>
<h1>{{ title }} at {{ company }}</h1>
<div class="muted">Interview prep · drafted {{ created }}</div>
{% if s.interview_details %}<h2>The interview</h2><ul>{% for x in s.interview_details %}<li>{{ x }}</li>{% endfor %}</ul>{% endif %}
<h2>The role</h2><p>{{ s.role_summary }}</p>
<h2>The company</h2><ul>{% for x in s.company_notes %}<li>{{ x }}</li>{% endfor %}</ul>
<h2>Why you</h2><ul>{% for x in s.why_you %}<li>{{ x }}</li>{% endfor %}</ul>
<h2>Likely questions</h2>
{% for q in s.likely_questions %}<div class="q"><b>{{ loop.index }}. {{ q.question }}</b>
<span class="muted">{{ q.why_they_ask }}</span><pre>{{ q.answer_outline }}</pre>
{% if q.use %}<div class="use">Use: {% for u in q.use %}{{ bullets.get(u, u) }}{% if not loop.last %} · {% endif %}{% endfor %}</div>{% endif %}</div>
{% endfor %}
{% if s.gaps %}<h2>Gaps to prepare for</h2><ul>{% for x in s.gaps %}<li>{{ x }}</li>{% endfor %}</ul>{% endif %}
<h2>Questions to ask them</h2><ul>{% for x in s.questions_to_ask %}<li>{{ x }}</li>{% endfor %}</ul>
{% if s.sources %}<h2>Sources</h2><ul>{% for u in s.sources %}<li><a href="{{ u }}">{{ u }}</a></li>{% endfor %}</ul>{% endif %}
</body></html>""")


def load_stories(directory: Path = PROFILE_DIR / "stories", limit: int = 20000) -> str:
    parts, total = [], 0
    for path in sorted(directory.glob("*.md")) if directory.exists() else []:
        text = path.read_text().strip()
        parts.append(f"### {path.stem}\n{text}")
        total += len(text)
        if total > limit:
            break
    return "\n\n".join(parts) or "(no stories yet: add STAR stories as profile/stories/*.md)"


def company_news(company: str, client: httpx.Client | None, key: str | None) -> list[dict]:
    """Top web results about the company, or [] without a Brave key."""
    if not key:
        return []
    own = client is None
    client = client or httpx.Client(timeout=20)
    try:
        resp = client.get(BRAVE_URL, params={"q": f'"{company}" company news product funding', "count": 8},
                          headers={"X-Subscription-Token": key, "Accept": "application/json"})
        resp.raise_for_status()
        return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("description", "")}
                for r in resp.json().get("web", {}).get("results", [])[:8]]
    except httpx.HTTPError as exc:
        log.warning("company search failed for %s: %s", company, exc)
        return []
    finally:
        if own:
            client.close()


def interview_email(store: JobStore, rowid: int) -> str:
    row = store.conn.execute(
        "SELECT from_addr, subject, snippet, received_at FROM inbox WHERE job_rowid=? AND kind='application' "
        "AND category IN ('interview_request', 'assessment') ORDER BY received_at DESC LIMIT 1", (rowid,)).fetchone()
    if row is None:
        return "(no interview email on file)"
    return f"From: {row['from_addr']}\nSubject: {row['subject']}\nReceived: {row['received_at']}\n\n{row['snippet']}"


def build_prep(store: JobStore, rowid: int, profile: Profile, *, model: str = DEFAULT_PREP_MODEL,
               client: anthropic.Anthropic | None = None, http_client: httpx.Client | None = None,
               stories_dir: Path = PROFILE_DIR / "stories") -> dict:
    row = store.get_job(rowid)
    if row is None:
        raise KeyError(f"no job {rowid}")
    job = row_to_job(row)
    tailored = store.get_tailored(rowid)
    description = (tailored or {}).get("description") or job_description(job, http_client)
    news = company_news(job.company, http_client, os.environ.get("BRAVE_API_KEY"))
    sent = "(not tailored)"
    if tailored:
        d = tailored["data"]
        sent = ("Resume bullets sent: " + "; ".join(f"[{b['id']}] {b['text']}" for b in d["bullets"])
                + f"\n\nCover letter:\n{d['cover_letter']}")
    user = (f"Job: {job.title} at {job.company} ({job.location})\n\nJob description:\n{(description or '')[:20000]}"
            f"\n\nWhat the candidate sent:\n{sent}\n\nInterview email:\n{interview_email(store, rowid)}"
            f"\n\nCompany search results:\n{json.dumps(news, indent=1) if news else '(none)'}")
    client = client or anthropic.Anthropic()
    try:
        resp = client.messages.parse(
            model=model, max_tokens=12000,
            system=[{"type": "text", "text": SYSTEM.format(profile=profile.as_prompt_text(),
                                                           stories=load_stories(stories_dir)),
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_format=PrepSheet, output_config={"effort": "medium"})
    except anthropic.APIError as exc:
        raise TailorError(f"Couldn't draft the prep sheet: {exc}") from exc
    if resp.parsed_output is None:
        raise TailorError(f"Couldn't draft the prep sheet (stop_reason={resp.stop_reason}).")
    sheet = resp.parsed_output
    store.conn.execute(
        """INSERT INTO prep_sheets (job_rowid, data, model, created_at) VALUES (?, ?, ?, ?)
           ON CONFLICT (job_rowid) DO UPDATE SET data=excluded.data, model=excluded.model, created_at=excluded.created_at""",
        (rowid, sheet.model_dump_json(), model, now()))
    store.conn.commit()
    return get_prep(store, rowid)


def get_prep(store: JobStore, rowid: int) -> dict | None:
    row = store.conn.execute("SELECT * FROM prep_sheets WHERE job_rowid=?", (rowid,)).fetchone()
    if row is None:
        return None
    return {"job_rowid": rowid, "data": PrepSheet.model_validate_json(row["data"]), "model": row["model"],
            "created_at": row["created_at"]}


def jobs_needing_prep(store: JobStore) -> list:
    return store.conn.execute(
        "SELECT rowid, * FROM jobs WHERE app_status='interview' AND rowid NOT IN (SELECT job_rowid FROM prep_sheets)"
    ).fetchall()


def prep_html(store: JobStore, rowid: int, profile: Profile) -> str:
    rec = get_prep(store, rowid)
    row = store.get_job(rowid)
    bullets = {bid: f"{bid} ({b.owner})" for bid, b in profile.bullets.items()}
    return PAGE.render(s=rec["data"], title=row["title"], company=row["company"], created=rec["created_at"][:10],
                       bullets=bullets)
