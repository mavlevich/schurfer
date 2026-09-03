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
# since RETENTION_COUNT cleanup only ever runs after a successful backup
# (further below in this file). That leftover file was itself most of the remaining
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
# (6.5 GiB) -- at the time, the raw dump AND the growing .gz coexisted on
# disk at once during a run (see the streaming rewrite below, which
# removes that), so peak need was well over one backup's own size, and a
# fixed floor sized below that could pass pre-flight and still run out of
# room. Fixed here: `flock` before anything else touches disk (5), and a
# required-space calculation sized off real data instead of the floor
# alone for every ordinary run (6) -- see the basis_kb comment further
# below for which signal is actually preferred and why (that priority has
# changed since this paragraph was first written; this paragraph is kept
# for its own history, not as a description of the current calculation).
LOCK_WAIT_SECONDS="${LOCK_WAIT_SECONDS:-300}"
# 2x, not 3x: colleague review, 2026-09-03, fourth round. Now that pg_dump
# streams straight into gzip (see the backup-execution comment further
# below) instead of writing a full raw dump to disk first, a run's own
# peak NEW disk usage is just the growing compressed output -- there is no
# separate raw file to also budget room for. The real remaining risk this
# multiplier protects against is the NEW backup genuinely being bigger than
# basis_kb's own estimate (organic DB growth since the last backup, mostly)
# -- 2x still tolerates the database roughly doubling in size between two
# consecutive daily backups before this check blocks a run, which is
# already a generous margin for day-over-day growth.
MIN_FREE_MULTIPLIER="${MIN_FREE_MULTIPLIER:-2}"
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
# simultaneous streamed dump-and-compress pipelines chew through the exact
# headroom the check just certified as sufficient for one. `flock` on a
# dedicated lock file (not the script file itself, which may not be
# writable/stable across deploys) serializes every invocation of this
# script on this host. Bounded wait, not indefinite: a genuinely stuck
# concurrent run should surface as a clear failure here, not hang whichever
# caller (cron or `make prod-deploy`) is waiting on this one.
LOCK_FILE="${BACKUP_DIR}/.backup.lock"
exec 200>"$LOCK_FILE"
if ! flock -w "$LOCK_WAIT_SECONDS" 200; then
    echo "[$(date -Iseconds)] ERROR: could not acquire $LOCK_FILE within" \
        "${LOCK_WAIT_SECONDS}s -- another backup run is still in progress on this host." \
        "Not starting pg_dump." >&2
    exit 1
fi

DATE=$(date +%Y%m%d_%H%M%S)
FINAL_GZ="${BACKUP_DIR}/schurfer_${DATE}.dump.gz"
# gzip writes through its OWN separate temp file, never straight to
# FINAL_GZ, so a partial .gz can never exist under the real backup
# filename -- only `mv`'d there once the whole streamed pipeline below has
# fully succeeded. Colleague review, 2026-09-03, third round: an earlier
# version ran plain `gzip "$FILE"` in place and the cleanup trap only ever
# removed the raw dump file, leaving a PARTIAL .gz sitting under the real
# backup filename forever whenever gzip itself failed after already
# writing some output. Colleague review, 2026-09-03, fourth round: pg_dump
# no longer writes a separate raw dump file to disk at all (see the
# backup-execution comment below) -- there is only ever this one temp file
# to clean up now, not two.
TMP_GZ="${FINAL_GZ}.partial"

# (The lock above needs no explicit release: FD 200 closes, and the flock
# with it, whenever this script's process exits, on every exit path.)
trap 'rm -f "$TMP_GZ"' EXIT

# Basis for the multiplier: the PREVIOUS BACKUP's own compressed size,
# preferred over the live database's raw uncompressed size -- this is the
# CURRENT priority; see the "same-day follow-up" paragraph near the top of
# this file for why an earlier revision briefly had it backwards. Colleague
# review, 2026-09-03, third round: checked this against real production
# numbers -- live DB 22,771,971,763 bytes (21.2 GiB) vs. the actual
# retained backup 10,023,806,873 bytes (9.3 GiB), a ~2.3x compression ratio
# from pg_dump -Fc plus gzip together. The live DB size is still queried
# and used as a same-order-of-magnitude estimate ONLY when there is no
# previous backup to size against yet (first run on this host), divided by
# COMPRESSED_SIZE_ESTIMATE_DIVISOR to approximate what the eventual
# compressed backup would be rather than assuming no compression at all.
#
# What required_kb actually needs to cover: ONLY this run's own NEW
# artifact (the growing $TMP_GZ, streaming-compressed straight from
# pg_dump -- see the backup-execution comment below, which removed the
# separate raw-dump-file phase a previous revision needed room for
# alongside the compressed output). It does NOT need to additionally
# budget room for the old retained backup that basis_kb is measured
# from: that file already exists on disk, so `df` (available_kb below)
# already excludes it from "available" -- counting it again in
# required_kb would double-count space that was never actually free to
# begin with. Colleague review, 2026-09-03, fourth round: an earlier
# version of this comment described peak usage as "old retained backup +
# new raw dump + new growing .gz", which conflated total on-disk footprint
# during a run with the ADDITIONAL free space actually required beyond
# what `df` already treats as used -- conservative in effect, but wrong as
# a description of the arithmetic actually being performed below.
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
# Streamed straight from pg_dump into gzip -- no separate raw .dump file
# ever touches disk. Colleague review, 2026-09-03, fourth round: the
# previous version wrote the full uncompressed dump to $RAW_FILE first,
# THEN compressed it, so the raw dump and the growing .gz coexisted on
# disk during every run; on 2026-09-03's real production numbers (21.2 GiB
# raw vs. 9.3 GiB compressed) that meant the actual peak footprint was
# roughly raw-dump-sized, not compressed-backup-sized, even though
# required_kb was computed off the compressed basis -- the 3x multiplier
# happened to roughly cover that gap by coincidence (raw is ~2.3x
# compressed here), not by any principled accounting. Streaming removes
# the raw file entirely: pg_dump's own stdout feeds gzip directly, so peak
# NEW disk usage during a run is just $TMP_GZ growing to its final
# compressed size, which is what required_kb's own basis_kb (the previous
# backup's compressed size) is actually measuring.
#
# `set -euo pipefail` (set at the top of this file) makes this pipeline's
# own exit status the LAST non-zero status among pg_dump/gzip, not just
# gzip's: if pg_dump fails partway (dropped connection, statement
# timeout, disk error on the Postgres side), gzip still sees a normal EOF
# on its stdin and exits 0 having compressed whatever partial bytes it
# received -- pipefail is what makes that partial success not silently
# masked by gzip's own success, aborting this script and running the
# cleanup trap exactly as if gzip itself had failed.
docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc | gzip -c > "$TMP_GZ"
mv "$TMP_GZ" "$FINAL_GZ"

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
