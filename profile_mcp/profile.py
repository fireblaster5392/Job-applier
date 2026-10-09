"""Load your profile seed files, check them, and search them.

Seed layout (profile/README.md has every field):

    profile/me.yaml            contact details, links, headline, targets, education
    profile/experience.yaml    every role, newest first, with bullets
    profile/projects/*.yaml    one file per project (or a single profile/projects.yaml list)
    profile/skills.yaml        skill groups + posting-keyword synonyms
    profile/format.yaml        how the resume should look
    profile/answers.yaml       application-form answers (gitignored)

A single legacy profile/resume.yaml is still read when me.yaml doesn't exist.
"""
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

PROFILE_DIR = Path(__file__).resolve().parent.parent / "profile"

DEFAULT_FORMAT = {
    "sections": ["summary", "experience", "projects", "skills", "education"],
    "pages": 1,
    "max_bullets_total": 12,
    "max_bullets_per_role": 4,
    "min_bullets_per_role": 1,
    "max_projects": 3,
    "max_bullets_per_project": 2,
    "max_skills": 14,
    "group_skills": True,
    "always_include": [],
    "never_include": [],
    "date_format": "%b %Y",
    "font": "Helvetica, Arial, sans-serif",
    "font_size": 10.5,
    "bullet_style": "",
    "summary_style": "",
    "cover_letter": {"paragraphs": 3, "style": ""},
    "shortlist": 80,
}
SECTIONS = {"summary", "experience", "projects", "skills", "education"}
_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass(frozen=True)
class Bullet:
    id: str
    text: str
    tags: tuple[str, ...]       # the bullet's tags plus its role's or project's tech and keywords
    section: str                # "experience" | "projects"
    owner: str                  # company or project name
    metrics: tuple[str, ...] = ()   # the role's or project's metrics, which rewrites may quote


