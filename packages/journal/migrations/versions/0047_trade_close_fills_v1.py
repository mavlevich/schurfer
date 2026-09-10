"""preserve partial live close fills until terminal accounting

Revision ID: 0047
Revises: 0046
Create Date: 2026-09-09

A live position can require more than one reduce-only order to close.  The
trade row has only one exit_price, so recording only the last order silently
loses earlier partial fills.  This append-only table keeps every confirmed
close leg and lets the execution service use their amount-weighted price when
the exchange position finally reaches zero.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "live_order_attempts",
        sa.Column(
            "operation",
            sa.String(length=8),
            nullable=False,
            server_default="entry",
        ),
        schema="app",
    )
    op.create_check_constraint(
        "ck_live_order_attempts_operation",
        "live_order_attempts",
        "operation IN ('entry', 'close')",
        schema="app",
    )
    op.create_index(
        "ix_live_order_attempts_operation_status",
        "live_order_attempts",
        ["operation", "status"],
        unique=False,
        schema="app",
    )
    op.execute("ALTER TABLE app.live_order_attempts DROP CONSTRAINT ck_live_order_attempts_status")
    op.execute(
        "ALTER TABLE app.live_order_attempts ADD CONSTRAINT ck_live_order_attempts_status "
        "CHECK (status IN ('pending', 'accepted', 'partial', 'completed', 'failed', "
        "'submission_unknown', 'no_fill', 'manual_required'))"
    )
    op.create_table(
        "trade_close_fills",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("trade_id", sa.BigInteger(), nullable=False),
        sa.Column("exchange", sa.String(length=32), nullable=False),
        sa.Column("order_id", sa.String(length=128), nullable=False),
        sa.Column("fill_price", sa.Numeric(30, 14), nullable=False),
        sa.Column("filled_amount", sa.Numeric(30, 14), nullable=False),
        sa.Column("requested_amount", sa.Numeric(30, 14), nullable=False),
        sa.Column("remaining_amount", sa.Numeric(30, 14), nullable=False),
        sa.Column("terminal", sa.Boolean(), nullable=False),
        sa.Column("fill_source", sa.String(length=64), nullable=False),
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
        sa.CheckConstraint("fill_price > 0", name="ck_trade_close_fills_price_positive"),
        sa.CheckConstraint("filled_amount > 0", name="ck_trade_close_fills_amount_positive"),
        sa.CheckConstraint("requested_amount > 0", name="ck_trade_close_fills_requested_positive"),
        sa.CheckConstraint(
            "remaining_amount >= 0", name="ck_trade_close_fills_remaining_non_negative"
        ),
        sa.ForeignKeyConstraint(["trade_id"], ["app.trades.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        schema="app",
    )
    op.create_index(
        "ux_trade_close_fills_exchange_order_id",
        "trade_close_fills",
        ["exchange", "order_id"],
        unique=True,
        schema="app",
    )
    op.create_index(
        "ix_trade_close_fills_trade_id",
        "trade_close_fills",
        ["trade_id"],
        unique=False,
        schema="app",
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM app.live_order_attempts WHERE operation = 'close'
            ) OR EXISTS (
                SELECT 1 FROM app.trade_close_fills
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade 0047 with durable close lifecycle evidence';
            END IF;
        END $$
        """
    )
    op.drop_table("trade_close_fills", schema="app")
    op.drop_index(
        "ix_live_order_attempts_operation_status",
        table_name="live_order_attempts",
        schema="app",
    )
    op.drop_constraint(
        "ck_live_order_attempts_operation",
        "live_order_attempts",
        type_="check",
        schema="app",
    )
    op.execute("ALTER TABLE app.live_order_attempts DROP CONSTRAINT ck_live_order_attempts_status")
    op.execute(
        "ALTER TABLE app.live_order_attempts ADD CONSTRAINT ck_live_order_attempts_status "
        "CHECK (status IN ('pending', 'accepted', 'completed', 'failed', "
        "'submission_unknown', 'no_fill', 'manual_required'))"
    )
    op.drop_column("live_order_attempts", "operation", schema="app")
