.PHONY: help install install-golangci-lint dev dev-init dev-stop dev-reset dev-logs dev-test migrate measurement-report exchange-coverage-report exchange-source-economics-report source-lead-report episode-replay virtual-strategy-report virtual-entry-challenger-report virtual-threshold-challenger-report virtual-exit-policy-report virtual-exit-discovery-report virtual-score-challenger-report candle-anomaly-report derivatives-context-report decision-quality-report liquid-taker-report long-horizon-report maker-entry-report pump-magnitude-report orderflow-pilot-report exit-liquidity-calibration-report orderflow-start orderflow-stop orderflow-health test lint ci-lint format clean security deadcode check verify verify-docker \
        prod-deploy prod-runtime-metrics-install prod-runtime-metrics-health prod-measurement-report prod-exchange-coverage-report prod-exchange-source-economics-report prod-source-lead-report prod-episode-replay prod-virtual-strategy-report prod-virtual-entry-challenger-report prod-virtual-threshold-challenger-report prod-virtual-exit-policy-report prod-virtual-exit-discovery-report prod-virtual-score-challenger-report prod-candle-anomaly-report prod-derivatives-context-report prod-decision-quality-report prod-liquid-taker-report prod-long-horizon-report prod-maker-entry-report prod-pump-magnitude-report prod-orderflow-pilot-report prod-exit-liquidity-calibration-report prod-orderflow-start prod-orderflow-stop prod-orderflow-health prod-logs prod-backup prod-restore-local prod-health

GOLANGCI_LINT_VERSION = v2.1.6
PROD_REPORT_MIN_HEADROOM_MB ?= 1280
PROD_ORDERFLOW_MIN_AVAILABLE_MB ?= 768
PROD_ORDERFLOW_MIN_DISK_MB ?= 15360

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
	@echo "  make liquid-taker-report  Replay prospective low-impact taker shelf"
	@echo "  make liquid-taker-wider-stop-report  Compare prospective fixed-risk wider stop"
	@echo "  make long-horizon-report  Describe 24h/72h/7d returns and signed funding"
	@echo "  make maker-entry-report  Estimate the maker-entry OHLCV upper bound"
	@echo "  make pump-magnitude-report  Explore 20% to 200% pump entry floors"
	@echo "  make orderflow-pilot-report  Analyze bounded Bybit event/control captures"
	@echo "  make exit-liquidity-calibration-report  Compare modeled and close-time exit quotes"
	@echo "  make orderflow-start  Start the bounded local Bybit order-flow pilot"
	@echo "  make orderflow-health  Show local order-flow pilot health"
	@echo "  make orderflow-stop  Stop the local order-flow pilot"
	@echo ""
	@echo "Production (run on server with .env.prod present):"
	@echo "  make prod-deploy          Pull + rebuild + restart all services"
	@echo "  make prod-runtime-metrics-install  Install host container-metrics service"
	@echo "  make prod-runtime-metrics-health   Inspect host container-metrics service"
	@echo "  make prod-logs            Tail production service logs"
	@echo "  make prod-backup          Run database backup now"
	@echo "  make prod-restore-local   Download latest prod backup → local dev DB"
	@echo "  make prod-health          Show container status"
	@echo "  make prod-measurement-report  Read-only production report (ARGS='...')"
	@echo "  make prod-exchange-coverage-report  Production exchange source report"
	@echo "  make prod-exchange-source-economics-report  Production source economics replay"
	@echo "  make prod-source-lead-report  Production source-lead long screen"
	@echo "  make prod-episode-replay  Production replay-input readiness report"
	@echo "  make prod-virtual-strategy-report  Production pump-short v1 replay"
	@echo "  make prod-virtual-entry-challenger-report  Production entry challenger replay"
	@echo "  make prod-virtual-threshold-challenger-report  Production entry-floor replay"
	@echo "  make prod-virtual-exit-policy-report  Production exit-policy replay"
	@echo "  make prod-virtual-exit-discovery-report  Production exit discovery replay"
	@echo "  make prod-virtual-score-challenger-report  Production score-threshold replay"
	@echo "  make prod-candle-anomaly-report  Production candle anomaly research report"
	@echo "  make prod-derivatives-context-report  Production derivatives coverage probe"
	@echo "  make prod-decision-quality-report  Production score diagnostics"
	@echo "  make prod-liquid-taker-report  Production low-impact taker replay"
	@echo "  make prod-liquid-taker-wider-stop-report  Production wider-stop shadow replay"
	@echo "  make prod-long-horizon-report  Production long-horizon funding research"
	@echo "  make prod-maker-entry-report  Production maker-entry upper-bound report"
	@echo "  make prod-pump-magnitude-report  Production pump-magnitude discovery surface"
	@echo "  make prod-orderflow-pilot-report  Production Bybit order-flow pilot report"
	@echo "  make prod-exit-liquidity-calibration-report  Production exit quote calibration"
	@echo "  make prod-orderflow-start  Explicitly start the bounded order-flow trial"
	@echo "  make prod-orderflow-health  Show order-flow trial health and resource use"
	@echo "  make prod-orderflow-stop  Stop the order-flow trial"

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

