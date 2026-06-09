#!/usr/bin/env bash
#
# HR_DV_worker — Postgres backup with rotation.
# Dumps the dockerized Postgres to a gzip file and keeps the last N days.
#
# Manual run:   bash deploy/backup_postgres.sh
# Cron (daily 04:30):  add via `crontab -e` on the hrdv user:
#     30 4 * * * /opt/HR_DV_worker/deploy/backup_postgres.sh >> /opt/HR_DV_worker/runtime_logs/backup.log 2>&1
#
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
KEEP_DAYS="${KEEP_DAYS:-7}"
DB_USER="hr_dv_worker"
DB_NAME="hr_dv_worker"

cd "$APP_DIR"
mkdir -p "$BACKUP_DIR"

STAMP="$(date +%Y%m%d_%H%M%S)"
OUT="$BACKUP_DIR/${DB_NAME}_${STAMP}.sql.gz"

echo "[$(date -Is)] dumping $DB_NAME -> $OUT"
docker compose exec -T postgres pg_dump -U "$DB_USER" "$DB_NAME" | gzip > "$OUT"

# Rotation
find "$BACKUP_DIR" -name "${DB_NAME}_*.sql.gz" -type f -mtime "+${KEEP_DAYS}" -delete
echo "[$(date -Is)] backup done; kept last ${KEEP_DAYS} days"
