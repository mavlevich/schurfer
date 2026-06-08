.PHONY: help install dev dev-stop dev-reset dev-logs dev-test test lint format clean security deadcode check

help:
	@echo "Schurfer - common commands"
	@echo ""
	@echo "  make install    Install all dependencies"
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

dev:
	@echo "-> Starting Docker Compose..."
	docker compose -f infra/docker/docker-compose.dev.yml up -d
	@echo "-> Waiting for services..."
	@docker compose -f infra/docker/docker-compose.dev.yml exec -T postgres pg_isready -U schurfer -q && echo "  postgres: ready" || echo "  postgres: starting..."
	@docker compose -f infra/docker/docker-compose.dev.yml exec -T redis redis-cli ping -q && echo "  redis: ready" || echo "  redis: starting..."
	@echo "-> Services up: postgres (5432), redis (6379), nats (4222)"

dev-stop:
	docker compose -f infra/docker/docker-compose.dev.yml down

dev-reset:
	docker compose -f infra/docker/docker-compose.dev.yml down -v
	@echo "-> All data volumes removed"

dev-logs:
	docker compose -f infra/docker/docker-compose.dev.yml logs -f

dev-test:
	@bash infra/docker/smoke-test.sh

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
	uv run ruff format .
	@echo "-> Ruff fix..."
	uv run ruff check --fix .
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
