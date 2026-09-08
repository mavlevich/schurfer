FROM python:3.13-slim AS builder

WORKDIR /app

RUN pip install uv

# Copy workspace root and both runtime workspace packages. Analytics imports the
# journal's SQLAlchemy tables so the runtime query layer and Alembic share one schema.
COPY pyproject.toml uv.lock ./
COPY apps/analytics/ ./apps/analytics/
COPY packages/journal/ ./packages/journal/
COPY packages/performance/ ./packages/performance/

# Install only the analytics package and its dependencies (no dev extras).
# --no-editable builds a real wheel so the module exists inside .venv
# rather than as an editable pointer to the source tree (which isn't in the final image).
RUN uv sync --package schurfer-analytics --no-dev --frozen --no-editable

FROM python:3.13-slim

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH"

# Bake DuckDB's postgres extension into the image. `INSTALL postgres` downloads
# it from DuckDB's extension repository on first use, and the cold-bar export
# runs in `docker compose run --rm` containers whose home directory does not
# survive the run -- so without this every scheduled export would re-download
# it, and an export that exists to stop us losing history would depend on a
# third-party endpoint being up at 04:00.
ENV DUCKDB_HOME=/app/.duckdb
RUN mkdir -p "$DUCKDB_HOME" \
    && HOME=/app python -c "import duckdb; duckdb.connect().execute('INSTALL postgres')"

ENTRYPOINT ["pump-scanner"]
