#!/usr/bin/env bash
# Start everything on this machine with Docker: ./start.sh
# First run creates .env and profile/answers.yaml from the examples and stops so you can fill them.
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env ] || { cp .env.example .env; echo "Created .env: fill in your keys, then run ./start.sh again."; exit 1; }
[ -f profile/answers.yaml ] || { cp profile/answers.example.yaml profile/answers.yaml;
  echo "Created profile/answers.yaml: fill in your answers, then run ./start.sh again."; exit 1; }
mkdir -p data

echo "==> Building (first time takes a few minutes)"
docker compose build

echo "==> Checking setup"
docker compose run --rm --no-deps -v "$PWD/.env:/app/.env:ro" web python doctor.py --online

echo "==> Starting"
docker compose up -d
sleep 5
docker compose ps
cat <<DONE

Running. Dashboard: http://localhost:8000
Logs:  docker compose logs -f worker        Stop: docker compose down
Telegram messages arrive as matching jobs are found (the first poll of each board is stored
quietly as backlog, so only jobs posted from now on are sent).
DONE
