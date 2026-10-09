"""Hallucination checks: every output bullet must trace to a source bullet, with no new numbers."""
import re

from profile_mcp.profile import Profile

from .schema import TailoredApplication

_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def enforce(app: TailoredApplication, profile: Profile) -> list[str]:
    """Fix the application in place and return what was changed, for display."""
    warnings = []
    kept = []
    seen = set()
    for b in app.bullets:
        src = profile.bullets.get(b.id)
        if src is None:
            warnings.append(f"Dropped a bullet with unknown id {b.id!r}.")
            continue
        if b.id in seen:
            continue
        seen.add(b.id)
        new_nums = set(_NUM.findall(b.text)) - set(_NUM.findall(src.text))
        if new_nums:
            warnings.append(f"Reverted [{b.id}] to the original: the rewrite added {sorted(new_nums)}.")
            b.text = src.text
        kept.append(b)
    app.bullets = kept

    allowed = {s.lower(): s for group in profile.data.get("skills", {}).values() for s in group}
    skills = []
    for s in app.skills:
        if s.lower() in allowed:
            skills.append(allowed[s.lower()])
        else:
            warnings.append(f"Dropped skill {s!r}: not in your profile.")
    app.skills = skills

    app.fit_score = max(0, min(100, app.fit_score))
    return warnings
