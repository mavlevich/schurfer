#!/usr/bin/env bash
# Daily PostgreSQL backup: pg_dump → gzip → local retention → optional Telegram alert.
# Run via cron: 0 3 * * * /opt/schurfer/infra/scripts/backup.sh >> /var/log/schurfer-backup.log 2>&1
# Requires `flock` (util-linux) -- preinstalled on Ubuntu (prod, CI), not
# on macOS by default: `brew install flock` for local runs/tests.
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
# since RETENTION_COUNT cleanup (line ~90) only ever runs after a
# successful gzip. That leftover file was itself most of the remaining
# disk pressure the next run needed to succeed. Independent fixes: (1) a
# cleanup trap so a failed run never leaves its own partial output behind;
# (2) a pre-flight headroom check that fails fast with a clear message
# before pg_dump even starts, instead of mid-gzip once most of the work
# (and most of the disk) is already spent.
#
# Colleague review (2026-09-03), same-day follow-up, found two more real
# gaps in that first fix: (3) no lock -- a cron-scheduled run and a
# `make prod-deploy`-triggered run could both pass the pre-flight check
# before either had actually written anything, then both start pg_dump at
# once, silently invalidating the headroom check's own guarantee and
# reproducing the exact incident this script exists to prevent; (4) the
# fallback floor (8 GiB) undersized a real observed production backup
# (6.5 GiB) -- during gzip, the raw dump AND the growing .gz coexist on
# disk at once, so peak need is well over one backup's own size, and a
# fixed floor sized below that can pass pre-flight and still run out of
# room. Fixed here: `flock` before anything else touches disk (5), and the
# required-space calculation now prefers the LIVE database's own current
# size (the most accurate, least stale signal available) over a
# potentially-stale previous backup's size, with the floor only used as a
# last resort when neither signal is reachable (6).
LOCK_WAIT_SECONDS="${LOCK_WAIT_SECONDS:-300}"
MIN_FREE_MULTIPLIER="${MIN_FREE_MULTIPLIER:-3}"
# 16 GiB: a last-resort floor only, used when this host has no previous
# backup AND the live database size could not be queried -- effectively
# "first run ever, or something else is already wrong". Every ordinary run
# sizes itself off real data (see basis_kb below), not this constant.
MIN_FREE_FLOOR_KB="${MIN_FREE_FLOOR_KB:-16777216}"

mkdir -p "$BACKUP_DIR"

# Singleton lock, acquired before anything else touches disk: without this,
# a cron-scheduled run and a prod-deploy-triggered run can both pass the
# pre-flight headroom check (each individually correct, computed before
# either had written a byte), then both start pg_dump concurrently -- two
# simultaneous raw dumps plus two simultaneous growing .gz files chew
# through the exact headroom the check just certified as sufficient for
# one. `flock` on a dedicated lock file (not the script file itself, which
# may not be writable/stable across deploys) serializes every invocation
# of this script on this host. Bounded wait, not indefinite: a genuinely
# stuck concurrent run should surface as a clear failure here, not hang
# whichever caller (cron or `make prod-deploy`) is waiting on this one.
LOCK_FILE="${BACKUP_DIR}/.backup.lock"
exec 200>"$LOCK_FILE"
if ! flock -w "$LOCK_WAIT_SECONDS" 200; then
    echo "[$(date -Iseconds)] ERROR: could not acquire $LOCK_FILE within" \
        "${LOCK_WAIT_SECONDS}s -- another backup run is still in progress on this host." \
        "Not starting pg_dump." >&2
    exit 1
fi

DATE=$(date +%Y%m%d_%H%M%S)
RAW_FILE="${BACKUP_DIR}/schurfer_${DATE}.dump"
# gzip writes through its OWN separate temp file, never straight to
# FINAL_GZ: colleague review, 2026-09-03, third round -- the previous
# version ran plain `gzip "$FILE"` (writing directly to schurfer_*.dump.gz
# in place) and the cleanup trap only ever removed $RAW_FILE. Reviewer
# reproduced a forced gzip failure that had already written PART of the
# final .gz path before erroring out: the trap correctly removed the raw
# dump but left that partial .gz sitting in $BACKUP_DIR forever -- the
# exact class of leftover-file disk pressure this whole trap exists to
# prevent, just one step later in the pipeline. Writing to $TMP_GZ and
# only `mv`ing it to $FINAL_GZ after gzip fully succeeds means a partial
# .gz can never exist under the real backup filename; the trap covers
# BOTH files so a failure at any point (pg_dump, gzip, or in between)
# leaves $BACKUP_DIR exactly as it was before this run started.
TMP_GZ="${RAW_FILE}.gz.partial"
FINAL_GZ="${RAW_FILE}.gz"

# (The lock above needs no explicit release: FD 200 closes, and the flock
# with it, whenever this script's process exits, on every exit path.)
trap 'rm -f "$RAW_FILE" "$TMP_GZ"' EXIT

