"""prevent duplicate journal trades for the same exchange order

Adds a partial unique index on app.trades (exchange, entry_order_id) WHERE
entry_order_id IS NOT NULL. journal.open_trade previously did a plain
INSERT: nothing stopped a retried/duplicated call for the same real
exchange order (a crash-recovery replay, a bug in a retry path) from
creating two open rows for what is actually one live position -- one of
them would then never be closeable against its real exchange counterpart.

Partial (not a plain unique constraint) because entry_order_id is NULL for
every paper trade (paper.py never calls journal.open_trade with a real
order id) -- Postgres never enforces uniqueness among NULLs anyway, but the
WHERE clause makes that exclusion explicit rather than incidental, and
lets journal.open_trade's INSERT ... ON CONFLICT (exchange, entry_order_id)
WHERE entry_order_id IS NOT NULL target this exact index.

Revision ID: 0034
Revises: 0033
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ux_trades_exchange_entry_order_id",
        "trades",
        ["exchange", "entry_order_id"],
        unique=True,
        schema="app",
        postgresql_where=sa.text("entry_order_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ux_trades_exchange_entry_order_id",
        table_name="trades",
        schema="app",
    )
