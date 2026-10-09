"""Answer an application's custom questions from the profile. Never used for legal questions."""
import anthropic
from pydantic import BaseModel, Field as PField

from profile_mcp.profile import Profile
from tailor.claude import TailorError

from .fields import Field

SYSTEM = """You fill in custom questions on a job application for one candidate, using only facts
from their profile below and the job description. Rules:
- If a question has options, answer with one option copied exactly.
- If the profile doesn't contain what's needed (a fact, a preference, a number), set
  can_answer=false instead of guessing.
- Free-text answers: first person, specific, 2-5 sentences unless the question asks for less.
- Never answer legal, demographic, salary or work-authorization questions: set can_answer=false.

Candidate profile:
{profile}"""


class Answer(BaseModel):
    field_id: str
    answer: str
    can_answer: bool = PField(description="false if the profile doesn't support an answer")


class Answers(BaseModel):
    answers: list[Answer]


def answer_questions(fields: list[Field], job: dict, description: str, profile: Profile, *,
                     model: str, client: anthropic.Anthropic | None = None) -> dict[str, str | None]:
    """{field id: answer or None when Claude can't answer from the profile}."""
    if not fields:
        return {}
    client = client or anthropic.Anthropic()
    qs = "\n".join(
        f"- id={f.id} ({'required' if f.required else 'optional'}, {f.type}): {f.label}"
        + (f"\n  options: {f.options}" if f.options else "") for f in fields)
    try:
        resp = client.beta.messages.parse(
            model=model, max_tokens=8000,
            system=[{"type": "text", "text": SYSTEM.format(profile=profile.as_prompt_text()),
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content":
                       f"Job: {job['title']} at {job['company']}\n\nDescription:\n{description[:12000]}\n\nQuestions:\n{qs}"}],
            output_format=Answers,
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.APIError as exc:
        raise TailorError(f"Claude couldn't answer the form questions: {exc}") from exc
    if resp.parsed_output is None:
        raise TailorError(f"Claude couldn't answer the form questions (stop_reason={resp.stop_reason}).")
    by_id = {f.id: f for f in fields}
    out: dict[str, str | None] = {f.id: None for f in fields}
    for a in resp.parsed_output.answers:
        f = by_id.get(a.field_id)
        if not f or not a.can_answer or not a.answer.strip():
            continue
        if f.options and a.answer not in f.options:
            continue                       # off-list answer: treat as unanswered
        out[f.id] = a.answer.strip()
    return out