# Basis for the multiplier: the PREVIOUS BACKUP's own compressed size,
# preferred over the live database's raw uncompressed size. Colleague
# review, 2026-09-03, third round: checked this against real production
# numbers (2026-09-03) -- live DB 22,771,971,763 bytes (21.2 GiB) vs. the
# actual retained backup 10,023,806,873 bytes (9.3 GiB), a ~2.3x
# compression ratio from pg_dump -Fc plus gzip together. The peak
# concurrent usage a backup run actually needs room for (old retained
# backup + this run's new raw dump + this run's new growing .gz, all
# roughly COMPRESSED-backup-sized, not raw-DB-sized) was previously
# multiplied against the much larger RAW size, overestimating the real
# requirement by roughly that same compression ratio. The live DB size is
# still queried and used as a same-order-of-magnitude estimate ONLY when
# there is no previous backup to size against yet (first run on this
# host), divided by COMPRESSED_SIZE_ESTIMATE_DIVISOR to approximate what
# the eventual compressed backup would be rather than assuming no
# compression at all.
COMPRESSED_SIZE_ESTIMATE_DIVISOR="${COMPRESSED_SIZE_ESTIMATE_DIVISOR:-2}"

live_db_size_bytes="$(
    docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tAc \
        "SELECT pg_database_size('${DB_NAME}')" 2>/dev/null | tr -d '[:space:]' || true
)"
if [[ "$live_db_size_bytes" =~ ^[0-9]+$ ]]; then
    live_db_size_kb=$((live_db_size_bytes / 1024))
else
    live_db_size_kb=0
fi

previous_backup="$(ls -1t "$BACKUP_DIR"/schurfer_*.dump.gz 2>/dev/null | head -n 1 || true)"
if [[ -n "$previous_backup" ]]; then
    previous_size_kb=$(du -sk "$previous_backup" | cut -f1)
else
    previous_size_kb=0
fi

if [[ "$previous_size_kb" -gt 0 ]]; then
    basis_kb=$previous_size_kb
elif [[ "$live_db_size_kb" -gt 0 ]]; then
    basis_kb=$((live_db_size_kb / COMPRESSED_SIZE_ESTIMATE_DIVISOR))
else
    basis_kb=0
fi

if [[ "$basis_kb" -eq 0 ]]; then
    required_kb="$MIN_FREE_FLOOR_KB"
else
    required_kb=$((basis_kb * MIN_FREE_MULTIPLIER))
    if [[ "$required_kb" -lt "$MIN_FREE_FLOOR_KB" ]]; then
        required_kb="$MIN_FREE_FLOOR_KB"
    fi
fi

available_kb=$(df -Pk "$BACKUP_DIR" | awk 'NR==2 {print $4}')
if [[ "$available_kb" -lt "$required_kb" ]]; then
    echo "[$(date -Iseconds)] ERROR: only ${available_kb}KB free in $BACKUP_DIR," \
        "need at least ${required_kb}KB (${MIN_FREE_MULTIPLIER}x basis ${basis_kb}KB --" \
        "previous backup ${previous_size_kb}KB if available, else live DB" \
        "${live_db_size_kb}KB / ${COMPRESSED_SIZE_ESTIMATE_DIVISOR} -- floored at" \
        "${MIN_FREE_FLOOR_KB}KB). Not starting pg_dump. Free disk space" \
        "(see: make prod-docker-prune-run) and retry." >&2
    if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
        curl -sf "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
            --data-urlencode "text=Backup SKIPPED: only ${available_kb}KB free, need ${required_kb}KB" \
            > /dev/null || echo "Warning: Telegram notification failed"
    fi
    exit 1
fi

echo "[$(date -Iseconds)] Starting backup..."
docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > "$RAW_FILE"
# -c (write to stdout, redirected here) rather than in-place: leaves
# $RAW_FILE untouched until $TMP_GZ has been written successfully in
# full, so a failure partway through never leaves a half-written file
# under the real backup filename -- see the trap's own comment above.
gzip -c "$RAW_FILE" > "$TMP_GZ"
mv "$TMP_GZ" "$FINAL_GZ"
rm -f "$RAW_FILE"

SIZE=$(du -sh "$FINAL_GZ" | cut -f1)
echo "[$(date -Iseconds)] Saved: $(basename "$FINAL_GZ") ($SIZE)"

# Keep only the most recent RETENTION_COUNT backups
ls -1t "$BACKUP_DIR"/schurfer_*.dump.gz | tail -n +$((RETENTION_COUNT + 1)) | xargs -r rm -f
echo "[$(date -Iseconds)] Retention: kept last ${RETENTION_COUNT} backups"

# Offsite upload — requires rclone configured with a remote named "r2" (or B2/S3).
# To enable: install rclone, run `rclone config`, then uncomment:
# rclone copy "$FINAL_GZ" r2:schurfer-backups/ \
#     && echo "[$(date -Iseconds)] Offsite: uploaded to r2:schurfer-backups/" \
#     || echo "[$(date -Iseconds)] WARNING: offsite upload failed"

# Telegram alert (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID to enable)
if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
    curl -sf "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=Backup OK: schurfer_${DATE}.dump.gz (${SIZE})" \
        > /dev/null || echo "Warning: Telegram notification failed"
fi
