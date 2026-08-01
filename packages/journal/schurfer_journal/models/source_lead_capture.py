from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin

if TYPE_CHECKING:
    from .pump_event import PumpEvent


class SourceLeadCapture(Base, TimestampMixin):
    """Immutable point-in-time classification of one observed source lead."""

    __tablename__ = "source_lead_captures"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("app.pump_events.id", ondelete="CASCADE"),
        nullable=False,
    )
    capture_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    base: Mapped[str] = mapped_column(String(64), nullable=False)
    source_symbol: Mapped[str] = mapped_column(String(128), nullable=False)
    source_identity_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_market_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_occurred_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_first_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    collector_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    capture_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    capture_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    eligibility_reason: Mapped[str] = mapped_column(String(64), nullable=False)
    source_change_pct: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    source_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 14), nullable=True)
    source_volume_24h_usd: Mapped[Decimal | None] = mapped_column(Numeric(24, 4), nullable=True)
    first_sources: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    source_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    event: Mapped["PumpEvent"] = relationship("PumpEvent")
    targets: Mapped[list["SourceLeadTargetObservation"]] = relationship(
        "SourceLeadTargetObservation",
        back_populates="capture",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index(
            "ux_source_lead_captures_event_version",
            "event_id",
            "capture_version",
            unique=True,
        ),
        Index("ix_source_lead_captures_observed", "source_first_observed_at"),
        Index("ix_source_lead_captures_status", "status"),
        CheckConstraint(
            "status IN ('collecting', 'complete', 'excluded', 'abandoned')",
            name="ck_source_lead_captures_status",
        ),
        CheckConstraint(
            "source_change_pct >= -5000 AND source_change_pct <= 5000",
            name="ck_source_lead_captures_change",
        ),
        CheckConstraint(
            "(status = 'collecting' AND capture_completed_at IS NULL) OR "
            "(status <> 'collecting' AND capture_completed_at IS NOT NULL)",
            name="ck_source_lead_captures_completion",
        ),
        {"schema": "app"},
    )


class SourceLeadTargetObservation(Base, TimestampMixin):
    """One bounded target-venue quote attempt made after a source lead."""

    __tablename__ = "source_lead_target_observations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    capture_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("app.source_lead_captures.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    eligibility_reason: Mapped[str] = mapped_column(String(64), nullable=False)
    identity_match_method: Mapped[str] = mapped_column(String(32), nullable=False)
    identity_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latency_ms: Mapped[int] = mapped_column(nullable=False)
    requested_notional_usd: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    instrument: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    ticker: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    liquidity: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    capture: Mapped["SourceLeadCapture"] = relationship(
        "SourceLeadCapture", back_populates="targets"
    )

    __table_args__ = (
        Index(
            "ux_source_lead_target_capture_exchange",
            "capture_id",
            "target_exchange",
            unique=True,
        ),
        Index("ix_source_lead_target_observed", "observed_at"),
        Index("ix_source_lead_target_status", "status"),
        CheckConstraint("latency_ms >= 0", name="ck_source_lead_target_latency"),
        CheckConstraint(
            "requested_notional_usd > 0",
            name="ck_source_lead_target_notional",
        ),
        CheckConstraint(
            "status IN ('sampled', 'excluded', 'fetch_failed')",
            name="ck_source_lead_target_status",
        ),
        CheckConstraint(
            "NOT (identity_match_method = 'base_symbol_v1' AND identity_verified)",
            name="ck_source_lead_target_provisional_identity",
        ),
        {"schema": "app"},
    )
