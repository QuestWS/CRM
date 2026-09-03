#!/usr/bin/env bash
#
# Back up Twenty: the database and the uploaded files.
#
#   bash scripts/backup.sh [destination-dir]
#
# Put it in cron. Twenty runs irreversible migrations on version upgrades, so
# take one before every `docker compose pull`.
set -euo pipefail

DEST="${1:-/var/backups/quest-crm}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%F-%H%M)"
cd "$REPO_DIR"

mkdir -p "$DEST"

PGUSER="$(grep -E '^PG_DATABASE_USER=' .env 2>/dev/null | cut -d= -f2- || true)"
PGUSER="${PGUSER:-postgres}"

echo "==> Database"
docker compose exec -T db pg_dump -U "$PGUSER" default | gzip > "$DEST/db-$STAMP.sql.gz"

echo "==> Uploaded files"
# Straight out of the volume: the server container may be mid-write, and a tar
# of the mount is consistent enough for attachments while pg_dump handles the
# part that has to be transactional.
docker run --rm \
  -v quest-crm_server-local-data:/data:ro \
  -v "$DEST":/backup \
  alpine tar czf "/backup/files-$STAMP.tar.gz" -C /data . 2>/dev/null \
  || echo "  (no file volume yet — nothing uploaded)"

echo "==> Keeping the last 14"
ls -1t "$DEST"/db-*.sql.gz 2>/dev/null | tail -n +15 | xargs -r rm --
ls -1t "$DEST"/files-*.tar.gz 2>/dev/null | tail -n +15 | xargs -r rm --

echo
echo "Done: $DEST"
ls -lh "$DEST" | tail -5
echo
echo "These are on the same disk as the server. Copy them off it —"
echo "Oracle's free tier includes object storage; use it."
