"""Postgres adapter for liquidation_maker_upper_bound_v1
(research/liquidation-maker-upper-bound-v1).

`fetch_trigger_minutes` -- ONE rolling-window query against
`timeseries.liquidation_events`, grouped to per-minute aggregate notional
per (exchange, native_market_id, position_side), with a
`CASCADE_TRIGGER_WINDOW_MINUTES`-minute trailing RANGE-window sum. No
notional threshold filter in SQL: the sensitivity family
(`SENSITIVITY_CASCADE_NOTIONAL_USD_FAMILY`, 100k/250k/500k) is applied in
pure Python from this ONE fetched dataset, not by running this query three
times. Liquidation events are a comparatively sparse stream (real
liquidations, not a dense per-minute bar for the whole instrument universe
the way HYP-016's own bars table is) -- this is a genuine window-function
query, not the kind of full-universe/dense-table scan that needed an
offline extraction (see momentum_flow_bidirectional_burst_offline_
repository.py's own docstring for why THAT one did), but it still runs
under its own short statement_timeout and a row-count safety guard,
matching this codebase's usual fail-loud-not-silently-large convention.

Exact-venue OHLCV fetching (resolving each episode's own native_market_id
to a CCXT unified symbol, then `ohlcv.fetch_symbol_candles`) lives in the
report module instead, which already owns the per-exchange CCXT client
pool this needs -- not duplicated here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .liquidation_maker_upper_bound import CASCADE_TRIGGER_WINDOW_MINUTES
from .outcome_repository import async_database_url

if TYPE_CHECKING:
    from collections.abc import Sequence

TRIGGER_MINUTE_QUERY_VERSION = "liquidation_maker_upper_bound_trigger_minutes_v1"
_STATEMENT_TIMEOUT = "60s"

_LIQUIDATION_CAPTURE_VERSION = "liquidation_event_v1"

_TRIGGER_MINUTES_SQL = text("""
    WITH events AS (
        SELECT exchange, native_market_id, position_side, event_at,
               estimated_liquidation_notional
        FROM timeseries.liquidation_events
        WHERE capture_version = :capture_version
          AND estimated_liquidation_notional IS NOT NULL
          AND event_at >= :since - (:window_precede_minutes || ' minutes')::interval
          AND event_at < :until
    ),
    per_minute AS (
        SELECT exchange, native_market_id, position_side,
               date_trunc('minute', event_at) AS bucket_start,
               SUM(estimated_liquidation_notional) AS notional_usd
        FROM events
        GROUP BY exchange, native_market_id, position_side, date_trunc('minute', event_at)
    ),
    rolling AS (
        SELECT exchange, native_market_id, position_side, bucket_start,
               SUM(notional_usd) OVER (
                   PARTITION BY exchange, native_market_id, position_side
                   ORDER BY bucket_start
                   RANGE BETWEEN
                       (:window_precede_minutes || ' minutes')::interval PRECEDING
                       AND CURRENT ROW
               ) AS trailing_notional_usd
        FROM per_minute
    )
    SELECT exchange, native_market_id, position_side, bucket_start, trailing_notional_usd
    FROM rolling
    WHERE bucket_start >= :since
    ORDER BY exchange, native_market_id, position_side, bucket_start
    LIMIT :limit
""")


@dataclass(frozen=True)
class RawTriggerMinute:
    exchange: str
    native_market_id: str
    position_side: str
    bucket_start: datetime
    trailing_notional_usd: float


def check_trigger_minute_count(count: int, max_trigger_minutes: int) -> None:
    if count > max_trigger_minutes:
        raise ValueError(
            f"query produced {count} per-minute rows, over "
            f"--max-trigger-minutes={max_trigger_minutes}; narrow --since/--until or raise "
            "the bound explicitly rather than silently evaluating an unexpectedly large result"
        )


class LiquidationMakerUpperBoundRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, database_url: str) -> LiquidationMakerUpperBoundRepository:
        return cls(
            create_async_engine(
                async_database_url(database_url),
                pool_pre_ping=True,
                pool_size=2,
                max_overflow=0,
            )
        )

    async def database_now(self) -> datetime:
        async with self._engine.connect() as connection:
            value = (await connection.execute(text("SELECT now()"))).scalar_one()
        if not isinstance(value, datetime):
            raise TypeError("database now() did not return a datetime")
        return value

    async def fetch_trigger_minutes(
        self,
        *,
        since: datetime,
        until: datetime,
        limit: int,
        trigger_window_minutes: int = CASCADE_TRIGGER_WINDOW_MINUTES,
    ) -> Sequence[RawTriggerMinute]:
        if since >= until:
            raise ValueError("since must be earlier than until")
        if limit <= 0:
            raise ValueError("limit must be positive")
        async with self._engine.connect() as connection, connection.begin():
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            await connection.execute(
                text("SELECT set_config('statement_timeout', :timeout, true)"),
                {"timeout": _STATEMENT_TIMEOUT},
            )
            result = await connection.execute(
                _TRIGGER_MINUTES_SQL,
                {
                    "capture_version": _LIQUIDATION_CAPTURE_VERSION,
                    "since": since,
                    "until": until,
                    "window_precede_minutes": trigger_window_minutes - 1,
                    "limit": limit,
                },
            )
            rows = result.all()
        return tuple(
            RawTriggerMinute(
                exchange=str(row.exchange),
                native_market_id=str(row.native_market_id),
                position_side=str(row.position_side),
                bucket_start=row.bucket_start,
                trailing_notional_usd=float(row.trailing_notional_usd),
            )
            for row in rows
        )
