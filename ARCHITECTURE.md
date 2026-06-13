# Architecture

> High-level system design. Updated as decisions are made.

## Overview

Schurfer is a private monolithic-ish multi-service trading platform.
One product, multiple services. Web UI behind login, no public exposure.

## Services

### Hot path (latency-sensitive)

- **collectors** (Go) - one process per exchange.
  Maintains WebSocket subscriptions for spot+perp markets. Normalizes
  events and publishes to NATS bus.
- **execution** (Go) - receives signals from
  analytics, runs them through risk manager, places orders on exchanges,
  tracks positions.
- **api-gateway** (Go) - REST + WS endpoints for web UI. Reads from
  Redis (hot state) and Postgres (cold storage).

### Warm path (analytical)

- **analytics** (Python) - signal generation, backtests, news pipeline.
  Subscribes to NATS, computes indicators, publishes signals back.
- **telegram-bot** (Python) - sends alerts to user's Telegram, accepts
  approve/skip actions on suggested trades.

### UI

- **web** (TypeScript + React + Vite) - dashboard, journal, analytics
  views, configuration. Communicates with api-gateway via REST + WS.

## Data flow

```
Exchanges (WS)
    |
Collectors (Go) --> NATS --> Analytics (Python)
                       |            |
                       v            v
                  Storage      Signals
                  (Postgres,        |
                   TimescaleDB,  Decision Engine
                   Redis)            |
                       ^         Risk Manager
                       |            |
                       +---- Execution (Go)
                                          |
                                      Exchanges (REST)
```

## Storage

| Database        | Purpose                                                |
| --------------- | ------------------------------------------------------ |
| **PostgreSQL**  | orders, positions, journal, configs, users             |
| **TimescaleDB** | tick data, OHLCV, funding history, OI series           |
| **Redis**       | hot state (current price, OI, funding), pub/sub for UI |

## Exchanges

| Exchange        | Priority | Status     |
| --------------- | -------- | ---------- |
| **Bybit**       | First    | Sprint 2   |
| **OKX**         | Second   | Sprint 4   |
| **Hyperliquid** | Third    | Sprint 4-6 |

Binance perps excluded (blocked in Poland).

## Deployment

- **Production**: AWS EC2 t4g.medium (ARM/Graviton) in Frankfurt (eu-central-1)
  - Docker Compose initially, migrate to ECS in Sprint 5
  - ARM images for all services (Go and Python work natively)
- **Dev**: local Docker Compose (same compose file, different config)
- **CI**: GitHub Actions self-hosted runner on AWS spot instance
- **Secrets**: sops + age, encrypted in repo
- **Access**: Tailscale VPN for SSH, Cloudflare Tunnel for web UI
- **DNS**: Cloudflare

## Logging

All services use structured JSON logging from day one:

- Python: `structlog`
- Go: `slog` (stdlib)
- Collected via CloudWatch (production) or stdout (dev)

## Tax / regulatory

Trades only own capital. No third-party funds, no investment advice
service. Stays within "personal trading" category, no CASP licensing
required.
