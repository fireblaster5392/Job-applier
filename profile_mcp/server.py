"""Profile MCP server: lets Claude (Claude Code, Desktop, or the Agent SDK) use your profile.

    python -m profile_mcp.server            # stdio, for `claude mcp add profile -- python -m profile_mcp.server`
"""
import json
import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer

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


if __name__ == "__main__":
    mcp.run()
