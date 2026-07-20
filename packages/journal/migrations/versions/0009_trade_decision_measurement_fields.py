"""add measurement fields to trade_decisions

Adds decision_id, strategy_version, features, and a liquidity snapshot so every
decision (taken or skipped) carries the full decision context and the order-book
state at decision time. Liquidity is the one piece that cannot be reconstructed
from historical OHLCV later, so it is captured live.

decision_id is unique so a retried write (writer reconnect after an ambiguous
commit) cannot create a duplicate decision row.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "trade_decisions",
        sa.Column("decision_id", postgresql.UUID(as_uuid=False), nullable=True),
        schema="app",
    )
    op.add_column(
        "trade_decisions",
        sa.Column("strategy_version", sa.String(32), nullable=True),
        schema="app",
    )
    op.add_column(
        "trade_decisions",
        sa.Column("features", postgresql.JSONB(), nullable=True),
        schema="app",
    )
    op.add_column(
        "trade_decisions",
        sa.Column("liquidity", postgresql.JSONB(), nullable=True),
        schema="app",
    )
    # Unique so a retry after an ambiguous commit cannot duplicate a decision.
    # NULLs are distinct in Postgres, so pre-existing rows without a decision_id
    # are unaffected.
    op.create_index(
        "uq_trade_decisions_decision_id",
        "trade_decisions",
        ["decision_id"],
        unique=True,
        schema="app",
    )


def downgrade() -> None:
    op.drop_index("uq_trade_decisions_decision_id", table_name="trade_decisions", schema="app")
    op.drop_column("trade_decisions", "liquidity", schema="app")
    op.drop_column("trade_decisions", "features", schema="app")
    op.drop_column("trade_decisions", "strategy_version", schema="app")
    op.drop_column("trade_decisions", "decision_id", schema="app")
