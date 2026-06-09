"""create journal tables

Revision ID: 0001
Revises:
Create Date: 2026-06-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Ensure app schema exists (also created in init-db.sql, idempotent)
    op.execute("CREATE SCHEMA IF NOT EXISTS app")

    op.create_table(
        "strategies",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("version", sa.String(16), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", "version", name="ix_strategies_name_version"),
        schema="app",
    )

    op.create_table(
        "alerts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("strategy_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("exchange", sa.String(32), nullable=False),
        sa.Column("market_type", sa.String(16), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("setup_context", JSONB(), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["strategy_id"], ["app.strategies.id"]),
        sa.PrimaryKeyConstraint("id"),
        schema="app",
    )
    op.create_index("ix_alerts_strategy_id", "alerts", ["strategy_id"], schema="app")
    op.create_index("ix_alerts_symbol", "alerts", ["symbol"], schema="app")
    op.create_index("ix_alerts_status", "alerts", ["status"], schema="app")

    op.create_table(
        "trades",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("strategy_id", sa.BigInteger(), nullable=False),
        sa.Column("alert_id", sa.BigInteger(), nullable=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("exchange", sa.String(32), nullable=False),
        sa.Column("market_type", sa.String(16), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("entry_order_id", sa.String(64), nullable=True),
        sa.Column("exit_order_id", sa.String(64), nullable=True),
        sa.Column("size_usd", sa.Numeric(18, 4), nullable=False),
        sa.Column("leverage", sa.Numeric(6, 2), nullable=False, server_default="1"),
        sa.Column("entry_price", sa.Numeric(18, 8), nullable=False),
        sa.Column("entry_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_slippage_bps", sa.Numeric(10, 4), nullable=True),
        sa.Column("exit_price", sa.Numeric(18, 8), nullable=True),
        sa.Column("exit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_slippage_bps", sa.Numeric(10, 4), nullable=True),
        sa.Column("fees_usd", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("funding_usd", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("pnl_usd", sa.Numeric(18, 4), nullable=True),
        sa.Column("pnl_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("outcome_label", sa.String(16), nullable=True),
        sa.Column("outcome_quality", sa.String(32), nullable=True),
        sa.Column("setup_context", JSONB(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["strategy_id"], ["app.strategies.id"]),
        sa.ForeignKeyConstraint(["alert_id"], ["app.alerts.id"]),
        sa.PrimaryKeyConstraint("id"),
        schema="app",
    )
    op.create_index("ix_trades_strategy_id", "trades", ["strategy_id"], schema="app")
    op.create_index("ix_trades_symbol", "trades", ["symbol"], schema="app")
    op.create_index("ix_trades_status", "trades", ["status"], schema="app")
    op.create_index("ix_trades_entry_at", "trades", ["entry_at"], schema="app")
    op.create_index(
        "ix_trades_setup_context",
        "trades",
        ["setup_context"],
        schema="app",
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_table("trades", schema="app")
    op.drop_table("alerts", schema="app")
    op.drop_table("strategies", schema="app")
