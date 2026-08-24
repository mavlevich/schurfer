"""generalize trade_decisions for non-pump_short shadow evidence

Adds strategy_id (FK to app.strategies, the same registry trades.strategy_id
already points at), trading_mode, and side to app.trade_decisions. All three
are nullable and additive-only: every existing pump_short row, and
pump_short's own ongoing write_decision calls, keep writing NULL for all
three -- pump_short already records its own decision evidence unconditionally
in trader.py and is not touched by this migration or by
feat/execution-shadow-evidence-v1's ShadowBroker (see execution_intent.py's
ShadowBroker docstring for why pump_short must never route through it).

strategy_id lets a shadow decision from early_momentum/liquidation_cascade
carry the same normalized (name, version) identity trades.strategy_id
already uses, instead of overloading the pump_short-shaped strategy_version
string column trade_decisions had before this. Its FK has no ondelete
clause (NO ACTION/RESTRICT), matching trades.strategy_id's own FK exactly
(see 0001_create_journal_tables.py) -- unlike pump_event_id's SET NULL,
canonical strategy identity on a decision must never be silently severed by
deleting the referenced app.strategies row.

side records the intent's direction (long/short) so a future directional
outcome resolver can compute correct return/MFE/MAE for a LONG shadow
decision -- outcome_repository.due_decisions_statement excludes
trading_mode='shadow' rows entirely for now (see that file's own comment):
compute_outcome's return/MFE/MAE math is short-only, and would silently
invert a LONG decision's evidence if it were resolved unchanged (colleague
review). This column exists so that fix doesn't need another migration.

Two CHECK constraints (mirroring notification_deliveries' own pattern):
trading_mode is restricted to the currently-legitimate closed set (today
just 'shadow' -- ShadowBroker is the only writer of a non-NULL value; add to
this constraint, in its own migration, when a future broker actually starts
writing a new value here), and strategy_id/trading_mode must be both NULL or
both set -- a partial write (e.g. ShadowBroker recording strategy_id=NULL
after a failed identity lookup) must never reach this table at all rather
than pass silently (colleague review; see ShadowBroker.open's own
strategy_id is None guard).

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
        # No ondelete -- matches trades.strategy_id's own FK (NO ACTION):
        # canonical strategy identity on a decision must block the delete,
        # never be silently severed to NULL (unlike pump_event_id above).
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
    op.add_column(
        "trade_decisions",
        sa.Column("side", sa.String(length=8), nullable=True),
        schema="app",
    )
    op.create_check_constraint(
        "ck_trade_decisions_trading_mode",
        "trade_decisions",
        "trading_mode IS NULL OR trading_mode IN ('shadow')",
        schema="app",
    )
    op.create_check_constraint(
        "ck_trade_decisions_strategy_identity_pair",
        "trade_decisions",
        "(strategy_id IS NULL AND trading_mode IS NULL) OR "
        "(strategy_id IS NOT NULL AND trading_mode IS NOT NULL)",
        schema="app",
    )


def downgrade() -> None:
    op.drop_constraint("ck_trade_decisions_strategy_identity_pair", "trade_decisions", schema="app")
    op.drop_constraint("ck_trade_decisions_trading_mode", "trade_decisions", schema="app")
    op.drop_column("trade_decisions", "side", schema="app")
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
