#!/usr/bin/env bash
# Pull the latest code, rebuild, restart, and check everything came up. Run from the server.
set -euo pipefail
cd "$(dirname "$0")/.."
"$PWD/deploy/backup.sh"
git pull --ff-only
docker compose up -d --build --remove-orphans
sleep 10
docker compose ps
if docker compose ps --format '{{.Service}} {{.State}}' | grep -v ' running$'; then
  echo "Some services aren't running: docker compose logs <service>"
  exit 1
fi
echo "All services running."
