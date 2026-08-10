.PHONY: help install install-golangci-lint install-deadcode dev dev-init dev-stop dev-reset dev-logs dev-test migrate measurement-report exchange-coverage-report exchange-source-economics-report source-lead-report source-lead-identity-report gate-identity-candidate-tooling episode-replay virtual-strategy-report virtual-entry-challenger-report virtual-threshold-challenger-report virtual-exit-policy-report virtual-exit-discovery-report virtual-score-challenger-report virtual-banded-price-extent-report candle-anomaly-report derivatives-context-report decision-quality-report derivatives-regime-feasibility-report liquid-taker-report long-horizon-report open-ended-margin-report maker-entry-report pump-magnitude-report orderflow-pilot-report orderflow-endpoint-sensitivity-report exit-liquidity-calibration-report pump-short-failure-attribution-report pump-short-reentry-audit-report oi-growth-filter-report token-history-identity-preflight-report token-history-ohlcv-sample-report token-history-parquet-dataset orderflow-start orderflow-stop orderflow-health momentum-capture-start momentum-capture-stop momentum-capture-health test lint ci-lint format clean security deadcode check verify verify-docker \
		prod-deploy prod-runtime-metrics-install prod-runtime-metrics-health prod-research-checkpoints-install prod-research-checkpoints-run prod-research-checkpoints-health prod-measurement-report prod-exchange-coverage-report prod-exchange-source-economics-report prod-source-lead-report prod-source-lead-identity-report prod-gate-identity-candidate-tooling prod-source-lead-capture-health prod-episode-replay prod-virtual-strategy-report prod-virtual-entry-challenger-report prod-virtual-threshold-challenger-report prod-virtual-exit-policy-report prod-virtual-exit-discovery-report prod-virtual-score-challenger-report prod-virtual-banded-price-extent-report prod-candle-anomaly-report prod-derivatives-context-report prod-decision-quality-report prod-derivatives-regime-feasibility-report prod-liquid-taker-report prod-long-horizon-report prod-open-ended-margin-report prod-open-ended-margin-health prod-maker-entry-report prod-pump-magnitude-report prod-orderflow-pilot-report prod-orderflow-endpoint-sensitivity-report prod-exit-liquidity-calibration-report prod-pump-short-failure-attribution-report prod-pump-short-reentry-audit-report prod-oi-growth-filter-report prod-token-history-identity-preflight-report prod-token-history-ohlcv-sample-report prod-token-history-parquet-dataset prod-orderflow-start prod-orderflow-stop prod-orderflow-health prod-momentum-capture-start prod-momentum-capture-stop prod-momentum-capture-health prod-logs prod-backup prod-restore-local prod-health

GOLANGCI_LINT_VERSION = v2.1.6
DEADCODE_VERSION = v0.48.0
PROD_REPORT_MIN_HEADROOM_MB ?= 1280
PROD_REPORT_MIN_AVAILABLE_MB ?= 1024
PROD_ORDERFLOW_MIN_AVAILABLE_MB ?= 768
PROD_ORDERFLOW_MIN_DISK_MB ?= 15360
# mem_limit is 512m; requiring roughly 2x that available before starting
# mirrors the same margin PROD_ORDERFLOW_MIN_AVAILABLE_MB gives
# orderflow-pilot's 384m.
PROD_MOMENTUM_CAPTURE_MIN_AVAILABLE_MB ?= 1024
# momentum-capture has no local container volume of its own, but its data
# still lands on the SAME host disk as everything else, via Postgres's
# volume: at the measured ~1.14 GiB/day hot uncompressed
# (packages/journal/migrations/versions/0024_bybit_momentum_bars_1m.py),
# roughly 2 days of hot data before the first chunk compresses, plus WAL
# and index overhead, plausibly reaches several GiB over a 48-72h canary.
# 10 GiB is a deliberately conservative margin above that estimate, shared
# with whatever else is already on the disk; revisit once the canary has
# measured real WAL growth instead of estimating it.
PROD_MOMENTUM_CAPTURE_MIN_DISK_MB ?= 10240

