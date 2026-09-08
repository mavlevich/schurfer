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
# The database archive runs FIRST and the two are independent: a research
# failure must not cost us the dump, which is the more valuable artifact. The
# job's own exit status is still failure if either part failed.
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
CURL_CONNECT_TIMEOUT="${CURL_CONNECT_TIMEOUT:-10}"
CURL_MAX_TIME="${CURL_MAX_TIME:-30}"

# The paths whose contents are archived and then verified. Note the limit of
# what that verification proves: it establishes that every file found under
# THESE paths reached the archive. A new directory that nobody added to this
# list is invisible to it.
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
BARS_STAMP="${STATE_DIR}/offsite-backup-bars.stamp"

# Exported cold minute bars. Timescale drops the source after 35 days and the
# PostgreSQL dump only holds what is still in the database, so once a day is
# exported and archived here, this is the only copy that exists anywhere.
COLD_BARS_DIR="${COLD_BARS_DIR:-runtime/cold-bars}"

log() { echo "[$(date -Iseconds)] $*"; }

notify() {
    [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]] || return 0
    # Bounded on purpose: an unbounded curl on a hung connection would keep
    # this oneshot unit running, and every later timer firing would then be
    # skipped because the previous run never finished.
    curl -sf --connect-timeout "$CURL_CONNECT_TIMEOUT" --max-time "$CURL_MAX_TIME" \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=$1" \
        > /dev/null || log "Warning: Telegram notification failed"
}

fail() {
    log "ERROR: $*" >&2
    notify "Offsite backup FAILED: $*"
    exit 1
}

