.PHONY: help install dev test lint format clean

help:
	@echo "Schurfer - common commands"
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
