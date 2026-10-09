from pydantic import BaseModel, Field


class SelectedBullet(BaseModel):
    id: str = Field(description="id of the source bullet in the master resume, copied exactly")
    text: str = Field(description="the bullet, lightly reworded for this job; same facts and numbers as the source")


class TailoredApplication(BaseModel):
    fit_score: int = Field(description="0-100: how well the candidate fits this job")
    fit_reasons: list[str] = Field(description="2-4 short reasons for the score")
    gaps: list[str] = Field(description="requirements in the job the profile doesn't show")
    summary: str = Field(description="2-sentence resume summary aimed at this job, using only facts from the profile")
    bullets: list[SelectedBullet] = Field(description="6-10 most relevant bullets, most relevant first within each role")
    skills: list[str] = Field(description="skills from the profile's skills list, most relevant first")
    cover_letter: str = Field(description="cover letter body, 3 short paragraphs, plain text, no greeting or signature")
