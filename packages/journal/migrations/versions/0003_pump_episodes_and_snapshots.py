"""pump episodes: multi-episode support + retrace tracking + price snapshots

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Drop unique constraint on base — one token can now have many episodes
    op.drop_constraint("uq_pump_events_base", "pump_events", schema="app", type_="unique")

    # episode number per token (1-based, increments on each new pump cycle)
    op.add_column(
        "pump_events",
        sa.Column("episode", sa.Integer(), nullable=False, server_default="1"),
        schema="app",
    )

    # cooling: consecutive scans where the token was absent (reset to 0 when seen again)
    op.add_column(
        "pump_events",
        sa.Column("miss_count", sa.Integer(), nullable=False, server_default="0"),
        schema="app",
    )

    # retrace tracking: set when token drops below threshold or disappears
    op.add_column(
        "pump_events",
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        schema="app",
    )
    op.add_column(
        "pump_events",
        sa.Column("retrace_pct", sa.Double(), nullable=True),
        schema="app",
    )

    # composite unique: one active episode per token at a time
    op.create_unique_constraint(
        "uq_pump_events_active_episode",
        "pump_events",
        ["base", "episode"],
        schema="app",
    )

    # index for per-token history lookups
    op.create_index(
        "ix_pump_events_base_first_seen",
        "pump_events",
        ["base", "first_seen_at"],
        schema="app",
    )

    # index for finding open episodes quickly
    op.create_index(
        "ix_pump_events_open",
        "pump_events",
        ["base"],
        schema="app",
        postgresql_where=sa.text("closed_at IS NULL"),
    )

    # price snapshots at fixed intervals after first detection
    op.create_table(
        "pump_event_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "event_id",
            sa.BigInteger(),
            sa.ForeignKey("app.pump_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("offset_label", sa.String(8), nullable=False),  # '+1h', '+4h', '+24h'
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("price", sa.Double(), nullable=True),
        sa.Column("change_pct", sa.Double(), nullable=True),
        sa.Column("exchanges", JSONB(), nullable=False, server_default="[]"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "offset_label", name="uq_snapshot_event_offset"),
        schema="app",
    )
    op.create_index(
        "ix_pump_event_snapshots_event_id",
        "pump_event_snapshots",
        ["event_id"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("pump_event_snapshots", schema="app")
    op.drop_index("ix_pump_events_open", table_name="pump_events", schema="app")
    op.drop_index("ix_pump_events_base_first_seen", table_name="pump_events", schema="app")
    op.drop_constraint("uq_pump_events_active_episode", "pump_events", schema="app", type_="unique")
    op.drop_column("pump_events", "retrace_pct", schema="app")
    op.drop_column("pump_events", "closed_at", schema="app")
    op.drop_column("pump_events", "miss_count", schema="app")
    op.drop_column("pump_events", "episode", schema="app")
    op.create_unique_constraint("uq_pump_events_base", "pump_events", ["base"], schema="app")