help:
	@echo "Schurfer - common commands"
	@echo ""
	@echo "  make install    Install all dependencies"
	@echo "  make dev-init   Generate .env for local dev (run once)"
	@echo "  make dev        Start local dev environment (Docker)"
	@echo "  make dev-stop   Stop dev environment"
	@echo "  make dev-reset  Stop and remove all dev data"
	@echo "  make dev-logs   Tail dev service logs"
	@echo "  make dev-test   Smoke test dev environment"
	@echo "  make test       Run all tests with coverage"
	@echo "  make lint       Run all linters"
	@echo "  make ci-lint    Run the exact all-files CI lint gate"
	@echo "  make format     Format all code"
	@echo "  make security   Run security scans"
	@echo "  make deadcode   Detect unused code"
	@echo "  make clean      Clean build artifacts"
	@echo "  make check      Run lint + test + security (full CI locally)"
	@echo "  make verify     Pre-PR gate: lock, lint, types, tests, build"
	@echo "  make verify-docker  verify + analytics Docker import check"
	@echo "  make measurement-report  Read-only local report (ARGS='...')"
	@echo "  make exchange-coverage-report  Read-only exchange source report (ARGS='...')"
	@echo "  make exchange-source-economics-report  Discover source-to-execution economics"
	@echo "  make source-lead-report  Screen MEXC/Gate lead before Binance/Bybit confirmation"
	@echo "  make source-lead-identity-report  Build prospective identity review queue"
	@echo "  make gate-identity-candidate-tooling  Propose Gate/Binance/Bybit identity candidates (ARGS='--base X')"
	@echo "  make episode-replay  Validate and group local replay inputs (ARGS='...')"
	@echo "  make virtual-strategy-report  Replay pump-short v1 by episode (ARGS='...')"
	@echo "  make virtual-entry-challenger-report  Compare registered entry challengers"
	@echo "  make virtual-threshold-challenger-report  Compare registered entry floors"
	@echo "  make virtual-exit-policy-report  Compare registered exit policies"
	@echo "  make virtual-exit-discovery-report  Explore fixed-risk exit variants"
	@echo "  make virtual-score-challenger-report  Compare registered score thresholds"
	@echo "  make candle-anomaly-report  Describe registered candle anomaly buckets"
	@echo "  make derivatives-context-report  Probe recoverable derivatives history"
	@echo "  make decision-quality-report  Compare score and component quality"
	@echo "  make derivatives-regime-feasibility-report  Coverage-only LSR feature feasibility"
	@echo "  make liquid-taker-report  Replay prospective low-impact taker shelf"
	@echo "  make liquid-taker-wider-stop-report  Compare prospective fixed-risk wider stop"
	@echo "  make long-horizon-report  Describe 24h/72h/7d returns and signed funding"
	@echo "  make open-ended-margin-report  Describe 14d/21d/28d margin-buffer survival"
	@echo "  make maker-entry-report  Estimate the maker-entry OHLCV upper bound"
	@echo "  make pump-magnitude-report  Explore 20% to 200% pump entry floors"
	@echo "  make orderflow-pilot-report  Analyze bounded Bybit event/control captures"
	@echo "  make orderflow-endpoint-sensitivity-report  Compare pilot readiness across staleness bounds"
	@echo "  make exit-liquidity-calibration-report  Compare modeled and close-time exit quotes"
	@echo "  make pump-short-failure-attribution-report  Historical discovery: where pump-short loses money"
	@echo "  make pump-short-reentry-audit-report  Measurement-only audit of actual vs modeled re-entries"
	@echo "  make oi-growth-filter-report  Forward challenger: confirmed-OI-growth baseline filter"
	@echo "  make token-history-identity-preflight-report  DB-only identity preflight for token-behavior-history"
	@echo "  make token-history-ohlcv-sample-report  Bounded live-exchange OHLCV sample (step 2 of 3)"
	@echo "  make token-history-parquet-dataset  Scoped historical Parquet backfill (step 3 of 3)"
	@echo "  make orderflow-start  Start the bounded local Bybit order-flow pilot"
	@echo "  make orderflow-health  Show local order-flow pilot health"
	@echo "  make orderflow-stop  Stop the local order-flow pilot"
	@echo "  make momentum-capture-start  Start the bounded local Bybit momentum-capture line"
	@echo "  make momentum-capture-health  Show local momentum-capture health"
	@echo "  make momentum-capture-stop  Stop the local momentum-capture line"
	@echo ""
	@echo "Production (run on server with .env.prod present):"
	@echo "  make prod-deploy          Pull + rebuild + restart all services"
	@echo "  make prod-runtime-metrics-install  Install host container-metrics service"
	@echo "  make prod-runtime-metrics-health   Inspect host container-metrics service"
	@echo "  make prod-research-checkpoints-install  Install bounded research timer"
	@echo "  make prod-research-checkpoints-run      Run one due checkpoint now"
	@echo "  make prod-research-checkpoints-health   Inspect timer and sanitized status"
	@echo "  make prod-logs            Tail production service logs"
	@echo "  make prod-backup          Run database backup now"
	@echo "  make prod-restore-local   Download latest prod backup → local dev DB"
	@echo "  make prod-health          Show container status"
	@echo "  make prod-measurement-report  Read-only production report (ARGS='...')"
	@echo "  make prod-exchange-coverage-report  Production exchange source report"
	@echo "  make prod-exchange-source-economics-report  Production source economics replay"
	@echo "  make prod-source-lead-report  Production source-lead long screen"
	@echo "  make prod-source-lead-identity-report  Production identity review queue"
	@echo "  make prod-gate-identity-candidate-tooling  Production identity candidate tooling (ARGS='--base X')"
	@echo "  make prod-source-lead-capture-health  Prospective Gate lead capture health"
	@echo "  make prod-episode-replay  Production replay-input readiness report"
	@echo "  make prod-virtual-strategy-report  Production pump-short v1 replay"
	@echo "  make prod-virtual-entry-challenger-report  Production entry challenger replay"
	@echo "  make prod-virtual-threshold-challenger-report  Production entry-floor replay"
	@echo "  make prod-virtual-exit-policy-report  Production exit-policy replay"
	@echo "  make prod-virtual-exit-discovery-report  Production exit discovery replay"
	@echo "  make prod-virtual-score-challenger-report  Production score-threshold replay"
	@echo "  make prod-virtual-banded-price-extent-report  Production banded price-extent challenger replay"
	@echo "  make prod-candle-anomaly-report  Production candle anomaly research report"
	@echo "  make prod-derivatives-context-report  Production derivatives coverage probe"
	@echo "  make prod-decision-quality-report  Production score diagnostics"
	@echo "  make prod-derivatives-regime-feasibility-report  Production LSR feature feasibility"
	@echo "  make prod-liquid-taker-report  Production low-impact taker replay"
	@echo "  make prod-liquid-taker-wider-stop-report  Production wider-stop shadow replay"
	@echo "  make prod-long-horizon-report  Production long-horizon funding research"
	@echo "  make prod-open-ended-margin-report  Production open-ended margin research"
	@echo "  make prod-open-ended-margin-health  Show extended outcome/funding progress"
	@echo "  make prod-maker-entry-report  Production maker-entry upper-bound report"
	@echo "  make prod-pump-magnitude-report  Production pump-magnitude discovery surface"
	@echo "  make prod-orderflow-pilot-report  Production Bybit order-flow pilot report"
	@echo "  make prod-orderflow-endpoint-sensitivity-report  Production staleness-bound sensitivity report"
	@echo "  make prod-exit-liquidity-calibration-report  Production exit quote calibration"
	@echo "  make prod-pump-short-failure-attribution-report  Production pump-short failure attribution"
	@echo "  make prod-pump-short-reentry-audit-report  Production re-entry vs modeled-episode audit"
	@echo "  make prod-oi-growth-filter-report  Production confirmed-OI-growth baseline filter"
	@echo "  make prod-token-history-identity-preflight-report  Production token-history identity preflight"
	@echo "  make prod-token-history-ohlcv-sample-report  Production bounded live-exchange OHLCV sample"
	@echo "  make prod-token-history-parquet-dataset  Production scoped historical Parquet backfill"
	@echo "  make prod-orderflow-start  Explicitly start the bounded order-flow trial"
	@echo "  make prod-orderflow-health  Show order-flow trial health and resource use"
	@echo "  make prod-orderflow-stop  Stop the order-flow trial"
	@echo "  make prod-momentum-capture-start  Explicitly start the bounded momentum-capture canary"
	@echo "  make prod-momentum-capture-health  Show momentum-capture canary health and resource use"
	@echo "  make prod-momentum-capture-stop  Stop the momentum-capture canary"

install:
	@echo "-> Installing Python deps via uv..."
	uv sync --all-extras
	@echo "-> Installing Node deps via pnpm..."
	pnpm install
	@echo "-> Syncing Go workspaces..."
	@if find . -name 'go.mod' -not -path './vendor/*' 2>/dev/null | grep -q .; then \
		go work sync; \
	else \
		echo "  (no Go modules yet)"; \
	fi
	@echo "-> Installing pre-commit hooks..."
	pre-commit install
	pre-commit install --hook-type commit-msg
	@$(MAKE) install-golangci-lint

install-golangci-lint:
	@echo "-> Installing golangci-lint $(GOLANGCI_LINT_VERSION)..."
	go install github.com/golangci/golangci-lint/v2/cmd/golangci-lint@$(GOLANGCI_LINT_VERSION)

install-deadcode:
	@go_bin="$$(go env GOBIN)"; \
	if test -z "$$go_bin"; then go_bin="$$(go env GOPATH)/bin"; fi; \
	test -x "$$go_bin/deadcode" || { \
		echo "-> Installing deadcode $(DEADCODE_VERSION)..."; \
		go install golang.org/x/tools/cmd/deadcode@$(DEADCODE_VERSION); \
	}

dev-init:
	@test ! -f .env || (echo ".env already exists, skipping" && exit 0)
	@HASH=$$(uv run --with bcrypt python3 -c "import bcrypt; print(bcrypt.hashpw(b'admin', bcrypt.gensalt(10)).decode())"); \
	JWT=$$(python3 -c "import secrets; print(secrets.token_hex(32))"); \
	printf "ADMIN_PASSWORD_HASH=%s\nJWT_SECRET=%s\n" "$$HASH" "$$JWT" > .env
	@echo "-> .env created (password: admin)"

