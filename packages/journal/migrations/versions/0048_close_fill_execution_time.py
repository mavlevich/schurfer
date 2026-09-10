"""Preserve exchange execution time separately from journal write time.

Revision ID: 0048
Revises: 0047
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "trade_close_fills",
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        schema="app",
    )
    op.add_column(
        "trade_close_fills",
        sa.Column("execution_time_source", sa.String(length=64), nullable=True),
        schema="app",
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM app.trade_close_fills
                WHERE executed_at IS NOT NULL OR execution_time_source IS NOT NULL
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade 0048 with recorded close execution timestamps';
            END IF;
        END
        $$
        """
    )
    op.drop_column("trade_close_fills", "execution_time_source", schema="app")
    op.drop_column("trade_close_fills", "executed_at", schema="app")
