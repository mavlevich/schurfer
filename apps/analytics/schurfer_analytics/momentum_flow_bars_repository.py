"""Read-only SQLAlchemy access to `timeseries.bybit_momentum_bars_1m`
(collector-written, see `packages/journal/migrations/versions/
0024_bybit_momentum_bars_1m.py`). No ORM model backs this table -- it is
written exclusively by the Go collector, never by Python analytics code --
so this module declares only the columns `momentum_flow_timeline.py`'s v0
feature set actually reads via SQLAlchemy Core, not a full mapped model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Double,
    MetaData,
    String,
    Table,
    and_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .outcome_repository import async_database_url

if TYPE_CHECKING:
    from datetime import datetime
    from typing import Any

    from sqlalchemy.sql import Select

# Must track apps/collector/internal/momentumcapture/writer.go's
# CaptureVersion. Analytics does not depend on the Go module, so this is a
# deliberate, commented duplication rather than a cross-language import --
# same convention as oi_growth_filter_report.py's OI_CHANGE_THRESHOLD_PCT.
BYBIT_MOMENTUM_CAPTURE_VERSION = "v1"
BYBIT_MOMENTUM_EXCHANGE = "bybit"
BYBIT_MOMENTUM_MARKET_TYPE = "linear"

_metadata = MetaData()
_bars_table = Table(
    "bybit_momentum_bars_1m",
    _metadata,
    Column("exchange", String(32)),
    Column("market_type", String(16)),
    Column("symbol", String(32)),
    Column("capture_version", String(32)),
    # TIMESTAMPTZ in the real table (0024_bybit_momentum_bars_1m.py) -- a
    # colleague review (2026-08-11, before any real run) caught this
    # originally declared as BigInteger, which would have made every
    # `bucket_start >= since` comparison below compare a bigint column
    # against a Python datetime and either fail outright or silently
    # misbehave depending on dialect coercion.
    Column("bucket_start", DateTime(timezone=True)),
    Column("close_price", Double),
    Column("buy_total_notional_usd", Double),
    Column("sell_total_notional_usd", Double),
    Column("open_interest", Double),
    Column("open_interest_value", Double),
    # `open_interest is not None` alone does NOT mean this bar observed OI
    # fresh -- the Go collector carries the previous reading forward into
    # a bar whose own minute saw no NEW OI value (momentum.go's
    # AddTickerObservation only touches OpenInterestObservedAt inside its
    # `if o.OpenInterest != nil` branch). `ticker_observed_this_minute`
    # alone is NOT sufficient either (colleague review, 2026-08-12,
    # second amendment): it is set on any successful ticker message that
    # minute, independent of whether that message carried an OI update.
    # Each metric's own event/observed timestamp pair is the real signal:
    # amount and USD value can refresh independently. See
    # momentum_flow_protocol.py's "Open-interest staleness policy" and
    # `momentum_flow_timeline.py`'s `_closest_known_oi_at_or_before`.
    Column("open_interest_event_at", DateTime(timezone=True)),
    Column("open_interest_observed_at", DateTime(timezone=True)),
    Column("open_interest_value_event_at", DateTime(timezone=True)),
    Column("open_interest_value_observed_at", DateTime(timezone=True)),
    Column("ticker_observed_this_minute", Boolean),
    Column("complete", Boolean),
    schema="timeseries",
)


@dataclass(frozen=True)
class MomentumFlowBarRow:
    symbol: str
    bucket_start: datetime
    close_price: float | None
    buy_total_notional_usd: float
    sell_total_notional_usd: float
    open_interest: float | None
    open_interest_value: float | None
    open_interest_event_at: datetime | None
    open_interest_observed_at: datetime | None
    open_interest_value_event_at: datetime | None
    open_interest_value_observed_at: datetime | None
    ticker_observed_this_minute: bool
    complete: bool


def bars_statement(
    *,
    symbols: tuple[str, ...],
    since: datetime,
    until: datetime,
) -> Select[Any]:
    """`since`/`until` are both inclusive-open on the caller's own timeline
    math (`momentum_flow_timeline.py` filters by exact millisecond
    boundaries itself); this just narrows the DB round trip to a superset
    covering every symbol's requested window in one query."""
    if not symbols:
        raise ValueError("symbols must not be empty")
    columns = _bars_table.c
    return (
        select(
            columns.symbol,
            columns.bucket_start,
            columns.close_price,
            columns.buy_total_notional_usd,
            columns.sell_total_notional_usd,
            columns.open_interest,
            columns.open_interest_value,
            columns.open_interest_event_at,
            columns.open_interest_observed_at,
            columns.open_interest_value_event_at,
            columns.open_interest_value_observed_at,
            columns.ticker_observed_this_minute,
            columns.complete,
        )
        .where(
            and_(
                columns.exchange == BYBIT_MOMENTUM_EXCHANGE,
                columns.market_type == BYBIT_MOMENTUM_MARKET_TYPE,
                columns.capture_version == BYBIT_MOMENTUM_CAPTURE_VERSION,
                columns.symbol.in_(symbols),
                columns.bucket_start >= since,
                columns.bucket_start <= until,
            )
        )
        .order_by(columns.symbol, columns.bucket_start)
    )


class MomentumFlowBarsRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, db_url: str) -> MomentumFlowBarsRepository:
        engine = create_async_engine(
            async_database_url(db_url),
            pool_pre_ping=True,
            pool_size=1,
            max_overflow=0,
        )
        return cls(engine)

    async def load(
        self,
        *,
        symbols: tuple[str, ...],
        since: datetime,
        until: datetime,
    ) -> tuple[MomentumFlowBarRow, ...]:
        if not symbols:
            return ()
        statement = bars_statement(symbols=symbols, since=since, until=until)
        async with self._engine.connect() as connection:
            result = await connection.execute(statement)
            rows = result.all()
        return tuple(
            MomentumFlowBarRow(
                symbol=symbol,
                bucket_start=bucket_start,
                close_price=float(close_price) if close_price is not None else None,
                buy_total_notional_usd=float(buy_total_notional_usd or 0.0),
                sell_total_notional_usd=float(sell_total_notional_usd or 0.0),
                open_interest=float(open_interest) if open_interest is not None else None,
                open_interest_value=(
                    float(open_interest_value) if open_interest_value is not None else None
                ),
                open_interest_event_at=open_interest_event_at,
                open_interest_observed_at=open_interest_observed_at,
                open_interest_value_event_at=open_interest_value_event_at,
                open_interest_value_observed_at=open_interest_value_observed_at,
                ticker_observed_this_minute=bool(ticker_observed_this_minute),
                complete=bool(complete),
            )
            for (
                symbol,
                bucket_start,
                close_price,
                buy_total_notional_usd,
                sell_total_notional_usd,
                open_interest,
                open_interest_value,
                open_interest_event_at,
                open_interest_observed_at,
                open_interest_value_event_at,
                open_interest_value_observed_at,
                ticker_observed_this_minute,
                complete,
            ) in rows
        )

    async def close(self) -> None:
        await self._engine.dispose()


def bybit_linear_symbol(base: str) -> str:
    """The frozen matched-control/flow-join key (see `momentum_flow_
    protocol.py`): flow is observed on Bybit regardless of which exchange a
    decision itself executed on -- a cross-venue proxy, not this specific
    venue's own order flow. Documented, not hidden, in the report's own
    manifest (`flow_source_exchange`)."""
    return f"{base.strip().upper()}USDT"
