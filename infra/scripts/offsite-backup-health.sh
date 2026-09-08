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
set -euo pipefail

STATE_DIR="${STATE_DIR:-/opt/schurfer/runtime}"
DB_STAMP="${STATE_DIR}/offsite-backup-db.stamp"
RESEARCH_STAMP="${STATE_DIR}/offsite-backup-research.stamp"

# 36 hours, not 24: a daily timer plus RandomizedDelaySec plus one skipped run
# during a long deploy is normal and must not page anyone. Two consecutive
# missed days is not normal.
MAX_AGE_HOURS="${MAX_AGE_HOURS:-36}"
# Below this, the disk stops being able to absorb a Storage Box outage.
MIN_FREE_GB="${MIN_FREE_GB:-15}"
DISK_PATH="${DISK_PATH:-/opt/schurfer}"

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

free_gb=$(( $(df -Pk "$DISK_PATH" | awk 'NR==2 {print $4}') / 1024 / 1024 ))
if [[ "$free_gb" -lt "$MIN_FREE_GB" ]]; then
    problems+=("only ${free_gb}GB free on ${DISK_PATH} (limit ${MIN_FREE_GB}GB)")
fi

if [[ ${#problems[@]} -eq 0 ]]; then
    echo "[$(date -Iseconds)] offsite backup healthy; ${free_gb}GB free"
    exit 0
fi

message="Offsite backup health: $(printf '%s; ' "${problems[@]}")"
echo "[$(date -Iseconds)] ${message}" >&2
if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
    curl -sf "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=${message}" \
        > /dev/null || echo "Warning: Telegram notification failed" >&2
fi
exit 1
