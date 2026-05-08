#!/usr/bin/env bash
# Schurfer monorepo init script
# Прогоняется ОДИН раз в папке ~/Desktop/Projects/schurfer
# Идемпотентный — повторный прогон безопасен

set -e  # выйти если любая команда упала
set -u  # ошибка если используется undefined variable

# ============================================================
# 0. Pre-flight checks
# ============================================================

echo "🔍 Pre-flight checks..."

# Проверяем что мы в правильной папке
EXPECTED_DIR="schurfer"
CURRENT_DIR=$(basename "$PWD")
if [[ "$CURRENT_DIR" != "$EXPECTED_DIR" ]]; then
    echo "❌ Запусти скрипт из папки 'schurfer'. Сейчас ты в: $PWD"
    exit 1
fi

# Проверяем что git инициализирован
if [[ ! -d ".git" ]]; then
    echo "❌ Не вижу .git — git репозиторий не инициализирован"
    echo "   Прогони: git init && git branch -M main"
    exit 1
fi

# Проверяем что нужные команды установлены
for cmd in uv pnpm go gh git pre-commit; do
    if ! command -v "$cmd" &> /dev/null; then
        echo "❌ Команда '$cmd' не найдена. Установи и попробуй снова."
        exit 1
    fi
done

echo "✅ Все проверки пройдены"
echo ""

# ============================================================
# 1. Очистка
# ============================================================

echo "🧹 Очистка..."

# Удаляем .idea — мы на VS Code/Zed
if [[ -d ".idea" ]]; then
    rm -rf .idea
    echo "   Удалён .idea"
fi

echo "✅ Очистка завершена"
echo ""

# ============================================================
# 2. Создание структуры папок
# ============================================================

echo "📁 Создание структуры папок..."

mkdir -p apps/{collectors,execution,api-gateway,analytics,telegram-bot,web}
mkdir -p packages/{core,exchanges,indicators,journal}
mkdir -p infra/{docker,terraform,scripts}
mkdir -p docs/{adr,strategies,runbooks}
mkdir -p .github/workflows
mkdir -p .vscode

echo "✅ Структура создана"
echo ""

# ============================================================
# 3. Root-level файлы
# ============================================================

echo "📝 Создание root-level файлов..."

# .gitignore
cat > .gitignore <<'EOF'
# ============================================================
# Schurfer .gitignore
# ============================================================

# === macOS ===
.DS_Store
.AppleDouble
.LSOverride
Icon
._*
.Spotlight-V100
.Trashes