orderflow-pilot-report:
	@uv run --package schurfer-analytics orderflow-pilot-report \
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
		deadcode ./...; \
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
	uv run --extra dev ruff check apps/analytics apps/execution packages
	MYPYPATH=apps/analytics:packages/journal:packages/performance uv run --extra dev --with sqlalchemy --with psycopg mypy apps/analytics/schurfer_analytics apps/analytics/tests packages/journal/schurfer_journal packages/performance/schurfer_performance
	MYPYPATH=packages/performance uv run --extra dev --all-packages mypy apps/execution/schurfer_execution
	uv run --extra dev --with ccxt --with greenlet --with redis --with sqlalchemy --with structlog --with "psycopg[binary]" pytest apps/analytics -q
	uv run --extra dev --with sqlalchemy --with alembic --with "psycopg[binary]" pytest packages/journal packages/performance -q
	uv run --extra dev --all-packages pytest apps/execution/tests -q
	@echo "=== [4/6] Go: test + vet ==="
	go test ./apps/api-gateway/... ./apps/collector/... ./apps/notifier/...
	go vet ./apps/api-gateway/... ./apps/collector/... ./apps/notifier/...
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

prod-exit-liquidity-calibration-report:
	@test -f .env.prod || (echo "ERROR: .env.prod not found. Copy .env.prod.example and fill in." && exit 1)
	@$(_PROD) run --rm --no-deps --entrypoint exit-liquidity-calibration-report analytics \
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

verify-docker: verify
	@echo "=== Docker: analytics build + import check ==="
	docker build -f apps/analytics/Dockerfile -t schurfer-analytics:ci . -q
	docker run --rm --entrypoint python schurfer-analytics:ci -c "import schurfer_analytics; print('ok')"
	docker run --rm --entrypoint outcome-resolver schurfer-analytics:ci --help
	docker run --rm --entrypoint measurement-report schurfer-analytics:ci --help
	docker run --rm --entrypoint exchange-coverage-report schurfer-analytics:ci --help
	docker run --rm --entrypoint exchange-source-economics-report schurfer-analytics:ci --help
	docker run --rm --entrypoint source-lead-report schurfer-analytics:ci --help
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
	docker run --rm --entrypoint pump-magnitude-report schurfer-analytics:ci --help
	docker run --rm --entrypoint maker-entry-report schurfer-analytics:ci --help
	docker run --rm --entrypoint orderflow-pilot-report schurfer-analytics:ci --help
	docker run --rm --entrypoint exit-liquidity-calibration-report schurfer-analytics:ci --help
	@docker rmi schurfer-analytics:ci --force > /dev/null
	@echo "=== Docker: execution build + import check ==="
	docker build -f apps/execution/Dockerfile -t schurfer-execution:ci . -q
	docker run --rm --entrypoint python schurfer-execution:ci -c "import schurfer_execution; print('ok')"
	@docker rmi schurfer-execution:ci --force > /dev/null
	@echo "=== verify-docker passed ==="
