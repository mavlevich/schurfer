"""add bid-side columns to trade exit liquidity observations

Revision ID: 0031
Revises: 0030
Create Date: 2026-08-21

The table only recorded the ask side (correct for the short-only strategy it
was built for). A LONG position's exit prices off the bid side instead --
these columns let that observation be recorded without losing the ask-side
reading, which stays useful as context either way.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "trade_exit_liquidity_observations",
        sa.Column("bid_vwap", sa.Numeric(30, 14), nullable=True),
        schema="app",
    )
    op.add_column(
        "trade_exit_liquidity_observations",
        sa.Column("bid_impact_bps", sa.Numeric(12, 4), nullable=True),
        schema="app",
    )


def downgrade() -> None:
    op.drop_column("trade_exit_liquidity_observations", "bid_impact_bps", schema="app")
    op.drop_column("trade_exit_liquidity_observations", "bid_vwap", schema="app")
