"""Job-search MCP server: your profile and your job pipeline, for Claude Code, Claude Desktop or any MCP client.

    python -m profile_mcp.server                         # stdio (local): claude mcp add job-search -- python -m profile_mcp.server
    python -m profile_mcp.server --http --port 8765      # streamable HTTP at http://HOST:8765/mcp (the VPS service)

The pipeline (worker, apply queue) uses the same profile code directly; this server is how you
and Claude reach it interactively.
"""
import argparse
import json
import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from tailor.render import cover_letter_html, resume_html
from tailor.guardrails import enforce
from tailor.schema import TailoredApplication

from .profile import check_seed, default_profile

OUTPUT_DIR = Path(os.environ.get("RESUME_OUTPUT_DIR", "output"))
mcp = MCPServer("job-search", instructions=(
    "The candidate's profile (roles, project library, skills, resume format rules) and job pipeline. "
    "Tailored resumes may only select and reword bullets returned by search_experience or "
    "get_resume_profile, keeping every fact and number, and must follow the profile's format rules."))


@mcp.tool()
def search_experience(query: str, k: int = 8) -> list[dict]:
    """Find resume bullets relevant to a query (skills, job requirements). Returns id, text, tags, role."""
    return [{"id": b.id, "text": b.text, "tags": list(b.tags), "role": b.owner}
            for b in default_profile().search_experience(query, k)]


@mcp.tool()
def get_profile() -> dict:
    """Contact info, links, skills, education and standard application answers."""
    return default_profile().get_profile()


@mcp.tool()
def render_resume(selection_json: str, company: str, slug: str) -> dict:
    """Render a tailored resume + cover letter to HTML files.

    selection_json is a TailoredApplication: {fit_score, fit_reasons, gaps, summary,
    bullets: [{id, text}], skills, cover_letter}. Unknown bullet ids, invented numbers and
    skills outside the profile are removed; the fixes are returned as warnings.
    """
    profile = default_profile()
    app = TailoredApplication.model_validate(json.loads(selection_json))
    warnings = enforce(app, profile)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    resume = OUTPUT_DIR / f"{slug}-resume.html"
    letter = OUTPUT_DIR / f"{slug}-cover-letter.html"
    resume.write_text(resume_html(profile, app))
    letter.write_text(cover_letter_html(profile, app, company))
    return {"resume": str(resume), "cover_letter": str(letter), "warnings": warnings}


def _store():
    from watcher.db import JobStore
    return JobStore(os.environ.get("DB_PATH", "data/jobs.db"))


@mcp.tool()
def check_profile() -> dict:
    """Check the seed files in profile/: errors stop the profile loading, warnings are worth fixing.
    Also counts roles, projects, bullets and skills."""
    errors, warnings = check_seed()
    counts = {}
    if not errors:
        p = default_profile()
        counts = {"roles": len(p.data.get("experience", [])), "projects": len(p.data.get("projects", [])),
                  "bullets": len(p.bullets), "skills": sum(len(v) for v in p.data.get("skills", {}).values())}
    return {"ok": not errors, "errors": errors, "warnings": warnings, **counts}


# --- The tailoring contract (docs/tailoring-server.md). This server is the reference
# implementation: same answers as the built-in Claude tailoring. A custom server exposes these
# three tools with the same inputs and outputs and the pipeline uses it via [tailor] provider = "mcp".

@mcp.tool()
def get_resume_profile() -> dict:
    """The whole merged profile: basics, targets, education, experience and projects (with bullet
    ids, tags, tech, keywords, metrics), skill groups, keyword synonyms and format rules."""
    return default_profile().data


@mcp.tool()
def score_fit(job: dict, description: str) -> dict:
    """Quick fit score. job: title, company, location, ats, url. Returns {score: 0-100, reasons: [...]}."""
    from tailor.provider import ClaudeProvider
    return ClaudeProvider(profile_loader=default_profile).score(job, description).model_dump()


@mcp.tool()
def tailor_application(job: dict, description: str) -> dict:
    """Tailored resume selection and cover letter for one job, as a TailoredApplication plus usage."""
    from tailor.provider import ClaudeProvider
    app, usage = ClaudeProvider(profile_loader=default_profile).tailor(job, description)
    return {**app.model_dump(), "usage": usage}


@mcp.tool()
def list_jobs(status: str = "", limit: int = 25) -> list[dict]:
    """Matching jobs in the pipeline, newest first. status: tailored, needs_review, needs_you,
    submitted, below_threshold, ... or empty for all."""
    rows = _store().list_jobs(status=status or None, limit=limit)
    return [{"id": r["rowid"], "title": r["title"], "company": r["company"], "location": r["location"],
             "status": r["status"], "score": r["score"], "application": r["app_status"], "url": r["url"]}
            for r in rows]


@mcp.tool()
def get_job(job_id: int) -> dict:
    """One job with its tailored resume selection, cover letter, and application status."""
    store = _store()
    row = store.get_job(job_id)
    if row is None:
        return {"error": f"no job {job_id}"}
    t = store.get_tailored(job_id)
    return {"id": job_id, "title": row["title"], "company": row["company"], "location": row["location"],
            "status": row["status"], "application": row["app_status"], "url": row["url"],
            "tailored": t["data"] if t else None, "guardrail_fixes": t["warnings"] if t else []}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    ap.add_argument("--host", default=os.environ.get("MCP_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "8765")))
    args = ap.parse_args()
    if args.http:
        kwargs = {}
        hosts = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
        if hosts:
            # Only these Host headers are served (DNS-rebinding protection), e.g. the Tailscale name.
            kwargs["transport_security"] = TransportSecuritySettings(
                allowed_hosts=hosts + [f"{h}:*" for h in hosts if ":" not in h])
        mcp.run("streamable-http", host=args.host, port=args.port, **kwargs)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
