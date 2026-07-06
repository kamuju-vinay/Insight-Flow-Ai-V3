#!/usr/bin/env bash
# ============================================================================
# entrypoint.sh — runs inside the container on every start/restart.
#
# Purpose: keep crawler.db (SQLite) on Railway's persistent Volume, which
# survives redeploys, instead of the container's ephemeral filesystem, which
# is wiped every time you push new code. This is purely infrastructure —
# backend/db.py itself is untouched; it still just opens ../crawler.db
# relative to backend/, we just make that path point at the volume.
#
# DATA_DIR is set as a Railway environment variable pointing at wherever you
# mount the Volume (this repo assumes /data — see README).
# ============================================================================
set -e

DATA_DIR="${DATA_DIR:-/data}"
mkdir -p "$DATA_DIR"

DB_PATH="/app/crawler.db"
PERSISTED_DB="$DATA_DIR/crawler.db"

# First-ever boot: if the app already created a fresh crawler.db inside the
# image (it won't have, since it's not shipped in the repo) move it over.
if [ -e "$DB_PATH" ] && [ ! -L "$DB_PATH" ]; then
  mv "$DB_PATH" "$PERSISTED_DB" 2>/dev/null || true
fi

# Always point /app/crawler.db at the volume, so backend/db.py's relative
# path resolves to persistent storage without any code changes.
ln -sf "$PERSISTED_DB" "$DB_PATH"

cd /app
exec python -m backend.main