dev:
	@test -f .env || (echo "ERROR: .env not found. Run:\n  make dev-init\nto generate one." && exit 1)
	@echo "-> Starting Docker Compose..."
	docker compose --env-file .env -f infra/docker/docker-compose.dev.yml up -d
	@echo "-> Waiting for services..."
	@docker compose --env-file .env -f infra/docker/docker-compose.dev.yml exec -T postgres pg_isready -U schurfer -q && echo "  postgres: ready" || echo "  postgres: starting..."
	@docker compose --env-file .env -f infra/docker/docker-compose.dev.yml exec -T redis redis-cli ping -q && echo "  redis: ready" || echo "  redis: starting..."
	@echo "-> Services up: postgres (5432), redis (6379), nats (4222)"

dev-stop:
	docker compose --env-file .env -f infra/docker/docker-compose.dev.yml down

dev-reset:
	docker compose --env-file .env -f infra/docker/docker-compose.dev.yml down -v
	@echo "-> All data volumes removed"

dev-logs:
	docker compose --env-file .env -f infra/docker/docker-compose.dev.yml logs -f

dev-test:
	@bash infra/docker/smoke-test.sh

orderflow-start:
	@test -f .env || (echo "ERROR: .env not found. Run make dev-init first." && exit 1)
	docker compose --env-file .env -f infra/docker/docker-compose.dev.yml \
		--profile orderflow up -d --build orderflow-pilot

orderflow-stop:
	docker compose --env-file .env -f infra/docker/docker-compose.dev.yml \
		--profile orderflow stop orderflow-pilot

orderflow-health:
	@docker compose --env-file .env -f infra/docker/docker-compose.dev.yml \
		--profile orderflow ps orderflow-pilot
	@docker compose --env-file .env -f infra/docker/docker-compose.dev.yml \
		exec -T redis redis-cli --raw HGETALL market:orderflow:health

momentum-capture-start:
	@test -f .env || (echo "ERROR: .env not found. Run make dev-init first." && exit 1)
	docker compose --env-file .env -f infra/docker/docker-compose.dev.yml \
		--profile momentum-capture up -d --build momentum-capture

momentum-capture-stop:
	docker compose --env-file .env -f infra/docker/docker-compose.dev.yml \
		--profile momentum-capture stop momentum-capture

momentum-capture-health:
	@docker compose --env-file .env -f infra/docker/docker-compose.dev.yml \
		--profile momentum-capture ps momentum-capture
	@docker compose --env-file .env -f infra/docker/docker-compose.dev.yml \
		exec -T redis redis-cli --raw HGETALL market:momentumcapture:health

orderflow-pilot-report:
	@uv run --package schurfer-analytics orderflow-pilot-report \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

orderflow-endpoint-sensitivity-report:
	@uv run --package schurfer-analytics orderflow-endpoint-sensitivity-report \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

exit-liquidity-calibration-report:
	@DATABASE_URL="$${DATABASE_URL:-postgresql://schurfer:schurfer_dev@localhost:5432/schurfer}" \
		uv run --package schurfer-analytics exit-liquidity-calibration-report \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

pump-short-failure-attribution-report:
	@DATABASE_URL="$${DATABASE_URL:-postgresql://schurfer:schurfer_dev@localhost:5432/schurfer}" \
		uv run --package schurfer-analytics pump-short-failure-attribution-report \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

pump-short-reentry-audit-report:
	@DATABASE_URL="$${DATABASE_URL:-postgresql://schurfer:schurfer_dev@localhost:5432/schurfer}" \
		uv run --package schurfer-analytics pump-short-reentry-audit-report \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

migrate:
	@echo "-> Running Alembic migrations..."
	cd packages/journal && \
	DATABASE_URL=$$(grep DATABASE_URL ../../.env 2>/dev/null | cut -d= -f2 || echo "postgresql://schurfer:schurfer_dev@localhost:5432/schurfer") \
	uv run --package schurfer-journal alembic upgrade head

measurement-report:
	@DATABASE_URL="$${DATABASE_URL:-postgresql://schurfer:schurfer_dev@localhost:5432/schurfer}" \
		uv run --package schurfer-analytics measurement-report $(ARGS)

exchange-coverage-report:
	@DATABASE_URL="$${DATABASE_URL:-postgresql://schurfer:schurfer_dev@localhost:5432/schurfer}" \
		uv run --package schurfer-analytics exchange-coverage-report $(ARGS)

exchange-source-economics-report:
	@DATABASE_URL="$${DATABASE_URL:-postgresql://schurfer:schurfer_dev@localhost:5432/schurfer}" \
		uv run --package schurfer-analytics exchange-source-economics-report \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

source-lead-report:
	@DATABASE_URL="$${DATABASE_URL:-postgresql://schurfer:schurfer_dev@localhost:5432/schurfer}" \
		uv run --package schurfer-analytics source-lead-report \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

source-lead-identity-report:
	@DATABASE_URL="$${DATABASE_URL:-postgresql://schurfer:schurfer_dev@localhost:5432/schurfer}" \
		uv run --package schurfer-analytics source-lead-identity-report \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

gate-identity-candidate-tooling:
	@set -a; [ -f .env ] && . ./.env; set +a; \
	uv run --package schurfer-analytics gate-identity-candidate-tooling \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

episode-replay:
	@DATABASE_URL="$${DATABASE_URL:-postgresql://schurfer:schurfer_dev@localhost:5432/schurfer}" \
		uv run --package schurfer-analytics episode-replay \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

virtual-strategy-report:
	@DATABASE_URL="$${DATABASE_URL:-postgresql://schurfer:schurfer_dev@localhost:5432/schurfer}" \
		uv run --package schurfer-analytics virtual-strategy-report \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

virtual-entry-challenger-report:
	@DATABASE_URL="$${DATABASE_URL:-postgresql://schurfer:schurfer_dev@localhost:5432/schurfer}" \
		uv run --package schurfer-analytics virtual-entry-challenger-report \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

virtual-threshold-challenger-report:
	@DATABASE_URL="$${DATABASE_URL:-postgresql://schurfer:schurfer_dev@localhost:5432/schurfer}" \
		uv run --package schurfer-analytics virtual-threshold-challenger-report \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

virtual-exit-policy-report:
	@DATABASE_URL="$${DATABASE_URL:-postgresql://schurfer:schurfer_dev@localhost:5432/schurfer}" \
		uv run --package schurfer-analytics virtual-exit-policy-report \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

virtual-exit-discovery-report:
	@DATABASE_URL="$${DATABASE_URL:-postgresql://schurfer:schurfer_dev@localhost:5432/schurfer}" \
		uv run --package schurfer-analytics virtual-exit-discovery-report \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

virtual-score-challenger-report:
	@DATABASE_URL="$${DATABASE_URL:-postgresql://schurfer:schurfer_dev@localhost:5432/schurfer}" \
		uv run --package schurfer-analytics virtual-score-challenger-report \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

virtual-banded-price-extent-report:
	@DATABASE_URL="$${DATABASE_URL:-postgresql://schurfer:schurfer_dev@localhost:5432/schurfer}" \
		uv run --package schurfer-analytics virtual-banded-price-extent-report \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

oi-growth-filter-report:
	@DATABASE_URL="$${DATABASE_URL:-postgresql://schurfer:schurfer_dev@localhost:5432/schurfer}" \
		uv run --package schurfer-analytics oi-growth-filter-report \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

token-history-identity-preflight-report:
	@DATABASE_URL="$${DATABASE_URL:-postgresql://schurfer:schurfer_dev@localhost:5432/schurfer}" \
		uv run --package schurfer-analytics token-history-identity-preflight-report \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

