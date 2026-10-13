"""Quick fit score for every matching job, so only good fits get the full tailoring."""
import anthropic
from pydantic import BaseModel, Field

from profile_mcp.profile import Profile

from .claude import TailorError

DEFAULT_SCORE_MODEL = "claude-haiku-5-5"

SYSTEM = """You screen job postings for one candidate. Score how well the candidate fits the job,
0-100. Hard requirements the candidate clearly misses (years of experience, degree, clearance,
location or work authorization) cap the score at 40. Be calibrated: 70+ means worth applying.

Candidate profile:
{profile}

{targets}"""


def _targets(profile: Profile) -> str:
    t = profile.data.get("targets") or {}
    if not t:
        return ""
    return "What the candidate wants (roles they're not interested in score under 40):\n" + "\n".join(
        f"- {k}: {', '.join(map(str, v)) if isinstance(v, list) else v}" for k, v in t.items())


class FitScore(BaseModel):
    score: int = Field(description="0-100 fit")
    reasons: list[str] = Field(description="1-3 short reasons, including any dealbreaker")


def score_job(job: dict, description: str, profile: Profile, *, model: str = DEFAULT_SCORE_MODEL,
              client: anthropic.Anthropic | None = None) -> FitScore:
    client = client or anthropic.Anthropic()
    try:
        resp = client.messages.parse(
            model=model,
            max_tokens=1024,
            system=[{"type": "text", "text": SYSTEM.format(profile=profile.as_prompt_text(), targets=_targets(profile)),
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content":
                       f"Job: {job['title']} at {job['company']} ({job.get('location') or 'location not listed'})"
                       f"\n\n{description[:8000]}"}],
            output_format=FitScore,
            output_config={"effort": "low"},
        )
    except anthropic.APIStatusError as exc:
        raise TailorError(f"Claude API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise TailorError(f"Couldn't reach the Claude API: {exc}") from exc
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        raise TailorError(f"Scoring failed (stop_reason={resp.stop_reason}).")
    result = resp.parsed_output
    result.score = max(0, min(100, result.score))
    return result
