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

from .profile import default_profile

OUTPUT_DIR = Path(os.environ.get("RESUME_OUTPUT_DIR", "output"))
mcp = MCPServer("profile", instructions=(
    "The candidate's master resume. Tailored resumes may only select and lightly reword bullets "
    "returned by search_experience, keeping every fact and number."))


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
