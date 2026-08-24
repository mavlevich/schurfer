from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin
from .enums import (
    Exchange,
    MarketType,
    OutcomeLabel,
    OutcomeQuality,
    Side,
    TradeStatus,
)


class Strategy(Base, TimestampMixin):
    """Registered trading strategies."""

    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    trades: Mapped[list["Trade"]] = relationship("Trade", back_populates="strategy")
    alerts: Mapped[list["Alert"]] = relationship("Alert", back_populates="strategy")

    __table_args__ = (
        Index("ix_strategies_name_version", "name", "version", unique=True),
        {"schema": "app"},
    )


class Alert(Base, TimestampMixin):
    """Signal alert sent to Telegram for approve/skip decision."""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    strategy_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.strategies.id"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange: Mapped[Exchange] = mapped_column(String(32), nullable=False)
    market_type: Mapped[MarketType] = mapped_column(String(16), nullable=False)
    side: Mapped[Side] = mapped_column(String(8), nullable=False)

    # Signal context at the time of alert
    setup_context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # Telegram message reference
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    strategy: Mapped["Strategy"] = relationship("Strategy", back_populates="alerts")
    trade: Mapped["Trade | None"] = relationship("Trade", back_populates="alert")

    __table_args__ = (
        Index("ix_alerts_strategy_id", "strategy_id"),
        Index("ix_alerts_symbol", "symbol"),
        Index("ix_alerts_status", "status"),
        {"schema": "app"},
    )


class Trade(Base, TimestampMixin):
    """
    Core trade journal entry.

    Every executed trade is recorded here with full context.
    No strategy can go live without writing to this table (ADR-0007).
    """

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    strategy_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.strategies.id"), nullable=False
    )
    alert_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.alerts.id"), nullable=True
    )

    # Instrument
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange: Mapped[Exchange] = mapped_column(String(32), nullable=False)
    market_type: Mapped[MarketType] = mapped_column(String(16), nullable=False)
    side: Mapped[Side] = mapped_column(String(8), nullable=False)

    # Exchange order IDs
    entry_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exit_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Position sizing
    size_usd: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    leverage: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False, default=1)

    # Entry
    entry_price: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    entry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_slippage_bps: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)

    # Exit
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    exit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_slippage_bps: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)

    # Costs
    fees_usd: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, default=0)
    funding_usd: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, default=0)
    slippage_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)

    # Compatibility PnL. Legacy rows contain price-only gross PnL; rows with a
    # complete accounting contract contain net PnL. New consumers must use the
    # explicit gross/net fields and accounting_status below.
    pnl_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    pnl_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    gross_pnl_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    gross_pnl_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    net_pnl_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    net_pnl_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    accounting_version: Mapped[str] = mapped_column(
        String(40), nullable=False, default="legacy_price_only_v1"
    )
    accounting_status: Mapped[str] = mapped_column(String(16), nullable=False, default="legacy")
    accounting_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Status and outcome
    status: Mapped[TradeStatus] = mapped_column(
        String(16), nullable=False, default=TradeStatus.OPEN
    )
    outcome_label: Mapped[OutcomeLabel | None] = mapped_column(String(16), nullable=True)
    outcome_quality: Mapped[OutcomeQuality | None] = mapped_column(String(32), nullable=True)

    # Full signal context captured at entry time
    # Enables queries like: "winrate when funding > 0.05% AND OI growth > 100%"
    setup_context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # Free-form notes
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # early_momentum_v3+: which durable episode (if any) opened this trade.
    # Nullable for backward compat (pump-short, liquidation_cascade, and
    # early_momentum v1/v2 rows predate the episode lifecycle) and
    # deliberately NOT unique -- a future scale-in leg reuses the same
    # episode_id for a second trade row. entry_idempotency_key is the actual
    # idempotency guard (see migration 0032's partial unique index).
    episode_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("app.early_momentum_episodes.episode_id"),
        nullable=True,
    )
    entry_idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)

    strategy: Mapped["Strategy"] = relationship("Strategy", back_populates="trades")
    alert: Mapped["Alert | None"] = relationship("Alert", back_populates="trade")
    exit_liquidity_observation: Mapped["TradeExitLiquidityObservation | None"] = relationship(
        "TradeExitLiquidityObservation",
        back_populates="trade",
        uselist=False,
    )

    __table_args__ = (
        Index("ix_trades_strategy_id", "strategy_id"),
        Index("ix_trades_symbol", "symbol"),
        Index("ix_trades_status", "status"),
        Index("ix_trades_entry_at", "entry_at"),
        Index("ix_trades_setup_context", "setup_context", postgresql_using="gin"),
        Index(
            "ix_trades_episode_id",
            "episode_id",
            postgresql_where=text("episode_id IS NOT NULL"),
        ),
        Index(
            "ux_trades_entry_idempotency_key",
            "entry_idempotency_key",
            unique=True,
            postgresql_where=text("entry_idempotency_key IS NOT NULL"),
        ),
        # A retried/duplicated open_trade() call for the same real exchange
        # order (crash-recovery replay, a bug in a retry path) must return
        # the already-journaled row, never create a second one for the same
        # real position -- see journal.open_trade's own ON CONFLICT handling.
        Index(
            "ux_trades_exchange_entry_order_id",
            "exchange",
            "entry_order_id",
            unique=True,
            postgresql_where=text("entry_order_id IS NOT NULL"),
        ),
        {"schema": "app"},
    )


class TradeExitLiquidityObservation(Base, TimestampMixin):
    """First bounded executable quote observed when a paper trade closes."""

    __tablename__ = "trade_exit_liquidity_observations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trade_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("app.trades.id", ondelete="CASCADE"),
        nullable=False,
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exchange: Mapped[Exchange] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    market_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_notional_usd: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    filled_notional_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    best_bid: Mapped[Decimal | None] = mapped_column(Numeric(30, 14), nullable=True)
    best_ask: Mapped[Decimal | None] = mapped_column(Numeric(30, 14), nullable=True)
    mid: Mapped[Decimal | None] = mapped_column(Numeric(30, 14), nullable=True)
    spread_bps: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    bid_vwap: Mapped[Decimal | None] = mapped_column(Numeric(30, 14), nullable=True)
    bid_impact_bps: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    ask_vwap: Mapped[Decimal | None] = mapped_column(Numeric(30, 14), nullable=True)
    ask_impact_bps: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    contract_size: Mapped[Decimal | None] = mapped_column(Numeric(24, 12), nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    trade: Mapped["Trade"] = relationship("Trade", back_populates="exit_liquidity_observation")

    __table_args__ = (
        Index(
            "ux_trade_exit_liquidity_observations_trade_id",
            "trade_id",
            unique=True,
        ),
        Index(
            "ix_trade_exit_liquidity_observations_observed_at",
            "observed_at",
        ),
        Index(
            "ix_trade_exit_liquidity_observations_status",
            "status",
        ),
        {"schema": "app"},
    )
