"""durable pre-flight record for a live order, before the exchange call

app.live_order_attempts is written BEFORE orders.place_order ever calls the
exchange, keyed by a locally-generated client_order_id that is then passed
to the exchange as clientOrderId (bybit: orderLinkId). If this durable
write itself fails (e.g. a full Postgres outage, not just one bad query),
place_order now fails closed and never calls the exchange at all -- so
there is no longer any window where a real exchange order can exist with
zero durable trace of it anywhere. Previously the only durable write was
attempted AFTER the exchange call (the journal write, or the incident
fallback on its failure), both against the same Postgres instance a total
outage would also take down (colleague review).

Revision ID: 0035
Revises: 0034
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "live_order_attempts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("client_order_id", sa.String(length=64), nullable=False),
        sa.Column("exchange", sa.String(length=32), nullable=False),
        sa.Column("base", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("size_usd", sa.Numeric(18, 4), nullable=False),
        sa.Column("leverage", sa.Numeric(6, 2), nullable=False),
        sa.Column("contract_size", sa.Numeric(18, 8), nullable=True),
        sa.Column("exit_params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("setup_context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("order_id", sa.String(length=128), nullable=True),
        sa.Column("filled_amount", sa.Numeric(18, 8), nullable=True),
        sa.Column("trade_id", sa.BigInteger(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'completed', 'failed')",
            name="ck_live_order_attempts_status",
        ),
        sa.ForeignKeyConstraint(["trade_id"], ["app.trades.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        schema="app",
    )
    op.create_index(
        "ux_live_order_attempts_client_order_id",
        "live_order_attempts",
        ["client_order_id"],
        unique=True,
        schema="app",
    )
    op.create_index(
        "ix_live_order_attempts_status",
        "live_order_attempts",
        ["status"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("live_order_attempts", schema="app")
