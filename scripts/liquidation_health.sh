#!/usr/bin/env bash
set -e

EXCHANGE=$1
COMPOSE_FILE="infra/docker/docker-compose.prod.yml"

echo "=== $EXCHANGE Container Status ==="
docker ps --filter "name=liquidation-capture-$EXCHANGE" --format "table {{.Names}}\t{{.Status}}\t{{.State}}\t{{.RunningFor}}"
echo ""

echo "=== Redis Operational State ==="
docker-compose -f $COMPOSE_FILE exec -T redis redis-cli HGETALL market:liquidationcapture:health:$EXCHANGE | awk 'NR%2{printf "%-35s: ",$0;next;}1'
echo ""

echo "=== Container Stats ==="
docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}\t{{.BlockIO}}" liquidation-capture-$EXCHANGE || true
echo ""

# We can query Postgres for gaps and storage size if we can reach it
echo "=== PostgreSQL Stats ==="
docker-compose --env-file infra/docker/.env.prod -f $COMPOSE_FILE exec -T postgres psql -U schurfer -d schurfer -c "
SELECT
    pg_size_pretty(pg_total_relation_size('liquidation_capture_heartbeat')) AS heartbeat_size,
    pg_size_pretty(pg_total_relation_size('liquidation_capture_event')) AS events_size;
" || echo "Postgres query failed"
echo ""
