"""add durable per-exchange pump discovery attribution

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pump_event_sources",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "event_id",
            sa.BigInteger(),
            sa.ForeignKey("app.pump_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("exchange", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(128), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("first_change_pct", sa.Double(), nullable=False),
        sa.Column("last_change_pct", sa.Double(), nullable=False),
        sa.Column("peak_change_pct", sa.Double(), nullable=False),
        sa.Column("first_price", sa.Double(), nullable=True),
        sa.Column("last_price", sa.Double(), nullable=True),
        sa.Column("first_volume_24h_usd", sa.Double(), nullable=True),
        sa.Column("last_volume_24h_usd", sa.Double(), nullable=True),
        sa.Column("observation_count", sa.Integer(), server_default="1", nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "exchange", name="uq_pump_event_source_venue"),
        schema="app",
    )
    op.create_index(
        "ix_pump_event_sources_event_id",
        "pump_event_sources",
        ["event_id"],
        schema="app",
    )
    op.create_index(
        "ix_pump_event_sources_exchange_first_seen",
        "pump_event_sources",
        ["exchange", "first_seen_at"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("pump_event_sources", schema="app")
