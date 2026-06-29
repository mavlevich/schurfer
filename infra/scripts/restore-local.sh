#!/usr/bin/env bash
# Download the latest prod backup and restore it into local dev PostgreSQL.
# Usage: PROD_HOST=user@1.2.3.4 bash infra/scripts/restore-local.sh
set -euo pipefail

PROD_HOST="${PROD_HOST:?Set PROD_HOST (e.g. deploy@your-server-ip)}"
REMOTE_DIR="${REMOTE_BACKUP_DIR:-/opt/schurfer/backups}"
LOCAL_PG_CONTAINER="${LOCAL_PG_CONTAINER:-schurfer-postgres}"
LOCAL_PG_USER="${LOCAL_PG_USER:-schurfer}"
LOCAL_DB="${LOCAL_DB:-schurfer}"

echo "-> Listing backups on $PROD_HOST..."
LATEST=$(ssh "$PROD_HOST" "ls -t ${REMOTE_DIR}/schurfer_*.dump.gz 2>/dev/null | head -1")

if [[ -z "$LATEST" ]]; then
    echo "ERROR: No backups found at ${PROD_HOST}:${REMOTE_DIR}"
    exit 1
fi

echo "-> Latest backup: $LATEST"
LOCAL_FILE="/tmp/$(basename "$LATEST")"

echo "-> Downloading..."
scp "${PROD_HOST}:${LATEST}" "$LOCAL_FILE"

echo "-> Restoring to local '$LOCAL_DB' (existing data will be replaced)..."
gunzip -c "$LOCAL_FILE" | docker exec -i "$LOCAL_PG_CONTAINER" pg_restore \
    -U "$LOCAL_PG_USER" -d "$LOCAL_DB" --clean --if-exists \
    --exit-on-error --single-transaction

rm "$LOCAL_FILE"
echo "-> Done. Local database now matches prod snapshot."
