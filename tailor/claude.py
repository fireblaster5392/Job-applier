"""Call Claude to tailor one application."""
import logging

import anthropic

from profile_mcp.profile import Profile

from .schema import TailoredApplication

log = logging.getLogger(__name__)
DEFAULT_MODEL = "claude-opus-5-5"

SYSTEM = """You tailor job applications for one candidate. You get the candidate's master resume,
where every bullet has an id, and a job description.

Rules:
- Select bullets only from the master resume and copy their ids exactly.
- You may lightly reword a bullet to mirror the job's language, but keep every fact and number
  as written. Never add a technology, metric, employer or claim that isn't in the source.
- Skills must come from the candidate's skills list.
- The cover letter may only reference experience in the master resume. Be specific and plain;
  no clichés like "I am writing to express my interest".
- Score fit honestly. Missing a hard requirement (years, degree, clearance, location) lowers it a lot.

Master resume:
{profile}

The candidate's resume rules (follow them; they never override the rules above):
{rules}"""


class TailorError(Exception):
    pass


def tailor(job: dict, description: str, profile: Profile, *, model: str = DEFAULT_MODEL,
           client: anthropic.Anthropic | None = None) -> tuple[TailoredApplication, dict]:
    """Return the tailored application and usage info. job needs title, company, location."""
    client = client or anthropic.Anthropic()
    user = (f"Job: {job['title']} at {job['company']} ({job.get('location') or 'location not listed'})\n\n"
            f"Job description:\n{description[:30000]}")
    system = SYSTEM.format(profile=profile.as_prompt_text(profile.shortlist(description)),
                           rules=profile.rules_text())
    try:
        resp = client.beta.messages.parse(
            model=model,
            max_tokens=16000,
            # The profile is identical across jobs (unless a big library is shortlisted), so cache it.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_format=TailoredApplication,
            output_config={"effort": "medium"},
            # If a safety classifier declines, retry on a fallback model in the same call.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.APIStatusError as exc:
        raise TailorError(f"Claude API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise TailorError(f"Couldn't reach the Claude API: {exc}") from exc

    if resp.stop_reason == "refusal":
        raise TailorError("Claude declined to tailor this job.")
    if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
        raise TailorError(f"Claude's response was incomplete (stop_reason={resp.stop_reason}).")
    usage = {
        "model": resp.model,
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
    }
    log.info("tailored %s at %s: %s", job["title"], job["company"], usage)
    return resp.parsed_output, usage
