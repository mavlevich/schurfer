#!/usr/bin/env bash
set -euo pipefail

# Companion to runtime-metrics.sh: same host-side loop + atomic-write
# pattern, for the one thing runtime-metrics.sh's own container stats/state
# don't cover -- disk usage broken down into reclaimable build artifacts
# (Docker images, build cache) versus real data (Postgres, backups). Must
# run on the host, not in a container: `docker system df` needs the host's
# own Docker socket, which api-gateway is deliberately never given (see
# apps/api-gateway/internal/health/system.go's own /proc + statfs approach
# instead of shelling out to docker).

output_path="${DISK_USAGE_PATH:-/opt/schurfer/runtime/disk-usage.snapshot}"
interval_seconds="${DISK_USAGE_INTERVAL_SECONDS:-300}"
backups_dir="${DISK_USAGE_BACKUPS_DIR:-/opt/schurfer/backups}"

mkdir -p "$(dirname "$output_path")"

while true; do
  tmp_path="$(mktemp "${output_path}.tmp.XXXXXX")"
  trap 'rm -f "$tmp_path"' EXIT

  # `docker system df -v`'s own per-volume sizes avoid needing `du` on
  # Docker's own volume backing paths directly: those are typically owned
  # by the container's own uid (e.g. postgres), not readable by the host
  # deploy user even with docker-group membership. The backups directory
  # IS deploy-owned, so `du` works fine there.
  backups_bytes="$(du -sb "$backups_dir" 2>/dev/null | cut -f1 || true)"

  {
    echo "SCHURFER_DISK_USAGE_V1"
    date +%s%3N
    echo "[docker_summary]"
    docker system df --format '{{json .}}' 2>/dev/null || true
    echo "[docker_volumes]"
    # Explicit trailing echo, not relying on the docker command's own
    # output ending in a newline: `docker system df -v --format
    # '{{json .Volumes}}'` prints a single JSON array with no guaranteed
    # trailing newline on every Docker version, which glued its own
    # closing `]` directly onto the next line's `[extra]` marker on this
    # host -- disk_usage.go's own reader fails closed on any structural
    # mismatch, so this silently zeroed out the whole disk_usage block on
    # the frontend with no visible error anywhere.
    docker system df -v --format '{{json .Volumes}}' 2>/dev/null || true
    echo
    echo "[extra]"
    echo "backups_bytes=${backups_bytes:-0}"
  } >"$tmp_path"

  chmod 0644 "$tmp_path"
  mv "$tmp_path" "$output_path"
  trap - EXIT
  sleep "$interval_seconds"
done
