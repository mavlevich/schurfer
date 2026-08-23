FROM python:3.13-slim AS builder

WORKDIR /app

RUN pip install uv

COPY pyproject.toml uv.lock ./
COPY apps/execution/ ./apps/execution/
COPY packages/performance/ ./packages/performance/
COPY packages/market-quality/ ./packages/market-quality/

RUN uv sync --package schurfer-execution --no-dev --frozen --no-editable

FROM python:3.13-slim

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH"


# Fail the image build if the installed production package cannot import.
RUN python -c "import schurfer_execution.main"
ENTRYPOINT ["execution-service"]
