#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."

if (( $# == 0 )); then
    echo "usage: verify_isolated_db.sh COMMAND [ARGS...]" >&2
    exit 2
fi
command -v docker >/dev/null || { echo "Docker is required for make verify" >&2; exit 2; }
docker info >/dev/null || { echo "Docker is not running" >&2; exit 2; }

# A SIGKILL cannot run the EXIT trap. Reap only old verify containers so
# concurrent fresh runs in other worktrees keep their own databases.
while IFS= read -r stale_id; do
    [[ -n "$stale_id" ]] || continue
    created="$(docker inspect --format '{{.Created}}' "$stale_id" 2>/dev/null)" || continue
    if python3 - "$created" <<'PY'
import sys
from datetime import datetime, timedelta, timezone

created_at = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
raise SystemExit(0 if datetime.now(timezone.utc) - created_at > timedelta(hours=6) else 1)
PY
    then
        docker rm -f -v "$stale_id" >/dev/null 2>&1 || true
    fi
done < <(docker ps -aq --filter label=schurfer.verify=disposable)

image="$(awk '/^[[:space:]]*image: timescale\/timescaledb@sha256:/ {print $2; exit}' infra/docker/docker-compose.dev.yml)"
if [[ -z "$image" ]]; then
    echo "Pinned TimescaleDB image is missing from docker-compose.dev.yml" >&2
    exit 2
fi

suffix="$(python3 -c 'import secrets; print(secrets.token_hex(6))')"
db_name="schurfer_verify_${suffix}"
container_name="schurfer-verify-${suffix}"
container_id=""

cleanup() {
    result=$?
    trap - EXIT INT TERM
    if [[ -n "$container_id" ]]; then
        docker rm -f -v "$container_id" >/dev/null 2>&1 || true
    fi
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

container_id="$(docker run -d --rm \
    --name "$container_name" \
    --label schurfer.verify=disposable \
    -e POSTGRES_USER=schurfer \
    -e POSTGRES_PASSWORD=schurfer_dev \
    -e "POSTGRES_DB=$db_name" \
    -p 127.0.0.1::5432 \
    "$image")"

ready=0
for _ in $(seq 1 60); do
    if docker exec "$container_id" pg_isready -q -h 127.0.0.1 -U schurfer -d "$db_name"; then
        ready=1
        break
    fi
    sleep 1
done
if [[ "$ready" != 1 ]]; then
    echo "Disposable TimescaleDB did not become ready" >&2
    exit 1
fi

port_mapping="$(docker port "$container_id" 5432/tcp | sed -n '/^127\.0\.0\.1:[0-9][0-9]*$/p' | head -1)"
if [[ -z "$port_mapping" ]]; then
    echo "Disposable TimescaleDB did not bind to IPv4 loopback" >&2
    exit 1
fi
port="${port_mapping##*:}"
export SCHURFER_TEST_DATABASE_URL="postgresql://schurfer:schurfer_dev@127.0.0.1:${port}/${db_name}"
export DATABASE_URL="$SCHURFER_TEST_DATABASE_URL"
export REQUIRE_INTEGRATION_DB=1

docker exec -i "$container_id" psql -X -q -v ON_ERROR_STOP=1 \
    -U schurfer -d "$db_name" < infra/docker/init-db.sql
uv run --package schurfer-journal alembic -c packages/journal/alembic.ini upgrade head

"$@"