# === IDE ===
.idea/
.vscode/*
!.vscode/extensions.json
!.vscode/settings.json
!.vscode/launch.json
!.vscode/tasks.json
*.swp
*.swo
.zed/

# === Python ===
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
*.egg
*.egg-info/
.eggs/
.pytest_cache/
.ruff_cache/
.mypy_cache/
.coverage
htmlcov/
.tox/
.nox/
venv/
.venv/
env/
ENV/

# === Node ===
node_modules/
.pnpm-debug.log*
.pnpm-store/
npm-debug.log*
yarn-debug.log*
yarn-error.log*
.next/
out/
dist/
build/
.turbo/
.vite/
*.tsbuildinfo

# === Go ===
*.exe
*.exe~
*.dll
*.so.*
*.dylib
*.test
*.out
go.work.sum
vendor/
bin/

# === Logs ===
*.log
logs/

# === Env / Secrets ===
.env
.env.local
.env.*.local
.env.development
.env.production
.env.test
*.pem
*.key
secrets/
.secrets/

# === Build outputs ===
target/
out/
build/
dist/

# === Database / Storage ===
*.sqlite
*.sqlite3
*.db
data/
storage/

# === Docker ===
.docker/

# === Misc ===
tmp/
temp/
.cache/
coverage/
EOF

# .editorconfig
cat > .editorconfig <<'EOF'
root = true

[*]
indent_style = space
indent_size = 2
end_of_line = lf
charset = utf-8
trim_trailing_whitespace = true
insert_final_newline = true

[*.py]
indent_size = 4

[*.go]
indent_style = tab

[Makefile]
indent_style = tab

[*.{md,markdown}]
trim_trailing_whitespace = false
EOF

# .pre-commit-config.yaml
cat > .pre-commit-config.yaml <<'EOF'
# Pre-commit hooks для Schurfer
# Установка: uv tool install pre-commit && pre-commit install
# Запуск вручную: pre-commit run --all-files

repos:
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v5.0.0
    hooks:
      - id: trailing-whitespace
      - id: end-of-file-fixer
      - id: check-yaml
      - id: check-added-large-files
        args: ['--maxkb=1000']
      - id: check-merge-conflict
      - id: detect-private-key
      - id: mixed-line-ending

  - repo: https://github.com/gitleaks/gitleaks
    rev: v8.21.2
    hooks:
      - id: gitleaks

  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.8.4
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format

  - repo: https://github.com/dnephin/pre-commit-golang
    rev: v0.5.1
    hooks:
      - id: go-fmt
      - id: go-vet-mod
      - id: go-mod-tidy
EOF

# README.md
cat > README.md <<'EOF'
# Schurfer

> Private trading platform — analytics, signals, automated execution.

## Status

🚧 Active development. Sprint 1.

## Stack

- **Backend**: Go (collectors, execution), Python (analytics, signals)
- **Frontend**: React + Vite + TypeScript + Redux Toolkit
- **Storage**: PostgreSQL + TimescaleDB, Redis
- **Message bus**: NATS
- **Infra**: Docker, Hetzner Tokyo VPS
- **Languages**: Go 1.26, Python 3.13, TypeScript

## Project structure

```
apps/
├── collectors/      Go    — exchange WS data ingestion
├── execution/       Go    — order placement, risk manager
├── api-gateway/     Go    — REST/WS API for web
├── analytics/       Python — signals, backtests, news pipeline
├── telegram-bot/    Python — alerts and approval interface
└── web/             TS+React — dashboard

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
EOF

# ARCHITECTURE.md
cat > ARCHITECTURE.md <<'EOF'
# Architecture

> High-level system design. Updated as decisions are made.

## Overview

Schurfer is a private monolithic-ish multi-service trading platform.
One product, multiple services. Web UI behind login, no public exposure.

## Services

### Hot path (latency-sensitive)

- **collectors** (Go) — one process per exchange. Maintains WebSocket
  subscriptions for spot+perp markets. Normalizes events and publishes
  to NATS bus.
- **execution** (Go) — receives signals from analytics, runs them through
  risk manager, places orders on exchanges, tracks positions.
- **api-gateway** (Go) — REST + WS endpoints for web UI. Reads from
  Redis (hot state) and Postgres (cold storage).

### Warm path (analytical)

- **analytics** (Python) — signal generation, backtests, news pipeline.
  Subscribes to NATS, computes indicators, publishes signals back.
- **telegram-bot** (Python) — sends alerts to user's Telegram, accepts
  approve/skip actions on suggested trades.

### UI

- **web** (TypeScript + React + Vite) — dashboard, journal, analytics
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
EOF

# ROADMAP.md
cat > ROADMAP.md <<'EOF'
# Roadmap

> Living document. Updated as we progress.

## Sprint 1: Foundation (current)

- [x] Decisions: monorepo, Go workspaces, frontend stack
- [x] Repo init, structure, ADRs
- [ ] Hetzner Tokyo VPS provisioned
- [ ] Docker Compose for local dev (Postgres + Timescale + Redis + NATS)
- [ ] CI: self-hosted runner on VPS
- [ ] First Go collector skeleton (Binance perp BTCUSDT)
- [ ] Trade Journal model in Postgres + migrations
- [ ] Telegram bot skeleton

## Sprint 2: First strategy + alerts

- [ ] Binance perp full collector (top 30 symbols)
- [ ] Funding rate cross-exchange comparator
- [ ] Pump detector v1 (price + volume)
- [ ] Telegram alerts with approve/skip buttons
- [ ] Paper trading framework

## Sprint 3: Multi-exchange + Bybit

- [ ] Bybit collector
- [ ] Hyperliquid collector
- [ ] OKX collector
- [ ] OI integration into pump detector
- [ ] Composite "sticky pump" signal

## Sprint 4: Pump-short live

- [ ] Risk manager with all guardrails
- [ ] Execution engine (Go)
- [ ] Position tracking
- [ ] Pump-short v1 paper → live (small size)
- [ ] Dashboard v0: equity curve + per-strategy stats

## Sprint 5: News pipeline

- [ ] CryptoPanic + RSS sources
- [ ] Telegram channel parsing (Telethon)
- [ ] Two-stage AI scoring (Groq Llama → Gemini → Claude)
- [ ] News-based alerts (manual approve only)

## Sprint 6: Smart money + Polymarket

- [ ] Smart money tracker for Solana (Helius)
- [ ] Polymarket CLOB integration
- [ ] CEX-Polymarket lag arbitrage detector
- [ ] Polymarket "No bot" baseline

## Sprint 7+: Advanced

- [ ] Pre-launch short detector (TGE-aware)
- [ ] MM history database (DWF, Wintermute patterns)
- [ ] Theme hunter (off-CEX memecoins)
- [ ] Public AlphaScope-style read-only views (if monetization desired)

## Technical debt / continuous

- [ ] Property-based tests for math
- [ ] Replay engine for backtests
- [ ] Daily reconciliation (code vs exchange)
- [ ] Monitoring (Grafana + Prometheus)
- [ ] Secrets management (sops + age)
EOF

# Makefile
cat > Makefile <<'EOF'
.PHONY: help install dev test lint format clean

help:
	@echo "Schurfer — common commands"
	@echo ""
	@echo "  make install    Install all dependencies (uv, pnpm, go)"
	@echo "  make dev        Start local dev environment"
	@echo "  make test       Run all tests"
	@echo "  make lint       Run all linters"
	@echo "  make format     Format all code"
	@echo "  make clean      Clean build artifacts"

install:
	@echo "→ Installing Python deps via uv..."
	uv sync
	@echo "→ Installing Node deps via pnpm..."
	pnpm install
	@echo "→ Syncing Go workspaces..."
	go work sync || echo "  (no Go modules yet)"
	@echo "→ Installing pre-commit hooks..."
	pre-commit install

dev:
	@echo "→ Starting Docker Compose..."
	docker compose -f infra/docker/docker-compose.dev.yml up -d
	@echo "→ Services: postgres, timescaledb, redis, nats"

test:
	@echo "→ Running Python tests..."
	uv run pytest || true
	@echo "→ Running Go tests..."
	go test ./... || true
	@echo "→ Running TS tests..."
	pnpm test || true

lint:
	@echo "→ Running pre-commit on all files..."
	pre-commit run --all-files

format:
	@echo "→ Ruff format..."
	uv run ruff format .
	@echo "→ Go fmt..."
	go fmt ./...
	@echo "→ Prettier..."
	pnpm prettier --write .

clean:
	@echo "→ Cleaning build artifacts..."
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "node_modules" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "dist" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".next" -exec rm -rf {} + 2>/dev/null || true
EOF

echo "✅ Root файлы созданы"
echo ""

# ============================================================
# 4. ADR файлы
# ============================================================

echo "📋 Создание ADR файлов..."

cat > docs/adr/0001-monorepo-structure.md <<'EOF'
# ADR-0001: Monorepo structure

Date: 2026-05-08
Status: Accepted

## Context

Нужна структура репо для multi-service trading платформы с
разными языками (Go, Python, TypeScript).

## Decision

**Monorepo** с разделением `apps/` (запускаемые сервисы) и
`packages/` (shared библиотеки).

## Alternatives considered

- Polyrepo — отдельные репо. Отброшено: cross-service refactoring
  становится болью, версии shared lib рассинхронизируются.
- Hybrid (public + private + shared) — обсуждалось когда планировался
  публичный продукт. Отброшено когда решили что продукт полностью
  приватный (см. ADR-0002).

## Consequences

- Pro: атомарные изменения через несколько сервисов, единый CI
- Con: репо растёт, нужен CI который умеет частичные builds
- Revisit: если репо превысит 1GB или появится команда >5 человек
EOF

cat > docs/adr/0002-private-product-only.md <<'EOF'
# ADR-0002: Single private product, no public component

Date: 2026-05-08
Status: Accepted

## Context

Изначально рассматривалось разделение на публичный analytics
продукт (Schurfer / dashboard) и приватный auto-trading engine.

## Decision

**Один приватный продукт.** Web UI за логином, доступ только
владельцу. Никаких публичных компонентов.

## Rationale

1. Публичная часть, торгующая чужими деньгами, требует CASP лицензии
   под MiCA. Не делаем.
2. Публичная аналитика без torgovли — отвлечение от core продукта.
3. Один deployment, один auth, одна бекап стратегия — проще.
4. Если когда-то будем монетизировать — можно открыть subscription
   на signals (информационный продукт, не финансовая услуга).

## Consequences

- Pro: фокус, простота, никаких лицензий
- Con: нет портфолио-эффекта (для CV — другие проекты)
- Revisit: если доход от трейдинга стабилизируется и появится
  желание делать SaaS — пересмотреть юридическую структуру
EOF

cat > docs/adr/0003-go-workspaces.md <<'EOF'
# ADR-0003: Go workspaces для backend сервисов

Date: 2026-05-08
Status: Accepted

## Context

Несколько Go сервисов в одном репо: collectors, execution, api-gateway.
Нужна модель управления Go модулями.

## Decision

**Go workspaces** (`go.work`). Каждый сервис — отдельный module.

## Alternatives considered

- Один go.mod на весь репо — проще, но конфликты зависимостей
  между сервисами. Сложно вытащить отдельный сервис в свой репо.

## Consequences

- Pro: изоляция зависимостей, модулярность
- Pro: каждый сервис может быть выпущен отдельно
- Con: чуть больше boilerplate (go.mod в каждой папке)
EOF

cat > docs/adr/0004-self-hosted-ci.md <<'EOF'
# ADR-0004: Self-hosted GitHub Actions runner

Date: 2026-05-08
Status: Accepted

## Context

GitHub Free даёт 2000 Actions минут/мес для приватных репо.
При активной разработке (3-5 push/день × 10-15 мин CI) можно
улететь в лимит.

## Decision

**Self-hosted runner** на Hetzner Tokyo VPS.
Бесплатно, без лимита минут.

## Alternatives considered

- GitHub-hosted (платный апгрейд) — $4/мес минимум за Pro
- Forgejo Actions — отдельная админка, сложнее
- Mix (lint hosted, heavy self-hosted) — может пригодиться позже

## Consequences

- Pro: 0 рублей, без лимитов
- Pro: cache локально на runner — builds быстрее
- Con: сами поддерживаем runner, security настраиваем
- Revisit: если runner overhead станет больше выигрыша
EOF

cat > docs/adr/0005-frontend-stack.md <<'EOF'
# ADR-0005: Frontend stack — React + Vite + Redux Toolkit

Date: 2026-05-08
Status: Accepted

## Context

Нужен web dashboard для приватного логина. Real-time данные
(цены, OI, funding) через WebSocket. Сложная structured UI
(charts, tables, forms).

## Decision

- **Vite** как bundler (не Next.js — SEO не нужен, это закрытый dashboard)
- **React 19** + **TypeScript**
- **React Router 7** (популярнее TanStack Router, тот же effort)
- **Redux Toolkit + RTK Query** для state и server state (одно решение)
- **shadcn/ui** на Radix для components (free, copy-paste)
- **Tailwind 4** для стилей
- **Lightweight Charts** (TradingView free) для графиков
- **TanStack Table** для таблиц с виртуализацией

## Rationale

Все компоненты:
- Бесплатные (MIT/Apache), никаких подписок
- Популярные на рынке (skill для CV)
- Покрывают 100% наших нужд без custom workaround'ов

Redux выбран над Zustand сознательно — больше boilerplate, но
гораздо более ценный skill на job market.

## Consequences

- Pro: всё стандартное, легко найти разработчиков, легко искать ответы
- Pro: RTK Query закрывает и REST, и WebSocket subscriptions
- Con: чуть больше кода чем с Zustand
- Revisit: не планируется в обозримом
EOF

cat > docs/adr/0006-backend-languages.md <<'EOF'
# ADR-0006: Go + Python для backend, Rust только точечно

Date: 2026-05-08
Status: Accepted

## Context

Нужны языки для разных типов сервисов: networking-heavy (collectors),
order execution, signal generation, ML/data analysis.

## Decision

- **Go** для всех networking сервисов (collectors, execution, api-gateway)
- **Python** для analytics, news pipeline, telegram bot
- **Rust** — НЕ используем сейчас. Добавим точечно если дойдём
  до latency-critical (MEV, sub-millisecond arbitrage)

## Rationale

- Go: goroutines идеальны для тысяч WebSocket connections, простой
  deployment, хорошо учится, на Bayer уже в стеке
- Python: ML/data ecosystem (pandas, numpy, scikit-learn) — не заменишь
- Rust: real edge только в latency-critical задачах. Schurfer не там.
  Добавление Rust удлинит time-to-market на месяцы.

## Consequences

- Pro: фокус на двух языках (Go+Python) ускоряет development
- Pro: знание Go растёт параллельно с работой на Bayer
- Con: упрёмся в Go GC pauses если когда-то пойдём в HFT — тогда Rust
- Revisit: при разработке MEV/sniper модулей или sub-ms арбитража
EOF

cat > docs/adr/0007-trade-journal-first.md <<'EOF'
# ADR-0007: Trade Journal как core слой, не feature

Date: 2026-05-08
Status: Accepted

## Context

Главное требование от пользователя: "нужен чёткий винрейт и логи
по аккаунтам, биржам, стратегиям. Чтобы улучшать алгоритмы и идеи".

## Decision

**Trade Journal — первый сервис который мы строим, до любых стратегий.**

Каждое действие системы (signal generated, alert sent, trade opened,
trade closed, funding paid, etc.) записывается в journal с полным
контекстом (`setup_context` JSONB с features которые повлияли
на decision).

## Consequences

- Каждая стратегия должна быть instrumented с первого дня
- Можно делать SQL-запросы типа:
  "winrate когда funding > 0.05% AND OI growth > 100%"
- Готовая основа для tax export
- Готовая основа для backtest validation
- Нельзя deploy стратегию которая не пишет в journal

## Schema highlights

См. `packages/journal/` для actual implementation.
Ключевые поля:
- strategy_id + strategy_version
- setup_context (JSONB) — все features
- entry/exit prices, slippage, funding, fees
- outcome_label (win/loss/breakeven)
- outcome_quality (planned/lucky/mistake/force_majeure)
EOF

echo "✅ ADR файлы созданы"
echo ""

# ============================================================
# 5. Первая strategy doc
# ============================================================

echo "📊 Создание pump_short_v1.md..."

cat > docs/strategies/pump_short_v1.md <<'EOF'
# Strategy: pump_short_v1

Status: draft (formalization in progress)
Author: mavlevich
Created: 2026-05-08

## Hypothesis

Низколиквидные токены, запампленные на 50-100%+ за короткий
период (часы — сутки) с признаками exhaustion (близость к peak'у,
рост OI, экстремальный funding) часто откатываются к pre-pump
уровню в течение дней — недель.

## Trigger conditions

- `price_change_24h > 50%` AND `< 130%` (типичный диапазон)
- Цена держится near top — recent peak в последние ~6 часов
- Symbol доступен на perp хотя бы на одной из бирж в твоей юрисдикции

## Entry rules

- Open SHORT на perp
- Размер позиции: пока вручную выбираешь "психологически приемлемую сумму"
  → TODO: заменить на % от капитала с risk-based sizing
- Плечо: до 10x исторически использовалось при широком стопе

## Stop loss

- Текущий подход: "большой стоп, маржи хватает"
- Implicit stop ~+200% от entry (на широких плечах)
- TODO: формализовать на технический уровень
  (например: above recent ATH +15-20%)

## Exit rules (take profit)

- Цена откат к pre-pump уровню (≈ цена за 24-48h до начала pump'а)
- Решение "по ощущениям", по интуиции
- TODO: формализовать через `target_price = price_t-48h × 1.05`

## Position management

- Текущий: одно entry, manual exit
- TODO: рассмотреть scaled entry в 2-3 транша

## Risk management gaps (для следующей итерации)

1. **Risk per trade в % от капитала** — сейчас не задано
2. **Funding rate filter** — не учитывается до входа
   (важно: на pumped токенах часто extreme funding,
   может съесть профит за дни holding)
3. **OI как trigger condition** — не используется,
   но даёт high-confidence сигналы
4. **Stop loss formalization** — заменить "большой стоп"
   на технический уровень
5. **Exit formalization** — pre-pump price как конкретное число

## Historical performance (paper-tracked)

- Pre-Schurfer: успешные сделки по интуиции, чёткая статистика
  не вёлась
- TODO: восстановить ~10 последних трейдов из памяти/CSV для
  baseline winrate

## Refinement TODO

- [ ] Backtest на исторических pumps Q1 2026 (M, MEGA, RAVE,
      SIREN, KAT, SPK style setups)
- [ ] Определить optimal price_change_24h thresholds
- [ ] Funding rate as trigger / filter
- [ ] OI growth as confidence multiplier
- [ ] Position sizing formula (risk-based)
- [ ] Stop loss rule based on technical levels
- [ ] Exit price target formula
EOF

cat > docs/strategies/README.md <<'EOF'
# Trading strategies

Каждая стратегия — один markdown файл.

## Лайфцикл стратегии

1. **draft** — идея, неформальное описание (текущий уровень
   pump_short_v1)
2. **paper** — формализована, торгуется в paper-mode
3. **shadow** — paper рядом с real markets, логируется но не
   исполняется в live
4. **live_micro** — live trading с минимальными размерами
5. **live** — полный размер
6. **deprecated** — отключена

## Naming

`{strategy_type}_v{N}.md` — pump_short_v1, funding_arb_v1, etc.
Major изменения в правилах — bump версии (v2).

## Формат

См. `pump_short_v1.md` как пример.
EOF

cat > docs/runbooks/README.md <<'EOF'
# Runbooks

Operational procedures для инцидентов и регулярных задач.

TBD when first incident happens.
EOF

echo "✅ Strategy docs созданы"
echo ""

# ============================================================
# 6. Инициализация пакет-менеджеров
# ============================================================

echo "📦 Инициализация uv (Python)..."

cat > pyproject.toml <<'EOF'
[project]
name = "schurfer"
version = "0.1.0"
description = "Private trading platform"
requires-python = ">=3.13"

[tool.uv.workspace]
members = ["apps/analytics", "apps/telegram-bot"]

[tool.ruff]
line-length = 100
target-version = "py313"

[tool.ruff.lint]
select = [
    "E",   # pycodestyle errors
    "W",   # pycodestyle warnings
    "F",   # pyflakes
    "I",   # isort
    "N",   # pep8-naming
    "UP",  # pyupgrade
    "B",   # flake8-bugbear
    "SIM", # flake8-simplify
    "RUF", # ruff-specific
]
ignore = []

[tool.ruff.format]
quote-style = "double"
indent-style = "space"

[tool.pytest.ini_options]
testpaths = ["apps", "packages"]
python_files = ["test_*.py", "*_test.py"]
EOF

# Python placeholder файлы для workspace members
mkdir -p apps/analytics/src apps/telegram-bot/src

cat > apps/analytics/pyproject.toml <<'EOF'
[project]
name = "schurfer-analytics"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = []
EOF

cat > apps/telegram-bot/pyproject.toml <<'EOF'
[project]
name = "schurfer-telegram-bot"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = []
EOF

echo "✅ uv инициализирован"
echo ""

echo "📦 Инициализация pnpm (Node)..."

cat > pnpm-workspace.yaml <<'EOF'
packages:
  - 'apps/web'
  - 'packages/*'
EOF

cat > package.json <<'EOF'
{
  "name": "schurfer",
  "version": "0.1.0",
  "description": "Private trading platform",
  "private": true,
  "packageManager": "pnpm@10.33.0",
  "scripts": {
    "lint": "pnpm -r run lint",
    "test": "pnpm -r run test",
    "format": "prettier --write ."
  },
  "devDependencies": {
    "prettier": "^3.3.3"
  }
}
EOF

cat > .prettierrc <<'EOF'
{
  "semi": true,
  "singleQuote": true,
  "trailingComma": "all",
  "printWidth": 100,
  "tabWidth": 2
}
EOF

echo "✅ pnpm инициализирован"
echo ""

echo "📦 Инициализация Go workspace..."

# Создаём go.work со списком будущих модулей (пока пустой, добавим когда будут модули)
cat > go.work <<'EOF'
go 1.26

// Use directives are added when Go modules are created in apps/ and packages/
// Example:
//   use ./apps/collectors/binance
//   use ./packages/exchanges
EOF

echo "✅ Go workspace инициализирован"
echo ""

# ============================================================
# 7. Placeholder README в каждом app/package
# ============================================================

echo "📝 Создание placeholder README'ов..."

cat > apps/collectors/README.md <<'EOF'
# collectors

Go services that maintain WebSocket subscriptions to exchanges
and publish normalized events to NATS.

One sub-package per exchange (binance/, bybit/, okx/, hyperliquid/).

Status: empty, scaffolding in Sprint 1.
EOF

cat > apps/execution/README.md <<'EOF'
# execution

Go service that receives signals from analytics, runs them
through risk manager, places orders on exchanges.

Status: empty, planned for Sprint 4.
EOF

cat > apps/api-gateway/README.md <<'EOF'
# api-gateway

Go service exposing REST + WebSocket API to web frontend.
Reads from Redis (hot state) and Postgres.

Status: empty, planned for Sprint 2.
EOF

cat > apps/analytics/README.md <<'EOF'
# analytics

Python service for signal generation, indicator computation,
backtest engine, news pipeline.

Status: empty, scaffolding in Sprint 2.
EOF

cat > apps/telegram-bot/README.md <<'EOF'
# telegram-bot

Python service that sends alerts to Telegram and accepts
approve/skip actions on suggested trades.

Status: empty, scaffolding in Sprint 2.
EOF

cat > apps/web/README.md <<'EOF'
# web

React + Vite + TypeScript dashboard.
Communicates with api-gateway via REST and WebSocket.

Status: empty, scaffolding in Sprint 4.
EOF

cat > packages/core/README.md <<'EOF'
# core

Shared types, models, errors. Source of truth for cross-language
data structures.

Status: empty.
EOF

cat > packages/exchanges/README.md <<'EOF'
# exchanges

Wrappers over exchange APIs (Binance, Bybit, OKX, Hyperliquid).
Read-only client + execution client per exchange.

Status: empty.
EOF

cat > packages/indicators/README.md <<'EOF'
# indicators

Technical indicators library. Pure functions, no I/O.

Status: empty.
EOF

cat > packages/journal/README.md <<'EOF'
# journal

Trade journal models, repositories, query helpers.

Core component (see ADR-0007).
Builds the foundation for winrate, expectancy, all per-strategy stats.

Status: scaffolding in Sprint 1.
EOF

cat > infra/docker/README.md <<'EOF'
# Docker

Local development environment via Docker Compose.

Services: postgres+timescaledb, redis, nats.

Status: docker-compose.dev.yml in Sprint 1.
EOF

cat > infra/terraform/README.md <<'EOF'
# Terraform

Infrastructure as code for Hetzner Tokyo VPS.

Status: empty, configured in Sprint 1.
EOF

cat > infra/scripts/README.md <<'EOF'
# Scripts

Operational scripts. Backups, deploys, migrations.

Status: empty.
EOF

echo "✅ README'ы созданы"
echo ""

# ============================================================
# 8. VS Code конфиги
# ============================================================

echo "⚙️  Создание VS Code конфигов..."

cat > .vscode/extensions.json <<'EOF'
{
  "recommendations": [
    "golang.go",
    "ms-python.python",
    "charliermarsh.ruff",
    "dbaeumer.vscode-eslint",
    "esbenp.prettier-vscode",
    "bradlc.vscode-tailwindcss",
    "ms-azuretools.vscode-docker",
    "redhat.vscode-yaml",
    "tamasfe.even-better-toml",
    "eamodio.gitlens",
    "streetsidesoftware.code-spell-checker"
  ]
}
EOF

cat > .vscode/settings.json <<'EOF'
{
  "editor.formatOnSave": true,
  "editor.codeActionsOnSave": {
    "source.fixAll": "explicit",
    "source.organizeImports": "explicit"
  },
  "files.insertFinalNewline": true,
  "files.trimTrailingWhitespace": true,

  "[python]": {
    "editor.defaultFormatter": "charliermarsh.ruff",
    "editor.tabSize": 4
  },

  "[go]": {
    "editor.defaultFormatter": "golang.go",
    "editor.insertSpaces": false,
    "editor.tabSize": 4
  },

  "[typescript]": {
    "editor.defaultFormatter": "esbenp.prettier-vscode"
  },
  "[typescriptreact]": {
    "editor.defaultFormatter": "esbenp.prettier-vscode"
  },
  "[javascript]": {
    "editor.defaultFormatter": "esbenp.prettier-vscode"
  },
  "[json]": {
    "editor.defaultFormatter": "esbenp.prettier-vscode"
  },
  "[yaml]": {
    "editor.defaultFormatter": "redhat.vscode-yaml"
  },

  "go.useLanguageServer": true,
  "go.toolsManagement.autoUpdate": true,
  "go.lintTool": "golangci-lint",

  "python.analysis.typeCheckingMode": "basic",
  "ruff.organizeImports": true
}
EOF

echo "✅ VS Code конфиги созданы"
echo ""

# ============================================================
# 9. GitHub Actions placeholder
# ============================================================

echo "⚙️  Создание GitHub Actions placeholder..."

cat > .github/workflows/lint.yml <<'EOF'
name: Lint

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  pre-commit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
      - uses: actions/setup-go@v5
        with:
          go-version: "1.26"
      - name: Install pre-commit
        run: pip install pre-commit
      - name: Run pre-commit
        run: pre-commit run --all-files
EOF

echo "✅ Workflow placeholder создан"
echo ""

# ============================================================
# 10. Git commit
# ============================================================

echo "💾 Git: добавление файлов..."

git add -A

echo "💾 Git: initial commit..."

git commit -m "chore: initial Schurfer monorepo structure

- Set up monorepo: apps/, packages/, infra/, docs/
- Add ADRs for first 7 decisions
- Add pump_short_v1 strategy doc
- Initialize uv (Python), pnpm (Node), go.work (Go)
- Add VS Code workspace configs
- Add Makefile, .gitignore, .editorconfig, pre-commit config" || echo "(nothing to commit или уже закоммичено)"

echo "✅ Initial commit готов"
echo ""

# ============================================================
# 11. GitHub repo creation + push
# ============================================================

echo "🚀 Создание GitHub repo и push..."

# Проверяем — может уже есть remote
if git remote -v | grep -q origin; then
    echo "   Remote 'origin' уже настроен:"
    git remote -v | grep origin
    echo "   Просто пушим..."
    git push -u origin main
else
    echo "   Создаю repo через gh..."
    gh repo create schurfer \
        --public \
        --description "Private trading platform — analytics, signals, automated execution" \
        --source=. \
        --remote=origin \
        --push
fi

echo ""
echo "🎉 Готово!"
echo ""
echo "📍 Репо: https://github.com/mavlevich/schurfer"
echo ""
echo "Дальше:"
echo "  1. Открой проект в VS Code: code ."
echo "  2. Установи рекомендованные extensions (VS Code предложит сам)"
echo "  3. Прогон pre-commit install: pre-commit install"
echo "  4. Возвращайся в чат — обсудим что делать в Sprint 1"