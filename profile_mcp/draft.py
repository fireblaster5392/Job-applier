"""Turn rough notes into profile seed files: python -m profile_mcp.draft [--apply]

Write what you know in profile/notes/*.md (or .txt), in any shape: your skills, each job and
project, what you built, the tools, any numbers you remember. This drafts experience.yaml,
projects/*.yaml and skills.yaml into profile/draft/, with bullets written the way recruiters
read them: a strong verb, what you built, how, and the result. Each project gets several bullets
from different angles (backend, ML, infra, product...), so tailoring can pick the ones that fit
each job.

Nothing is invented. Every bullet must come from your notes, and a bullet with a number that
isn't in your notes is dropped. Where a number would make a bullet stronger, it goes in
profile/draft/QUESTIONS.md for you to answer in your notes before drafting again.

Read the draft, edit anything, then:

    python -m profile_mcp.draft --apply    # copies the draft into profile/ (old files backed up)
    python -m profile_mcp.check
"""
import argparse
import re
import shutil
import sys
import time
import tomllib
from pathlib import Path

import anthropic
import yaml
from pydantic import BaseModel, Field

from .profile import PROFILE_DIR, _ID, check_seed

DEFAULT_MODEL = "claude-opus-5-5"
_NUM = re.compile(r"\d+(?:[.,]\d+)?")

SYSTEM = """You turn a candidate's rough notes into the source material for their resume.

Write bullets the way recruiters and hiring managers read them:
- Start with a strong past-tense verb (Built, Designed, Cut, Shipped, Led, Automated). Never
  "Responsible for", "Helped with" or "Worked on".
- Say what was built, the tools used, and the result or scale, in one line of 20-30 words.
- Use the plain industry terms a job posting would use (REST API, CI/CD, data pipeline, LLM),
  so keyword filters find them.
- For each job and project, write 3-6 bullets from different angles (for example backend,
  data/ML, infrastructure, product impact, leadership), each with its angle, so a resume for
  any kind of role can pick the right ones. Don't repeat the same fact in two bullets.

The one hard rule: everything comes from the notes. Never add a number, technology, employer,
user count, outcome or claim the notes don't state. Copy numbers exactly as the notes give them.
If a bullet would be much stronger with a number the notes don't give (users, speedup, scale,
money), write the bullet without it and add a short question asking for that number.

Also return the skill groups (only skills the notes show the candidate used) and posting
keywords mapped to words in the notes (for example "k8s" -> ["kubernetes"])."""


class DraftBullet(BaseModel):
    id: str = Field(description="short slug: lowercase letters, digits, dashes; prefix with the entry id")
    text: str = Field(description="one recruiter-ready line, facts only from the notes")
    angle: str = Field(description="what kind of role this bullet sells: backend, ml, infra, product, leadership...")


class DraftEntry(BaseModel):
    kind: str = Field(description='"experience" for a job or internship, "project" for a project')
    id: str = Field(description="short slug for the job or project")
    name: str = Field(description="company name for a job, project name for a project")
    title: str = Field(description="job title, or the candidate's role on the project; empty if unknown")
    location: str = Field(description="empty if unknown")
    start: str = Field(description="YYYY-MM, or empty if unknown")
    end: str = Field(description='YYYY-MM, "present", or empty if unknown')
    url: str = Field(description="project link if the notes give one, else empty")
    summary: str = Field(description="one line on what it is")
    tech: list[str] = Field(description="tools and languages used, as the notes name them")
    keywords: list[str] = Field(description="domains and themes: payments, nlp, distributed systems...")
    metrics: list[str] = Field(description="facts with numbers, quoted from the notes")
    bullets: list[DraftBullet]


class SkillGroup(BaseModel):
    name: str
    skills: list[str]


class KeywordMap(BaseModel):
    term: str = Field(description="what job postings say")
    means: list[str] = Field(description="the words in the candidate's notes it corresponds to")


class Draft(BaseModel):
    entries: list[DraftEntry] = Field(description="jobs newest first, then projects newest first")
    skills: list[SkillGroup]
    keywords: list[KeywordMap]
    questions: list[str] = Field(description="numbers or facts the candidate could add to strengthen bullets")


def read_notes(directory: Path) -> str:
    files = sorted(p for p in directory.glob("*") if p.suffix in {".md", ".txt", ".yaml", ".yml"})
    return "\n\n".join(f"## {p.name}\n{p.read_text()}" for p in files)


def _nums(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUM.findall(text)}


