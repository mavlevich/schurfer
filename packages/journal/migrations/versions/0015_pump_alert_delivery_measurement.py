"""add durable pump alert delivery measurement

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pump_alert_deliveries",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "event_id",
            sa.BigInteger(),
            sa.ForeignKey("app.pump_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("base", sa.String(64), nullable=False),
        sa.Column("exchange", sa.String(32), nullable=True),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("alert_kind", sa.String(32), nullable=False),
        sa.Column("payload_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("threshold_pct", sa.Double(), nullable=False),
        sa.Column("observed_change_pct", sa.Double(), nullable=False),
        sa.Column("exchange_24h_high_pct", sa.Double(), nullable=True),
        sa.Column("ticker_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scanner_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scan_published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notification_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notification_sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_id",
            "channel",
            "alert_kind",
            "threshold_pct",
            name="uq_pump_alert_delivery_event_channel_kind_threshold",
        ),
        schema="app",
    )
    op.create_index(
        "ix_pump_alert_deliveries_sent_at",
        "pump_alert_deliveries",
        ["notification_sent_at"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("pump_alert_deliveries", schema="app")
