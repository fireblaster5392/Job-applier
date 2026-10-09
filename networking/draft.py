"""Draft cold networking notes: interest in the company and a 15-minute chat, no job required."""
import anthropic

from outreach.draft import LINKEDIN_LIMIT, ContactDraft, OutreachDrafts
from outreach.finder import Contact
from profile_mcp.profile import Profile
from tailor.claude import TailorError

SYSTEM = """You write short, warm networking notes for a software engineer reaching out to people at
companies they admire, usually startups, even when there's no open role.

Each note asks for a 15-minute chat about the company and its engineering work, or says the
candidate would love to be considered when a fitting role opens. Mention one real thing from the
candidate's profile that fits the company. You only know about the company what you're given
(its name and any roles it has posted): never invent its product, funding, news or values.
No flattery, no "I hope this finds you well".

- linkedin_note: at most 280 characters, first name only, no sign-off.
- email_subject: under 8 words.
- email_body: 60-110 words, plain text, signed with the candidate's first name.

Candidate profile:
{profile}"""


def draft_network_messages(people: list[tuple[Contact, list[str]]], profile: Profile, *, model: str,
                           client: anthropic.Anthropic | None = None) -> dict[int, ContactDraft]:
    """people: (contact, titles of roles that company has posted) pairs. Returns {index: draft}."""
    client = client or anthropic.Anthropic()
    lines = []
    for i, (c, roles) in enumerate(people):
        posted = f"; roles they've posted: {', '.join(roles[:5])}" if roles else ""
        lines.append(f"{i}. {c.name}, {c.title or 'title unknown'} at {c.company}{posted}")
    try:
        resp = client.beta.messages.parse(
            model=model, max_tokens=8000,
            system=[{"type": "text", "text": SYSTEM.format(profile=profile.as_prompt_text()),
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": "Write a note for each person:\n" + "\n".join(lines)}],
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
        if 0 <= d.contact_index < len(people) and d.contact_index not in out:
            if len(d.linkedin_note) > LINKEDIN_LIMIT:
                d.linkedin_note = d.linkedin_note[:LINKEDIN_LIMIT - 1].rsplit(" ", 1)[0] + "…"
            out[d.contact_index] = d
    return out
