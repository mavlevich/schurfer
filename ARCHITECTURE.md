# Architecture

> High-level system design. Updated as decisions are made.

## Overview

Schurfer is a private multi-service trading platform. One product, several services.
The web UI is behind a login with no public exposure.

## Services

### Hot path (latency sensitive)

- **api-gateway** (Go). REST and websocket endpoints for the web UI. Reads from Redis
  (hot state) and Postgres (cold storage). Runs a background ticker that computes the
  signal score (`scoreSignals`, 5 components) and writes `signals:{base}` to Redis.
- **execution** (Python, FastAPI, ccxt). Reads pump scores from Redis with a
  freshness check, runs risk checks, places and closes orders on exchanges, monitors
  open positions for TP, SL, and max-hold, and exposes manual control endpoints.
  Public dry-run market clients cover the scanner's 17 venues, while account,
  position, and order paths receive only separately constructed authenticated
  clients.
- **collector** (Go). Streams Bybit websocket tickers (including bid and ask) and
  publishes them to NATS `market.bybit.ticker.*`. This is a prototype and is not
  wired in yet. Nothing subscribes and nothing persists the stream. The scanner uses
  ccxt polling, not this feed. It is intended as the seed of a future websocket data
  layer (see ROADMAP Phase 2). Kept, not deleted.

### Warm path (analytical and research)

- **analytics** (Python). One image with three focused entry points:
  - the long-running pump scanner polls exchange tickers via ccxt (not via NATS),
    computes pump episodes, persists OI and funding snapshots, and writes
    `pumps:latest` to Redis;
  - the long-running outcome resolver idempotently backfills forward prices, MAE, and
    MFE for recorded decisions from exchange OHLCV;
  - the on-demand, read-only measurement report aggregates dataset health, outcome
    coverage, and descriptive cohort results from Postgres.
- **notifier** (Go). Reads `pumps:latest` from Redis, sends Telegram alerts on new
  pump episodes, and records successful point-in-time deliveries in Postgres. A
  Postgres measurement failure is retried from an AOF-backed Redis outbox and never
  causes a duplicate Telegram alert.

### UI

- **web** (TypeScript, React, Vite). Dashboard, pump scanner, token detail, account
  view. Talks to api-gateway over REST and websockets.

## Data flow

```
Exchanges (REST tickers)
    |
Analytics (Python, ccxt polling)
    |
pumps:latest (Redis)
    +----> Notifier (Telegram + app.pump_alert_deliveries)
    |
    +----> api-gateway ticker (scoreSignals)
    |
signals:{base} (Redis)   score 0-10, verdict, computed_at, components
    |
Execution signal trader (freshness checked, DRY_RUN or AUTO_TRADE)
    |
Risk checks
    |
Exchanges (REST, ccxt)

Trade decisions (Postgres) <---- Outcome resolver (Python, exchange OHLCV)
    |
On-demand measurement report (Python, read-only)

# collector (Go) --> NATS market.bybit.ticker.*  : prototype, no consumer yet
```

## Redis key registry

| Key                                                  | Owner       | TTL     | Schema / purpose                                                        |
| ---------------------------------------------------- | ----------- | ------- | ----------------------------------------------------------------------- |
| `pumps:latest`                                       | analytics   | 300s    | `{ts, published_at_ms, count, pumps: [...]}`                            |
| `notifier:seen:{pump_event_id}`                      | notifier    | 30d     | `"1"`, de-dupes one threshold alert per durable pump episode            |
| `notifier:alert_delivery_outbox`                     | notifier    | none    | AOF-backed retry list for Postgres alert-delivery measurements          |
| `notifier:alert_delivery_dlq`                        | notifier    | none    | malformed alert-delivery measurements requiring inspection              |
| `signals:{base}`                                     | api-gateway | 120s    | `{score, verdict, computed_at, components}`                             |
| `trader:seen:{base}`                                 | execution   | 24h/30m | `"1"`, de-dupes signal handling                                         |
| `execution:signal_readiness`                         | execution   | 180s    | latest trader tick: pumps, evaluated, ready, deferred, reason counts    |
| `trading:enabled`                                    | execution   | no TTL  | `"true"/"false"`, kill switch                                           |
| `trading:daily_pnl`                                  | execution   | none    | float string (USD), monitoring cache                                    |
| `risk:pnl_ready`                                     | execution   | 120s    | `"1"`, positive lease. Absent or stale means trading is blocked         |
| `journal:pending_close:{exchange}:{base}:{trade_id}` | execution   | none    | durable retry marker: close confirmed on-exchange, journal write failed |
| `lock:order:{exchange}:{base}`                       | execution   | 30s     | `{owner}` UUID, distributed order lock                                  |
| `position:opened_at:{exchange}:{base}`               | execution   | 24h     | Unix timestamp string                                                   |
| `position:sl_order_id:{exchange}:{base}`             | execution   | none    | exchange SL order id, used by position reconciliation                   |
| `position:entry:{exchange}:{base}`                   | execution   | none    | entry price plus context                                                |
| `position:side:{exchange}:{base}`                    | execution   | none    | `"long"/"short"`                                                        |
| `trade:id:{exchange}:{base}`                         | execution   | none    | open trade id pointer, CAS-guarded on close                             |
| `position:paper:*`                                   | execution   | none    | paper and DRY_RUN position state                                        |

> The source of truth for accounting is Postgres (`app.trades`, `realized_pnl_today`),
> not Redis. The durable-daily-PnL work replaced the old ephemeral `daily_loss:{date}`
> and `pnl:{exchange}:{date}` keys. Redis holds hot state plus the `risk:pnl_ready`
> lease only.

## Storage

| Database        | Purpose                                                             |
| --------------- | ------------------------------------------------------------------- |
| **PostgreSQL**  | pump episodes, market snapshots, decisions, outcomes, users, trades |
| **TimescaleDB** | OHLCV series, tick data, funding history                            |
| **Redis**       | hot state (pump list, signal scores, locks, position metadata)      |

## Exchanges

Scanning is wider than trading. Data coverage does not equal tradeable venues.

- Scanner (data): 12 CEX perp markets by default. binance, bybit, okx, gate, bitget,
  mexc, kucoin, bingx, coinex, phemex, cryptocom, htx. This includes Binance. We scan
  it for data even though we cannot trade its perps from Poland.
- Execution (trading): only exchanges with both an API key and secret configured are
  activated at startup. Binance perps are not traded (blocked for Poland residents).

## Deployment

- Production: Hetzner Cloud (Nuremberg), Docker Compose prod stack behind Caddy.
- Dev: local Docker Compose.
- CI: GitHub Actions.
- Access: Tailscale only. SSH and the web UI both go over the tailnet. Caddy serves
  the Tailscale hostname with a static `tailscale cert`, and public ports 80 and 443
  are closed with ufw. Postgres is reachable through an SSH tunnel over Tailscale.

## Logging

All services use structured JSON logging.

- Python: `structlog`.
- Go: `slog` (stdlib).

## Tax and regulatory

Trades only own capital. No third-party funds and no investment-advice service. This
stays in the "personal trading" category. Confirm specifics with a professional before
any monetization.
