"""Draft a LinkedIn note and an email per contact with Claude, grounded in the job and your profile."""
import anthropic
from pydantic import BaseModel, Field

from profile_mcp.profile import Profile
from tailor.claude import TailorError

from .finder import Contact

LINKEDIN_LIMIT = 300  # LinkedIn's limit for a connection-request note

SYSTEM = """You write short, specific outreach messages for a job seeker who just applied to a role.
Each message must mention one real overlap between the candidate's experience (from the profile
below) and the job or the contact's role. Never invent facts about the candidate or the contact.
No flattery, no "I hope this finds you well". Ask for something small: a quick chat, or that they
pass the application to the hiring team.

- linkedin_note: at most 280 characters, first name only, no sign-off.
- email_subject: under 8 words.
- email_body: 70-120 words, plain text, signed with the candidate's first name.

Candidate profile:
{profile}"""


class ContactDraft(BaseModel):
    contact_index: int = Field(description="index of the contact in the list given")
    linkedin_note: str
    email_subject: str
    email_body: str


class OutreachDrafts(BaseModel):
    drafts: list[ContactDraft]


def draft_messages(job: dict, contacts: list[Contact], profile: Profile, *, model: str,
                   client: anthropic.Anthropic | None = None) -> dict[int, ContactDraft]:
    """Returns {contact index: draft}."""
    client = client or anthropic.Anthropic()
    people = "\n".join(f"{i}. {c.name}, {c.title or 'title unknown'}" for i, c in enumerate(contacts))
    user = (f"The candidate applied to: {job['title']} at {job['company']} ({job.get('location', '')}).\n"
            f"Fit notes: {job.get('fit_notes', '')}\n\nContacts at {job['company']}:\n{people}")
    try:
        resp = client.beta.messages.parse(
            model=model, max_tokens=8000,
            system=[{"type": "text", "text": SYSTEM.format(profile=profile.as_prompt_text()),
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_format=OutreachDrafts,
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.APIStatusError as exc:
        raise TailorError(f"Claude API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise TailorError(f"Couldn't reach the Claude API: {exc}") from exc
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        raise TailorError(f"Drafting failed (stop_reason={resp.stop_reason}).")
    out = {}
    for d in resp.parsed_output.drafts:
        if 0 <= d.contact_index < len(contacts) and d.contact_index not in out:
            if len(d.linkedin_note) > LINKEDIN_LIMIT:
                d.linkedin_note = d.linkedin_note[:LINKEDIN_LIMIT - 1].rsplit(" ", 1)[0] + "…"
            out[d.contact_index] = d
    return out
