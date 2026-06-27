"""create trade_decisions table

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trade_decisions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("base", sa.String(32), nullable=False),
        sa.Column("exchange", sa.String(32), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("pump_pct", sa.Numeric(10, 2), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="app",
    )
    op.create_index("ix_trade_decisions_ts", "trade_decisions", ["ts"], schema="app")
    op.create_index(
        "ix_trade_decisions_base_ts",
        "trade_decisions",
        ["base", "ts"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_index("ix_trade_decisions_base_ts", table_name="trade_decisions", schema="app")
    op.drop_index("ix_trade_decisions_ts", table_name="trade_decisions", schema="app")
    op.drop_table("trade_decisions", schema="app")