class Profile:
    def __init__(self, data: dict, answers: dict | None = None):
        self.data = data
        self.answers = answers or {}
        self.format = {**DEFAULT_FORMAT, **(data.get("format") or {})}
        self.keywords = {str(k).lower(): [str(v).lower() for v in vs]
                         for k, vs in (data.get("keywords") or {}).items()}
        self.bullets: dict[str, Bullet] = {}
        for section, owner_key in (("experience", "company"), ("projects", "name")):
            for entry in data.get(section, []):
                shared = [str(t) for t in (entry.get("tech") or []) + (entry.get("keywords") or [])]
                for b in entry.get("bullets", []):
                    if b["id"] in self.bullets:
                        raise ValueError(f"duplicate bullet id {b['id']!r}")
                    tags = tuple(dict.fromkeys([str(t) for t in b.get("tags", [])] + shared))
                    self.bullets[b["id"]] = Bullet(b["id"], str(b["text"]).strip(), tags, section,
                                                   entry[owner_key], tuple(str(m) for m in entry.get("metrics") or []))

    @classmethod
    def load(cls, directory: Path = PROFILE_DIR) -> "Profile":
        directory = Path(directory)
        answers_path = directory / "answers.yaml"
        answers = (yaml.safe_load(answers_path.read_text()) or {}) if answers_path.exists() else {}
        return cls(load_seed(directory), answers)

    @property
    def basics(self) -> dict:
        return self.data.get("basics", {})

    def get_profile(self) -> dict:
        """Contact info, skills, education and standard form answers."""
        return {
            "basics": self.basics,
            "skills": self.data.get("skills", {}),
            "education": self.data.get("education", []),
            "answers": self.answers,
        }

    def entry_of(self, bullet_id: str) -> dict | None:
        """The role or project a bullet belongs to."""
        for section in ("experience", "projects"):
            for entry in self.data.get(section, []):
                if any(b["id"] == bullet_id for b in entry.get("bullets", [])):
                    return entry
        return None

    def _expand(self, text: str) -> set[str]:
        """Words in text plus what your skills.yaml keywords say they mean."""
        words = _words(text)
        low = text.lower()
        for key, meanings in self.keywords.items():
            if (key in words) if " " not in key else (key in low):
                for m in meanings:
                    words |= _words(m)
        return words

    def search_experience(self, query: str, k: int = 8) -> list[Bullet]:
        """Rank bullets by word overlap with the query (tags count double)."""
        words = self._expand(query)
        if not words:
            return list(self.bullets.values())[:k]
        scored = []
        for b in self.bullets.values():
            text_hits = len(words & _words(b.text))
            tag_hits = len(words & {w for t in b.tags for w in _words(t)})
            score = text_hits + 2 * tag_hits
            if score:
                scored.append((score, b))
        scored.sort(key=lambda s: -s[0])
        return [b for _, b in scored[:k]]

    def shortlist(self, description: str) -> set[str] | None:
        """For a big library, the bullet ids worth showing the tailoring for this job (None = all)."""
        limit = self.format.get("shortlist") or 0
        if not limit or len(self.bullets) <= limit:
            return None
        keep = {b.id for b in self.search_experience(description, limit)}
        keep |= set(self.format["always_include"])
        for entry in self.data.get("experience", []):   # every role stays visible
            keep |= {b["id"] for b in entry.get("bullets", [])[:2]}
        return keep - set(self.format["never_include"])

    def as_prompt_text(self, only: set[str] | None = None) -> str:
        """Every role and project with its bullets (by id), tech and metrics, plus skills."""
        never = set(self.format["never_include"])
        lines = []
        if self.basics.get("summary"):
            lines.append(f"Headline: {self.basics['summary']}")
        for section, owner_key in (("experience", "company"), ("projects", "name")):
            for entry in self.data.get(section, []):
                bullets = [b for b in entry.get("bullets", [])
                           if b["id"] not in never and (only is None or b["id"] in only)]
                if not bullets:
                    continue
                header = entry.get("title", "") + (" at " if entry.get("title") else "") + entry[owner_key]
                dates = " - ".join(str(entry[k]) for k in ("start", "end") if entry.get(k))
                lines.append(f"## {section}: {header}" + (f" ({dates})" if dates else ""))
                for key in ("role", "summary", "priority"):
                    if entry.get(key):
                        lines.append(f"{key}: {entry[key]}")
                for key in ("tech", "keywords", "metrics"):
                    if entry.get(key):
                        lines.append(f"{key}: {', '.join(map(str, entry[key]))}")
                for b in bullets:
                    tags = self.bullets[b["id"]].tags
                    lines.append(f"- [{b['id']}] {str(b['text']).strip()}  (tags: {', '.join(tags)})")
        lines.append("## skills")
        for group, items in self.data.get("skills", {}).items():
            lines.append(f"- {group}: {', '.join(map(str, items))}")
        return "\n".join(lines)

    def rules_text(self) -> str:
        """The format.yaml rules, as instructions for the tailoring."""
        f = self.format
        cl = {**DEFAULT_FORMAT["cover_letter"], **(f.get("cover_letter") or {})}
        lines = [
            f"- Length: {f['pages']} page(s). Choose at most {f['max_bullets_total']} bullets in total.",
            f"- Experience: {f['min_bullets_per_role']}-{f['max_bullets_per_role']} bullets per role; "
            "include every role that has bullets.",
            f"- Projects: at most {f['max_projects']} projects, {f['max_bullets_per_project']} bullets each.",
            f"- Skills: at most {f['max_skills']}, most relevant first.",
            f"- Cover letter: {cl['paragraphs']} paragraphs.",
        ]
        if "summary" not in f["sections"]:
            lines.append("- No summary on the resume: return an empty summary.")
        if f["always_include"]:
            lines.append(f"- Always include these bullet ids: {', '.join(f['always_include'])}.")
        for key, label in (("bullet_style", "Bullet style"), ("summary_style", "Summary style")):
            if f.get(key):
                lines.append(f"- {label}: {f[key]}")
        if cl.get("style"):
            lines.append(f"- Cover letter style: {cl['style']}")
        targets = self.data.get("targets") or {}
        if targets:
            lines.append("- What the candidate is looking for: "
                         + "; ".join(f"{k}: {', '.join(v) if isinstance(v, list) else v}" for k, v in targets.items()))
        return "\n".join(lines)


def _read(path: Path):
    return yaml.safe_load(path.read_text()) if path.exists() else None


def _date_key(entry: dict) -> str:
    end = str(entry.get("end") or entry.get("start") or "")
    return "9999" if end.lower() == "present" else end


