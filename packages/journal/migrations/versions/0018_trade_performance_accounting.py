"""add versioned trade performance accounting

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "trades",
        sa.Column("slippage_usd", sa.Numeric(18, 4), nullable=True),
        schema="app",
    )
    op.add_column(
        "trades",
        sa.Column("gross_pnl_usd", sa.Numeric(18, 4), nullable=True),
        schema="app",
    )
    op.add_column(
        "trades",
        sa.Column("gross_pnl_pct", sa.Numeric(10, 4), nullable=True),
        schema="app",
    )
    op.add_column(
        "trades",
        sa.Column("net_pnl_usd", sa.Numeric(18, 4), nullable=True),
        schema="app",
    )
    op.add_column(
        "trades",
        sa.Column("net_pnl_pct", sa.Numeric(10, 4), nullable=True),
        schema="app",
    )
    op.add_column(
        "trades",
        sa.Column(
            "accounting_version",
            sa.String(40),
            nullable=False,
            server_default="legacy_price_only_v1",
        ),
        schema="app",
    )
    op.add_column(
        "trades",
        sa.Column(
            "accounting_status",
            sa.String(16),
            nullable=False,
            server_default="legacy",
        ),
        schema="app",
    )
    op.add_column(
        "trades",
        sa.Column("accounting_error", sa.Text(), nullable=True),
        schema="app",
    )
    op.execute(
        """
        UPDATE app.trades
        SET gross_pnl_usd = pnl_usd,
            gross_pnl_pct = pnl_pct
        WHERE status = 'closed'
        """
    )


def downgrade() -> None:
    op.drop_column("trades", "accounting_error", schema="app")
    op.drop_column("trades", "accounting_status", schema="app")
    op.drop_column("trades", "accounting_version", schema="app")
    op.drop_column("trades", "net_pnl_pct", schema="app")
    op.drop_column("trades", "net_pnl_usd", schema="app")
    op.drop_column("trades", "gross_pnl_pct", schema="app")
    op.drop_column("trades", "gross_pnl_usd", schema="app")
    op.drop_column("trades", "slippage_usd", schema="app")
