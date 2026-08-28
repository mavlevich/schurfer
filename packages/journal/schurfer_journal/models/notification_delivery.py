from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class NotificationDelivery(Base):
    """Audit and idempotency state for one unified notification."""

    __tablename__ = "notification_deliveries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    notification_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    envelope_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    producer: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(256), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    stream_entry_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_enqueued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "envelope_version > 0",
            name="ck_notification_deliveries_envelope_version",
        ),
        CheckConstraint(
            "severity IN ('critical', 'warning', 'trade', 'research', 'info')",
            name="ck_notification_deliveries_severity",
        ),
        CheckConstraint(
            "channel = 'telegram'",
            name="ck_notification_deliveries_channel",
        ),
        CheckConstraint(
            "status IN ('pending', 'delivered', 'failed')",
            name="ck_notification_deliveries_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_notification_deliveries_attempt_count",
        ),
        CheckConstraint(
            "octet_length(payload_hash) = 32",
            name="ck_notification_deliveries_payload_hash",
        ),
        CheckConstraint(
            "(attempt_count = 0 AND last_attempted_at IS NULL) OR "
            "(attempt_count > 0 AND last_attempted_at IS NOT NULL)",
            name="ck_notification_deliveries_attempt_state",
        ),
        CheckConstraint(
            "(status = 'pending' AND delivered_at IS NULL) OR "
            "(status = 'delivered' AND delivered_at IS NOT NULL AND last_error IS NULL) OR "
            "(status = 'failed' AND delivered_at IS NULL AND "
            "last_error IS NOT NULL AND length(last_error) > 0)",
            name="ck_notification_deliveries_completion",
        ),
        UniqueConstraint(
            "notification_id",
            name="uq_notification_deliveries_notification_id",
        ),
        UniqueConstraint(
            "producer",
            "dedup_key",
            name="uq_notification_deliveries_producer_dedup",
        ),
        Index("ix_notification_deliveries_status_enqueued", "status", "first_enqueued_at"),
        Index("ix_notification_deliveries_kind_created", "kind", "created_at"),
        {"schema": "app"},
    )
