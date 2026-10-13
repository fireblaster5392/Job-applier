import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from profile_mcp import draft as d
from profile_mcp.profile import check_seed

NOTES = """Skills: Python, FastAPI, PostgreSQL
Project: Job Watcher (2025-01 to 2025-03). Polls 40 job boards every minute, alerts on Telegram.
Job: Acme, Software Intern, Calgary (2024-05 to 2024-08). Built billing API, 2,000 requests a day."""


def sample() -> d.Draft:
    return d.Draft(
        entries=[
            d.DraftEntry(kind="experience", id="acme", name="Acme", title="Software Intern", location="Calgary",
                         start="2024-05", end="2024-08", url="", summary="Billing team", tech=["Python", "FastAPI"],
                         keywords=["billing"], metrics=["2,000 requests a day", "99.9% uptime"],
                         bullets=[d.DraftBullet(id="acme-api", angle="backend",
                                                text="Built a FastAPI billing API serving 2,000 requests a day."),
                                  d.DraftBullet(id="Acme Speed", angle="infra",
                                                text="Cut billing latency by 35% with PostgreSQL indexes.")]),
            d.DraftEntry(kind="project", id="job-watcher", name="Job Watcher", title="Solo", location="",
                         start="2025-01", end="2025-03", url="", summary="Job alerts", tech=["Python"],
                         keywords=["automation"], metrics=["40 job boards"],
                         bullets=[d.DraftBullet(id="acme-api", angle="backend",
                                                text="Built a watcher polling 40 job boards every minute.")]),
        ],
        skills=[d.SkillGroup(name="Languages", skills=["Python"])],
        keywords=[d.KeywordMap(term="rest", means=["api", "fastapi"])],
        questions=["How many alerts a day does Job Watcher send?"])


class DraftTest(unittest.TestCase):
    def test_numbers_not_in_notes_are_dropped_and_ids_fixed(self):
        draft = sample()
        fixes = d.check_draft(draft, NOTES)
        acme, watcher = draft.entries
        self.assertEqual([b.id for b in acme.bullets], ["acme-api"])      # the 35% bullet is gone
        self.assertEqual(acme.metrics, ["2,000 requests a day"])          # 99.9 isn't in the notes
        self.assertEqual(watcher.bullets[0].id, "acme-api-2")             # duplicate id renamed
        self.assertEqual(len(fixes), 2)

    def test_write_and_apply_make_a_valid_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            prof = Path(tmp) / "profile"
            prof.mkdir()
            (prof / "me.yaml").write_text("name: Sam Test\nemail: s@example.com\n")
            (prof / "experience.yaml").write_text("[]\n")
            draft = sample()
            d.write_draft(draft, d.check_draft(draft, NOTES), prof / "draft")
            self.assertTrue((prof / "draft" / "projects" / "job-watcher.yaml").exists())
            self.assertIn("alerts a day", (prof / "draft" / "QUESTIONS.md").read_text())
            backup = d.apply_draft(prof / "draft", prof)
            self.assertTrue((backup / "experience.yaml").exists())
            errors, _ = check_seed(prof)
            self.assertEqual(errors, [])

    def test_draft_profile_calls_claude_once(self):
        calls = []

        class Messages:
            def parse(self, **kw):
                calls.append(kw)
                return SimpleNamespace(parsed_output=sample(), stop_reason="end_turn")

        draft, fixes = d.draft_profile(NOTES, client=SimpleNamespace(messages=Messages()))
        self.assertEqual(calls[0]["output_format"], d.Draft)
        self.assertIn("Job Watcher", calls[0]["messages"][0]["content"])
        self.assertEqual(len(fixes), 2)

    def test_cli_without_notes_says_what_to_do(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(d.main(["--profile", tmp]), 1)


if __name__ == "__main__":
    unittest.main()
