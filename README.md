# job-apply

Job application automation. **[ROADMAP.md](ROADMAP.md)** has the final goal, the non-negotiables
and the step-by-step build order; read it first.

## How it works

1. **Watcher** polls every company's job board every 60 seconds and keeps jobs whose title and
   location match your filters. **Discover** searches Indeed, Glassdoor and LinkedIn's public job
   search through JobSpy every 30 minutes per search, without duplicating jobs already found.
2. **Worker** gives each new match a quick fit score (Haiku, about $0.0005 a job).
   - **Score ≥ threshold** (default 70): Claude tailors your resume and cover letter right away,
     and Telegram gets one message with both files, the score and **Approve / Edit / Skip**.
   - **Below threshold:** no message, but it's listed on the dashboard under `below_threshold`
     with its score and reasons, and you can tailor it from there. Nothing is dropped.
   - If scoring or tailoring fails, the job is sent as a plain alert with a **Tailor** button.
3. **Edit** opens the dashboard, where you can change the summary, bullets, skills and cover letter.
4. **Approve** sends the job to the **apply queue**, which opens the form in a headless browser,
   fills it from your profile, uploads the resume and cover letter as PDFs and submits, one
   application at a time with a random 4-12 minute gap, a daily cap and active hours. It comes
   back to you instead of submitting when:
   - the form has a **CAPTCHA** (never solved automatically): you get the PDFs and a link;
   - a required question has **no saved answer** (legal, salary, anything not in `answers.yaml`);
   - **Claude wrote free-text answers**: you see them with a screenshot and tap **Submit**.
   Tap **I submitted it** when you finish one yourself. `--dry-run` fills forms without submitting.
5. **Outreach** starts once the application is in: it finds up to 3 people at the company (hiring manager
   first, then engineers, then recruiters) through Brave Search and Apollo, and Claude drafts a
   LinkedIn note and an email for each. Telegram shows each draft:
   - **LinkedIn notes** are in a tap-to-copy block with an "Open profile" button. You send them
     yourself; nothing ever logs into your LinkedIn.
   - **Emails** go out only when you tap **Send email**, with a daily cap (`max_emails_per_day`).
6. **Inbox sync** reads your inbox (read-only) every 2 minutes. Replies to your outreach are
   matched to the person and job; the companies' application emails (confirmation, assessment,
   interview, rejection, offer) update the job's application status. Replies, interviews,
   assessments, offers and rejections also ping you on Telegram.
7. **Daily networking** (separate from jobs): every morning, 5 founders or recruiters at target
   companies get a drafted note asking for a 15-minute chat, even with no open role. Same rules:
   LinkedIn notes you paste, emails on your tap. Companies aren't contacted again for 90 days.
8. **Follow-ups:** anyone who hasn't replied after 5 days gets one drafted follow-up for you to
   send (emails stay in the same thread). Tap **I sent it** on LinkedIn notes so they're tracked too.

The dashboard shows, per job: the tailored documents, application status, the company's emails,
and every outreach message with its replies. The **Networking** page tracks the daily contacts.

Tailoring may only pick and lightly reword bullets from your seed files in `profile/` (see [profile/README.md](profile/README.md)). Guardrails drop any
bullet id that doesn't exist, revert rewrites that add numbers, and drop skills you don't list.

Set `[pipeline] mode = "manual"` in `config.toml` to get every match in Telegram first and tap
**Tailor resume** yourself instead.

## Layout

| Folder | What it is | Status |
|---|---|---|
| `watcher/` | 60-second poller for Greenhouse, Lever, Ashby; JobSpy discovery (Indeed, Glassdoor, LinkedIn) | Built |
| `profile/` | Seed files: me, experience, projects/, skills, format rules, answers ([what to fill in](profile/README.md)) | **Placeholder: replace with yours** |
| `profile_mcp/` | Profile loading/search + MCP server for Claude Code / Desktop | Built |
| `tailor/` | Fit scoring, Claude tailoring, guardrails, resume + cover letter rendering | Built |
| `worker/` | Auto pipeline: score, tailor good fits, send to Telegram | Built |
| `bot/` | Telegram button handler | Built |
| `web/` | Approval queue + dashboard (FastAPI) | Built |
| `apply/` | Apply queue: form filling, PDFs, pacing, hand-offs; browser agent for Workday-style portals | Built (needs live testing) |
| `outreach/` | Contact finding, message drafts, email sending on your tap | Built |
| `mail/` | Inbox sync: replies + application emails | Built |
| `networking/` | Daily networking batch + follow-ups | Built |
| `prep/` | Interview prep sheets when an interview email lands | Built |
| `review/` | Weekly review: what's working, suggested setting changes | Built |
| `deploy/` | Server setup, update and backup scripts ([docs/deploy.md](docs/deploy.md)) | Built |
| `data/` | SQLite database (git-ignored) | |

Secrets go in `.env` (copy `.env.example`); never commit it.

## Run everything

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in Telegram + ANTHROPIC_API_KEY
set -a; source .env; set +a

python -m watcher.main --db data/jobs.db                          # finds jobs on company boards
python -m watcher.discover --db data/jobs.db                      # finds jobs on Indeed / Glassdoor / LinkedIn
python -m worker.main --db data/jobs.db                           # scores + tailors + sends
python -m bot.main --db data/jobs.db                              # handles Telegram buttons
python -m mail.main --db data/jobs.db                             # reads replies + application emails
python -m apply.main --db data/jobs.db --dry-run                  # apply queue (drop --dry-run when ready)
DB_PATH=data/jobs.db uvicorn --factory web.app:main_app --port 8000   # dashboard at localhost:8000
```

On a server: `docker compose up -d --build` runs all eight services. Setup from a blank server is
in [docs/deploy.md](docs/deploy.md).

The apply queue needs Chromium: `pip install playwright && playwright install chromium` locally
(the Docker image has it).

**Your own tailoring server:** set `[tailor] provider = "mcp"` in `config.toml`. The tool names,
schemas and a suggested profile structure are in [docs/tailoring-server.md](docs/tailoring-server.md).

**Job-search MCP server** (`profile_mcp/server.py`). Tools: `search_experience`, `get_profile`,
`render_resume`, `list_jobs`, `get_job`. It reads the same `profile/` files and `data/jobs.db` the
pipeline uses, so you can ask Claude things like "which tailored jobs are waiting on me?" or "draft a
resume for this posting from my real bullets". The pipeline itself imports the profile code directly;
it doesn't need the server running.

- *Local, on your laptop (stdio):* `claude mcp add job-search -- python -m profile_mcp.server`
  (run from the repo root). For Claude Desktop, add to `claude_desktop_config.json`:
  `{"mcpServers": {"job-search": {"command": "python", "args": ["-m", "profile_mcp.server"], "cwd": "/path/to/job-applier"}}}`
- *On the VPS (HTTP):* the `mcp` service in `docker-compose.yml` serves `http://127.0.0.1:8765/mcp`
  on the VPS only. Put it on your tailnet with `tailscale serve --bg --https=8765 http://127.0.0.1:8765`,
  set `MCP_ALLOWED_HOSTS=<vps>.<tailnet>.ts.net,127.0.0.1,localhost` in `.env` before `docker compose up -d mcp`,
  then from any of your devices: `claude mcp add --transport http job-search https://<vps>.<tailnet>.ts.net:8765/mcp`.
  Requests with any other Host header are refused. Never expose port 8765 publicly: the server has no login.
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

## Next steps

See [ROADMAP.md](ROADMAP.md): everything is built; what's left is the live run with your data.
