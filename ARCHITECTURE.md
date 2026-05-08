# Architecture

> High-level system design. Updated as decisions are made.

## Overview

Schurfer is a private monolithic-ish multi-service trading platform.
One product, multiple services. Web UI behind login, no public exposure.

## Services

### Hot path (latency-sensitive)

- **collectors** (Go) - one process per exchange. Maintains WebSocket
  subscriptions for spot+perp markets. Normalizes events and publishes
  to NATS bus.
- **execution** (Go) - receives signals from analytics, runs them through
  risk manager, places orders on exchanges, tracks positions.
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
    ↓
Collectors (Go) ──→ NATS ──→ Analytics (Python)
                       │            │
                       ↓            ↓
                  Storage      Signals
                  (Postgres,        ↓
                   TimescaleDB,  Decision Engine
                   Redis)            ↓
                       ↑         Risk Manager
                       │            ↓
                       └──── Execution (Go)
                                    ↓
                                Exchanges (REST)
```

## Storage

| Database | Purpose |
|---|---|
| **PostgreSQL** | orders, positions, journal, configs, users |
| **TimescaleDB** | tick data, OHLCV, funding history, OI series |
| **Redis** | hot state (current price, OI, funding), pub/sub for UI |

## Deployment

- **Production**: single Hetzner CCX23 VPS in Tokyo (close to exchanges)
- **Dev**: local Docker Compose
- **Secrets**: sops + age, encrypted in repo
- **Access**: Tailscale VPN to production, Cloudflare Tunnel for web UI

## Tax / regulatory

Trades only own capital. No third-party funds, no investment advice
service. Stays within "personal trading" category, no CASP licensing
required.
