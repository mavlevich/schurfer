#!/usr/bin/env bash
# Run a command while holding the cold-bars lock.
#
# The offsite backup ARCHIVES and then RECLAIMS (deletes) the cold-bar Parquet
# files, while the exporter and the fingerprint backfill WRITE them. If those
# overlap, a reclaim can delete a day out from under an export that is mid-write
# (a spurious per-day failure at best). This wrapper takes a lock the offsite
# backup also takes around its cold-bar section, so the two never run at once.
#
# The lock file is shared between root (the backup) and deploy (the exporter), so
# it is created mode 0666 -- it carries no data, only the flock. Fail-closed: if
# the lock cannot be taken within the wait, the command does NOT run. A skipped
# export is retried next run; one racing a reclaim is not.
set -euo pipefail

LOCK_FILE="${COLD_BARS_LOCK_FILE:-/opt/schurfer/runtime/.cold-bars.lock}"
LOCK_WAIT_SECONDS="${COLD_BARS_LOCK_WAIT_SECONDS:-600}"

if [[ "$#" -eq 0 ]]; then
    echo "with-cold-bars-lock: no command given" >&2
    exit 64
fi

if [[ ! -e "$LOCK_FILE" ]]; then
    # umask 0 so the file is 0666 and BOTH the root backup and the deploy exporter
    # can open it for locking, whichever creates it first.
    (umask 0 && : > "$LOCK_FILE") 2>/dev/null \
        || { echo "with-cold-bars-lock: cannot create $LOCK_FILE" >&2; exit 75; }
fi

exec 9>"$LOCK_FILE" || { echo "with-cold-bars-lock: cannot open $LOCK_FILE" >&2; exit 75; }
if ! flock -w "$LOCK_WAIT_SECONDS" 9; then
    echo "with-cold-bars-lock: offsite backup or another cold-bar job holds" \
        "$LOCK_FILE (waited ${LOCK_WAIT_SECONDS}s); not running" >&2
    exit 75
fi

# exec so the command inherits fd 9 and holds the lock until it exits, and its
# exit status is this script's status.
exec "$@"
