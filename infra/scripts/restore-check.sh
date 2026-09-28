#!/usr/bin/env bash
# ENG-025 weekly restore drill. Restores the critical table set of the newest
# offsite db archive that has a receipt into a throwaway container and compares
# every row hash with the receipt (infra/scripts/restore_check.py check).
#
# Serialized with offsite-backup.sh through the same lock: both hold the Borg
# repository, and the drill must never read an archive that is being written.
set -euo pipefail

ENV_FILE="${OFFSITE_BACKUP_ENV:-/opt/schurfer/runtime/backup.env}"
STATE_DIR="${STATE_DIR:-/opt/schurfer/runtime}"
CONTAINER="${POSTGRES_CONTAINER:-schurfer-postgres}"
LOCK_WAIT_SECONDS="${LOCK_WAIT_SECONDS:-3600}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { echo "[$(date -Iseconds)] $*"; }

notify() {
    [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]] || return 0
    curl -sf --connect-timeout 10 --max-time 30 \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=$1" > /dev/null || log "Warning: Telegram notification failed"
}

[[ -r "$ENV_FILE" ]] || { notify "Restore drill FAILED: cannot read $ENV_FILE"; exit 1; }
set -a
# shellcheck source=/dev/null
. "$ENV_FILE"
set +a
: "${BORG_REPO:?BORG_REPO missing from $ENV_FILE}"

exec 200>"${STATE_DIR}/.offsite-backup.lock"
if ! flock -w "$LOCK_WAIT_SECONDS" 200; then
    notify "Restore drill FAILED: the offsite backup still holds the lock after ${LOCK_WAIT_SECONDS}s"
    exit 1
fi

# The drill runs the same image as production, by digest, so the restored rows
# render byte for byte as in the receipt.
image="$(docker inspect -f '{{.Config.Image}}' "$CONTAINER")"
log "restore drill: image ${image}"
if python3 "${SCRIPT_DIR}/restore_check.py" check --image "$image" --state-dir "$STATE_DIR"; then
    log "restore drill: passed"
else
    latest="$(ls -1t "${STATE_DIR}/restore-check/"*.json 2>/dev/null | head -1 || true)"
    reason="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("error","unknown"))' "$latest" 2>/dev/null || echo unknown)"
    notify "Restore drill FAILED: ${reason}"
    exit 1
fi