# Records a failure of one archive family without abandoning the other.
FAILURES=()
part_failed() {
    log "ERROR: $*" >&2
    FAILURES+=("$1")
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

# Best effort: a failed `borg create` can leave a partial archive under the
# name it was given. Removing it keeps the repository free of archives that
# look like backups and are not.
drop_archive() {
    borg delete "::$1" >/dev/null 2>&1 || log "Warning: could not delete archive $1"
}

# --- database archive -------------------------------------------------------
#
# First, because it is the artifact that matters most and nothing about the
# research archive should be able to prevent it.
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
if borg create --stats --compression zstd,3 \
    --content-from-command --stdin-name schurfer.dump \
    "::${db_archive}" -- \
    docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc -Z0
then
    log "database: ${db_archive} complete"
    date -Iseconds > "$DB_STAMP"
else
    drop_archive "$db_archive"
    part_failed "borg create failed for ${db_archive}"
fi

# --- research archive -------------------------------------------------------
#
# The file list is captured once and handed to borg explicitly, rather than
# letting borg walk the directories and then comparing counts. Those
# directories are written by running containers: a file appearing between the
# walk and the archive made a perfectly good archive look wrong, and the
# earlier version of this script deleted it and aborted the whole job over an
# ordinary new cache entry.
#
# `--paths-from-stdin` archives exactly the listed paths, no more and no less,
# so the same list is both the input and the expected result. A file created
# mid-run is simply picked up by the next run.
if research_files=$(find "${RESEARCH_PATHS[@]}" -type f -print 2>/dev/null | sort); then
    :
else
    research_files=""
fi

if [[ -z "$research_files" ]]; then
    part_failed "no research files found under: ${RESEARCH_PATHS[*]}"
elif printf '%s' "$research_files" | grep -q '[[:cntrl:]]'; then
    # Paths are joined by newline below; a newline inside a filename would
    # silently split one path into two and the comparison would be nonsense.
    part_failed "a research path contains a control character; refusing to archive"
else
    expected_count=$(printf '%s\n' "$research_files" | wc -l)
    research_archive="research-$(date -u +%Y-%m-%dT%H:%M:%S)"
    log "research: archiving ${expected_count} files as ${research_archive}"

    if printf '%s\n' "$research_files" \
        | borg create --compression zstd,3 --paths-from-stdin "::${research_archive}"
    then
        # borg list's own exit status is checked before its output is used.
        # Piping straight into a counter and swallowing errors with `|| true`
        # let a borg failure that had already printed plausible output be
        # recorded as a verified, complete archive.
        if archived_files=$(borg list --format '{path}{NL}' "::${research_archive}"); then
            if [[ "$(printf '%s\n' "$archived_files" | sort)" == "$research_files" ]]; then
                log "research: ${expected_count} files archived, contents match"
                date -Iseconds > "$RESEARCH_STAMP"
            else
                drop_archive "$research_archive"
                part_failed "research archive contents differ from the list requested. Deleted."
            fi
        else
            drop_archive "$research_archive"
            part_failed "could not list ${research_archive} to verify it. Deleted."
        fi
    else
        drop_archive "$research_archive"
        part_failed "borg create failed for ${research_archive}"
    fi
fi

# --- cold bar archive -------------------------------------------------------
#
# Parquet files are immutable once written, so re-archiving the directory costs
# only the new day: everything else deduplicates against the previous archive.
#
# The local Parquet is removed once it is confirmed present in the archive,
# because 324 MB a day would fill this disk in a season. The manifest stays: it
# is kilobytes, and it is what tells the exporter which days are already done.
#
# That deletion is only safe because bars archives are NEVER pruned -- see the
# retention section. Once a local file is gone, the day exists solely in the
# archives that already contain it, and no later archive will list it again.
# `! -name '.*'` skips the exporter's own staging files. It writes each day
# under `.bars-<date>.parquet.partial` and renames it into place only once the
# row count matches, so a staging file is by definition an unfinished export --
# and it can vanish mid-archive when that rename happens. That is not
# hypothetical: it failed a deploy on 2026-09-08, when the backfill was still
# running as the backup started.
if [[ -d "$COLD_BARS_DIR" ]] \
    && bars_files=$(find "$COLD_BARS_DIR" -type f ! -name '.*' -print | sort) \
    && [[ -n "$bars_files" ]]; then
    if printf '%s' "$bars_files" | grep -q '[[:cntrl:]]'; then
        part_failed "a cold-bar path contains a control character; refusing to archive"
    else
        bars_archive="bars-$(date -u +%Y-%m-%dT%H:%M:%S)"
        log "bars: archiving $(printf '%s\n' "$bars_files" | wc -l) files as ${bars_archive}"
        if printf '%s\n' "$bars_files" \
            | borg create --compression zstd,3 --paths-from-stdin "::${bars_archive}"
        then
            if archived_bars=$(borg list --format '{path}{NL}' "::${bars_archive}"); then
                if [[ "$(printf '%s\n' "$archived_bars" | sort)" == "$bars_files" ]]; then
                    log "bars: contents match"
                    date -Iseconds > "$BARS_STAMP"
                    # Only the Parquet is reclaimed, and only what this archive
                    # was just verified to contain.
                    printf '%s\n' "$bars_files" | grep '\.parquet$' | while read -r file; do
                        rm -f "$file" && log "bars: reclaimed ${file}"
                    done
                else
                    drop_archive "$bars_archive"
                    part_failed "cold-bar archive contents differ from the list requested. Deleted."
                fi
            else
                drop_archive "$bars_archive"
                part_failed "could not list ${bars_archive} to verify it. Deleted."
            fi
        else
            drop_archive "$bars_archive"
            part_failed "borg create failed for ${bars_archive}"
        fi
    fi
else
    log "bars: nothing to archive"
fi

# --- retention --------------------------------------------------------------
#
# Separate globs, separate rules. Research inputs are excluded from the dump
# schedule on purpose: a report frozen a year ago is only reproducible while
# the candles it was built from still exist somewhere.
borg prune --glob-archives 'db-*' \
    --keep-daily 7 --keep-weekly 4 --keep-monthly 6 \
    || part_failed "borg prune failed for db-*"
borg prune --glob-archives 'research-*' \
    --keep-daily 7 --keep-weekly 8 --keep-monthly 24 \
    || part_failed "borg prune failed for research-*"

# bars-* is deliberately absent from the prune rules above, and must stay that
# way. The local Parquet is deleted once archived, so a given day lives only in
# the archives that already contained it; no later archive lists it again.
# Pruning this family by age would therefore delete data rather than delete a
# redundant copy of it. Archives are metadata and references -- what costs space
# is the chunks, and those must never be freed.
borg compact || log "Warning: borg compact failed; space will be reclaimed next run"

if [[ ${#FAILURES[@]} -gt 0 ]]; then
    summary=$(printf '%s; ' "${FAILURES[@]}")
    notify "Offsite backup FAILED: ${summary}"
    log "ERROR: ${#FAILURES[@]} part(s) failed: ${summary}" >&2
    exit 1
fi

log "done"
borg info | tail -6
