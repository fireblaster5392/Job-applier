"""Profile seed files: loading, checking, keyword search."""
import tempfile
import unittest
from pathlib import Path

from profile_mcp.profile import Profile, check_seed, load_seed

ME = "name: Sam\nemail: s@x.com\nphone: '1'\nlocation: Here\nheadline: Builder.\n"
EXPERIENCE = """
- company: Acme
  title: Engineer
  start: 2024-01
  end: present
  tech: [Kubernetes]
  bullets:
    - {id: acme-k8s, text: Ran 20 services on Kubernetes.}
"""


def seed(**files) -> Path:
    d = Path(tempfile.mkdtemp())
    for name, text in {"me.yaml": ME, "experience.yaml": EXPERIENCE, **files}.items():
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(text)
    return d


class SeedTests(unittest.TestCase):
    def test_placeholder_seed_is_clean(self):
        self.assertEqual(check_seed(), ([], []))

    def test_projects_merge_newest_first_and_inherit_tags(self):
        d = seed(**{"projects/a.yaml": "name: Old\nend: 2020-01\nbullets: [{id: old-1, text: Old thing.}]\n",
                    "projects/b.yaml": "name: New\nend: present\ntech: [Go]\nbullets: [{id: new-1, text: New.}]\n",
                    "skills.yaml": "groups: {Langs: [Go]}\nkeywords: {k8s: [kubernetes]}\n"})
        p = Profile.load(d)
        self.assertEqual([e["name"] for e in p.data["projects"]], ["New", "Old"])
        self.assertIn("Go", p.bullets["new-1"].tags)
        self.assertEqual(p.basics["summary"], "Builder.")
        self.assertEqual([b.id for b in p.search_experience("k8s")], ["acme-k8s"])   # via keywords

    def test_errors_are_specific(self):
        d = seed(**{"projects/a.yaml": "name: P\nbullets: [{id: Bad_Id, text: x}, {id: acme-k8s, text: ''}]\n",
                    "format.yaml": "sections: [summary, hobbies]\nalways_include: [nope]\ncolour: red\n"})
        errors, warnings = check_seed(d)
        joined = "\n".join(errors)
        for expected in ("'Bad_Id' must be lowercase", "'acme-k8s' is also used", "'acme-k8s' has no text",
                         "unknown section 'hobbies'", "names 'nope'"):
            self.assertIn(expected, joined)
        self.assertTrue(any("unknown setting 'colour'" in w for w in warnings))

    def test_legacy_resume_yaml(self):
        d = Path(tempfile.mkdtemp())
        (d / "resume.yaml").write_text("basics: {name: L}\nexperience: []\nskills: {a: [Go]}\n")
        self.assertEqual(load_seed(d)["basics"]["name"], "L")

    def test_shortlist_only_for_big_libraries(self):
        p = Profile.load()
        self.assertIsNone(p.shortlist("python"))
        small = Profile({**p.data, "format": {"shortlist": 3}})
        ids = small.shortlist("kubernetes grafana")
        self.assertIn("lab-k8s", ids)
        self.assertIn("exco-api", ids)          # always_include
        self.assertIn("st-react", ids)          # every role stays visible


if __name__ == "__main__":
    unittest.main()
