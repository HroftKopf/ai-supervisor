#!/usr/bin/env bash
# Consistent SQLite backup while the service is running (WAL-safe .backup).
set -euo pipefail
cd "$(dirname "$0")/.."
DB="var/ai_supervisor.db"
OUT_DIR="var/backups"
STAMP="$(date +%Y%m%d-%H%M%S)"
if [[ ! -f "$DB" ]]; then
  echo "error: database not found: $DB" >&2
  exit 1
fi
mkdir -p "$OUT_DIR"
chmod 700 "$OUT_DIR"
OUT="$OUT_DIR/ai_supervisor-$STAMP.db"
sqlite3 "$DB" ".backup '$OUT'"
chmod 600 "$OUT"
echo "$OUT"
