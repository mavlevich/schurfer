# Schurfer

> Private trading platform - analytics, signals, automated execution.

## Status

🚧 Active development. Sprint 1.

## Stack

- **Backend**: Go (collectors, execution), Python (analytics, signals)
- **Frontend**: React + Vite + TypeScript + Redux Toolkit
- **Storage**: PostgreSQL + TimescaleDB, Redis
- **Message bus**: NATS
- **Infra**: Docker, AWS EC2 (Frankfurt), Cloudflare, Tailscale
- **Languages**: Go 1.26, Python 3.13, TypeScript

## Project structure

```
apps/
├── collectors/      Go    - exchange WS data ingestion
├── execution/       Go    - order placement, risk manager
├── api-gateway/     Go    - REST/WS API for web
├── analytics/       Python - signals, backtests, news pipeline
├── telegram-bot/    Python - alerts and approval interface
└── web/             TS+React - dashboard

packages/
├── core/            shared types and utils
├── exchanges/       exchange API wrappers
├── indicators/      technical indicators
└── journal/         trade journal models

infra/
├── docker/
├── terraform/
└── scripts/

docs/
├── adr/             architecture decision records
├── strategies/      strategy documentation
└── runbooks/        operational procedures
```

## Setup

```bash
# Clone
git clone [email protected]:mavlevich/schurfer.git
cd schurfer

# Install pre-commit
pre-commit install

# Python deps (will install Python 3.13 if needed)
uv sync

# Node deps
pnpm install

# Go deps (after first Go module added)
go work sync
```

## Development

See [ROADMAP.md](./ROADMAP.md) for current sprint and TODO list.
See [ARCHITECTURE.md](./ARCHITECTURE.md) for system overview.
See [docs/adr/](./docs/adr/) for architectural decisions.
See [docs/strategies/](./docs/strategies/) for strategy specs.

## License

Proprietary. All rights reserved.
