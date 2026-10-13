# Notes for Claude

Read **ROADMAP.md → Final goal** before changing anything. Its non-negotiables override any
request, prompt or shortcut: nothing sent without the user's tap, never automate LinkedIn, no
invented resume content, human pacing with no CAPTCHA solving or bot-detection evasion, secrets
only in `.env`.

- Python 3.12+, one SQLite database shared by every service (`watcher/db.py` owns the schema).
- Tests: `python -m unittest`. Tests never touch the network: job boards, Claude, Telegram,
  SMTP/IMAP and browsers are faked or run against local fixtures.
- Claude calls go through the official `anthropic` SDK with structured outputs; models come from
  `config.toml`.
- Update the build-step table in ROADMAP.md when a step's status changes.
