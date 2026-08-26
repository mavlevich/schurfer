#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <bybit|binance>" >&2
    exit 2
fi
if [[ "$1" != "bybit" && "$1" != "binance" ]]; then
    echo "Usage: $0 <bybit|binance>" >&2
    exit 2
fi
if [[ ! -f .env.prod ]]; then
    echo "ERROR: .env.prod not found; run from the repository root." >&2
    exit 1
fi

EXCHANGE="$1"
SERVICE="liquidation-capture-$EXCHANGE"
CONTAINER="schurfer-$SERVICE"
COMPOSE=(docker compose --env-file .env.prod -f infra/docker/docker-compose.prod.yml)

echo "=== $EXCHANGE Container Status ==="
"${COMPOSE[@]}" --profile "$SERVICE" ps "$SERVICE"
echo ""

echo "=== Redis Operational State ==="
"${COMPOSE[@]}" exec -T redis redis-cli --raw HGETALL "market:liquidationcapture:health:$EXCHANGE" \
    | awk 'NR%2{printf "%-35s: ",$0;next;}1'
echo ""

echo "=== Container Stats ==="
docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}\t{{.BlockIO}}" "$CONTAINER"
echo ""

echo "=== PostgreSQL Stats ==="
"${COMPOSE[@]}" exec -T postgres psql -v ON_ERROR_STOP=1 -U schurfer -d schurfer -c "
SELECT
    pg_size_pretty(pg_total_relation_size('timeseries.liquidation_capture_heartbeats_1m')) AS heartbeat_size,
    pg_size_pretty(pg_total_relation_size('timeseries.liquidation_events')) AS events_size;
SELECT bucket_start, complete, data_loss_detected,
       connected_connections, expected_connections
FROM timeseries.liquidation_capture_heartbeats_1m
WHERE exchange = '$EXCHANGE'
ORDER BY bucket_start DESC
LIMIT 5;
"
echo ""
