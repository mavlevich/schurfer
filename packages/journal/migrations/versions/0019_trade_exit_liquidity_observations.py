"""add paper trade exit liquidity observations

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trade_exit_liquidity_observations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("trade_id", sa.BigInteger(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exchange", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("market_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("requested_notional_usd", sa.Numeric(18, 4), nullable=False),
        sa.Column("filled_notional_usd", sa.Numeric(18, 4), nullable=True),
        sa.Column("best_bid", sa.Numeric(30, 14), nullable=True),
        sa.Column("best_ask", sa.Numeric(30, 14), nullable=True),
        sa.Column("mid", sa.Numeric(30, 14), nullable=True),
        sa.Column("spread_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("ask_vwap", sa.Numeric(30, 14), nullable=True),
        sa.Column("ask_impact_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("contract_size", sa.Numeric(24, 12), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["trade_id"],
            ["app.trades.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="app",
    )
    op.create_index(
        "ux_trade_exit_liquidity_observations_trade_id",
        "trade_exit_liquidity_observations",
        ["trade_id"],
        unique=True,
        schema="app",
    )
    op.create_index(
        "ix_trade_exit_liquidity_observations_observed_at",
        "trade_exit_liquidity_observations",
        ["observed_at"],
        unique=False,
        schema="app",
    )
    op.create_index(
        "ix_trade_exit_liquidity_observations_status",
        "trade_exit_liquidity_observations",
        ["status"],
        unique=False,
        schema="app",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_trade_exit_liquidity_observations_status",
        table_name="trade_exit_liquidity_observations",
        schema="app",
    )
    op.drop_index(
        "ix_trade_exit_liquidity_observations_observed_at",
        table_name="trade_exit_liquidity_observations",
        schema="app",
    )
    op.drop_index(
        "ux_trade_exit_liquidity_observations_trade_id",
        table_name="trade_exit_liquidity_observations",
        schema="app",
    )
    op.drop_table("trade_exit_liquidity_observations", schema="app")