token-history-ohlcv-sample-report:
	@DATABASE_URL="$${DATABASE_URL:-postgresql://schurfer:schurfer_dev@localhost:5432/schurfer}" \
		uv run --package schurfer-analytics token-history-ohlcv-sample-report \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

token-history-parquet-dataset:
	@DATABASE_URL="$${DATABASE_URL:-postgresql://schurfer:schurfer_dev@localhost:5432/schurfer}" \
		uv run --package schurfer-analytics token-history-parquet-dataset \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

candle-anomaly-report:
	@DATABASE_URL="$${DATABASE_URL:-postgresql://schurfer:schurfer_dev@localhost:5432/schurfer}" \
		uv run --package schurfer-analytics candle-anomaly-report \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

derivatives-context-report:
	@DATABASE_URL="$${DATABASE_URL:-postgresql://schurfer:schurfer_dev@localhost:5432/schurfer}" \
		uv run --package schurfer-analytics derivatives-context-report \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

decision-quality-report:
	@DATABASE_URL="$${DATABASE_URL:-postgresql://schurfer:schurfer_dev@localhost:5432/schurfer}" \
		uv run --package schurfer-analytics decision-quality-report \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

derivatives-regime-feasibility-report:
	@DATABASE_URL="$${DATABASE_URL:-postgresql://schurfer:schurfer_dev@localhost:5432/schurfer}" \
		uv run --package schurfer-analytics derivatives-regime-feasibility-report \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

liquid-taker-report:
	@DATABASE_URL="$${DATABASE_URL:-postgresql://schurfer:schurfer_dev@localhost:5432/schurfer}" \
		uv run --package schurfer-analytics liquid-taker-report \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

liquid-taker-wider-stop-report:
	@DATABASE_URL="$${DATABASE_URL:-postgresql://schurfer:schurfer_dev@localhost:5432/schurfer}" \
		uv run --package schurfer-analytics liquid-taker-wider-stop-report \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

long-horizon-report:
	@DATABASE_URL="$${DATABASE_URL:-postgresql://schurfer:schurfer_dev@localhost:5432/schurfer}" \
		uv run --package schurfer-analytics long-horizon-report \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

open-ended-margin-report:
	@DATABASE_URL="$${DATABASE_URL:-postgresql://schurfer:schurfer_dev@localhost:5432/schurfer}" \
		uv run --package schurfer-analytics open-ended-margin-report \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

maker-entry-report:
	@DATABASE_URL="$${DATABASE_URL:-postgresql://schurfer:schurfer_dev@localhost:5432/schurfer}" \
		uv run --package schurfer-analytics maker-entry-report \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

pump-magnitude-report:
	@DATABASE_URL="$${DATABASE_URL:-postgresql://schurfer:schurfer_dev@localhost:5432/schurfer}" \
		uv run --package schurfer-analytics pump-magnitude-report \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

test:
	@echo "-> Running Python tests..."
	@if find apps packages -name 'test_*.py' -o -name '*_test.py' 2>/dev/null | grep -q .; then \
		uv run pytest --cov --cov-report=term; \
	else \
		echo "  (no Python tests yet)"; \
	fi
	@echo "-> Running Go tests..."
	@if find . -name 'go.mod' -not -path './vendor/*' 2>/dev/null | grep -q .; then \
		go test -race -cover ./...; \
	else \
		echo "  (no Go modules yet)"; \
	fi
	@echo "-> Running TS tests..."
	@if find apps/web packages -name 'tsconfig.json' 2>/dev/null | grep -q .; then \
		pnpm -r run test; \
	else \
		echo "  (no TS projects yet)"; \
	fi

lint:
	@echo "-> Running pre-commit on all files..."
	pre-commit run --all-files

ci-lint:
	@echo "-> Running the exact all-files CI lint gate..."
	SKIP=no-commit-to-branch pre-commit run --all-files

format:
	@echo "-> Ruff format..."
	uv run --extra dev ruff format .
	@echo "-> Ruff fix..."
	uv run --extra dev ruff check --fix .
	@echo "-> Go fmt..."
	@if find . -name '*.go' 2>/dev/null | grep -q .; then \
		go fmt ./...; \
	fi
	@echo "-> Prettier..."
	pnpm prettier --write .

security:
	@echo "-> Python dependency audit..."
	uv run pip-audit
	@echo "-> Go vulnerability check..."
	@if find . -name 'go.mod' -not -path './vendor/*' 2>/dev/null | grep -q .; then \
		govulncheck ./...; \
	else \
		echo "  (no Go modules yet)"; \
	fi
	@echo "-> Node dependency audit..."
	pnpm audit --audit-level=high

deadcode:
	@echo "-> Python dead code..."
	@if find apps packages -name '*.py' 2>/dev/null | grep -q .; then \
		uv run vulture apps/ packages/ --min-confidence 90; \
	else \
		echo "  (no Python code yet)"; \
	fi
	@echo "-> Go dead code..."
	@if find . -name 'go.mod' -not -path './vendor/*' 2>/dev/null | grep -q .; then \
		$(MAKE) install-deadcode; \
		go_bin="$$(go env GOBIN)"; \
		if test -z "$$go_bin"; then go_bin="$$(go env GOPATH)/bin"; fi; \
		grep '^use ' go.work | awk '{print $$2}' | while read -r dir; do \
			echo "=== deadcode $$dir ==="; \
			(cd "$$dir" && "$$go_bin/deadcode" ./...); \
		done; \
	else \
		echo "  (no Go code yet)"; \
	fi

clean:
	@echo "-> Cleaning build artifacts..."
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "node_modules" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "dist" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".next" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "coverage" -exec rm -rf {} + 2>/dev/null || true

check: lint test security
	@echo "-> All checks passed"

verify:
	@echo "=== [1/6] CI-equivalent all-files lint ==="
	$(MAKE) ci-lint
	@echo "=== [2/6] uv lock check ==="
	uv lock --check
	@echo "=== [3/6] Python: ruff + mypy + pytest ==="
	uv run --extra dev ruff check apps/analytics apps/execution packages infra/scripts/research_checkpoints.py
	MYPYPATH=apps/analytics:packages/journal:packages/performance uv run --extra dev --with sqlalchemy --with psycopg mypy apps/analytics/schurfer_analytics apps/analytics/tests packages/journal/schurfer_journal packages/performance/schurfer_performance infra/scripts/research_checkpoints.py
	MYPYPATH=packages/performance uv run --extra dev --all-packages mypy apps/execution/schurfer_execution
	uv run --extra dev --with ccxt --with greenlet --with redis --with sqlalchemy --with structlog --with "psycopg[binary]" pytest apps/analytics -q
	uv run --extra dev --with sqlalchemy --with alembic --with "psycopg[binary]" pytest packages/journal packages/performance -q
	uv run --extra dev --all-packages pytest apps/execution/tests -q
	@echo "=== [4/6] Go: test + vet ==="
	go test ./apps/api-gateway/... ./apps/collector/... ./apps/notifier/...
	go vet ./apps/api-gateway/... ./apps/collector/... ./apps/notifier/...
	$(MAKE) deadcode
	@echo "=== [5/6] Web: lint + typecheck + build ==="
	pnpm --filter @schurfer/web lint
	pnpm --filter @schurfer/web typecheck
	pnpm --filter @schurfer/web build
	@echo "=== [6/6] Compose config ==="
	docker compose --env-file .env.ci -f infra/docker/docker-compose.dev.yml config --quiet
	@echo "=== verify passed ==="

