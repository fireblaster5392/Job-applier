#!/usr/bin/env bash
# Consistent copy of the SQLite database (safe while every service is running), kept 14 days.
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
DB="$DIR/data/jobs.db"
OUT="$DIR/data/backups"
mkdir -p "$OUT"
[ -f "$DB" ] || { echo "$(date -Is) no database yet"; exit 0; }
STAMP="$(date +%Y-%m-%d)"
python3 - "$DB" "$OUT/jobs-$STAMP.db" <<'PY'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
PY
gzip -f "$OUT/jobs-$STAMP.db"
find "$OUT" -name 'jobs-*.db.gz' -mtime +14 -delete
echo "$(date -Is) backed up to $OUT/jobs-$STAMP.db.gz"
