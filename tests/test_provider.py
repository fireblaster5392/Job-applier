"""The tailoring interface: a custom MCP tailoring server plugged into the pipeline."""
import os
import tempfile
import unittest
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from profile_mcp.profile import Profile
from tailor.pipeline import tailor_job
from tailor.provider import ClaudeProvider, McpProvider, TailorError, load_provider
from tests.helpers import store_with_job

ROOT = Path(__file__).resolve().parent.parent
PROFILE = Profile.load().data


def fake_server(calls):
    """A minimal tailoring server implementing the documented contract."""
    srv = MCPServer("fake-tailor")

    @srv.tool()
    def get_resume_profile() -> dict:
        return PROFILE

    @srv.tool()
    def score_fit(job: dict, description: str) -> dict:
        calls.append(("score", job, description))
        return {"score": 140, "reasons": ["Python match"]}

    @srv.tool()
    def tailor_application(job: dict, description: str) -> dict:
        calls.append(("tailor", job, description))
        return {"fit_score": 82, "fit_reasons": ["backend"], "gaps": [], "summary": "Backend engineer.",
                "bullets": [{"id": "exco-api", "text": "Built a Python/FastAPI service handling 2M requests a day."},
                            {"id": "made-up", "text": "Founded a unicorn."}],
                "skills": ["Python", "Rust"], "cover_letter": "Hello.\n\nMore.\n\nBye.",
                "usage": {"model": "my-model", "input_tokens": 10}}

    @srv.tool()
    def broken(job: dict, description: str) -> dict:
        raise ValueError("boom")
    return srv


class McpProviderTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.tmp = tempfile.TemporaryDirectory()
        Path(self.tmp.name, "answers.yaml").write_text("legal:\n  authorized_to_work: {canada: true}\n")
        self.provider = McpProvider(fake_server(self.calls), answers_dir=Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_profile_from_server_with_local_answers(self):
        p = self.provider.profile()
        self.assertIn("exco-api", p.bullets)
        self.assertTrue(p.answers["legal"]["authorized_to_work"]["canada"])

    def test_score_is_clamped(self):
        fit = self.provider.score({"title": "Engineer", "company": "acme"}, "Python")
        self.assertEqual((fit.score, fit.reasons), (100, ["Python match"]))

    def test_tailor_job_through_server_still_runs_guardrails(self):
        with tempfile.TemporaryDirectory() as d:
            store, rowid = store_with_job(os.path.join(d, "jobs.db"))
            rec = tailor_job(store, rowid, self.provider.profile(), description="We need Python.",
                             provider=self.provider)
            self.assertEqual(store.get_job(rowid)["status"], "tailored")
        self.assertEqual([b["id"] for b in rec["data"]["bullets"]], ["exco-api", "st-react"])   # st-react: every role keeps a bullet
        self.assertEqual(rec["data"]["skills"], ["Python"])
        self.assertEqual(rec["model"], "my-model")
        self.assertTrue(any("made-up" in w for w in rec["warnings"]))
        kind, job, description = self.calls[-1]
        self.assertEqual((kind, job["company"], description), ("tailor", "acme", "We need Python."))

    def test_server_errors_become_tailor_errors(self):
        with self.assertRaises(TailorError):
            self.provider.call("broken", {"job": {}, "description": ""})


class LoadProviderTests(unittest.TestCase):
    def test_default_is_claude(self):
        self.assertIsInstance(load_provider({}), ClaudeProvider)

    def test_mcp_needs_server(self):
        with self.assertRaises(ValueError):
            load_provider({"tailor": {"provider": "mcp"}})
        p = load_provider({"tailor": {"provider": "mcp", "mcp_server": "http://127.0.0.1:9000/mcp"}})
        self.assertIsInstance(p, McpProvider)


if __name__ == "__main__":
    unittest.main()
