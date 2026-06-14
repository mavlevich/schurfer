"""add pump_events table

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pump_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("base", sa.String(20), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("peak_pct", sa.Double(), nullable=False),
        sa.Column("last_pct", sa.Double(), nullable=False),
        sa.Column("exchanges", JSONB(), nullable=False, server_default="[]"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("base", name="uq_pump_events_base"),
        schema="app",
    )
    op.create_index(
        "ix_pump_events_history",
        "pump_events",
        ["last_seen_at", "peak_pct"],
        schema="app",
        postgresql_ops={"last_seen_at": "DESC", "peak_pct": "DESC"},
    )


def downgrade() -> None:
    op.drop_index("ix_pump_events_history", table_name="pump_events", schema="app")
    op.drop_table("pump_events", schema="app")
