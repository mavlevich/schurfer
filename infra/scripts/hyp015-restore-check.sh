#!/usr/bin/env bash
# HYP-015 reader inputs: restore one verified snapshot set into a throwaway TimescaleDB
# container and run the restore check (docs/runbooks/hyp015-inputs-archive-design-v1.md).
#
# The ENG-025 pattern: the same pinned image as production, no volume, on the compose
# network so the analytics container can reach it, always removed at exit. The
# production database is only read (catalog and set rows); nothing is restored into it.
# No verdict is computed and no formal-read claim is opened.
#
# usage: hyp015-restore-check.sh SET_ID
set -euo pipefail

cd "$(dirname "$0")/../.."

set_id="${1:?usage: hyp015-restore-check.sh SET_ID}"
compose=(docker compose --env-file .env.prod -f infra/docker/docker-compose.prod.yml)
image="$(awk '/^[[:space:]]*image: timescale\/timescaledb@sha256:/ {print $2; exit}' \
    infra/docker/docker-compose.prod.yml)"
[[ -n "$image" ]] || { echo "pinned TimescaleDB image not found" >&2; exit 2; }
network="$(docker inspect schurfer-postgres \
    --format '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}}{{"\n"}}{{end}}' | head -1)"
[[ -n "$network" ]] || { echo "compose network of schurfer-postgres not found" >&2; exit 2; }

suffix="$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"
name="hyp015-restore-${suffix}"
password="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
container=""

cleanup() {
    result=$?
    trap - EXIT INT TERM
    if [[ -n "$container" ]]; then
        docker rm -f -v "$container" >/dev/null 2>&1 || true
    fi
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

container="$(docker run -d --rm --name "$name" --network "$network" \
    --label schurfer.restore=disposable \
    -e POSTGRES_USER=restore -e POSTGRES_PASSWORD="$password" -e POSTGRES_DB=restore \
    "$image")"
for _ in $(seq 1 60); do
    if docker exec "$container" pg_isready -q -U restore -d restore; then
        break
    fi
    sleep 1
done
docker exec "$container" pg_isready -q -U restore -d restore \
    || { echo "throwaway TimescaleDB did not become ready" >&2; exit 1; }

mkdir -p /opt/schurfer/runtime/history-archive/hyp015-restore
"${compose[@]}" run --rm --no-deps \
    -e RESTORE_DATABASE_URL="postgresql://restore:${password}@${name}:5432/restore" \
    -v /opt/schurfer/runtime/history-archive/hyp015-restore:/hyp015-restore \
    -v /opt/schurfer/runtime/backup.env:/backup.env:ro \
    -v /opt/schurfer/runtime/borg-home:/opt/schurfer/runtime/borg-home \
    -v /opt/schurfer/runtime/borg-passphrase:/opt/schurfer/runtime/borg-passphrase:ro \
    -v /opt/schurfer/runtime/storagebox_known_hosts:/opt/schurfer/runtime/storagebox_known_hosts:ro \
    -v /home/deploy/.ssh/schurfer_storagebox:/home/deploy/.ssh/schurfer_storagebox:ro \
    --entrypoint hyp015-inputs-archive analytics \
    restore-check --out-dir /hyp015-restore --backup-env /backup.env --set-id "$set_id"