def check_draft(draft: Draft, notes: str) -> list[str]:
    """Drop anything with a number the notes don't have, and fix ids. Returns what was changed."""
    fixes, allowed, seen = [], _nums(notes), set()

    def slug(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "item"

    for e in draft.entries:
        e.id = e.id if _ID.match(e.id) else slug(e.id or e.name)
        bad = [m for m in e.metrics if _nums(m) - allowed]
        for m in bad:
            fixes.append(f"{e.name}: dropped metric {m!r}, a number isn't in your notes.")
        e.metrics = [m for m in e.metrics if m not in bad]
        kept = []
        for b in e.bullets:
            extra = _nums(b.text) - allowed
            if extra:
                fixes.append(f"{e.name}: dropped {b.text!r}, {sorted(extra)} isn't in your notes.")
                continue
            bid = b.id if _ID.match(b.id) else slug(b.id)
            while bid in seen:
                bid += "-2"
            seen.add(bid)
            b.id = bid
            kept.append(b)
        e.bullets = kept
    return fixes


def draft_profile(notes: str, *, model: str = DEFAULT_MODEL, client=None) -> tuple[Draft, list[str]]:
    client = client or anthropic.Anthropic()
    resp = client.messages.parse(
        model=model,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": f"My notes:\n\n{notes}"}],
        output_format=Draft,
        output_config={"effort": "high"},
    )
    if resp.parsed_output is None:
        raise RuntimeError(f"Claude's draft was incomplete (stop_reason={resp.stop_reason}).")
    draft = resp.parsed_output
    return draft, check_draft(draft, notes)


def _entry_yaml(e: DraftEntry) -> dict:
    out = {"id": e.id}
    if e.kind == "project":
        out |= {"name": e.name, "role": e.title, "url": e.url}
    else:
        out |= {"company": e.name, "title": e.title, "location": e.location}
    out |= {"start": e.start, "end": e.end, "summary": e.summary, "tech": e.tech,
            "keywords": e.keywords, "metrics": e.metrics,
            "bullets": [{"id": b.id, "text": b.text, "tags": [b.angle]} for b in e.bullets]}
    return {k: v for k, v in out.items() if v not in ("", [], None)}


def _dump(data) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110)


def write_draft(draft: Draft, fixes: list[str], out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    (out / "projects").mkdir(parents=True)
    header = "# DRAFTED from profile/notes. Check every line is true, edit freely, then --apply.\n"
    jobs = [_entry_yaml(e) for e in draft.entries if e.kind != "project"]
    (out / "experience.yaml").write_text(header + _dump(jobs))
    for e in draft.entries:
        if e.kind == "project":
            (out / "projects" / f"{e.id}.yaml").write_text(header + _dump(_entry_yaml(e)))
    skills = {"groups": {g.name: g.skills for g in draft.skills},
              "keywords": {k.term: k.means for k in draft.keywords}}
    (out / "skills.yaml").write_text(header + _dump(skills))
    lines = ["# Questions", "", "Answer these in your notes (profile/notes/) and run the draft again.",
             "Each answer can become a number on your resume.", ""]
    lines += [f"- {q}" for q in draft.questions] or ["- None."]
    if fixes:
        lines += ["", "# Dropped", "", "These weren't backed by your notes:", ""] + [f"- {f}" for f in fixes]
    (out / "QUESTIONS.md").write_text("\n".join(lines) + "\n")


def apply_draft(draft_dir: Path, profile_dir: Path) -> Path:
    """Copy the draft into profile/, backing up the files it replaces. Returns the backup dir."""
    backup = profile_dir / ".backup" / time.strftime("%Y%m%d-%H%M%S")
    backup.mkdir(parents=True)
    for name in ("experience.yaml", "skills.yaml", "projects"):
        src, dst = draft_dir / name, profile_dir / name
        if dst.exists():
            shutil.move(str(dst), str(backup / name))
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    return backup


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="copy profile/draft into profile/")
    ap.add_argument("--profile", type=Path, default=PROFILE_DIR)
    args = ap.parse_args(argv)
    notes_dir, draft_dir = args.profile / "notes", args.profile / "draft"

    if args.apply:
        if not (draft_dir / "experience.yaml").exists():
            print("No draft yet. Run: python -m profile_mcp.draft")
            return 1
        backup = apply_draft(draft_dir, args.profile)
        print(f"Copied the draft into {args.profile} (old files in {backup}).")
        errors, warnings = check_seed(args.profile)
        for line in [f"ERROR    {e}" for e in errors] + [f"warning  {w}" for w in warnings]:
            print(line)
        return 1 if errors else 0

    notes = read_notes(notes_dir)
    if not notes.strip():
        print(f"Write your notes in {notes_dir}/ first (any .md or .txt). See profile/notes/README.md.")
        return 1
    cfg_path = args.profile.parent / "config.toml"
    cfg = tomllib.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    model = cfg.get("tailor", {}).get("model", DEFAULT_MODEL)
    print(f"Drafting from {notes_dir} with {model}...")
    draft, fixes = draft_profile(notes, model=model)
    write_draft(draft, fixes, draft_dir)
    n = sum(len(e.bullets) for e in draft.entries)
    print(f"Wrote {draft_dir}: {len(draft.entries)} jobs and projects, {n} bullets, "
          f"{len(draft.questions)} questions, {len(fixes)} dropped.")
    print(f"Read {draft_dir}/QUESTIONS.md, check the draft, then: python -m profile_mcp.draft --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