_PROD = docker compose --env-file .env.prod -f infra/docker/docker-compose.prod.yml

prod-migrate:
	@echo "-> Running Alembic migrations..."
	@$(_PROD) exec -T postgres pg_isready -U schurfer -q
	@POSTGRES_PASSWORD=$$(grep ^POSTGRES_PASSWORD .env.prod | cut -d= -f2-); \
	docker run --rm \
		--network "container:schurfer-postgres" \
		-e "DATABASE_URL=postgresql://schurfer:$$POSTGRES_PASSWORD@127.0.0.1:5432/schurfer" \
		-v "$(CURDIR):/app" \
		-w /app \
		ghcr.io/astral-sh/uv:python3.13-bookworm-slim \
		uv run --package schurfer-journal \
			alembic -c packages/journal/alembic.ini upgrade head

prod-deploy:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@test "$$(git branch --show-current)" = "main" || (echo "ERROR: not on main (on '$$(git branch --show-current)'). Deploy only from main." && exit 1)
	@test -z "$$(git status --porcelain)" || (echo "ERROR: working tree not clean. Commit or stash first." && exit 1)
	@echo "-> [1/5] Backup..."
	@bash infra/scripts/backup.sh
	@echo "-> [2/5] Pull (fast-forward only)..."
	git pull --ff-only origin main
	@echo "-> [3/5] Start DB..."
	$(_PROD) up -d postgres redis nats
	@$(_PROD) exec -T postgres pg_isready -U schurfer -q --timeout=30
	@echo "-> [4/5] Migrate + deploy..."
	@$(MAKE) prod-migrate
	$(_PROD) up -d --build --wait --wait-timeout 180
	docker image prune -f
	@echo "-> [5/5] Health..."
	@$(_PROD) ps --format "table {{.Name}}\t{{.Status}}\t{{.Health}}"
	@echo "-> Done. Logs: make prod-logs"

prod-runtime-metrics-install:
	@test "$$(git branch --show-current)" = "main" || (echo "ERROR: not on main (on '$$(git branch --show-current)'). Install only from main." && exit 1)
	@test -z "$$(git status --porcelain)" || (echo "ERROR: working tree not clean. Commit or stash first." && exit 1)
	@mkdir -p runtime
	@chmod 0755 infra/scripts/runtime-metrics.sh
	sudo install -m 0644 infra/systemd/schurfer-runtime-metrics.service /etc/systemd/system/schurfer-runtime-metrics.service
	sudo systemctl daemon-reload
	sudo systemctl enable schurfer-runtime-metrics.service
	sudo systemctl restart schurfer-runtime-metrics.service
	@sleep 2
	@$(MAKE) prod-runtime-metrics-health

prod-runtime-metrics-health:
	@systemctl --no-pager --full status schurfer-runtime-metrics.service
	@test -s runtime/container-metrics.snapshot || (echo "ERROR: runtime metrics snapshot is missing." && exit 1)
	@head -n 4 runtime/container-metrics.snapshot

prod-research-checkpoints-install:
	@test "$$(git branch --show-current)" = "main" || (echo "ERROR: not on main (on '$$(git branch --show-current)'). Install only from main." && exit 1)
	@test -z "$$(git status --porcelain)" || (echo "ERROR: working tree not clean. Commit or stash first." && exit 1)
	@mkdir -p runtime backups/reports/automated
	@chmod 0700 backups/reports/automated
	sudo install -m 0644 infra/systemd/schurfer-research-checkpoints.service /etc/systemd/system/schurfer-research-checkpoints.service
	sudo install -m 0644 infra/systemd/schurfer-research-checkpoints.timer /etc/systemd/system/schurfer-research-checkpoints.timer
	sudo systemctl daemon-reload
	sudo systemctl enable --now schurfer-research-checkpoints.timer
	@echo "Starting the first due checkpoint. This command waits for the bounded report to finish."
	sudo systemctl start schurfer-research-checkpoints.service
	@$(MAKE) prod-research-checkpoints-health

prod-research-checkpoints-run:
	@echo "Starting one due checkpoint. Progress is also available in the systemd journal."
	sudo systemctl start schurfer-research-checkpoints.service
	@$(MAKE) prod-research-checkpoints-health

prod-research-checkpoints-health:
	@systemctl --no-pager --full status schurfer-research-checkpoints.timer
	@systemctl --no-pager --full status schurfer-research-checkpoints.service || true
	@test -s runtime/research-checkpoints.json || (echo "ERROR: research checkpoint snapshot is missing." && exit 1)
	@python3 -m json.tool runtime/research-checkpoints.json

# Rebuild a single service from current main. Guarded like prod-deploy, but skips
# backup and migration, so use it only for a code-only change with NO new migration.
prod-deploy-svc:
	@test -n "$(SERVICE)" || (echo "ERROR: set SERVICE=<name>, e.g. make prod-deploy-svc SERVICE=execution" && exit 1)
	@test "$$(git branch --show-current)" = "main" || (echo "ERROR: not on main (on '$$(git branch --show-current)'). Deploy only from main." && exit 1)
	@test -z "$$(git status --porcelain)" || (echo "ERROR: working tree not clean. Commit or stash first." && exit 1)
	@echo "-> Single-service deploy of $(SERVICE) (NO backup, NO migration; use prod-deploy if the change has a migration)..."
	git pull --ff-only origin main
	$(_PROD) up -d --build --wait --wait-timeout 180 $(SERVICE)
	@$(_PROD) ps --format "table {{.Name}}\t{{.Status}}\t{{.Health}}"

prod-measurement-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	$(_PROD) run --rm --no-deps --entrypoint measurement-report analytics $(ARGS)

prod-exchange-coverage-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	$(_PROD) run --rm --no-deps --entrypoint exchange-coverage-report analytics $(ARGS)

prod-exchange-source-economics-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@if test -r /proc/meminfo; then \
		available_kb=$$(awk '/^MemAvailable:/ {print $$2}' /proc/meminfo); \
		swap_kb=$$(awk '/^SwapFree:/ {print $$2}' /proc/meminfo); \
		headroom_kb=$$((available_kb + swap_kb)); \
		required_kb=$$(( $(PROD_REPORT_MIN_HEADROOM_MB) * 1024 )); \
		if test "$$headroom_kb" -lt "$$required_kb"; then \
			echo "ERROR: exchange-source economics requires at least $(PROD_REPORT_MIN_HEADROOM_MB) MiB of available RAM + free swap."; \
			echo "Current headroom: $$((headroom_kb / 1024)) MiB. Refusing to risk a host OOM."; \
			exit 1; \
		fi; \
	fi
	@$(_PROD) run --rm --no-deps --entrypoint exchange-source-economics-report analytics \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

prod-source-lead-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@if test -r /proc/meminfo; then \
		available_kb=$$(awk '/^MemAvailable:/ {print $$2}' /proc/meminfo); \
		swap_kb=$$(awk '/^SwapFree:/ {print $$2}' /proc/meminfo); \
		headroom_kb=$$((available_kb + swap_kb)); \
		required_kb=$$(( $(PROD_REPORT_MIN_HEADROOM_MB) * 1024 )); \
		if test "$$headroom_kb" -lt "$$required_kb"; then \
			echo "ERROR: source-lead report requires at least $(PROD_REPORT_MIN_HEADROOM_MB) MiB of available RAM + free swap."; \
			echo "Current headroom: $$((headroom_kb / 1024)) MiB. Refusing to risk a host OOM."; \
			exit 1; \
		fi; \
	fi
	@$(_PROD) run --rm --no-deps --entrypoint source-lead-report analytics \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

