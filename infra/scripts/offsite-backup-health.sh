#!/usr/bin/env bash
# Alert when the last SUCCESSFUL offsite archive is too old, or when the disk
# runway is short enough that the "never delete unconfirmed data" rule is about
# to collide with a finite disk.
#
# Two signals, deliberately not one:
#
#   Archive age    watches the stamp files offsite-backup.sh writes only after
#                  an archive actually succeeded -- not the timer's last run,
#                  not the unit's exit status. This repository has already
#                  shipped a unit that fired punctually and did nothing for
#                  months (schurfer-docker-prune, broken by a permissions
#                  error). A schedule that runs is not evidence of a backup.
#
#   Disk runway    with a finite disk you cannot simultaneously keep
#                  collecting, tolerate the Storage Box being unreachable, and
#                  refuse to delete unconfirmed data. Something has to give,
#                  and it should give with warning rather than at 100% full.
#
#   Bar coverage   captured flow features cannot be replaced by OHLCV. Automatic
#                  35-day deletion was removed in migration 0050; gated deletion
#                  now requires verified offsite evidence. Missing exports still
#                  need attention. An operator-declared collection-end closes the
#                  expected interval after a deliberate capture stop, while its
#                  historical manifests and all backup freshness checks remain.
set -euo pipefail

STATE_DIR="${STATE_DIR:-/opt/schurfer/runtime}"
DB_STAMP="${STATE_DIR}/offsite-backup-db.stamp"
RESEARCH_STAMP="${STATE_DIR}/offsite-backup-research.stamp"
BARS_STAMP="${STATE_DIR}/offsite-backup-bars.stamp"
COLD_BARS_DIR="${COLD_BARS_DIR:-${STATE_DIR}/cold-bars}"
COLD_BARS_START_FILE="${COLD_BARS_START_FILE:-${COLD_BARS_DIR}/collection-start}"
COLD_BARS_END_FILE="${COLD_BARS_END_FILE:-${COLD_BARS_DIR}/collection-end}"

# 36 hours, not 24. The 12-hour margin covers ordinary jitter: the timer's
# RandomizedDelaySec, a run that started late because a deploy held the lock,
# a run that took longer than usual. It deliberately does NOT tolerate a fully
# missed daily run -- that leaves roughly 48 hours between successful archives
# and should alert. An earlier version of this comment claimed the opposite,
# which was arithmetic nobody had done.
MAX_AGE_HOURS="${MAX_AGE_HOURS:-36}"
# Below this, the disk stops being able to absorb a Storage Box outage.
MIN_FREE_GB="${MIN_FREE_GB:-15}"
DISK_PATH="${DISK_PATH:-/opt/schurfer}"
# Existing rolling coverage horizon for an active collector, not a claim about
# the deployed deletion policy. A closed collection checks its full fixed span.
RETENTION_DAYS="${RETENTION_DAYS:-35}"
# The freshest day the exporter is expected to have finished. It runs at 03:30
# UTC for the previous day, so two days of slack absorbs one missed run without
# crying about a day that is merely not exported yet.
EXPORT_LAG_DAYS="${EXPORT_LAG_DAYS:-2}"

problems=()
collection_note=""

check_stamp() {
    local label="$1" file="$2"
    if [[ ! -f "$file" ]]; then
        problems+=("no successful ${label} archive has ever been recorded (${file} missing)")
        return
    fi
    local age_hours
    age_hours=$(( ( $(date +%s) - $(date -r "$file" +%s) ) / 3600 ))
    if [[ "$age_hours" -gt "$MAX_AGE_HOURS" ]]; then
        problems+=("last successful ${label} archive is ${age_hours}h old (limit ${MAX_AGE_HOURS}h)")
    fi
}

check_stamp "database" "$DB_STAMP"
check_stamp "research" "$RESEARCH_STAMP"
check_stamp "cold bars" "$BARS_STAMP"

# The weekly restore drill (ENG-025), once its timer is installed: a drill that
# stopped passing, or stopped running, is as much a backup failure as a missing
# archive. Eight days allows one missed Sunday run to be retried.
RESTORE_TIMER="${RESTORE_TIMER:-/etc/systemd/system/schurfer-restore-check.timer}"
RESTORE_STAMP="${STATE_DIR}/restore-check.stamp"
RESTORE_MAX_AGE_HOURS="${RESTORE_MAX_AGE_HOURS:-192}"
if [[ -f "$RESTORE_TIMER" ]]; then
    if [[ ! -f "$RESTORE_STAMP" ]]; then
        problems+=("the restore drill has never passed (${RESTORE_STAMP} missing)")
    else
        restore_age=$(( ( $(date +%s) - $(date -r "$RESTORE_STAMP" +%s) ) / 3600 ))
        if [[ "$restore_age" -gt "$RESTORE_MAX_AGE_HOURS" ]]; then
            problems+=("the last passing restore drill is ${restore_age}h old (limit ${RESTORE_MAX_AGE_HOURS}h)")
        fi
    fi
fi

