#!/usr/bin/env bash
set -euo pipefail

output_path="${RUNTIME_METRICS_PATH:-/opt/schurfer/runtime/container-metrics.snapshot}"
interval_seconds="${RUNTIME_METRICS_INTERVAL_SECONDS:-10}"
docker_config="${DOCKER_CONFIG:-/tmp/schurfer-runtime-docker-config}"

mkdir -p "$(dirname "$output_path")"
mkdir -p "$docker_config"
chmod 0700 "$docker_config"
export DOCKER_CONFIG="$docker_config"

while true; do
  tmp_path="$(mktemp "${output_path}.tmp.XXXXXX")"
  trap 'rm -f "$tmp_path"' EXIT

  mapfile -t container_ids < <(docker ps -aq --filter 'name=schurfer-')
  mapfile -t running_ids < <(docker ps -q --filter 'name=schurfer-')
  {
    echo "SCHURFER_RUNTIME_METRICS_V1"
    date +%s%3N
    echo "[stats]"
    if ((${#running_ids[@]} > 0)); then
      docker stats --no-stream --format '{{json .}}' "${running_ids[@]}"
    fi
    echo "[states]"
    if ((${#container_ids[@]} > 0)); then
      docker inspect --format \
        '{"name":{{json .Name}},"restart_count":{{.RestartCount}},"status":{{json .State.Status}},"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}"none"{{end}},"started_at":{{json .State.StartedAt}}}' \
        "${container_ids[@]}"
    fi
  } >"$tmp_path"

  chmod 0644 "$tmp_path"
  mv "$tmp_path" "$output_path"
  trap - EXIT
  sleep "$interval_seconds"
done
