# job-apply

Job application automation, following `architecture/job-automation-architecture.md`.

## How it works

1. **Watcher** finds a new job that matches your filters and sends it to Telegram with
   **Tailor resume** and **Skip** buttons.
2. **Tailor resume** has Claude pick and lightly reword bullets from `profile/resume.yaml`,
   write a cover letter and score your fit. Guardrails drop any bullet id that doesn't exist,
   revert rewrites that add numbers, and drop skills you don't list.
3. Telegram shows the score with **Approve / Reject** and a link to the **dashboard**, where you
   read the resume and cover letter side by side and print them to PDF.
4. **Approve** marks the job approved. Nothing is submitted anywhere yet; auto-apply is the next step.

## Layout

| Folder | What it is | Status |
|---|---|---|
| `watcher/` | 60-second poller for Greenhouse, Lever, Ashby + Telegram alerts | Built |
| `profile/` | Master resume (`resume.yaml`), standard answers | **Placeholder: replace with yours** |
| `profile_mcp/` | Profile loading/search + MCP server for Claude Code / Desktop | Built |
| `tailor/` | Claude tailoring, guardrails, resume + cover letter rendering | Built |
| `bot/` | Telegram button handler | Built |
| `web/` | Approval queue + dashboard (FastAPI) | Built |
| `apply/` | Greenhouse / Lever / Ashby form adapters, stop before Submit | Next |
| `outreach/` | Contact finding + message drafts (you send) | Later |
| `mail/` | Gmail reply tracking + classifier | Later |
| `data/` | SQLite database (git-ignored) | |

Secrets go in `.env` (copy `.env.example`); never commit it.

## Run everything

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in Telegram + ANTHROPIC_API_KEY
set -a; source .env; set +a

python -m watcher.main --db data/jobs.db                          # terminal 1
python -m bot.main --db data/jobs.db                              # terminal 2
DB_PATH=data/jobs.db uvicorn --factory web.app:main_app --port 8000   # terminal 3, open localhost:8000
```

On a VPS: `docker compose up -d` runs all three.

**Use your profile from Claude Code:** `claude mcp add profile -- python -m profile_mcp.server`
gives Claude `search_experience`, `get_profile` and `render_resume`.

**Cost:** tailoring uses `claude-opus-5-5` by default (about $0.14 a job, profile cached).
Change `[tailor] model` in `config.toml` to `claude-sonnet-5-5` to roughly halve it.
The watcher, bot and dashboard make no Claude calls.

## The watcher

Polls the public Greenhouse, Lever and Ashby job-board APIs for every company in
`config.toml` every 60 seconds, stores each job once in SQLite (`jobs.db`), and sends a
Telegram message for new jobs whose title and location match your filters.

- **No backlog flood:** the first poll of each company stores its existing jobs silently.
  Only jobs that appear after that count as new.
- **Dedupe:** by `(ats, company, job_id)`. A job seen again just updates `last_seen`; a job
  that disappears gets `closed_at`.
- **Retries:** a failed Telegram send is retried on the next poll; 429/5xx from a board are
  retried with backoff; a wrong slug logs "board not found" and the rest keep running.
- **No Claude calls** in this step; it's plain code.

## Run it

```bash
python -m watcher.main --once --dry-run   # one pass, prints matches instead of sending
```

## Telegram setup (about 2 minutes)

1. In Telegram, message **@BotFather**, send `/newbot`, and copy the token.
2. Send your new bot any message.
3. `export TELEGRAM_BOT_TOKEN=...` then `python -m watcher.telegram_setup` prints your chat id.
4. `export TELEGRAM_CHAT_ID=...` and run the watcher.

Without both variables set, the watcher prints matches instead of sending them.

## Configure

Edit `config.toml`: `[filters]` holds title include/exclude words and locations, and
`[companies]` lists board slugs per ATS (the name in `boards.greenhouse.io/<slug>`,
`jobs.lever.co/<slug>`, `jobs.ashbyhq.com/<slug>`). The current list is a starter set of
well-known tech companies; replace it with your targets.

## Tests

```bash
python -m unittest -v
```

Offline tests (fake job boards, fake Claude and fake Telegram) cover parsing, filters, dedupe,
tailoring guardrails, rendering, the bot buttons and the dashboard flow.

## Next steps (from architecture/job-automation-architecture.md)

Per-ATS apply adapters that fill the form and stop before Submit, then outreach drafts and
Gmail tracking.
