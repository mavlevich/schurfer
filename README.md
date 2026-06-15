# Schurfer

> Private crypto trading platform: pump scanner, signal analytics, automated execution.

## Status

Sprint 3 complete. Multi-exchange pump scanner live with history, Telegram alerts, and per-token charts.

## What it does

- Scans 12 CEX perpetual markets every 60 seconds for price pumps above a configurable threshold
- Persists pump episodes with peak %, retrace %, and timeline snapshots (+1h/+4h/+24h)
- Tracks multi-episode lifecycle: cooling period (3 missed scans) before closing an episode
- Token detail page: OHLCV chart (5m/15m/1h/4h), exchange breakdown, episode history
- Telegram alerts on new pump detection, deduplication across a 24h window

## Stack

| Layer              | Technology                                                |
| ------------------ | --------------------------------------------------------- |
| Analytics scanner  | Python 3.13, ccxt, psycopg3, redis-py, structlog          |
| API gateway        | Go 1.26, chi, pgx, go-redis                               |
| Bybit WS collector | Go 1.26, NATS                                             |
| Telegram notifier  | Go 1.26                                                   |
| Frontend           | React 19, Vite, TypeScript, shadcn/ui, lightweight-charts |
| Storage            | PostgreSQL 17 + TimescaleDB, Redis 7                      |
| Message bus        | NATS 2 with JetStream                                     |
| Infra              | Docker Compose (dev), AWS EC2 Frankfurt (prod, planned)   |

## Project structure

```
apps/
├── analytics/       Python  - pump scanner, persistence, snapshots
├── api-gateway/     Go      - REST API, OHLCV proxy, pump history
├── collector/       Go      - Bybit WebSocket → NATS publisher
├── notifier/        Go      - Telegram bot, reads Redis pumps:latest
├── web/             TS      - React dashboard (/pumps, /pumps/:base)
├── collectors/              - multi-exchange WS stubs (Sprint 5)
└── execution/               - order execution stub (Sprint 6)

packages/
└── journal/         Python  - SQLAlchemy models, Alembic migrations

infra/
└── docker/
    ├── docker-compose.dev.yml
    └── init-db.sql

docs/
├── adr/             architecture decision records
├── strategies/      strategy specs
└── runbooks/        operational procedures
```

## Quick start

### Prerequisites

- Docker + Docker Compose
- Python 3.13 + [uv](https://docs.astral.sh/uv/)
- Node 22 + [pnpm](https://pnpm.io/)
- Go 1.26

### 1. Install dependencies

```bash
make install
```

### 2. Configure environment

```bash
make dev-init          # generates .env with hashed admin password
```

Add optional variables to `.env`:

```env
# Telegram alerts (optional)
TELEGRAM_BOT_TOKEN=<your bot token>
TELEGRAM_CHAT_ID=<your chat or channel id>

# Scanner tuning (optional, defaults shown)
PUMP_MIN_PCT=30
SCAN_INTERVAL=60
PUMP_EXCHANGES=binance,bybit,okx,gate,bitget,mexc,bingx,coinex,phemex,cryptocom,htx,kucoin
```

### 3. Start infrastructure

```bash
make dev               # starts postgres, redis, nats, api-gateway, analytics, collector, notifier
```

### 4. Run database migrations

```bash
make migrate
```

### 5. Start the frontend

```bash
cd apps/web && pnpm dev
```

Open [http://localhost:5173](http://localhost:5173) — login with `admin` / the password set during `dev-init`.

## Services

| Service        | URL / Port            | Notes                                    |
| -------------- | --------------------- | ---------------------------------------- |
| Frontend (dev) | http://localhost:5173 | Vite dev server, proxies /api to gateway |
| API gateway    | http://localhost:8000 | REST API                                 |
| PostgreSQL     | localhost:5432        | user: schurfer, db: schurfer             |
| Redis          | localhost:6379        | pump scan results, OHLCV cache           |
| NATS           | localhost:4222        | collector → analytics pub/sub            |

### Key API endpoints

```
GET /api/pumps                     current pump list (from Redis)
GET /api/pumps/:base               single token current data
GET /api/pumps/:base/ohlcv         OHLCV candles (interval=5|15|60|240, limit=N)
GET /api/pumps/:base/history       all episodes for a token
GET /api/pumps/history             filtered history (exchange, since, until)
GET /healthz                       service health check
```

## Development commands

```bash
make verify       # full pre-PR gate: lint, types, tests, build, compose config
make test         # run all tests (Python + Go + TS)
make lint         # run all linters via pre-commit
make format       # auto-format Python, Go, TypeScript
make security     # pip-audit + govulncheck + pnpm audit
make dev-logs     # tail all service logs
make dev-stop     # stop containers
make dev-reset    # stop containers and wipe all data volumes
make migrate      # run Alembic migrations against local DB
```

## License

Proprietary. All rights reserved.
