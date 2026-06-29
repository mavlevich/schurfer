#!/usr/bin/env bash
# Daily PostgreSQL backup: pg_dump → gzip → local retention → optional Telegram alert.
# Run via cron: 0 3 * * * /opt/schurfer/infra/scripts/backup.sh >> /var/log/schurfer-backup.log 2>&1
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/opt/schurfer/backups}"
CONTAINER="${POSTGRES_CONTAINER:-schurfer-postgres}"
DB_USER="${DB_USER:-schurfer}"
DB_NAME="${DB_NAME:-schurfer}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"

DATE=$(date +%Y%m%d_%H%M%S)
FILE="${BACKUP_DIR}/schurfer_${DATE}.dump"

mkdir -p "$BACKUP_DIR"

echo "[$(date -Iseconds)] Starting backup..."
docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > "$FILE"
gzip "$FILE"
FILE="${FILE}.gz"

SIZE=$(du -sh "$FILE" | cut -f1)
echo "[$(date -Iseconds)] Saved: $(basename "$FILE") ($SIZE)"

# Remove backups older than RETENTION_DAYS
find "$BACKUP_DIR" -name "schurfer_*.dump.gz" -mtime +"$RETENTION_DAYS" -delete
echo "[$(date -Iseconds)] Retention: kept last ${RETENTION_DAYS} days"

# Offsite upload — requires rclone configured with a remote named "r2" (or B2/S3).
# To enable: install rclone, run `rclone config`, then uncomment:
# rclone copy "$FILE" r2:schurfer-backups/ \
#     && echo "[$(date -Iseconds)] Offsite: uploaded to r2:schurfer-backups/" \
#     || echo "[$(date -Iseconds)] WARNING: offsite upload failed"

# Telegram alert (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID to enable)
if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
    curl -sf "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=Backup OK: schurfer_${DATE}.dump.gz (${SIZE})" \
        > /dev/null || echo "Warning: Telegram notification failed"
fi
