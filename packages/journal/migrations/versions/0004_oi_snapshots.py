"""open interest snapshots per exchange, scoped to a pump episode

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oi_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "event_id",
            sa.BigInteger(),
            sa.ForeignKey("app.pump_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("base", sa.String(20), nullable=False),
        sa.Column("exchange", sa.String(20), nullable=False),
        sa.Column("oi_usd", sa.Double(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="app",
    )
    op.create_index(
        "ix_oi_snapshots_event_id_recorded_at",
        "oi_snapshots",
        ["event_id", "recorded_at"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_index("ix_oi_snapshots_event_id_recorded_at", table_name="oi_snapshots", schema="app")
    op.drop_table("oi_snapshots", schema="app")
