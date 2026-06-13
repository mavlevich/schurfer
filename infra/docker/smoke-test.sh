#!/usr/bin/env bash
# Smoke test for local dev environment.
# Run after `make dev` to verify all services are healthy.

set -euo pipefail

COMPOSE_FILE="infra/docker/docker-compose.dev.yml"
FAIL=0

check() {
  local name="$1"
  local cmd="$2"
  if eval "$cmd" > /dev/null 2>&1; then
    echo "  ok: $name"
  else
    echo "  FAIL: $name"
    FAIL=1
  fi
}

echo "Running smoke tests..."

# Check containers are running and healthy
check "postgres container healthy" \
  "docker compose -f $COMPOSE_FILE ps postgres --format json | grep -q '\"healthy\"'"

check "redis container healthy" \
  "docker compose -f $COMPOSE_FILE ps redis --format json | grep -q '\"healthy\"'"

check "nats container healthy" \
  "docker compose -f $COMPOSE_FILE ps nats --format json | grep -q '\"healthy\"'"

# Check service connectivity
check "postgres accepts connections" \
  "docker compose -f $COMPOSE_FILE exec -T postgres pg_isready -U schurfer"

check "redis responds to ping" \
  "docker compose -f $COMPOSE_FILE exec -T redis redis-cli ping"

check "nats http monitoring up" \
  "curl -sf http://localhost:8222/healthz"

# Check TimescaleDB extension
check "timescaledb extension enabled" \
  "docker compose -f $COMPOSE_FILE exec -T postgres psql -U schurfer -c 'SELECT extname FROM pg_extension WHERE extname = '\''timescaledb'\'' ;' | grep -q timescaledb"

# Check schemas created
check "app schema exists" \
  "docker compose -f $COMPOSE_FILE exec -T postgres psql -U schurfer -c '\dn' | grep -q app"

check "timeseries schema exists" \
  "docker compose -f $COMPOSE_FILE exec -T postgres psql -U schurfer -c '\dn' | grep -q timeseries"

# Check NATS JetStream
check "nats jetstream enabled" \
  "curl -sf http://localhost:8222/jsz | grep -q server_id"

# Check api-gateway
check "api-gateway liveness" \
  "curl -sf http://localhost:8000/healthz"

echo ""
if [ $FAIL -eq 0 ]; then
  echo "All checks passed."
else
  echo "Some checks failed."
  exit 1
fi
