# Architecture

> High-level system design. Updated as decisions are made.

## Overview

Schurfer is a private monolithic-ish multi-service trading platform.
One product, multiple services. Web UI behind login, no public exposure.

## Services

### Hot path (latency-sensitive)

- **collector** (Go) — one process per exchange.
  Maintains WebSocket subscriptions for spot+perp markets. Normalizes
  events and publishes to NATS bus.
- **api-gateway** (Go) — REST + WS endpoints for web UI. Reads from
  Redis (hot state) and Postgres (cold storage). Runs background ticker
  that writes signal scores (`signals:{base}`) to Redis every 60s.
- **execution** (Python, FastAPI + ccxt) — reads pump scores from Redis,
  runs risk checks, places/closes orders on exchanges, monitors open
  positions for TP/SL/max-hold, exposes manual control endpoints.

### Warm path (analytical)

- **analytics** (Python) — pump scanner. Subscribes to NATS, computes
  pump episodes, persists OI/funding snapshots, writes `pumps:latest`
  to Redis.
- **notifier** (Go) — reads `pumps:latest` from Redis, sends Telegram
  alerts on new pump detection.

### UI

- **web** (TypeScript + React + Vite) — dashboard, pump scanner, token
  detail, account view. Communicates with api-gateway via REST + WS.

## Data flow

```
Exchanges (WS)
    |
Collector (Go) --> NATS --> Analytics (Python)
                                    |
                              pumps:latest (Redis)
                                    |
                            api-gateway ticker
                                    |
                           signals:{base} (Redis)   <-- score 0-10, computed_at
                                    |
                          Execution signal trader
                          (when AUTO_TRADE=true)
                                    |
                              Risk checks
                                    |
                            Exchanges (REST/ccxt)
```

## Redis key registry

| Key                                    | Owner       | TTL     | Schema                          |
| -------------------------------------- | ----------- | ------- | ------------------------------- |
| `pumps:latest`                         | analytics   | 300s    | `{ts, count, pumps: [...]}`     |
| `signals:{base}`                       | api-gateway | 120s    | `{score, verdict, computed_at}` |
| `trader:seen:{base}`                   | execution   | 24h/30m | `"1"`                           |
| `trading:enabled`                      | execution   | no TTL  | `"true"/"false"`                |
| `lock:order:{exchange}:{base}`         | execution   | 30s     | `{owner}`                       |
| `position:opened_at:{exchange}:{base}` | execution   | 24h     | Unix timestamp string           |
| `daily_loss:{date}`                    | execution   | 48h     | float string (USD)              |
| `pnl:{exchange}:{date}`                | execution   | 48h     | float string (USD)              |

## Storage

| Database        | Purpose                                                        |
| --------------- | -------------------------------------------------------------- |
| **PostgreSQL**  | pump episodes, OI snapshots, funding snapshots, users          |
| **TimescaleDB** | OHLCV series, tick data, funding history                       |
| **Redis**       | hot state (pump list, signal scores, locks, position metadata) |

## Exchanges

Execution service supports: Binance, Bybit, OKX, Gate, KuCoin, BingX, MEXC.
Only exchanges with both API key + secret configured are activated at startup.

Binance perps excluded from pump scanner (blocked in Poland).

## Deployment

- **Production**: AWS EC2 t4g.medium (ARM/Graviton) in Frankfurt (eu-central-1), Docker Compose
- **Dev**: local Docker Compose
- **CI**: GitHub Actions
- **Access**: Tailscale VPN for SSH, Cloudflare Tunnel for web UI
- **DNS**: Cloudflare

## Logging

All services use structured JSON logging:

- Python: `structlog`
- Go: `slog` (stdlib)

## Tax / regulatory

Trades only own capital. No third-party funds, no investment advice
service. Stays within "personal trading" category, no CASP licensing
required.
