"""Hallucination checks and resume rules.

Every output bullet must trace to a source bullet, with no numbers that aren't in that bullet or its
role's or project's metrics. Then the format.yaml limits are applied (counts, always/never include).
"""
import re

from profile_mcp.profile import Profile

from .schema import SelectedBullet, TailoredApplication

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
        allowed_nums = set(_NUM.findall(src.text)) | {n for m in src.metrics for n in _NUM.findall(m)}
        new_nums = set(_NUM.findall(b.text)) - allowed_nums
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
    app.skills = skills[:profile.format["max_skills"]]

    warnings += _apply_format(app, profile)
    app.fit_score = max(0, min(100, app.fit_score))
    return warnings


def _apply_format(app: TailoredApplication, profile: Profile) -> list[str]:
    f, warnings = profile.format, []
    never, always = set(f["never_include"]), [i for i in f["always_include"] if i in profile.bullets]
    bullets = [b for b in app.bullets if b.id not in never]
    if len(bullets) < len(app.bullets):
        warnings.append("Removed bullets your format.yaml never includes.")
    have = {b.id for b in bullets}
    for bid in always:
        if bid not in have:
            bullets.append(SelectedBullet(id=bid, text=profile.bullets[bid].text))
            warnings.append(f"Added [{bid}]: format.yaml always includes it.")

    owner = {bid: profile.bullets[bid].owner for bid in profile.bullets}
    kept, per_owner, projects = [], {}, []
    for b in bullets:   # per-role and per-project caps, and the project count
        src = profile.bullets[b.id]
        if src.section == "projects":
            if src.owner not in projects:
                if len(projects) >= f["max_projects"] and b.id not in always:
                    continue
                projects.append(src.owner)
            cap = f["max_bullets_per_project"]
        else:
            cap = f["max_bullets_per_role"]
        if per_owner.get(src.owner, 0) >= cap and b.id not in always:
            continue
        per_owner[src.owner] = per_owner.get(src.owner, 0) + 1
        kept.append(b)
    if len(kept) < len(bullets):
        warnings.append("Trimmed bullets over the per-role, per-project or project limits in format.yaml.")

    for entry in profile.data.get("experience", []):   # every role keeps its minimum
        picks = [b for b in kept if owner[b.id] == entry["company"]]
        for b in entry.get("bullets", []):
            if len(picks) >= f["min_bullets_per_role"]:
                break
            if b["id"] not in never and b["id"] not in {p.id for p in picks}:
                new = SelectedBullet(id=b["id"], text=profile.bullets[b["id"]].text)
                kept.append(new)
                picks.append(new)
                warnings.append(f"Added [{b['id']}] so {entry['company']} keeps at least "
                                f"{f['min_bullets_per_role']} bullet(s).")

    def removable(b):
        if b.id in always:
            return False
        src = profile.bullets[b.id]
        if src.section == "projects":
            return True
        return sum(owner[k.id] == src.owner for k in kept) > f["min_bullets_per_role"]

    trimmed = False
    while len(kept) > f["max_bullets_total"]:
        # Drop projects first, then the least relevant experience bullet, from the end of the list.
        victim = next((b for b in reversed(kept) if removable(b) and profile.bullets[b.id].section == "projects"),
                      None) or next((b for b in reversed(kept) if removable(b)), None)
        if victim is None:
            break
        kept.remove(victim)
        trimmed = True
    if trimmed:
        warnings.append(f"Trimmed to {f['max_bullets_total']} bullets (format.yaml max_bullets_total).")
    app.bullets = kept
    return warnings
