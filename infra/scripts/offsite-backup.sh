#!/usr/bin/env bash
# Offsite backup to a Hetzner Storage Box via BorgBackup.
#
# Runs alongside infra/scripts/backup.sh rather than replacing it. The local
# dump stays until a restore has actually been performed from this repository
# into a throwaway instance -- an unverified offsite archive is not a reason to
# drop the only verified copy. See docs/runbooks/offsite-backup-restore.md.
#
# Two archive families, deliberately separate:
#
#   db-*        the PostgreSQL dump. Pruned on a normal schedule.
#   research-*  runtime/market-path-cache, runtime/research-dataset-artifacts
#               and backups/reports. These are the inputs a frozen research
#               report needs to be reproducible at all, they are not in the
#               PostgreSQL dump, and they must outlive the dump retention (see
#               market_path_cache.py: a formal 100-episode sample silently lost
#               resolution from 99/100 to 94/100 when instruments delisted
#               between two runs of the same frozen cohort).
#
# Why this runs as root: the analytics container writes
# runtime/market-path-cache as root:root mode 600. The first run of this
# backup, as `deploy`, archived 57 of 1624 research files and exited 1 -- a
# warning, not an error. That is the failure mode this script is built around:
# a backup that reports success while being empty in the part that mattered.
set -euo pipefail

ENV_FILE="${OFFSITE_BACKUP_ENV:-/opt/schurfer/runtime/backup.env}"
REPO_ROOT="${REPO_ROOT:-/opt/schurfer}"
STATE_DIR="${STATE_DIR:-/opt/schurfer/runtime}"
CONTAINER="${POSTGRES_CONTAINER:-schurfer-postgres}"
DB_USER="${DB_USER:-schurfer}"
DB_NAME="${DB_NAME:-schurfer}"
LOCK_WAIT_SECONDS="${LOCK_WAIT_SECONDS:-300}"

# The paths whose completeness is asserted below. Order is irrelevant; every
# regular file under each must appear in the archive.
RESEARCH_PATHS=(
    "runtime/market-path-cache"
    "runtime/research-dataset-artifacts"
    "backups/reports"
)

# Written only after an archive actually succeeded. The alert watches THIS,
# not the timer's own last run: a unit that fires punctually and does nothing
# is the failure this repository has already shipped once (the docker prune
# unit was silently a no-op for months because of a permissions error).
DB_STAMP="${STATE_DIR}/offsite-backup-db.stamp"
RESEARCH_STAMP="${STATE_DIR}/offsite-backup-research.stamp"

log() { echo "[$(date -Iseconds)] $*"; }

fail() {
    log "ERROR: $*" >&2
    if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
        curl -sf "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
            --data-urlencode "text=Offsite backup FAILED: $*" \
            > /dev/null || log "Warning: Telegram notification failed"
    fi
    exit 1
}

[[ -r "$ENV_FILE" ]] || fail "cannot read $ENV_FILE"
set -a
# shellcheck source=/dev/null
. "$ENV_FILE"
set +a
: "${BORG_REPO:?BORG_REPO missing from $ENV_FILE}"

command -v borg >/dev/null || fail "borg is not installed"

# One run at a time on this host. Borg locks the repository itself, but a
# second run would then fail halfway with a lock error instead of waiting,
# and the caller (timer or deploy) would see a spurious failure.
LOCK_FILE="${STATE_DIR}/.offsite-backup.lock"
exec 200>"$LOCK_FILE"
flock -w "$LOCK_WAIT_SECONDS" 200 \
    || fail "another offsite backup run is still in progress (waited ${LOCK_WAIT_SECONDS}s)"

cd "$REPO_ROOT"

# --- research archive -------------------------------------------------------
#
# Counted before and after. borg exits 1 on "some files could not be read",
# which `set -e` does not catch and a human reading logs does not notice; the
# count is what actually enforces completeness, and it also catches the cases
# nobody has thought of yet (a new path, a new container, a different umask).
expected_files=0
for path in "${RESEARCH_PATHS[@]}"; do
    [[ -e "$path" ]] || fail "research path missing: $path"
    expected_files=$((expected_files + $(find "$path" -type f | wc -l)))
done
log "research: ${expected_files} files expected"

research_archive="research-$(date -u +%Y-%m-%dT%H:%M:%S)"
borg create --compression zstd,3 "::${research_archive}" "${RESEARCH_PATHS[@]}" \
    || fail "borg create failed for ${research_archive}"

archived_files=$(borg list --format '{type}{NL}' "::${research_archive}" | grep -c '^-' || true)
if [[ "$archived_files" -ne "$expected_files" ]]; then
    borg delete "::${research_archive}" || log "Warning: could not delete incomplete archive"
    fail "research archive incomplete: ${archived_files} of ${expected_files} files. Deleted."
fi
log "research: ${archived_files} files archived, complete"
date -Iseconds > "$RESEARCH_STAMP"

# --- database archive -------------------------------------------------------
#
# -Z0 because pg_dump's custom format compresses by default: handing borg an
# already-compressed stream dedups badly and pays for the same work twice.
#
# --content-from-command, not a shell pipe: a pipe lets borg see a clean EOF
# when pg_dump dies partway and store a truncated dump as a complete archive.
# Verified on this repository, 2026-09-08: a producer writing partial output
# and exiting 3 makes borg exit 2 and leave no archive at all.
db_archive="db-$(date -u +%Y-%m-%dT%H:%M:%S)"
log "database: starting ${db_archive}"
borg create --stats --compression zstd,3 \
    --content-from-command --stdin-name schurfer.dump \
    "::${db_archive}" -- \
    docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc -Z0 \
    || fail "borg create failed for ${db_archive}"
log "database: ${db_archive} complete"
date -Iseconds > "$DB_STAMP"

# --- retention --------------------------------------------------------------
#
# Separate globs, separate rules. Research inputs are excluded from the dump
# schedule on purpose: a report frozen a year ago is only reproducible while
# the candles it was built from still exist somewhere.
borg prune --glob-archives 'db-*' \
    --keep-daily 7 --keep-weekly 4 --keep-monthly 6 \
    || fail "borg prune failed for db-*"
borg prune --glob-archives 'research-*' \
    --keep-daily 7 --keep-weekly 8 --keep-monthly 24 \
    || fail "borg prune failed for research-*"

borg compact || log "Warning: borg compact failed; space will be reclaimed next run"

log "done"
borg info | tail -6
