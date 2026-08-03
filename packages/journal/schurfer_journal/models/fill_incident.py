from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin

if TYPE_CHECKING:
    from .trade import Trade


class FillResolutionIncident(Base, TimestampMixin):
    """Durable record of an exchange order whose fill price could not be
    confirmed by resolve_fill_price. Never fabricates a price — this row exists
    so the position/order is tracked until a real price is confirmed or a human
    takes over."""

    __tablename__ = "fill_resolution_incidents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    base: Mapped[str] = mapped_column(String(64), nullable=False)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trade_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("app.trades.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 14), nullable=True)
    resolved_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recovery_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Snapshot of whatever the caller needs to finish the operation once a price
    # is confirmed (e.g. leverage/size/setup_context for an open, reason for a
    # close). Never read for anything except completing this exact incident.
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    trade: Mapped["Trade | None"] = relationship("Trade")

    __table_args__ = (
        Index(
            "ux_fill_resolution_incidents_exchange_order",
            "exchange",
            "order_id",
            unique=True,
        ),
        Index("ix_fill_resolution_incidents_status", "status"),
        CheckConstraint(
            "operation IN ('open', 'close')",
            name="ck_fill_resolution_incidents_operation",
        ),
        CheckConstraint(
            "status IN ('pending', 'resolving', 'resolved', 'manual_required')",
            name="ck_fill_resolution_incidents_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_fill_resolution_incidents_attempt_count",
        ),
        CheckConstraint(
            "(status IN ('pending', 'resolving', 'manual_required') "
            " AND resolved_at IS NULL AND resolved_price IS NULL AND resolved_source IS NULL) "
            "OR "
            "(status = 'resolved' "
            " AND resolved_at IS NOT NULL AND resolved_price IS NOT NULL "
            " AND resolved_source IS NOT NULL)",
            name="ck_fill_resolution_incidents_resolution",
        ),
        {"schema": "app"},
    )
