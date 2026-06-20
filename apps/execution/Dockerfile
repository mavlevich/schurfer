FROM python:3.13-slim AS builder

WORKDIR /app

RUN pip install uv

COPY pyproject.toml uv.lock ./
COPY apps/execution/ ./apps/execution/

RUN uv sync --package schurfer-execution --no-dev --frozen --no-editable

FROM python:3.13-slim

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH"

ENTRYPOINT ["execution-service"]
