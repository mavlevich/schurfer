from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin

if TYPE_CHECKING:
    from .pump_event import PumpEvent


class PumpDerivativesContextRun(Base, TimestampMixin):
    """One versioned attempt to recover a derivatives series around a pump."""

    __tablename__ = "pump_derivatives_context_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("app.pump_events.id", ondelete="CASCADE"),
        nullable=False,
    )
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    method: Mapped[str] = mapped_column(String(40), nullable=False)
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    declared_support: Mapped[str] = mapped_column(String(16), nullable=False)
    resolver_version: Mapped[str] = mapped_column(String(32), nullable=False)
    unified_symbol: Mapped[str] = mapped_column(String(128), nullable=False)
    market_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    identity_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    anchor_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    requested_since: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    requested_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timeframe: Mapped[str | None] = mapped_column(String(8), nullable=True)
    request_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    returned_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    valid_timestamp_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    in_window_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    invalid_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expected_rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    coverage_ratio: Mapped[Decimal | None] = mapped_column(Numeric(8, 6), nullable=True)
    covers_start: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    covers_end: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    missing_rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duplicate_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_gap_minutes: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    pagination_exhausted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    ccxt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    event: Mapped["PumpEvent"] = relationship(
        "PumpEvent",
        back_populates="derivatives_context_runs",
    )
    samples: Mapped[list["PumpDerivativesContextSample"]] = relationship(
        "PumpDerivativesContextSample",
        back_populates="run",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_pump_derivatives_context_runs_event", "event_id"),
        Index(
            "ix_pump_derivatives_context_runs_status_updated",
            "status",
            "updated_at",
        ),
        UniqueConstraint(
            "event_id",
            "exchange",
            "method",
            "resolver_version",
            name="uq_pump_derivatives_context_run",
        ),
        {"schema": "app"},
    )


class PumpDerivativesContextSample(Base):
    """A normalized public CCXT row recovered by a context run."""

    __tablename__ = "pump_derivatives_context_samples"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("app.pump_derivatives_context_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sample_key: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any] | list[Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    run: Mapped[PumpDerivativesContextRun] = relationship(
        "PumpDerivativesContextRun",
        back_populates="samples",
    )

    __table_args__ = (
        Index(
            "ix_pump_derivatives_context_samples_run_source",
            "run_id",
            "source_at",
        ),
        UniqueConstraint(
            "run_id",
            "sample_key",
            name="uq_pump_derivatives_context_sample",
        ),
        {"schema": "app"},
    )