prod-source-lead-identity-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@$(_PROD) run --rm --no-deps --entrypoint source-lead-identity-report analytics \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

prod-gate-identity-candidate-tooling:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@$(_PROD) run --rm --no-deps --entrypoint gate-identity-candidate-tooling analytics \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

prod-source-lead-capture-health:
	@test -f .env.prod || (echo "ERROR: .env.prod not found." && exit 1)
	@printf '%s\n' "\
	SELECT status, eligibility_reason, count(*) AS captures, \
	       min(source_first_observed_at) AS first_observed, \
	       max(source_first_observed_at) AS last_observed \
	FROM app.source_lead_captures \
	WHERE source_first_observed_at >= TIMESTAMPTZ '2026-08-02T00:00:00Z' \
	GROUP BY status, eligibility_reason \
	ORDER BY captures DESC, status, eligibility_reason; \
	SELECT target.target_exchange, target.status, target.eligibility_reason, \
	       count(*) AS observations, round(avg(target.latency_ms), 1) AS mean_network_ms, \
	       round((avg(extract(epoch FROM (target.observed_at - capture.source_first_observed_at)) * 1000))::numeric, 1) AS mean_source_to_quote_ms \
	FROM app.source_lead_target_observations AS target \
	JOIN app.source_lead_captures AS capture ON capture.id = target.capture_id \
	WHERE capture.source_first_observed_at >= TIMESTAMPTZ '2026-08-02T00:00:00Z' \
	GROUP BY target.target_exchange, target.status, target.eligibility_reason \
	ORDER BY target_exchange, observations DESC; \
	SELECT count(*) AS collecting_older_than_10m \
	FROM app.source_lead_captures \
	WHERE source_first_observed_at >= TIMESTAMPTZ '2026-08-02T00:00:00Z' \
	  AND status = 'collecting' AND capture_started_at < now() - interval '10 minutes'; \
	SELECT error, count(*) AS abandoned \
	FROM app.source_lead_captures \
	WHERE source_first_observed_at >= TIMESTAMPTZ '2026-08-02T00:00:00Z' \
	  AND status = 'abandoned' \
	GROUP BY error ORDER BY abandoned DESC; \
	SELECT qualification.status, qualification.reason, \
	       qualification.identity_registry_version, \
	       qualification.identity_registry_fingerprint, \
	       qualification.selected_target_exchange, count(*) AS captures, \
	       round(avg(qualification.selected_round_trip_impact_bps), 2) AS mean_round_trip_impact_bps \
	FROM app.source_lead_qualifications AS qualification \
	JOIN app.source_lead_captures AS capture ON capture.id = qualification.capture_id \
	WHERE capture.source_first_observed_at >= TIMESTAMPTZ '2026-08-02T00:00:00Z' \
	GROUP BY qualification.status, qualification.reason, \
	         qualification.identity_registry_version, \
	         qualification.identity_registry_fingerprint, \
	         qualification.selected_target_exchange \
	ORDER BY captures DESC, qualification.status, qualification.reason;" \
	| $(_PROD) exec -T postgres psql -U schurfer -d schurfer -v ON_ERROR_STOP=1 -f -

prod-episode-replay:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	$(_PROD) run --rm --no-deps --entrypoint episode-replay analytics \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

prod-virtual-strategy-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	$(_PROD) run --rm --no-deps --entrypoint virtual-strategy-report analytics \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

prod-virtual-entry-challenger-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@$(_PROD) run --rm --no-deps --entrypoint virtual-entry-challenger-report analytics \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

prod-virtual-threshold-challenger-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@$(_PROD) run --rm --no-deps --entrypoint virtual-threshold-challenger-report analytics \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

prod-virtual-exit-policy-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@$(_PROD) run --rm --no-deps --entrypoint virtual-exit-policy-report analytics \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

prod-virtual-exit-discovery-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@$(_PROD) run --rm --no-deps --entrypoint virtual-exit-discovery-report analytics \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

prod-virtual-score-challenger-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@$(_PROD) run --rm --no-deps --entrypoint virtual-score-challenger-report analytics \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

prod-virtual-banded-price-extent-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@$(_PROD) run --rm --no-deps --entrypoint virtual-banded-price-extent-report analytics \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

prod-oi-growth-filter-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@$(_PROD) run --rm --no-deps --entrypoint oi-growth-filter-report analytics \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

prod-token-history-identity-preflight-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@$(_PROD) run --rm --no-deps --entrypoint token-history-identity-preflight-report analytics \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

prod-token-history-ohlcv-sample-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@if test -r /proc/meminfo; then \
		available_kb=$$(awk '/^MemAvailable:/ {print $$2}' /proc/meminfo); \
		swap_kb=$$(awk '/^SwapFree:/ {print $$2}' /proc/meminfo); \
		headroom_kb=$$((available_kb + swap_kb)); \
		required_available_kb=$$(( $(PROD_REPORT_MIN_AVAILABLE_MB) * 1024 )); \
		required_kb=$$(( $(PROD_REPORT_MIN_HEADROOM_MB) * 1024 )); \
		if test "$$available_kb" -lt "$$required_available_kb"; then \
			echo "ERROR: token-history OHLCV sample requires at least $(PROD_REPORT_MIN_AVAILABLE_MB) MiB of MemAvailable; free swap is not a substitute for working RAM."; \
			echo "Current MemAvailable: $$((available_kb / 1024)) MiB. Run the report locally through the DB tunnel or wait for host headroom."; \
			exit 1; \
		fi; \
		if test "$$headroom_kb" -lt "$$required_kb"; then \
			echo "ERROR: token-history OHLCV sample requires at least $(PROD_REPORT_MIN_HEADROOM_MB) MiB of available RAM + free swap."; \
			echo "Current headroom: $$((headroom_kb / 1024)) MiB. Refusing to risk a host OOM."; \
			exit 1; \
		fi; \
	fi
	@$(_PROD) run --rm --no-deps --entrypoint token-history-ohlcv-sample-report analytics \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

prod-token-history-parquet-dataset:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@if test -r /proc/meminfo; then \
		available_kb=$$(awk '/^MemAvailable:/ {print $$2}' /proc/meminfo); \
		swap_kb=$$(awk '/^SwapFree:/ {print $$2}' /proc/meminfo); \
		headroom_kb=$$((available_kb + swap_kb)); \
		required_available_kb=$$(( $(PROD_REPORT_MIN_AVAILABLE_MB) * 1024 )); \
		required_kb=$$(( $(PROD_REPORT_MIN_HEADROOM_MB) * 1024 )); \
		if test "$$available_kb" -lt "$$required_available_kb"; then \
			echo "ERROR: token-history parquet dataset requires at least $(PROD_REPORT_MIN_AVAILABLE_MB) MiB of MemAvailable; free swap is not a substitute for working RAM."; \
			echo "Current MemAvailable: $$((available_kb / 1024)) MiB. Run it locally through the DB tunnel or wait for host headroom."; \
			exit 1; \
		fi; \
		if test "$$headroom_kb" -lt "$$required_kb"; then \
			echo "ERROR: token-history parquet dataset requires at least $(PROD_REPORT_MIN_HEADROOM_MB) MiB of available RAM + free swap."; \
			echo "Current headroom: $$((headroom_kb / 1024)) MiB. Refusing to risk a host OOM."; \
			exit 1; \
		fi; \
	fi
	@mkdir -p backups/token-history
	@$(_PROD) run --rm --no-deps \
		--volume "$(CURDIR)/backups/token-history:/data/token-history" \
		--entrypoint token-history-parquet-dataset analytics \
		$(ARGS) \
		--output-root /data/token-history \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty')

