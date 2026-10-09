"""Load profile/resume.yaml and search its bullets."""
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

PROFILE_DIR = Path(__file__).resolve().parent.parent / "profile"


@dataclass(frozen=True)
class Bullet:
    id: str
    text: str
    tags: tuple[str, ...]
    section: str      # "experience" | "projects"
    owner: str        # company or project name


class Profile:
    def __init__(self, data: dict, answers: dict | None = None):
        self.data = data
        self.answers = answers or {}
        self.bullets: dict[str, Bullet] = {}
        for section, owner_key in (("experience", "company"), ("projects", "name")):
            for entry in data.get(section, []):
                for b in entry.get("bullets", []):
                    if b["id"] in self.bullets:
                        raise ValueError(f"duplicate bullet id {b['id']!r} in resume.yaml")
                    self.bullets[b["id"]] = Bullet(
                        b["id"], b["text"].strip(), tuple(b.get("tags", [])), section, entry[owner_key]
                    )

    @classmethod
    def load(cls, directory: Path = PROFILE_DIR) -> "Profile":
        data = yaml.safe_load((directory / "resume.yaml").read_text())
        answers_path = directory / "answers.yaml"
        answers = yaml.safe_load(answers_path.read_text()) if answers_path.exists() else {}
        return cls(data, answers)

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

    def search_experience(self, query: str, k: int = 8) -> list[Bullet]:
        """Rank bullets by word overlap with the query (tags count double)."""
        words = _words(query)
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

    def as_prompt_text(self) -> str:
        """Every bullet with its id, grouped by role, for the tailoring prompt."""
        lines = []
        for section, owner_key in (("experience", "company"), ("projects", "name")):
            for entry in self.data.get(section, []):
                header = entry.get("title", "") + (" at " if entry.get("title") else "") + entry[owner_key]
                lines.append(f"## {section}: {header}")
                for b in entry.get("bullets", []):
                    lines.append(f"- [{b['id']}] {b['text'].strip()}  (tags: {', '.join(b.get('tags', []))})")
        skills = self.data.get("skills", {})
        lines.append("## skills")
        for group, items in skills.items():
            lines.append(f"- {group}: {', '.join(items)}")
        return "\n".join(lines)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9+#.]+", text.lower()) if len(w) > 1}


@lru_cache(maxsize=1)
def default_profile() -> Profile:
    return Profile.load()
