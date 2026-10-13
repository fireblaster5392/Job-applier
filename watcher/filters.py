"""Decide whether a job is worth a notification. Cheap keyword rules, no LLM."""
import re

from .models import Job


class JobFilter:
    def __init__(self, cfg: dict):
        self.include = [k.lower() for k in cfg.get("title_include", [])]
        self.exclude = [k.lower() for k in cfg.get("title_exclude", [])]
        self.locations = [l.lower() for l in cfg.get("locations", [])]
        self.allow_remote = cfg.get("allow_remote", True)

    @staticmethod
    def _has(text: str, word: str) -> bool:
        # Whole-word match so "intern" doesn't hit "internal" and "ml" doesn't hit "html".
        return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) is not None

    def __call__(self, job: Job) -> bool:
        title = job.title.lower()
        if self.include and not any(self._has(title, k) for k in self.include):
            return False
        if any(self._has(title, k) for k in self.exclude):
            return False
        if not self.locations:
            return True
        if self.allow_remote and job.remote:
            return True
        loc = job.location.lower()
        return any(l in loc for l in self.locations)