def load_seed(directory: Path = PROFILE_DIR) -> dict:
    """Merge the seed files into one profile dict (the shape get_resume_profile returns)."""
    directory = Path(directory)
    me = _read(directory / "me.yaml")
    if me is None:
        legacy = _read(directory / "resume.yaml")
        if legacy is None:
            raise FileNotFoundError(f"no profile in {directory}: add me.yaml (see profile/README.md)")
        return legacy
    projects = list(_read(directory / "projects.yaml") or [])
    for path in sorted((directory / "projects").glob("*.y*ml")):
        doc = _read(path) or []
        projects.extend(doc if isinstance(doc, list) else [doc])
    projects.sort(key=_date_key, reverse=True)   # newest first on the resume
    skills = _read(directory / "skills.yaml") or {}
    basics = {k: me[k] for k in ("name", "email", "phone", "location", "links") if me.get(k)}
    if me.get("headline"):
        basics["summary"] = me["headline"]
    return {
        "basics": basics,
        "targets": me.get("targets") or {},
        "education": me.get("education") or [],
        "experience": _read(directory / "experience.yaml") or [],
        "projects": projects,
        "skills": skills.get("groups") or {},
        "keywords": skills.get("keywords") or {},
        "format": _read(directory / "format.yaml") or {},
    }


def check_seed(directory: Path = PROFILE_DIR) -> tuple[list[str], list[str]]:
    """Problems that stop the profile loading (errors) or that you probably want to fix (warnings)."""
    errors, warnings = [], []
    try:
        data = load_seed(directory)
    except (OSError, yaml.YAMLError) as exc:
        return [str(exc)], []
    if not data.get("basics", {}).get("name"):
        errors.append("me.yaml: name is missing")
    for key in ("email", "phone", "location"):
        if not data.get("basics", {}).get(key):
            warnings.append(f"me.yaml: {key} is empty")
    seen: dict[str, str] = {}
    for section, need in (("experience", ("company", "title", "start")), ("projects", ("name",))):
        for i, entry in enumerate(data.get(section) or []):
            where = f"{section} #{i + 1} ({entry.get('company') or entry.get('name') or '?'})"
            for key in need:
                if not entry.get(key):
                    errors.append(f"{where}: {key} is missing")
            if not entry.get("bullets"):
                warnings.append(f"{where}: no bullets, so it can't appear on a resume")
            for b in entry.get("bullets") or []:
                bid = str(b.get("id", ""))
                if not bid:
                    errors.append(f"{where}: a bullet has no id")
                    continue
                if not _ID.match(bid):
                    errors.append(f"{where}: bullet id {bid!r} must be lowercase letters, digits and dashes")
                if bid in seen:
                    errors.append(f"{where}: bullet id {bid!r} is also used in {seen[bid]}")
                seen[bid] = where
                if not str(b.get("text", "")).strip():
                    errors.append(f"{where}: bullet {bid!r} has no text")
                if not b.get("tags") and not entry.get("tech") and not entry.get("keywords"):
                    warnings.append(f"{where}: bullet {bid!r} has no tags, tech or keywords to match on")
    if not data.get("skills"):
        warnings.append("skills.yaml: no skill groups, so tailored resumes will list no skills")
    fmt = data.get("format") or {}
    for key in fmt:
        if key not in DEFAULT_FORMAT:
            warnings.append(f"format.yaml: unknown setting {key!r} is ignored")
    for name in fmt.get("sections") or []:
        if name not in SECTIONS:
            errors.append(f"format.yaml: unknown section {name!r} (use {', '.join(sorted(SECTIONS))})")
    for key in ("always_include", "never_include"):
        for bid in fmt.get(key) or []:
            if bid not in seen:
                errors.append(f"format.yaml: {key} names {bid!r}, which isn't a bullet id")
    return errors, warnings


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9+#.]+", text.lower()) if len(w) > 1}


def _seed_version(directory: Path = PROFILE_DIR) -> float:
    return max((p.stat().st_mtime for p in directory.rglob("*.y*ml")), default=0.0)


@lru_cache(maxsize=1)
def _cached_profile(version: float) -> Profile:
    return Profile.load()


def default_profile() -> Profile:
    """The profile from profile/, reloaded whenever a seed file changes."""
    return _cached_profile(_seed_version())
