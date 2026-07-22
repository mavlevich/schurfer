"""add strategy-agnostic forward outcomes for trade decisions

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trade_decision_outcomes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("decision_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("horizon_minutes", sa.Integer(), nullable=False),
        sa.Column("resolver_version", sa.String(length=32), nullable=False),
        sa.Column("anchor_exchange", sa.String(length=32), nullable=True),
        sa.Column("source_exchange", sa.String(length=32), nullable=True),
        sa.Column("timeframe_minutes", sa.Integer(), nullable=False),
        sa.Column("entry_price", sa.Numeric(), nullable=True),
        sa.Column("forward_price", sa.Numeric(), nullable=True),
        sa.Column("mfe_pct", sa.Numeric(12, 6), nullable=True),
        sa.Column("mae_pct", sa.Numeric(12, 6), nullable=True),
        sa.Column("short_return_pct", sa.Numeric(12, 6), nullable=True),
        sa.Column("bars_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expected_bars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("coverage_ratio", sa.Numeric(8, 6), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("error", sa.Text(), nullable=True),
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
            ["decision_id"],
            ["app.trade_decisions.decision_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "decision_id",
            "horizon_minutes",
            "resolver_version",
            name="uq_trade_decision_outcomes_decision_horizon_version",
        ),
        schema="app",
    )
    op.create_index(
        "ix_trade_decision_outcomes_status_updated",
        "trade_decision_outcomes",
        ["status", "updated_at"],
        schema="app",
    )
    op.create_index(
        "ix_trade_decision_outcomes_horizon",
        "trade_decision_outcomes",
        ["horizon_minutes"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_trade_decision_outcomes_horizon",
        table_name="trade_decision_outcomes",
        schema="app",
    )
    op.drop_index(
        "ix_trade_decision_outcomes_status_updated",
        table_name="trade_decision_outcomes",
        schema="app",
    )
    op.drop_table("trade_decision_outcomes", schema="app")