prod-candle-anomaly-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@$(_PROD) run --rm --no-deps --entrypoint candle-anomaly-report analytics \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

prod-derivatives-context-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@$(_PROD) run --rm --no-deps --entrypoint derivatives-context-report analytics \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

prod-decision-quality-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@$(_PROD) run --rm --no-deps --entrypoint decision-quality-report analytics \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

prod-derivatives-regime-feasibility-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@$(_PROD) run --rm --no-deps --entrypoint derivatives-regime-feasibility-report analytics \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

prod-liquid-taker-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@$(_PROD) run --rm --no-deps --entrypoint liquid-taker-report analytics \
		--code-revision="$$(git rev-parse HEAD)" \
		--record-run \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

prod-liquid-taker-wider-stop-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@$(_PROD) run --rm --no-deps --entrypoint liquid-taker-wider-stop-report analytics \
		--code-revision="$$(git rev-parse HEAD)" \
		--record-run \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

prod-long-horizon-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@$(_PROD) run --rm --no-deps --entrypoint long-horizon-report analytics \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

prod-open-ended-margin-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@$(_PROD) run --rm --no-deps --entrypoint open-ended-margin-report analytics \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

prod-open-ended-margin-health:
	@test -f .env.prod || (echo "ERROR: .env.prod not found." && exit 1)
	@$(_PROD) exec -T postgres psql -U schurfer -d schurfer -v ON_ERROR_STOP=1 -c "\
	SELECT horizon_minutes, status, count(*) AS outcomes, max(resolved_at) AS latest \
	FROM app.trade_decision_outcomes \
	WHERE resolver_version = 'forward_v1' \
	  AND horizon_minutes IN (20160, 30240, 40320) \
	GROUP BY horizon_minutes, status \
	ORDER BY horizon_minutes, status; \
	SELECT exchange, status, count(*) AS runs, sum(in_window_rows) AS samples, \
	       max(attempt_count) AS max_attempts, max(resolved_at) AS latest \
	FROM app.pump_derivatives_context_runs \
	WHERE resolver_version = 'open_ended_margin_funding_v1' \
	  AND method = 'funding_rate_history' \
	GROUP BY exchange, status \
	ORDER BY exchange, status;"

prod-maker-entry-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@$(_PROD) run --rm --no-deps --entrypoint maker-entry-report analytics \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

prod-pump-magnitude-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@if test -r /proc/meminfo; then \
		available_kb=$$(awk '/^MemAvailable:/ {print $$2}' /proc/meminfo); \
		swap_kb=$$(awk '/^SwapFree:/ {print $$2}' /proc/meminfo); \
		headroom_kb=$$((available_kb + swap_kb)); \
		required_kb=$$(( $(PROD_REPORT_MIN_HEADROOM_MB) * 1024 )); \
		if test "$$headroom_kb" -lt "$$required_kb"; then \
			echo "ERROR: pump-magnitude report requires at least $(PROD_REPORT_MIN_HEADROOM_MB) MiB of available RAM + free swap."; \
			echo "Current headroom: $$((headroom_kb / 1024)) MiB. Refusing to risk a host OOM."; \
			exit 1; \
		fi; \
	fi
	@$(_PROD) run --rm --no-deps --entrypoint pump-magnitude-report analytics \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

prod-orderflow-pilot-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@$(_PROD) run --rm --no-deps --entrypoint orderflow-pilot-report analytics \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

prod-orderflow-endpoint-sensitivity-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@$(_PROD) run --rm --no-deps --entrypoint orderflow-endpoint-sensitivity-report analytics \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

prod-exit-liquidity-calibration-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@$(_PROD) run --rm --no-deps --entrypoint exit-liquidity-calibration-report analytics \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

prod-pump-short-failure-attribution-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@if test -r /proc/meminfo; then \
		available_kb=$$(awk '/^MemAvailable:/ {print $$2}' /proc/meminfo); \
		swap_kb=$$(awk '/^SwapFree:/ {print $$2}' /proc/meminfo); \
		headroom_kb=$$((available_kb + swap_kb)); \
		required_kb=$$(( $(PROD_REPORT_MIN_HEADROOM_MB) * 1024 )); \
		if test "$$headroom_kb" -lt "$$required_kb"; then \
			echo "ERROR: failure-attribution report requires at least $(PROD_REPORT_MIN_HEADROOM_MB) MiB of available RAM + free swap."; \
			echo "Current headroom: $$((headroom_kb / 1024)) MiB. Refusing to risk a host OOM."; \
			exit 1; \
		fi; \
	fi
	@$(_PROD) run --rm --no-deps --entrypoint pump-short-failure-attribution-report analytics \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

prod-pump-short-reentry-audit-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@if test -r /proc/meminfo; then \
		available_kb=$$(awk '/^MemAvailable:/ {print $$2}' /proc/meminfo); \
		swap_kb=$$(awk '/^SwapFree:/ {print $$2}' /proc/meminfo); \
		headroom_kb=$$((available_kb + swap_kb)); \
		required_kb=$$(( $(PROD_REPORT_MIN_HEADROOM_MB) * 1024 )); \
		if test "$$headroom_kb" -lt "$$required_kb"; then \
			echo "ERROR: reentry-audit report requires at least $(PROD_REPORT_MIN_HEADROOM_MB) MiB of available RAM + free swap."; \
			echo "Current headroom: $$((headroom_kb / 1024)) MiB. Refusing to risk a host OOM."; \
			exit 1; \
		fi; \
	fi
	@$(_PROD) run --rm --no-deps --entrypoint pump-short-reentry-audit-report analytics \
		--code-revision="$$(git rev-parse HEAD)" \
		$$(test -z "$$(git status --porcelain)" \
			&& printf '%s' '--no-working-tree-dirty' \
			|| printf '%s' '--working-tree-dirty') $(ARGS)

# Redeploy a previous known-good commit. No pull, no migration: a rollback must
# not fast-forward back to the broken main, and checking out old code does NOT
# revert a schema change (restore from backup or downgrade explicitly for that).
# Afterwards HEAD is detached; return to normal deploys with `git switch main`.
prod-rollback:
	@test -n "$(REV)" || (echo "ERROR: set REV=<sha>, e.g. make prod-rollback REV=abc123" && exit 1)
	@test -z "$$(git status --porcelain)" || (echo "ERROR: working tree not clean. Commit or stash first." && exit 1)
	@echo "-> Rolling back to $(REV) (no pull, no migration)..."
	git checkout --detach $(REV)
	$(_PROD) up -d --build --wait --wait-timeout 180
	@$(_PROD) ps --format "table {{.Name}}\t{{.Status}}\t{{.Health}}"
	@echo "-> Rolled back to $(REV). HEAD is now detached; run 'git switch main' before the next prod-deploy."
	@echo "-> Schema NOT reverted; restore from backup if a migration must be undone."

prod-logs:
	$(_PROD) logs -f --tail=100

prod-backup:
	@bash infra/scripts/backup.sh

prod-restore-local:
	@bash infra/scripts/restore-local.sh

