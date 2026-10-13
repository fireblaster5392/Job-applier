"""The shipped config.toml parses and has every section the services read."""
import tomllib
import unittest
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / "config.toml"


class ConfigTests(unittest.TestCase):
    def test_parses_with_every_section(self):
        cfg = tomllib.loads(CONFIG.read_text())
        for section in ("filters", "discovery", "pipeline", "tailor", "apply", "outreach", "networking",
                        "mail", "prep", "review", "companies"):
            self.assertIn(section, cfg)
        self.assertTrue(cfg["apply"]["dry_run"])     # first runs never submit


if __name__ == "__main__":
    unittest.main()
