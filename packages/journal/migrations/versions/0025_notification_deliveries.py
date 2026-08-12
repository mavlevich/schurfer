"""add unified notification delivery audit

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-12

The Redis Stream is the durable delivery queue. This table is the audit and
idempotency boundary used by the notifier consumer. It intentionally stores a
SHA-256 payload hash instead of the Telegram text itself. The original payload
remains in the stream until the consumer has recorded a terminal result.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("notification_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("envelope_version", sa.SmallInteger(), nullable=False),
        sa.Column("producer", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("dedup_key", sa.String(length=256), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("stream_entry_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("first_enqueued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("payload_hash", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "envelope_version > 0",
            name="ck_notification_deliveries_envelope_version",
        ),
        sa.CheckConstraint(
            "severity IN ('critical', 'trade', 'research', 'info')",
            name="ck_notification_deliveries_severity",
        ),
        sa.CheckConstraint(
            "channel = 'telegram'",
            name="ck_notification_deliveries_channel",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'delivered', 'failed')",
            name="ck_notification_deliveries_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_notification_deliveries_attempt_count",
        ),
        sa.CheckConstraint(
            "octet_length(payload_hash) = 32",
            name="ck_notification_deliveries_payload_hash",
        ),
        sa.CheckConstraint(
            "(attempt_count = 0 AND last_attempted_at IS NULL) OR "
            "(attempt_count > 0 AND last_attempted_at IS NOT NULL)",
            name="ck_notification_deliveries_attempt_state",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND delivered_at IS NULL) OR "
            "(status = 'delivered' AND delivered_at IS NOT NULL AND last_error IS NULL) OR "
            "(status = 'failed' AND delivered_at IS NULL AND "
            "last_error IS NOT NULL AND length(last_error) > 0)",
            name="ck_notification_deliveries_completion",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "notification_id",
            name="uq_notification_deliveries_notification_id",
        ),
        sa.UniqueConstraint(
            "producer",
            "dedup_key",
            name="uq_notification_deliveries_producer_dedup",
        ),
        schema="app",
    )
    op.create_index(
        "ix_notification_deliveries_status_enqueued",
        "notification_deliveries",
        ["status", "first_enqueued_at"],
        schema="app",
    )
    op.create_index(
        "ix_notification_deliveries_kind_created",
        "notification_deliveries",
        ["kind", "created_at"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("notification_deliveries", schema="app")
