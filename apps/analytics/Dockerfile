FROM python:3.13-slim AS builder

WORKDIR /app

RUN pip install uv

# Copy workspace root and both runtime workspace packages. Analytics imports the
# journal's SQLAlchemy tables so the runtime query layer and Alembic share one schema.
COPY pyproject.toml uv.lock ./
COPY apps/analytics/ ./apps/analytics/
COPY packages/journal/ ./packages/journal/

# Install only the analytics package and its dependencies (no dev extras).
# --no-editable builds a real wheel so the module exists inside .venv
# rather than as an editable pointer to the source tree (which isn't in the final image).
RUN uv sync --package schurfer-analytics --no-dev --frozen --no-editable

FROM python:3.13-slim

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH"

ENTRYPOINT ["pump-scanner"]
