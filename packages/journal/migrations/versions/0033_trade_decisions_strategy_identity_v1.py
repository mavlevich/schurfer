"""generalize trade_decisions for non-pump_short shadow evidence

Adds strategy_id (FK to app.strategies, the same registry trades.strategy_id
already points at) and trading_mode to app.trade_decisions. Both nullable and
additive-only: every existing pump_short row, and pump_short's own ongoing
write_decision calls, keep writing NULL for both -- pump_short already
records its own decision evidence unconditionally in trader.py and is not
touched by this migration or by feat/execution-shadow-evidence-v1's
ShadowBroker (see execution_intent.py's ShadowBroker docstring for why
pump_short must never route through it). strategy_id lets a shadow decision
from early_momentum/liquidation_cascade carry the same normalized (name,
version) identity trades.strategy_id already uses, instead of overloading
the pump_short-shaped strategy_version string column trade_decisions had
before this.

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "trade_decisions",
        sa.Column("strategy_id", sa.BigInteger(), nullable=True),
        schema="app",
    )
    op.create_foreign_key(
        "fk_trade_decisions_strategy_id",
        "trade_decisions",
        "strategies",
        ["strategy_id"],
        ["id"],
        source_schema="app",
        referent_schema="app",
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_trade_decisions_strategy_id",
        "trade_decisions",
        ["strategy_id"],
        schema="app",
    )
    op.add_column(
        "trade_decisions",
        sa.Column("trading_mode", sa.String(length=16), nullable=True),
        schema="app",
    )


def downgrade() -> None:
    op.drop_column("trade_decisions", "trading_mode", schema="app")
    op.drop_index(
        "ix_trade_decisions_strategy_id",
        table_name="trade_decisions",
        schema="app",
    )
    op.drop_constraint(
        "fk_trade_decisions_strategy_id",
        "trade_decisions",
        schema="app",
        type_="foreignkey",
    )
    op.drop_column("trade_decisions", "strategy_id", schema="app")
