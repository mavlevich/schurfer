from datetime import datetime
from decimal import Decimal
from typing import Any

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
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin


class TradeDecision(Base):
    """A point-in-time entry or skip decision made by execution."""

    __tablename__ = "trade_decisions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    base: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pump_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    decision_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), nullable=True)
    strategy_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    features: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    liquidity: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(), nullable=True)
    pump_event_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("app.pump_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    # strategy_id/trading_mode/side: added for feat/execution-shadow-evidence-v1's
    # ShadowBroker. Nullable and NULL on every pump_short row (past and
    # future) -- pump_short identifies itself via strategy_version alone,
    # unchanged, and never sets these. strategy_id mirrors trades.strategy_id
    # (same app.strategies registry, same no-ondelete FK -- canonical
    # identity must block a delete, never be silently severed to NULL like
    # pump_event_id above) rather than overloading strategy_version with a
    # name-carrying convention pump_short's own rows don't follow.
    strategy_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("app.strategies.id"),
        nullable=True,
    )
    trading_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Direction of the intent this decision came from -- outcome_repository's
    # due_decisions_statement excludes trading_mode='shadow' rows from
    # resolution entirely for now, since compute_outcome's return/MFE/MAE
    # math is short-only and would silently invert a LONG decision's
    # evidence (colleague review). Exists now so a future directional
    # resolver doesn't need another migration.
    side: Mapped[str | None] = mapped_column(String(8), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    outcomes: Mapped[list["TradeDecisionOutcome"]] = relationship(
        "TradeDecisionOutcome", back_populates="decision", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_trade_decisions_ts", "ts"),
        Index("ix_trade_decisions_base_ts", "base", "ts"),
        Index("ix_trade_decisions_pump_event_id", "pump_event_id"),
        Index("ix_trade_decisions_strategy_id", "strategy_id"),
        Index("uq_trade_decisions_decision_id", "decision_id", unique=True),
        # today's only writer of a non-NULL trading_mode is ShadowBroker --
        # extend this set, in its own migration, when a future broker
        # actually starts writing a new value here.
        CheckConstraint(
            "trading_mode IS NULL OR trading_mode IN ('shadow')",
            name="ck_trade_decisions_trading_mode",
        ),
        # A partial write (e.g. strategy_id=NULL after a failed identity
        # lookup, with trading_mode still set) must never reach this table --
        # see ShadowBroker.open's own strategy_id is None guard.
        CheckConstraint(
            "(strategy_id IS NULL AND trading_mode IS NULL) OR "
            "(strategy_id IS NOT NULL AND trading_mode IS NOT NULL)",
            name="ck_trade_decisions_strategy_identity_pair",
        ),
        {"schema": "app"},
    )


class TradeDecisionOutcome(Base, TimestampMixin):
    """Strategy-agnostic forward price path metrics for one decision horizon."""

    __tablename__ = "trade_decision_outcomes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("app.trade_decisions.decision_id", ondelete="CASCADE"),
        nullable=False,
    )
    horizon_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    resolver_version: Mapped[str] = mapped_column(String(32), nullable=False)
    anchor_exchange: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_exchange: Mapped[str | None] = mapped_column(String(32), nullable=True)
    timeframe_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(), nullable=True)
    forward_price: Mapped[Decimal | None] = mapped_column(Numeric(), nullable=True)
    mfe_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    mae_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    short_return_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    bars_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expected_bars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    coverage_ratio: Mapped[Decimal | None] = mapped_column(Numeric(8, 6), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    decision: Mapped[TradeDecision] = relationship("TradeDecision", back_populates="outcomes")

    __table_args__ = (
        Index("ix_trade_decision_outcomes_status_updated", "status", "updated_at"),
        Index("ix_trade_decision_outcomes_horizon", "horizon_minutes"),
        UniqueConstraint(
            "decision_id",
            "horizon_minutes",
            "resolver_version",
            name="uq_trade_decision_outcomes_decision_horizon_version",
        ),
        {"schema": "app"},
    )
