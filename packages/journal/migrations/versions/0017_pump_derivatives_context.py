"""add durable pump derivatives context

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pump_derivatives_context_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("exchange", sa.String(length=32), nullable=False),
        sa.Column("method", sa.String(length=40), nullable=False),
        sa.Column("capability", sa.String(length=64), nullable=False),
        sa.Column("declared_support", sa.String(length=16), nullable=False),
        sa.Column("resolver_version", sa.String(length=32), nullable=False),
        sa.Column("unified_symbol", sa.String(length=128), nullable=False),
        sa.Column("market_id", sa.String(length=128), nullable=True),
        sa.Column("identity_key", sa.String(length=512), nullable=True),
        sa.Column("anchor_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timeframe", sa.String(length=8), nullable=True),
        sa.Column("request_limit", sa.Integer(), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("returned_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "valid_timestamp_rows",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("in_window_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("invalid_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expected_rows", sa.Integer(), nullable=True),
        sa.Column("coverage_ratio", sa.Numeric(8, 6), nullable=True),
        sa.Column("covers_start", sa.Boolean(), nullable=True),
        sa.Column("covers_end", sa.Boolean(), nullable=True),
        sa.Column("missing_rows", sa.Integer(), nullable=True),
        sa.Column("duplicate_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_gap_minutes", sa.Numeric(12, 4), nullable=True),
        sa.Column(
            "pagination_exhausted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("ccxt_version", sa.String(length=32), nullable=False),
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["app.pump_events.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_id",
            "exchange",
            "method",
            "resolver_version",
            name="uq_pump_derivatives_context_run",
        ),
        schema="app",
    )
    op.create_index(
        "ix_pump_derivatives_context_runs_event",
        "pump_derivatives_context_runs",
        ["event_id"],
        schema="app",
    )
    op.create_index(
        "ix_pump_derivatives_context_runs_status_updated",
        "pump_derivatives_context_runs",
        ["status", "updated_at"],
        schema="app",
    )

    op.create_table(
        "pump_derivatives_context_samples",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("source_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sample_key", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["app.pump_derivatives_context_runs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "sample_key",
            name="uq_pump_derivatives_context_sample",
        ),
        schema="app",
    )
    op.create_index(
        "ix_pump_derivatives_context_samples_run_source",
        "pump_derivatives_context_samples",
        ["run_id", "source_at"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("pump_derivatives_context_samples", schema="app")
    op.drop_table("pump_derivatives_context_runs", schema="app")
