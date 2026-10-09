import unittest

from profile_mcp.profile import Profile
from tailor.claude import TailorError, tailor
from tailor.guardrails import enforce
from tailor.pipeline import tailor_job
from tailor.render import cover_letter_html, resume_html
from tailor.schema import TailoredApplication
from watcher.models import Job
from watcher.sources import job_description

from tests.helpers import GOOD_OUTPUT, fake_claude, store_with_job

PROFILE = Profile.load()


class ProfileTests(unittest.TestCase):
    def test_search_prefers_tag_matches(self):
        ids = [b.id for b in PROFILE.search_experience("spark etl data engineering")]
        self.assertEqual(ids[0], "exco-pipeline")

    def test_prompt_text_has_every_bullet_id(self):
        text = PROFILE.as_prompt_text()
        for bid in PROFILE.bullets:
            self.assertIn(f"[{bid}]", text)


class GuardrailTests(unittest.TestCase):
    def test_enforce(self):
        app = TailoredApplication.model_validate(GOOD_OUTPUT)
        warnings = enforce(app, PROFILE)
        # st-react is added so every role keeps min_bullets_per_role
        self.assertEqual([b.id for b in app.bullets], ["exco-api", "exco-pipeline", "proj-watcher", "st-react"])
        self.assertEqual(app.bullets[1].text, PROFILE.bullets["exco-pipeline"].text)  # reverted
        self.assertEqual(app.skills, ["Python", "FastAPI"])
        self.assertEqual(len(warnings), 4)

    def test_metrics_numbers_allowed_in_rewrites(self):
        app = TailoredApplication.model_validate({**GOOD_OUTPUT, "bullets": [
            {"id": "exco-oncall", "text": "Led on-call for 4 services handling 2M requests a day."}]})
        enforce(app, PROFILE)   # "2M" is in Example Corp's metrics
        self.assertIn("2M requests", app.bullets[0].text)

    def test_format_limits(self):
        data = {**PROFILE.data, "format": {"max_bullets_total": 3, "max_bullets_per_role": 1,
                                           "max_projects": 1, "always_include": ["lab-k8s"],
                                           "never_include": ["exco-pipeline"], "max_skills": 1}}
        profile = Profile(data)
        app = TailoredApplication.model_validate({**GOOD_OUTPUT, "bullets": [
            {"id": "exco-api", "text": "a"}, {"id": "exco-pipeline", "text": "b"},
            {"id": "exco-oncall", "text": "c"}, {"id": "rank-model", "text": "d"},
            {"id": "proj-watcher", "text": "e"}]})
        enforce(app, profile)
        ids = [b.id for b in app.bullets]
        self.assertNotIn("exco-pipeline", ids)          # never_include
        self.assertIn("lab-k8s", ids)                   # always_include, even past max_projects
        self.assertNotIn("exco-oncall", ids)            # max_bullets_per_role 1
        self.assertIn("st-react", ids)                  # every role keeps one bullet
        self.assertEqual(len(ids), 3)                   # max_bullets_total, projects trimmed first
        self.assertEqual(app.skills, ["Python"])


class ClaudeTests(unittest.TestCase):
    def test_request_shape(self):
        calls = []
        app, usage = tailor({"title": "SWE", "company": "acme"}, "Python", PROFILE, client=fake_claude(calls=calls))
        body = calls[0]
        self.assertEqual(body["model"], "claude-opus-5-5")
        self.assertEqual(body["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(body["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(app.fit_score, 82)
        self.assertEqual(usage["input_tokens"], 1200)

    def test_refusal_and_api_errors(self):
        with self.assertRaises(TailorError):
            tailor({"title": "x", "company": "y"}, "d", PROFILE, client=fake_claude(stop_reason="refusal"))
        with self.assertRaises(TailorError):
            tailor({"title": "x", "company": "y"}, "d", PROFILE, client=fake_claude(status=500))


class RenderTests(unittest.TestCase):
    def test_resume_keeps_chronological_order_and_escapes(self):
        app = TailoredApplication.model_validate({**GOOD_OUTPUT, "summary": "<script>x</script>"})
        enforce(app, PROFILE)
        html = resume_html(PROFILE, app)
        self.assertLess(html.index("Example Corp"), html.index("Job Watcher"))
        self.assertNotIn("<script>", html)
        self.assertIn("Startup Inc", html)  # min_bullets_per_role keeps every role
        self.assertIn("May 2024 – Present", html)
        self.assertLess(html.index("<h2>Skills"), html.index("<h2>Education"))
        letter = cover_letter_html(PROFILE, app, "acme")
        self.assertEqual(letter.count("<p>"), 7)  # contact, date, greeting, 3 paragraphs, sign-off


class DescriptionTests(unittest.TestCase):
    def test_lever_and_ashby_from_listing(self):
        lever = Job("lever", "a", "1", "t", "", "", raw={"descriptionPlain": "Intro", "lists": [
            {"text": "Requirements", "content": "<li>Python</li><li>SQL</li>"}]})
        self.assertIn("- Python", job_description(lever))
        ashby = Job("ashby", "a", "1", "t", "", "", raw={"descriptionHtml": "<p>Hi &amp; welcome</p>"})
        self.assertEqual(job_description(ashby), "Hi & welcome")

    def test_greenhouse_fetches_detail(self):
        import httpx
        client = httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"content": "&lt;p&gt;Build things&lt;/p&gt;"})))
        job = Job("greenhouse", "acme", "101", "t", "", "")
        self.assertEqual(job_description(job, client), "Build things")


class PipelineTests(unittest.TestCase):
    def test_tailor_job_saves_and_sets_status(self):
        store, rowid = store_with_job()
        rec = tailor_job(store, rowid, PROFILE, client=fake_claude())
        self.assertEqual(store.get_job(rowid)["status"], "tailored")
        self.assertEqual(rec["data"]["fit_score"], 82)
        self.assertEqual(len(rec["warnings"]), 4)
        self.assertIn("Python", rec["description"])

    def test_failure_marks_job_failed(self):
        store, rowid = store_with_job()
        with self.assertRaises(TailorError):
            tailor_job(store, rowid, PROFILE, client=fake_claude(status=500))
        job = store.get_job(rowid)
        self.assertEqual(job["status"], "failed")
        self.assertIn("500", job["last_error"])


if __name__ == "__main__":
    unittest.main()
