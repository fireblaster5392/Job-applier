# job-apply

Job application automation, following `architecture/job-automation-architecture.md`.

## Layout

| Folder | What it is | Status |
|---|---|---|
| `watcher/` | 60-second poller for Greenhouse, Lever, Ashby + Telegram alerts | **Built** |
| `profile/` | Your master resume, standard answers and stories | Waiting on your resume |
| `profile_mcp/` | MCP server for searching your experience and rendering resumes | Step 1 next |
| `web/` | Approval queue + dashboard | Step 3 |
| `bot/` | Telegram Approve / Skip buttons | Step 3 |
| `apply/` | Greenhouse / Lever / Ashby form adapters, stop before Submit | Step 4 |
| `outreach/` | Contact finding + message drafts (you send) | Step 5 |
| `mail/` | Gmail reply tracking + classifier | Step 6 |
| `data/` | SQLite database (git-ignored) | |

Secrets go in `.env` (copy `.env.example`); never commit it. On a VPS: `docker compose up -d`.

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
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                      # then fill in the Telegram values
set -a; source .env; set +a
python -m watcher.main --once --dry-run   # one pass, prints matches
python -m watcher.main                    # every 60s, sends to Telegram
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

Offline tests cover parsing for all three ATSs, the filters, first-poll seeding, dedupe,
failed-send retry and closed jobs.

## Next steps (from architecture/job-automation-architecture.md)

Profile + resume tailoring, LLM fit scoring, Approve/Skip buttons in Telegram, the
approval dashboard, then the per-ATS apply adapters.
