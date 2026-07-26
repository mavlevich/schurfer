from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    DateTime,
    Double,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .pump_event import PumpEvent


class PumpAlertDelivery(Base):
    """A successfully delivered point-in-time pump notification."""

    __tablename__ = "pump_alert_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "channel",
            "alert_kind",
            "threshold_pct",
            name="uq_pump_alert_delivery_event_channel_kind_threshold",
        ),
        {"schema": "app"},
    )

    id: Mapped[int] = mapped_column(BigInteger(), primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        BigInteger(), ForeignKey("app.pump_events.id", ondelete="CASCADE"), nullable=False
    )
    base: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange: Mapped[str | None] = mapped_column(String(32), nullable=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    alert_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_version: Mapped[int] = mapped_column(Integer(), nullable=False, default=1)
    threshold_pct: Mapped[float] = mapped_column(Double(), nullable=False)
    observed_change_pct: Mapped[float] = mapped_column(Double(), nullable=False)
    exchange_24h_high_pct: Mapped[float | None] = mapped_column(Double(), nullable=True)
    ticker_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scanner_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scan_published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    notification_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    notification_sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    event: Mapped["PumpEvent"] = relationship("PumpEvent", back_populates="alert_deliveries")
