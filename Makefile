.PHONY: help install dev dev-init dev-stop dev-reset dev-logs dev-test migrate test lint format clean security deadcode check verify verify-docker

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
	@echo "  make format     Format all code"
	@echo "  make security   Run security scans"
	@echo "  make deadcode   Detect unused code"
	@echo "  make clean      Clean build artifacts"
	@echo "  make check      Run lint + test + security (full CI locally)"
	@echo "  make verify     Pre-PR gate: lock, lint, types, tests, build"
	@echo "  make verify-docker  verify + analytics Docker import check"

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

migrate:
	@echo "-> Running Alembic migrations..."
	cd packages/journal && \
	DATABASE_URL=$$(grep DATABASE_URL ../../.env 2>/dev/null | cut -d= -f2 || echo "postgresql://schurfer:schurfer_dev@localhost:5432/schurfer") \
	uv run --package schurfer-journal alembic upgrade head

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
	@echo "=== [1/5] uv lock check ==="
	uv lock --check
	@echo "=== [2/5] Python: ruff + mypy + pytest ==="
	uv run --extra dev ruff check apps/analytics apps/execution packages
	MYPYPATH=apps/analytics:packages/journal uv run --extra dev --with sqlalchemy --with psycopg mypy apps/analytics/schurfer_analytics apps/analytics/tests packages/journal/schurfer_journal
	uv run --extra dev --all-packages mypy apps/execution/schurfer_execution
	uv run --extra dev --with ccxt --with redis --with structlog --with "psycopg[binary]" pytest apps/analytics -q
	uv run --extra dev --with sqlalchemy --with alembic --with "psycopg[binary]" pytest packages/journal -q
	uv run --extra dev --all-packages pytest apps/execution/tests -q
	@echo "=== [3/5] Go: test + vet ==="
	go test ./apps/api-gateway/... ./apps/collector/... ./apps/notifier/...
	go vet ./apps/api-gateway/... ./apps/collector/... ./apps/notifier/...
	@echo "=== [4/5] Web: lint + typecheck + build ==="
	pnpm --filter @schurfer/web lint
	pnpm --filter @schurfer/web typecheck
	pnpm --filter @schurfer/web build
	@echo "=== [5/5] Compose config ==="
	docker compose --env-file .env.ci -f infra/docker/docker-compose.dev.yml config --quiet
	@echo "=== verify passed ==="

verify-docker: verify
	@echo "=== Docker: analytics build + import check ==="
	docker build -f apps/analytics/Dockerfile -t schurfer-analytics:ci . -q
	docker run --rm --entrypoint python schurfer-analytics:ci -c "import schurfer_analytics; print('ok')"
	@docker rmi schurfer-analytics:ci --force > /dev/null
	@echo "=== Docker: execution build + import check ==="
	docker build -f apps/execution/Dockerfile -t schurfer-execution:ci . -q
	docker run --rm --entrypoint python schurfer-execution:ci -c "import schurfer_execution; print('ok')"
	@docker rmi schurfer-execution:ci --force > /dev/null
	@echo "=== verify-docker passed ==="