prod-health:
	@$(_PROD) ps --format "table {{.Name}}\t{{.Status}}\t{{.Health}}"

prod-orderflow-start:
	@test -f .env.prod || (echo "ERROR: .env.prod not found." && exit 1)
	@test "$$(git branch --show-current)" = "main" || (echo "ERROR: deploy only from main." && exit 1)
	@test -z "$$(git status --porcelain)" || (echo "ERROR: working tree not clean." && exit 1)
	@if test -r /proc/meminfo; then \
		available_mb=$$(awk '/^MemAvailable:/ {print int($$2 / 1024)}' /proc/meminfo); \
		if test "$$available_mb" -lt "$(PROD_ORDERFLOW_MIN_AVAILABLE_MB)"; then \
			echo "ERROR: order-flow trial requires $(PROD_ORDERFLOW_MIN_AVAILABLE_MB) MiB available RAM; found $$available_mb MiB."; \
			exit 1; \
		fi; \
	fi
	@available_disk_mb=$$(df -Pm / | awk 'NR == 2 {print $$4}'); \
	if test "$$available_disk_mb" -lt "$(PROD_ORDERFLOW_MIN_DISK_MB)"; then \
		echo "ERROR: order-flow trial requires $(PROD_ORDERFLOW_MIN_DISK_MB) MiB free disk; found $$available_disk_mb MiB."; \
		exit 1; \
	fi
	$(_PROD) --profile orderflow up -d --build orderflow-pilot
	@$(_PROD) --profile orderflow ps orderflow-pilot

prod-orderflow-stop:
	@test -f .env.prod || (echo "ERROR: .env.prod not found." && exit 1)
	$(_PROD) --profile orderflow stop orderflow-pilot

prod-orderflow-health:
	@test -f .env.prod || (echo "ERROR: .env.prod not found." && exit 1)
	@$(_PROD) --profile orderflow ps orderflow-pilot
	@$(_PROD) exec -T redis redis-cli --raw HGETALL market:orderflow:health
	@docker stats --no-stream schurfer-orderflow-pilot schurfer-collector schurfer-market-hotset

prod-momentum-capture-start:
	@test -f .env.prod || (echo "ERROR: .env.prod not found." && exit 1)
	@test "$$(git branch --show-current)" = "main" || (echo "ERROR: deploy only from main." && exit 1)
	@test -z "$$(git status --porcelain)" || (echo "ERROR: working tree not clean." && exit 1)
	@if test -r /proc/meminfo; then \
		available_mb=$$(awk '/^MemAvailable:/ {print int($$2 / 1024)}' /proc/meminfo); \
		if test "$$available_mb" -lt "$(PROD_MOMENTUM_CAPTURE_MIN_AVAILABLE_MB)"; then \
			echo "ERROR: momentum-capture requires $(PROD_MOMENTUM_CAPTURE_MIN_AVAILABLE_MB) MiB available RAM; found $$available_mb MiB."; \
			exit 1; \
		fi; \
	fi
	@available_disk_mb=$$(df -Pm / | awk 'NR == 2 {print $$4}'); \
	if test "$$available_disk_mb" -lt "$(PROD_MOMENTUM_CAPTURE_MIN_DISK_MB)"; then \
		echo "ERROR: momentum-capture requires $(PROD_MOMENTUM_CAPTURE_MIN_DISK_MB) MiB free disk; found $$available_disk_mb MiB."; \
		exit 1; \
	fi
	$(_PROD) --profile momentum-capture up -d --build momentum-capture
	@$(_PROD) --profile momentum-capture ps momentum-capture

prod-momentum-capture-stop:
	@test -f .env.prod || (echo "ERROR: .env.prod not found." && exit 1)
	$(_PROD) --profile momentum-capture stop momentum-capture

prod-momentum-capture-health:
	@test -f .env.prod || (echo "ERROR: .env.prod not found." && exit 1)
	@$(_PROD) --profile momentum-capture ps momentum-capture
	@$(_PROD) exec -T redis redis-cli --raw HGETALL market:momentumcapture:health
	@docker stats --no-stream schurfer-momentum-capture schurfer-collector

verify-docker: verify
	@echo "=== Docker: analytics build + import check ==="
	docker build -f apps/analytics/Dockerfile -t schurfer-analytics:ci . -q
	docker run --rm --entrypoint python schurfer-analytics:ci -c "import schurfer_analytics; print('ok')"
	docker run --rm --entrypoint outcome-resolver schurfer-analytics:ci --help
	docker run --rm --entrypoint measurement-report schurfer-analytics:ci --help
	docker run --rm --entrypoint exchange-coverage-report schurfer-analytics:ci --help
	docker run --rm --entrypoint exchange-source-economics-report schurfer-analytics:ci --help
	docker run --rm --entrypoint source-lead-report schurfer-analytics:ci --help
	docker run --rm --entrypoint source-lead-identity-report schurfer-analytics:ci --help
	docker run --rm --entrypoint episode-replay schurfer-analytics:ci --help
	docker run --rm --entrypoint virtual-strategy-report schurfer-analytics:ci --help
	docker run --rm --entrypoint virtual-entry-challenger-report schurfer-analytics:ci --help
	docker run --rm --entrypoint virtual-threshold-challenger-report schurfer-analytics:ci --help
	docker run --rm --entrypoint virtual-exit-policy-report schurfer-analytics:ci --help
	docker run --rm --entrypoint virtual-exit-discovery-report schurfer-analytics:ci --help
	docker run --rm --entrypoint virtual-score-challenger-report schurfer-analytics:ci --help
	docker run --rm --entrypoint candle-anomaly-report schurfer-analytics:ci --help
	docker run --rm --entrypoint derivatives-context-report schurfer-analytics:ci --help
	docker run --rm --entrypoint decision-quality-report schurfer-analytics:ci --help
	docker run --rm --entrypoint liquid-taker-report schurfer-analytics:ci --help
	docker run --rm --entrypoint long-horizon-report schurfer-analytics:ci --help
	docker run --rm --entrypoint open-ended-margin-report schurfer-analytics:ci --help
	docker run --rm --entrypoint pump-magnitude-report schurfer-analytics:ci --help
	docker run --rm --entrypoint maker-entry-report schurfer-analytics:ci --help
	docker run --rm --entrypoint orderflow-pilot-report schurfer-analytics:ci --help
	docker run --rm --entrypoint exit-liquidity-calibration-report schurfer-analytics:ci --help
	docker run --rm --entrypoint pump-short-failure-attribution-report schurfer-analytics:ci --help
	docker run --rm --entrypoint pump-short-reentry-audit-report schurfer-analytics:ci --help
	docker run --rm --entrypoint oi-growth-filter-report schurfer-analytics:ci --help
	docker run --rm --entrypoint token-history-identity-preflight-report schurfer-analytics:ci --help
	docker run --rm --entrypoint token-history-ohlcv-sample-report schurfer-analytics:ci --help
	docker run --rm --entrypoint token-history-parquet-dataset schurfer-analytics:ci --help
	@docker rmi schurfer-analytics:ci --force > /dev/null
	@echo "=== Docker: execution build + import check ==="
	docker build -f apps/execution/Dockerfile -t schurfer-execution:ci . -q
	docker run --rm --entrypoint python schurfer-execution:ci -c "import schurfer_execution; print('ok')"
	@docker rmi schurfer-execution:ci --force > /dev/null
	@echo "=== verify-docker passed ==="
