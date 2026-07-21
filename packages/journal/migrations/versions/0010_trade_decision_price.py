"""add decision-time price to trade_decisions

Records the reference price of the token at the moment a decision was made (the
chosen exchange's last price, or the top-moving exchange's when no exchange was
picked). Without it a decision cannot be scored later: the outcome of an entry or
a skip is measured against the price we saw when we decided, so it is the anchor
for any backtest of whether the strategy has an edge.

Nullable: pre-existing rows have no price, and a decision is still recorded even
if the price is missing or unparseable.

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "trade_decisions",
        sa.Column("price", sa.Numeric(), nullable=True),
        schema="app",
    )


def downgrade() -> None:
    op.drop_column("trade_decisions", "price", schema="app")
