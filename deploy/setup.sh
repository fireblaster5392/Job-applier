#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 24.04 server for the job pipeline. Run as root:
#
#   curl -fsSL https://raw.githubusercontent.com/fireblaster5392/job-applier/main/deploy/setup.sh -o setup.sh
#   sudo bash setup.sh
#
# Installs Docker and Tailscale, closes every public port except SSH, turns on automatic security
# updates, clones the repo to /opt/job-applier, and installs a nightly database backup.
# It does not start the pipeline: fill in .env and profile/ first (docs/deploy.md, step 4).
set -euo pipefail

REPO="${REPO:-https://github.com/fireblaster5392/job-applier.git}"
DIR="${DIR:-/opt/job-applier}"
APP_USER="${APP_USER:-jobs}"

[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo bash setup.sh)"; exit 1; }

echo "==> Packages and automatic security updates"
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -yq ca-certificates curl git ufw python3 unattended-upgrades
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "==> Docker"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

echo "==> Tailscale (private access to the dashboard and MCP server)"
if ! command -v tailscale >/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi

echo "==> Firewall: SSH only from the internet; everything else over Tailscale"
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow in on tailscale0
ufw --force enable

echo "==> App user and code in $DIR"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /bin/bash "$APP_USER"
usermod -aG docker "$APP_USER"
if [ ! -d "$DIR/.git" ]; then
  git clone "$REPO" "$DIR"
fi
mkdir -p "$DIR/data/backups"
if [ ! -f "$DIR/.env" ]; then
  cp "$DIR/.env.example" "$DIR/.env"
fi
if [ ! -f "$DIR/profile/answers.yaml" ]; then
  cp "$DIR/profile/answers.example.yaml" "$DIR/profile/answers.yaml"
fi
chmod 600 "$DIR/.env" "$DIR/profile/answers.yaml"
chown -R "$APP_USER:$APP_USER" "$DIR"

echo "==> Nightly backup at 03:30 (keeps 14 days)"
cat > /etc/cron.d/job-applier-backup <<CRON
30 3 * * * $APP_USER $DIR/deploy/backup.sh >> $DIR/data/backups/backup.log 2>&1
CRON

echo "==> Start on boot"
cat > /etc/systemd/system/job-applier.service <<UNIT
[Unit]
Description=Job pipeline (docker compose)
Requires=docker.service
After=docker.service network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
User=$APP_USER
WorkingDirectory=$DIR
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose stop

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable job-applier.service

cat <<NEXT

Done. Next (docs/deploy.md):
  1. sudo tailscale up                         # log in; note the machine's name
  2. sudo -u $APP_USER nano $DIR/.env          # keys, Telegram, email
  3. Fill $DIR/profile/ (me.yaml, experience.yaml, projects/, skills.yaml, format.yaml, answers.yaml)
  4. cd $DIR && sudo -u $APP_USER docker compose build
     sudo -u $APP_USER docker compose run --rm web python -m profile_mcp.check
     sudo -u $APP_USER docker compose up -d
  5. sudo tailscale serve --bg --https=443 http://127.0.0.1:8000     # dashboard
     sudo tailscale serve --bg --https=8765 http://127.0.0.1:8765    # MCP server
NEXT
