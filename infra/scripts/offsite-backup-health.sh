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
#   Bar coverage   minute bars are the one dataset here that cannot be
#                  regenerated: Timescale drops them after 35 days and no
#                  exchange sells them back. The export runs nightly, and until
#                  now nothing watched whether it still did. A broken exporter
#                  starts a silent 35-day countdown, because the days it skips
#                  stay in the database and stay recoverable right up to the
#                  moment retention deletes them.
set -euo pipefail

STATE_DIR="${STATE_DIR:-/opt/schurfer/runtime}"
DB_STAMP="${STATE_DIR}/offsite-backup-db.stamp"
RESEARCH_STAMP="${STATE_DIR}/offsite-backup-research.stamp"
BARS_STAMP="${STATE_DIR}/offsite-backup-bars.stamp"
COLD_BARS_DIR="${COLD_BARS_DIR:-${STATE_DIR}/cold-bars}"
COLD_BARS_START_FILE="${COLD_BARS_START_FILE:-${COLD_BARS_DIR}/collection-start}"

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
# How far back a missing day still counts as a gap. Matches the retention
# interval in migration 0024: older than this the source is already gone, so a
# missing manifest there is history that was lost before this check existed and
# alerting on it every hour forever would teach everyone to ignore the alert.
RETENTION_DAYS="${RETENTION_DAYS:-35}"
# The freshest day the exporter is expected to have finished. It runs at 03:30
# UTC for the previous day, so two days of slack absorbs one missed run without
# crying about a day that is merely not exported yet.
EXPORT_LAG_DAYS="${EXPORT_LAG_DAYS:-2}"

problems=()

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

    cursor="$earliest_checked"
    while [[ ! "$cursor" > "$newest_expected" ]]; do
        if [[ ! -f "${COLD_BARS_DIR}/bars-${cursor}.manifest.json" ]]; then
            missing+=("$cursor")
        fi
        cursor=$(date -u -d "${cursor} +1 day" +%Y-%m-%d)
    done

    if [[ ${#missing[@]} -gt 0 ]]; then
        problems+=("${#missing[@]} cold bar day(s) inside the ${RETENTION_DAYS}-day retention window have no export: $(printf '%s ' "${missing[@]}")")
    fi
}

check_bar_coverage

free_gb=$(( $(df -Pk "$DISK_PATH" | awk 'NR==2 {print $4}') / 1024 / 1024 ))
if [[ "$free_gb" -lt "$MIN_FREE_GB" ]]; then
    problems+=("only ${free_gb}GB free on ${DISK_PATH} (limit ${MIN_FREE_GB}GB)")
fi

if [[ ${#problems[@]} -eq 0 ]]; then
    exported_days=$(find "$COLD_BARS_DIR" -maxdepth 1 -name 'bars-*.manifest.json' | wc -l | tr -d ' ')
    echo "[$(date -Iseconds)] offsite backup healthy; ${free_gb}GB free; ${exported_days} bar days exported"
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
