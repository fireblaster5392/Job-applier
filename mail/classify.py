"""Classify a company's application email with the cheap model."""
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from tailor.claude import TailorError

DEFAULT_MODEL = "claude-haiku-5-5"


class ApplicationEmail(BaseModel):
    is_application_email: bool = Field(description="false for newsletters, job alerts, marketing")
    company: str = Field(description="the hiring company's name")
    category: Literal["confirmation", "rejection", "interview_request", "assessment", "offer", "other"]
    summary: str = Field(description="one short sentence, including any deadline")


def classify(mail: dict, *, model: str = DEFAULT_MODEL, client: anthropic.Anthropic | None = None) -> ApplicationEmail:
    client = client or anthropic.Anthropic()
    try:
        resp = client.messages.parse(
            model=model, max_tokens=1024,
            system="You classify emails a job seeker receives from companies about their job applications.",
            messages=[{"role": "user", "content":
                       f"From: {mail['from_addr']}\nSubject: {mail['subject']}\n\n{mail['body'][:4000]}"}],
            output_format=ApplicationEmail,
            output_config={"effort": "low"},
        )
    except anthropic.APIError as exc:
        raise TailorError(f"Couldn't classify email: {exc}") from exc
    if resp.parsed_output is None:
        raise TailorError(f"Couldn't classify email (stop_reason={resp.stop_reason}).")
    return resp.parsed_output
