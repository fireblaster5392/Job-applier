"""Browser-agent fallback, driven by a scripted fake Claude on a local Workday-style form."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from anthropic.types import ToolUseBlock

from apply.agent import run_agent
from apply.runner import agent_host
from tailor.schema import TailoredApplication
from tests.helpers import GOOD_OUTPUT
from tests.test_apply import CHROMIUM, FORMS, HAS_BROWSER, PROFILE


class ScriptedClaude:
    """Plays a list of steps. Each step gets the latest page snapshot and returns (tool, input)."""

    def __init__(self, steps):
        self.steps, self.snapshot, self.calls = list(steps), {}, []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw):
        self.calls.append(kw)
        last = kw["messages"][-1]["content"]
        if isinstance(last, list):
            for r in last:
                if str(r.get("content", "")).startswith('{"url"'):
                    self.snapshot = json.loads(r["content"])
                self.last_result = r.get("content")
        name, args = self.steps.pop(0)(self)
        return SimpleNamespace(content=[ToolUseBlock(type="tool_use", id=f"t{len(self.calls)}", name=name, input=args)],
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5))

    def el(self, label_start):
        return next(e["id"] for e in self.snapshot["elements"] if e["label"].startswith(label_start))


def look(c):
    return "look", {}


def step(name, **args):
    def f(c):
        return name, {k: (c.el(v[1:]) if isinstance(v, str) and v.startswith("@") else v) for k, v in args.items()}
    return f


FILL_PAGES = [
    look, step("fill", element="@First Name", value="Alex", label="First Name", source="profile"),
    step("choose", element="@How Did You Hear", option="Company careers page", label="How did you hear", source="answers"),
    step("click", element="@Save and Continue"), look,
    step("choose", element="@Yes", option="Yes", label="Authorized in the US", source="answers"),
    step("upload", element="@Select files", file="resume"),
    step("click", element="@Save and Continue"), look,
]


@unittest.skipUnless(HAS_BROWSER, "Chromium not installed")
class AgentTests(unittest.TestCase):
    def run_steps(self, steps, approved=None):
        from playwright.sync_api import sync_playwright
        os.environ["CHROMIUM_PATH"] = CHROMIUM
        claude = ScriptedClaude(steps)
        with tempfile.TemporaryDirectory() as d, sync_playwright() as pw:
            resume = Path(d, "Alex-Resume.pdf")
            resume.write_bytes(b"%PDF-1.4")
            b = pw.chromium.launch(executable_path=CHROMIUM)
            page = b.new_page()
            page.goto((FORMS / "workday.html").as_uri())
            r = run_agent(page, job={"title": "Backend Engineer", "company": "acme"}, profile=PROFILE,
                          app=TailoredApplication.model_validate(GOOD_OUTPUT),
                          files={"resume": str(resume), "cover_letter": str(resume)}, client=claude, approved=approved)
            r["text"] = page.inner_text("body")
            b.close()
        return r, claude

    def test_first_pass_stops_at_review_and_refuses_submit(self):
        r, claude = self.run_steps(FILL_PAGES + [
            step("click", element="@Submit"),
            lambda c: ("finish", {"outcome": "ready", "reasons": []})])
        self.assertIn("refused", claude.last_result)
        self.assertEqual(r["outcome"], "needs_review")
        self.assertIn("Alex / yes / Alex-Resume.pdf", r["text"])     # the review page shows every answer
        self.assertNotIn("Thank you for applying", r["text"])
        self.assertEqual({a["label"]: a["answer"] for a in r["answers"]},
                         {"First Name": "Alex", "How did you hear": "Company careers page", "Authorized in the US": "Yes"})
        system = claude.calls[0]["system"][0]["text"]
        self.assertIn("Do NOT click the final Submit", system)
        self.assertIn('"united states": "No"', system)     # saved legal answers are in the prompt

    def test_approved_pass_submits(self):
        r, claude = self.run_steps(FILL_PAGES + [
            step("click", element="@Submit"),
            lambda c: ("finish", {"outcome": "submitted", "reasons": []})],
            approved=[{"label": "authorized in the us", "answer": "Yes"}])
        self.assertEqual(r["outcome"], "submitted", r["reasons"])
        self.assertIn("Thank you for applying", r["text"])
        self.assertIn("authorized in the us: Yes", claude.calls[0]["system"][0]["text"])

    def test_claimed_submit_without_confirmation_fails(self):
        r, _ = self.run_steps([look, lambda c: ("finish", {"outcome": "submitted", "reasons": []})],
                              approved=[])
        self.assertEqual(r["outcome"], "failed")

    def test_needs_you_and_step_limit(self):
        r, _ = self.run_steps([look, lambda c: ("finish", {"outcome": "needs_you",
                                                           "reasons": ["Workday asks to verify your email."]})])
        self.assertEqual((r["outcome"], r["reasons"]), ("needs_you", ["Workday asks to verify your email."]))

    def test_old_snapshots_are_trimmed(self):
        _, claude = self.run_steps([look, look, lambda c: ("finish", {"outcome": "failed", "reasons": ["x"]})])
        first_result = claude.calls[2]["messages"][2]["content"][0]["content"]
        self.assertEqual(first_result, "(old page snapshot)")


class RoutingTests(unittest.TestCase):
    def test_agent_hosts(self):
        self.assertTrue(agent_host("https://acme.wd5.myworkdayjobs.com/en-US/careers/job/123/apply"))
        self.assertTrue(agent_host("https://careers-acme.icims.com/jobs/1/job"))
        self.assertFalse(agent_host("https://job-boards.greenhouse.io/acme/jobs/1"))
        self.assertFalse(agent_host("file:///tmp/form.html"))


if __name__ == "__main__":
    unittest.main()
