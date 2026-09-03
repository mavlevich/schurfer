#!/usr/bin/env bash
# Daily PostgreSQL backup: pg_dump → gzip → local retention → optional Telegram alert.
# Run via cron: 0 3 * * * /opt/schurfer/infra/scripts/backup.sh >> /var/log/schurfer-backup.log 2>&1
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/opt/schurfer/backups}"
CONTAINER="${POSTGRES_CONTAINER:-schurfer-postgres}"
DB_USER="${DB_USER:-schurfer}"
DB_NAME="${DB_NAME:-schurfer}"
RETENTION_COUNT="${RETENTION_COUNT:-1}"
# Found 2026-09-03: a real backup run hit "No space left on device" mid-gzip
# (root disk at 88% used, weekly Docker build-cache prune hadn't run in 3
# days) -- `set -euo pipefail` correctly aborted the whole run, but left the
# uncompressed .dump it had already written sitting in $BACKUP_DIR forever,
# since RETENTION_COUNT cleanup (line ~55) only ever runs after a
# successful gzip. That leftover file was itself most of the remaining
# disk pressure the next run needed to succeed. Two independent fixes:
# (1) a cleanup trap so a failed run never leaves its own partial output
# behind; (2) a pre-flight headroom check that fails fast with a clear
# message before pg_dump even starts, instead of mid-gzip once most of the
# work (and most of the disk) is already spent.
MIN_FREE_MULTIPLIER="${MIN_FREE_MULTIPLIER:-3}"
MIN_FREE_FLOOR_KB="${MIN_FREE_FLOOR_KB:-8388608}" # 8 GiB, used when no prior backup exists to size against

DATE=$(date +%Y%m%d_%H%M%S)
RAW_FILE="${BACKUP_DIR}/schurfer_${DATE}.dump"
FILE="$RAW_FILE"

mkdir -p "$BACKUP_DIR"

# Cleanup-on-failure: if pg_dump or gzip fails partway (including via
# `set -e`), remove the raw dump this run itself was writing, so a failed
# run's own leftovers can never become next run's disk-pressure cause.
# Deliberately targets ONLY $RAW_FILE, which gzip itself already deletes
# on success -- never $FILE, whose value changes to the finished .gz path
# after a successful gzip; trapping that too would risk deleting a
# just-completed backup if anything failed between the gzip line and
# retention cleanup below.
trap 'rm -f "$RAW_FILE"' EXIT

previous_backup="$(ls -1t "$BACKUP_DIR"/schurfer_*.dump.gz 2>/dev/null | head -n 1 || true)"
if [[ -n "$previous_backup" ]]; then
    previous_size_kb=$(du -sk "$previous_backup" | cut -f1)
    required_kb=$((previous_size_kb * MIN_FREE_MULTIPLIER))
else
    required_kb="$MIN_FREE_FLOOR_KB"
fi
available_kb=$(df -Pk "$BACKUP_DIR" | awk 'NR==2 {print $4}')
if [[ "$available_kb" -lt "$required_kb" ]]; then
    echo "[$(date -Iseconds)] ERROR: only ${available_kb}KB free in $BACKUP_DIR," \
        "need at least ${required_kb}KB (${MIN_FREE_MULTIPLIER}x the previous backup," \
        "or the ${MIN_FREE_FLOOR_KB}KB floor with no previous backup to size against)." \
        "Not starting pg_dump. Free disk space (see: make prod-docker-prune-run)" \
        "and retry." >&2
    if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
        curl -sf "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
            --data-urlencode "text=Backup SKIPPED: only ${available_kb}KB free, need ${required_kb}KB" \
            > /dev/null || echo "Warning: Telegram notification failed"
    fi
    exit 1
fi

echo "[$(date -Iseconds)] Starting backup..."
docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > "$FILE"
gzip "$FILE"
FILE="${FILE}.gz"

SIZE=$(du -sh "$FILE" | cut -f1)
echo "[$(date -Iseconds)] Saved: $(basename "$FILE") ($SIZE)"

# Keep only the most recent RETENTION_COUNT backups
ls -1t "$BACKUP_DIR"/schurfer_*.dump.gz | tail -n +$((RETENTION_COUNT + 1)) | xargs -r rm -f
echo "[$(date -Iseconds)] Retention: kept last ${RETENTION_COUNT} backups"

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
