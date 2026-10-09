# job-apply

Job application automation, following `architecture/job-automation-architecture.md`.

## How it works

1. **Watcher** polls every company's job board every 60 seconds and keeps jobs whose title and
   location match your filters.
2. **Worker** gives each new match a quick fit score (Haiku, about $0.0005 a job).
   - **Score ≥ threshold** (default 70): Claude tailors your resume and cover letter right away,
     and Telegram gets one message with both files, the score and **Approve / Edit / Skip**.
   - **Below threshold:** no message, but it's listed on the dashboard under `below_threshold`
     with its score and reasons, and you can tailor it from there. Nothing is dropped.
   - If scoring or tailoring fails, the job is sent as a plain alert with a **Tailor** button.
3. **Edit** opens the dashboard, where you can change the summary, bullets, skills and cover letter.
4. **Approve** marks the job approved. Nothing is submitted anywhere yet; auto-apply is the next step.

Tailoring may only pick and lightly reword bullets from `profile/resume.yaml`. Guardrails drop any
bullet id that doesn't exist, revert rewrites that add numbers, and drop skills you don't list.

Set `[pipeline] mode = "manual"` in `config.toml` to get every match in Telegram first and tap
**Tailor resume** yourself instead.

## Layout

| Folder | What it is | Status |
|---|---|---|
| `watcher/` | 60-second poller for Greenhouse, Lever, Ashby + Telegram alerts | Built |
| `profile/` | Master resume (`resume.yaml`), standard answers | **Placeholder: replace with yours** |
| `profile_mcp/` | Profile loading/search + MCP server for Claude Code / Desktop | Built |
| `tailor/` | Fit scoring, Claude tailoring, guardrails, resume + cover letter rendering | Built |
| `worker/` | Auto pipeline: score, tailor good fits, send to Telegram | Built |
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

python -m watcher.main --db data/jobs.db                          # finds jobs
python -m worker.main --db data/jobs.db                           # scores + tailors + sends
python -m bot.main --db data/jobs.db                              # handles Telegram buttons
DB_PATH=data/jobs.db uvicorn --factory web.app:main_app --port 8000   # dashboard at localhost:8000
```

On a VPS: `docker compose up -d` runs all four.

**Use your profile from Claude Code:** `claude mcp add profile -- python -m profile_mcp.server`
gives Claude `search_experience`, `get_profile` and `render_resume`.

**Cost:** scoring every match with Haiku is about $0.0005 a job; tailoring a good fit with
`claude-opus-5-5` is about $0.14 (profile cached). Change `[tailor] model` to `claude-sonnet-5-5`
to roughly halve tailoring, or raise `score_threshold` to tailor fewer jobs. The watcher, bot and
dashboard make no Claude calls.

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
