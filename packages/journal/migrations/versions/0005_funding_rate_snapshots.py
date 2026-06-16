"""funding rate snapshots per exchange, scoped to a pump episode

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "funding_rate_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "event_id",
            sa.BigInteger(),
            sa.ForeignKey("app.pump_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("base", sa.String(20), nullable=False),
        sa.Column("exchange", sa.String(20), nullable=False),
        sa.Column("rate", sa.Double(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="app",
    )
    # (event_id, exchange, recorded_at DESC) matches the DISTINCT ON (exchange)
    # query in the Funding handler: filters by event_id, groups by exchange,
    # picks the latest row per group — all without a seq scan.
    op.create_index(
        "ix_funding_rate_snapshots_event_id_exchange_recorded_at",
        "funding_rate_snapshots",
        ["event_id", "exchange", sa.text("recorded_at DESC")],
        schema="app",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_funding_rate_snapshots_event_id_exchange_recorded_at",
        table_name="funding_rate_snapshots",
        schema="app",
    )
    op.drop_table("funding_rate_snapshots", schema="app")
