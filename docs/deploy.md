# Deploying to a cloud server

Everything runs on one small Linux server with Docker Compose. The dashboard and MCP server are
only reachable over your Tailscale network, so nothing but SSH is open to the internet.

## 1. Get a server

Any provider works. You need Ubuntu 24.04 with **2 vCPU and 4 GB RAM**: the apply queue runs
Chromium, which needs more than 2 GB. 40 GB of disk is plenty. Good picks:

- Hetzner Cloud CX22 (cheapest; EU or US locations)
- DigitalOcean / Vultr / Linode "regular" 4 GB droplet
- AWS Lightsail 4 GB

Add your SSH key when you create it and pick the region closest to you.

## 2. Run the setup script

```bash
ssh root@<server-ip>
curl -fsSL https://raw.githubusercontent.com/fireblaster5392/job-applier/main/deploy/setup.sh -o setup.sh
bash setup.sh
```

It installs Docker and Tailscale, sets the firewall to SSH plus Tailscale only, turns on automatic
security updates, creates a `jobs` user, clones the repo to `/opt/job-applier`, installs a nightly
database backup (03:30, kept 14 days in `data/backups/`) and makes the pipeline start on boot.
It's safe to run again.

If the repo is private, clone it yourself first (`git clone` with a deploy key or token into
`/opt/job-applier`), then run the script: it uses the existing clone.

## 3. Join your Tailscale network

```bash
sudo tailscale up
```

Open the link it prints and log in with the same account as your laptop and phone. The server
shows up as a machine name like `job-server`. Install Tailscale on your phone too, so the Telegram
"Open dashboard" buttons work there.

## 4. Secrets and profile

```bash
sudo -u jobs nano /opt/job-applier/.env
```

| Variable | Needed for |
|---|---|
| `ANTHROPIC_API_KEY` | scoring, tailoring, form answers, the browser agent, prep sheets |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | approvals and alerts (`python -m watcher.telegram_setup` prints the chat id) |
| `DASHBOARD_URL` | buttons in Telegram, e.g. `https://job-server.<tailnet>.ts.net` |
| `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`, ... | sending outreach email and reading replies (Gmail app password) |
| `BRAVE_API_KEY`, `APOLLO_API_KEY` | finding people for outreach; company news in prep sheets |
| `APPLY_LOGIN_EMAIL`, `APPLY_LOGIN_PASSWORD` | optional: lets the browser agent sign in to Workday-style portals |
| `MCP_ALLOWED_HOSTS` | `job-server.<tailnet>.ts.net,127.0.0.1,localhost` |

Then fill your profile in `/opt/job-applier/profile/`: what goes where is in
[profile/README.md](../profile/README.md). An easy way is to edit the files on your laptop and
copy them up: `scp -r profile/* root@<server>:/opt/job-applier/profile/` and then
`chown -R jobs:jobs /opt/job-applier/profile`.

Review `config.toml` too: target companies, title filters, discovery searches, daily caps,
timezone.

## 5. Build, check, start

```bash
cd /opt/job-applier
sudo -u jobs docker compose build
sudo -u jobs docker compose run --rm web python -m profile_mcp.check
sudo -u jobs docker compose up -d
sudo -u jobs docker compose ps
```

For the first day, set `dry_run = true` under `[apply]` in `config.toml`: the apply queue fills
forms and sends you screenshots but never submits. Turn it off once the screenshots look right
(`docker compose restart apply`).

## 6. Dashboard and MCP server on your tailnet

```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:8000     # dashboard
sudo tailscale serve --bg --https=8765 http://127.0.0.1:8765    # MCP server
```

The dashboard is then at `https://job-server.<tailnet>.ts.net` from any of your devices. To use
the MCP server from Claude Code: `claude mcp add --transport http job-search https://job-server.<tailnet>.ts.net:8765/mcp`.

## Day to day

| Task | Command (in `/opt/job-applier`) |
|---|---|
| Update to the latest code | `sudo -u jobs deploy/update.sh` (backs up, pulls, rebuilds, checks) |
| Logs for one service | `sudo -u jobs docker compose logs -f --tail 100 worker` |
| Restart after editing config.toml | `sudo -u jobs docker compose restart` |
| Profile edits | none needed: picked up on the next job |
| Backup now | `sudo -u jobs deploy/backup.sh` |
| Restore a backup | `docker compose stop`, `gunzip -c data/backups/jobs-DATE.db.gz > data/jobs.db`, `docker compose up -d` |

The services: `watcher` (company boards every 60 s), `discover` (Indeed / Glassdoor / LinkedIn
search), `worker` (scoring, tailoring, outreach, networking, follow-ups, prep sheets, weekly
review), `apply` (submissions at a human pace), `web` (dashboard), `bot` (Telegram buttons),
`mail` (inbox), `mcp` (MCP server). Each restarts on its own if it crashes, and logs are capped at
30 MB per service.

## Costs

The server is the fixed cost, roughly $5-25 a month depending on provider. Claude usage depends
on volume: under $0.001 per scored job, about $0.14 per tailored application on Opus, $0.30-1.50 per
browser-agent application, $0.10-0.20 per prep sheet. Brave Search and Apollo have free tiers
that cover a personal search.
