"""The tailoring interface the pipeline calls, and its two implementations.

Everything that needs the candidate's profile, a fit score, or a tailored resume and cover letter
goes through a TailorProvider:

- ClaudeProvider (default): profile from the seed files in profile/, scoring and tailoring with Claude.
- McpProvider: your own MCP server supplies all three. Tool names and schemas are in
  docs/tailoring-server.md.

Whichever provider runs, the pipeline still applies tailor.guardrails to its output against the
provider's own profile, so nothing reaches an application that isn't in the profile.

Pick one in config.toml:

    [tailor]
    provider = "mcp"                          # default "claude"
    mcp_server = "http://127.0.0.1:9000/mcp"  # or a command: "python -m my_server"
"""
import asyncio
import json
import logging
import shlex
import time
from typing import Protocol

import yaml

from profile_mcp.profile import Profile, PROFILE_DIR

from .claude import DEFAULT_MODEL, TailorError, tailor
from .schema import TailoredApplication
from .score import DEFAULT_SCORE_MODEL, FitScore, score_job

log = logging.getLogger(__name__)

# The tools a tailoring server must expose. See docs/tailoring-server.md.
TOOL_PROFILE = "get_resume_profile"
TOOL_SCORE = "score_fit"
TOOL_TAILOR = "tailor_application"


class TailorProvider(Protocol):
    def profile(self) -> Profile:
        """The candidate's master profile, as get_resume_profile returns it, with local answers.yaml."""

    def score(self, job: dict, description: str) -> FitScore:
        """Quick 0-100 fit score. job has title, company, location (plus ats, url when known)."""

    def tailor(self, job: dict, description: str) -> tuple[TailoredApplication, dict]:
        """Tailored resume selection + cover letter, and a usage dict with at least "model"."""


class ClaudeProvider:
    def __init__(self, *, profile_loader=Profile.load, model: str = DEFAULT_MODEL,
                 score_model: str = DEFAULT_SCORE_MODEL, client=None):
        self.profile_loader, self.model, self.score_model, self.client = profile_loader, model, score_model, client

    def profile(self) -> Profile:
        return self.profile_loader()

    def score(self, job: dict, description: str) -> FitScore:
        return score_job(job, description, self.profile(), model=self.score_model, client=self.client)

    def tailor(self, job: dict, description: str) -> tuple[TailoredApplication, dict]:
        return tailor(job, description, self.profile(), model=self.model, client=self.client)


class McpProvider:
    """Calls a tailoring MCP server. server is an http(s) URL, a shell command, or (in tests) an
    MCPServer instance. The profile is cached for profile_ttl seconds."""

    def __init__(self, server, *, profile_ttl: float = 300, answers_dir=PROFILE_DIR):
        self.server, self.profile_ttl, self.answers_dir = server, profile_ttl, answers_dir
        self._profile: Profile | None = None
        self._profile_at = 0.0

    def _target(self):
        from mcp import StdioServerParameters
        if isinstance(self.server, str) and not self.server.startswith(("http://", "https://")):
            cmd = shlex.split(self.server)
            return StdioServerParameters(command=cmd[0], args=cmd[1:])
        return self.server

    def call(self, tool: str, args: dict) -> dict:
        from mcp import Client

        async def go():
            async with Client(self._target()) as client:
                return await client.call_tool(tool, args)
        try:
            result = asyncio.run(go())
        except Exception as exc:  # noqa: BLE001 - connection errors surface as a tailoring failure
            raise TailorError(f"Tailoring server unreachable ({tool}): {exc}") from exc
        text = " ".join(getattr(c, "text", "") for c in result.content)
        if result.is_error:
            raise TailorError(f"Tailoring server error in {tool}: {text[:500]}")
        data = result.structured_content
        if data is None:
            try:
                data = json.loads(text)
            except ValueError as exc:
                raise TailorError(f"{tool} didn't return JSON: {text[:200]}") from exc
        # Servers that return a non-object get wrapped as {"result": ...} by some SDKs.
        if isinstance(data, dict) and set(data) == {"result"} and isinstance(data["result"], dict):
            data = data["result"]
        return data

    def profile(self) -> Profile:
        if self._profile is None or time.monotonic() - self._profile_at > self.profile_ttl:
            data = self.call(TOOL_PROFILE, {})
            # Legal and EEO answers stay on this machine, in profile/answers.yaml.
            path = self.answers_dir / "answers.yaml"
            local = yaml.safe_load(path.read_text()) or {} if path.exists() else {}
            self._profile, self._profile_at = Profile(data, local), time.monotonic()
        return self._profile

    def score(self, job: dict, description: str) -> FitScore:
        fit = FitScore.model_validate(self.call(TOOL_SCORE, {"job": job, "description": description}))
        fit.score = max(0, min(100, fit.score))
        return fit

    def tailor(self, job: dict, description: str) -> tuple[TailoredApplication, dict]:
        data = self.call(TOOL_TAILOR, {"job": job, "description": description})
        usage = data.pop("usage", None) or {}
        app = TailoredApplication.model_validate(data)
        usage.setdefault("model", f"mcp:{self.server if isinstance(self.server, str) else 'server'}")
        return app, usage


def load_provider(cfg: dict, *, client=None) -> TailorProvider:
    t = cfg.get("tailor", {})
    kind = t.get("provider", "claude")
    if kind == "claude":
        return ClaudeProvider(model=t.get("model", DEFAULT_MODEL),
                              score_model=cfg.get("pipeline", {}).get("score_model", DEFAULT_SCORE_MODEL),
                              client=client)
    if kind == "mcp":
        if not t.get("mcp_server"):
            raise ValueError('[tailor] provider = "mcp" needs mcp_server (a URL or a command)')
        return McpProvider(t["mcp_server"])
    raise ValueError(f"unknown [tailor] provider {kind!r}: use 'claude' or 'mcp'")