# Bar coverage, from the manifests rather than the Parquet files. The backup
# reclaims each `.parquet` once it is confirmed inside a `bars-*` archive and
# leaves the `.manifest.json` beside it, so the manifests are a permanent local
# index of which days were exported -- available without the repository
# passphrase, without reaching the Storage Box, and without reading 8 GB of
# Parquet to answer a question about filenames.
check_bar_coverage() {
    if [[ ! -d "$COLD_BARS_DIR" ]]; then
        problems+=("cold bar directory ${COLD_BARS_DIR} does not exist")
        return
    fi
    local days=() path day
    for path in "$COLD_BARS_DIR"/bars-*.manifest.json; do
        [[ -e "$path" ]] || continue
        day="${path##*/bars-}"
        days+=("${day%.manifest.json}")
    done
    if [[ ${#days[@]} -eq 0 ]]; then
        problems+=("no cold bar day has ever been exported to ${COLD_BARS_DIR}")
        return
    fi

    # Where expected coverage begins, read rather than derived. Deriving it from
    # the oldest surviving manifest cannot work: delete the five oldest and the
    # expected range shrinks to match, so a real loss reports healthy. A
    # colleague reproduced exactly that. The exporter records this once, on its
    # first run, from the oldest day the source then held.
    local collection_start
    if [[ -n "${COLLECTION_START:-}" ]]; then
        collection_start="$COLLECTION_START"
    elif [[ -f "$COLD_BARS_START_FILE" ]]; then
        collection_start=$(tr -d '[:space:]' < "$COLD_BARS_START_FILE")
    else
        # Failing closed on purpose. Without this the check has no idea what it
        # is supposed to have, and silence would read as health.
        problems+=("no collection start recorded (${COLD_BARS_START_FILE} missing); \
cold bar coverage cannot be checked")
        return
    fi
    if [[ ! "$collection_start" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
        problems+=("collection start ${collection_start} is not a YYYY-MM-DD date")
        return
    fi

    local newest_expected earliest_checked cursor missing=()
    newest_expected=$(date -u -d "${EXPORT_LAG_DAYS} days ago" +%Y-%m-%d)
    earliest_checked=$(date -u -d "${RETENTION_DAYS} days ago" +%Y-%m-%d)
    # max(collection start, retention edge). Days before collection began never
    # existed; days past the retention edge are gone from the source and cannot
    # be recovered, so alerting on them hourly forever teaches everyone to
    # ignore the alert.
    [[ "$collection_start" > "$earliest_checked" ]] && earliest_checked="$collection_start"

    if [[ -e "$COLD_BARS_END_FILE" || -L "$COLD_BARS_END_FILE" ]]; then
        local collection_end normalized_end today
        if [[ ! -f "$COLD_BARS_END_FILE" || ! -r "$COLD_BARS_END_FILE" ]]; then
            problems+=("collection end is not a readable file (${COLD_BARS_END_FILE})")
            return
        fi
        collection_end=$(sed 's/^[[:space:]]*//; s/[[:space:]]*$//' "$COLD_BARS_END_FILE")
        if [[ ! "$collection_end" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
            problems+=("collection end is not a YYYY-MM-DD date")
            return
        fi
        normalized_end=$(date -u -d "$collection_end" +%Y-%m-%d 2>/dev/null) || normalized_end=""
        today=$(date -u +%Y-%m-%d)
        if [[ "$normalized_end" != "$collection_end" || ! "$collection_end" < "$today" ]]; then
            problems+=("collection end must name a valid past UTC day")
            return
        fi
        if [[ "$collection_end" < "$collection_start" ]]; then
            problems+=("collection end is before collection start")
            return
        fi
        # Closure must never age an existing missing manifest out of monitoring.
        # The declared final day is exportable already; do not apply active lag.
        earliest_checked="$collection_start"
        newest_expected="$collection_end"
        collection_note="; collection closed through ${collection_end}"
    fi

    cursor="$earliest_checked"
    while [[ ! "$cursor" > "$newest_expected" ]]; do
        if [[ ! -f "${COLD_BARS_DIR}/bars-${cursor}.manifest.json" ]]; then
            missing+=("$cursor")
        fi
        cursor=$(date -u -d "${cursor} +1 day" +%Y-%m-%d)
    done

    if [[ ${#missing[@]} -gt 0 ]]; then
        if [[ -n "$collection_note" ]]; then
            problems+=("${#missing[@]} cold bar day(s) in the closed collection have no export: $(printf '%s ' "${missing[@]}")")
        else
            problems+=("${#missing[@]} cold bar day(s) inside the ${RETENTION_DAYS}-day coverage horizon have no export: $(printf '%s ' "${missing[@]}")")
        fi
    fi
}

check_bar_coverage

free_gb=$(( $(df -Pk "$DISK_PATH" | awk 'NR==2 {print $4}') / 1024 / 1024 ))
if [[ "$free_gb" -lt "$MIN_FREE_GB" ]]; then
    problems+=("only ${free_gb}GB free on ${DISK_PATH} (limit ${MIN_FREE_GB}GB)")
fi

if [[ ${#problems[@]} -eq 0 ]]; then
    exported_days=$(find "$COLD_BARS_DIR" -maxdepth 1 -name 'bars-*.manifest.json' | wc -l | tr -d ' ')
    echo "[$(date -Iseconds)] offsite backup healthy; ${free_gb}GB free; ${exported_days} bar days exported${collection_note}"
    exit 0
fi

message="Offsite backup health: $(printf '%s; ' "${problems[@]}")"
echo "[$(date -Iseconds)] ${message}" >&2
if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
    # Bounded on purpose. An unbounded curl on a hung connection keeps this
    # oneshot unit running, and systemd skips every later firing of the timer
    # while the previous run is still active -- so the notification that
    # something is wrong would be the thing that stops anyone finding out.
    curl -sf --connect-timeout "${CURL_CONNECT_TIMEOUT:-10}" \
        --max-time "${CURL_MAX_TIME:-30}" \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=${message}" \
        > /dev/null || echo "Warning: Telegram notification failed" >&2
fi
exit 1
