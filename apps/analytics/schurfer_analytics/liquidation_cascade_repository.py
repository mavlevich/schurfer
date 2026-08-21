"""Postgres adapter for analysis/liquidation-cascade-validation-v2.

Bounded, read-only, raw `text()` window SQL -- the same convention
`momentum_flow_bidirectional_burst_repository.py` uses for its own RANGE-
window burst query, not SQLAlchemy Core's `func.lag(...).over(...)` (no
window-function usage via Core exists anywhere else in this codebase; raw
SQL is how every prior report expressed one).

`fetch_minute_observations` mirrors
apps/execution/schurfer_execution/liquidation_cascade.py's own
`_SQL_SCANNER` LAG(15)/15-minute-lookback math -- this validation report
must measure the SAME causal rule the live scanner runs, not a redefinition
of it. There is no cross-app Python import precedent in this repo
(`pump_short_reentry_audit_report.py` uses the same manual-sync-with-comment
convention for its own runtime cross-reference), so the threshold constants
below are a deliberate, commented duplicate -- keep both files in sync by
hand.

Every query pins `capture_version` explicitly (colleague review,
2026-08-21): the table's own primary key is `(exchange, market_type, symbol,
capture_version, bucket_start)`, so a capture-version bump mid-window (a
schema/contract change, see migration `0024_bybit_momentum_bars_1m.py`'s own
docstring) leaves MORE THAN ONE row per `(exchange, market_type, symbol,
bucket_start)` in the table. Without a `capture_version` filter, `LAG(...)
OVER (PARTITION BY exchange, symbol ORDER BY bucket_start)` would silently
interleave rows from two different capture contracts, and a bare `WHERE
bucket_start = :bucket_start` quote lookup could return more than one row
non-deterministically.

It returns THRESHOLD-INDEPENDENT observations (raw price/OI-drop ratios, no
`is_qualifying` baked in) for every minute with a resolvable 15-row lag, not
only the ones crossing the production thresholds -- both because
`liquidation_cascade_grid_search.py` needs to re-threshold the same rows
many times, and because `liquidation_cascade_episodes.
decluster_cascade_episodes` needs the non-qualifying minutes in between two
qualifying ones to tell a genuine recovery from a mere gap in the data.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .liquidation_cascade_grid_search import MinuteObservation
from .momentum_flow_capture_contract import (
    BYBIT_MOMENTUM_CAPTURE_VERSION,
    BYBIT_MOMENTUM_MARKET_TYPE,
)
from .outcome_repository import async_database_url

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

# Must track apps/execution/schurfer_execution/liquidation_cascade.py's own
# _SQL_SCANNER thresholds exactly -- see this module's own doc comment.
PRICE_DROP_TRIGGER_PCT = -0.05
OI_DROP_TRIGGER_PCT = -0.15
LOOKBACK_MINUTES = 15

# LAG(..., 15) is row-position-based, exactly like the live scanner's own
# query -- this validation must score what actually fires live, not a
# corrected-but-different rule. The known risk (the same class of bug a
# colleague review already found in the first-pass volume-burst screen: a
# ROWS-based lookback silently spans more than 15 real minutes once a gap
# exists) is measured, not silently ignored: `lag_span_minutes` exposes
# whether the row 15 back was actually 15 calendar minutes back, and any
# minute where it was not is folded into the completeness flags below.
_MINUTE_STATES_SQL = text("""
    WITH bars AS (
        SELECT exchange, symbol, bucket_start, close_price, open_interest,
               price_complete, open_interest_complete
        FROM timeseries.bybit_momentum_bars_1m
        WHERE exchange = :exchange AND market_type = :market_type
          AND capture_version = :capture_version
          AND bucket_start >= :since - INTERVAL '15 minutes' AND bucket_start < :until
          AND close_price IS NOT NULL AND open_interest IS NOT NULL
    ),
    lagged AS (
        SELECT exchange, symbol, bucket_start, close_price, open_interest,
               price_complete, open_interest_complete,
               LAG(close_price, :lookback_minutes) OVER w AS price_lag,
               LAG(open_interest, :lookback_minutes) OVER w AS oi_lag,
               LAG(bucket_start, :lookback_minutes) OVER w AS bucket_lag
        FROM bars
        WINDOW w AS (PARTITION BY exchange, symbol ORDER BY bucket_start)
    )
    SELECT exchange, symbol, bucket_start,
           (close_price - price_lag) / price_lag AS price_drop_pct,
           (open_interest - oi_lag) / oi_lag AS oi_drop_pct,
           price_complete, open_interest_complete,
           EXTRACT(EPOCH FROM (bucket_start - bucket_lag)) / 60.0 AS lag_span_minutes
    FROM lagged
    WHERE price_lag IS NOT NULL AND oi_lag IS NOT NULL
      AND price_lag > 0 AND oi_lag > 0
      AND bucket_start >= :since AND bucket_start < :until
    ORDER BY exchange, symbol, bucket_start
""")

_OUTCOME_PATH_SQL = text("""
    SELECT bucket_start, close_price, high_price, low_price, complete
    FROM timeseries.bybit_momentum_bars_1m
    WHERE exchange = :exchange AND market_type = :market_type
      AND capture_version = :capture_version AND symbol = :symbol
      AND bucket_start >= :since AND bucket_start < :until
    ORDER BY symbol, bucket_start
""")

_BARS_FOR_SYMBOLS_SQL = text("""
    SELECT symbol, bucket_start, close_price, high_price, low_price, complete
    FROM timeseries.bybit_momentum_bars_1m
    WHERE exchange = :exchange AND market_type = :market_type
      AND capture_version = :capture_version AND symbol = ANY(:symbols)
      AND bucket_start >= :since AND bucket_start < :until
    ORDER BY symbol, bucket_start
""")

_QUOTES_FOR_SYMBOLS_SQL = text("""
    SELECT symbol, bucket_start, last_bid_price, last_ask_price, price_complete
    FROM timeseries.bybit_momentum_bars_1m
    WHERE exchange = :exchange AND market_type = :market_type
      AND capture_version = :capture_version AND symbol = ANY(:symbols)
      AND bucket_start >= :since AND bucket_start < :until
""")

# `_instruments`/`_snapshots` share (exchange, universe_version,
# catalog_version) as their linkage -- see momentum_universe_identity_
# repository.py's own module doc, "atomically linked, one row set per
# fetch". `native_market_id` is the exchange's own raw ticker id, the same
# format `bybit_momentum_bars_1m.symbol` already uses (confirmed by
# apps/execution/schurfer_execution/liquidation_cascade.py's own SQL, which
# treats `symbol` as the raw Bybit market id, not a CCXT unified one).
#
# `catalog_version` is a hash of the WHOLE catalog snapshot, not a per-
# instrument marker (colleague review, 2026-08-21): any new listing on
# either venue changes it for every instrument, so comparing it across a
# symbol's own observations would treat nearly every symbol as unstable.
# Per-instrument stability instead compares `identity_key`/`onboarded_at`
# for that ONE symbol across the window -- a genuine delisted-and-relisted
# ticker under the same native market id changes its own `onboarded_at`
# even though the surrounding catalog_version churns constantly for
# unrelated reasons.
_IDENTITY_STATUS_SQL = text("""
    SELECT i.native_market_id, i.identity_status, i.identity_key, i.onboarded_at,
           s.captured_at
    FROM app.momentum_universe_instruments i
    JOIN app.momentum_universe_snapshots s
      ON s.exchange = i.exchange
     AND s.universe_version = i.universe_version
     AND s.catalog_version = i.catalog_version
    WHERE i.exchange = :exchange AND i.native_market_id = ANY(:symbols)
      AND s.captured_at >= :since AND s.captured_at < :until
    ORDER BY i.native_market_id, s.captured_at
""")


@dataclass(frozen=True)
class OutcomeBar:
    bucket_start: datetime
    close_price: float | None
    high_price: float | None
    low_price: float | None
    complete: bool


@dataclass(frozen=True)
class Quote:
    last_bid_price: float | None
    last_ask_price: float | None
    price_complete: bool


@dataclass(frozen=True)
class IdentityObservation:
    native_market_id: str
    identity_status: str
    identity_key: str | None
    onboarded_at: datetime | None
    captured_at: datetime


class LiquidationCascadeRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, database_url: str) -> LiquidationCascadeRepository:
        return cls(
            create_async_engine(
                async_database_url(database_url),
                pool_pre_ping=True,
                pool_size=2,
                max_overflow=0,
            )
        )

    async def fetch_minute_observations(
        self,
        *,
        exchange: str,
        since: datetime,
        until: datetime,
        market_type: str = BYBIT_MOMENTUM_MARKET_TYPE,
        capture_version: str = BYBIT_MOMENTUM_CAPTURE_VERSION,
        lookback_minutes: int = LOOKBACK_MINUTES,
    ) -> tuple[MinuteObservation, ...]:
        if since >= until:
            raise ValueError("since must be earlier than until")
        async with self._engine.connect() as connection:
            result = await connection.execute(
                _MINUTE_STATES_SQL,
                {
                    "exchange": exchange,
                    "market_type": market_type,
                    "capture_version": capture_version,
                    "since": since,
                    "until": until,
                    "lookback_minutes": lookback_minutes,
                },
            )
            rows = result.all()
        observations = []
        for row in rows:
            clean_lookback = row.lag_span_minutes is not None and math.isclose(
                float(row.lag_span_minutes), float(lookback_minutes), abs_tol=1e-6
            )
            observations.append(
                MinuteObservation(
                    exchange=exchange,
                    symbol=str(row.symbol),
                    bucket_start=row.bucket_start,
                    price_drop_pct=float(row.price_drop_pct),
                    oi_drop_pct=float(row.oi_drop_pct),
                    price_complete=bool(row.price_complete) and clean_lookback,
                    open_interest_complete=bool(row.open_interest_complete) and clean_lookback,
                )
            )
        return tuple(observations)

    async def fetch_outcome_path(
        self,
        *,
        exchange: str,
        symbol: str,
        since: datetime,
        until: datetime,
        market_type: str = BYBIT_MOMENTUM_MARKET_TYPE,
        capture_version: str = BYBIT_MOMENTUM_CAPTURE_VERSION,
    ) -> tuple[OutcomeBar, ...]:
        if since >= until:
            raise ValueError("since must be earlier than until")
        async with self._engine.connect() as connection:
            result = await connection.execute(
                _OUTCOME_PATH_SQL,
                {
                    "exchange": exchange,
                    "market_type": market_type,
                    "capture_version": capture_version,
                    "symbol": symbol,
                    "since": since,
                    "until": until,
                },
            )
            rows = result.all()
        return tuple(
            OutcomeBar(
                bucket_start=row.bucket_start,
                close_price=float(row.close_price) if row.close_price is not None else None,
                high_price=float(row.high_price) if row.high_price is not None else None,
                low_price=float(row.low_price) if row.low_price is not None else None,
                complete=bool(row.complete),
            )
            for row in rows
        )

    async def fetch_bars_for_symbols(
        self,
        *,
        exchange: str,
        symbols: Sequence[str],
        since: datetime,
        until: datetime,
        market_type: str = BYBIT_MOMENTUM_MARKET_TYPE,
        capture_version: str = BYBIT_MOMENTUM_CAPTURE_VERSION,
    ) -> dict[str, tuple[OutcomeBar, ...]]:
        """Bulk per-symbol bar fetch -- one round trip for every symbol that
        needs a replay path, instead of one round trip per qualifying
        minute (colleague review, 2026-08-21: the original per-minute
        `fetch_outcome_path` calls could reach thousands of round trips
        across a full analysis window)."""
        if not symbols:
            return {}
        if since >= until:
            raise ValueError("since must be earlier than until")
        async with self._engine.connect() as connection:
            result = await connection.execute(
                _BARS_FOR_SYMBOLS_SQL,
                {
                    "exchange": exchange,
                    "market_type": market_type,
                    "capture_version": capture_version,
                    "symbols": list(dict.fromkeys(symbols)),
                    "since": since,
                    "until": until,
                },
            )
            rows = result.all()
        by_symbol: dict[str, list[OutcomeBar]] = defaultdict(list)
        for row in rows:
            by_symbol[str(row.symbol)].append(
                OutcomeBar(
                    bucket_start=row.bucket_start,
                    close_price=float(row.close_price) if row.close_price is not None else None,
                    high_price=float(row.high_price) if row.high_price is not None else None,
                    low_price=float(row.low_price) if row.low_price is not None else None,
                    complete=bool(row.complete),
                )
            )
        return {symbol: tuple(bars) for symbol, bars in by_symbol.items()}

    async def fetch_quotes_for_symbols(
        self,
        *,
        exchange: str,
        symbols: Sequence[str],
        since: datetime,
        until: datetime,
        market_type: str = BYBIT_MOMENTUM_MARKET_TYPE,
        capture_version: str = BYBIT_MOMENTUM_CAPTURE_VERSION,
    ) -> dict[tuple[str, datetime], Quote]:
        """Bulk (symbol, bucket_start) -> Quote lookup, replacing one
        `fetch_quote_at` round trip per entry/exit instant."""
        if not symbols:
            return {}
        if since >= until:
            raise ValueError("since must be earlier than until")
        async with self._engine.connect() as connection:
            result = await connection.execute(
                _QUOTES_FOR_SYMBOLS_SQL,
                {
                    "exchange": exchange,
                    "market_type": market_type,
                    "capture_version": capture_version,
                    "symbols": list(dict.fromkeys(symbols)),
                    "since": since,
                    "until": until,
                },
            )
            rows = result.all()
        return {
            (str(row.symbol), row.bucket_start): Quote(
                last_bid_price=(
                    float(row.last_bid_price) if row.last_bid_price is not None else None
                ),
                last_ask_price=(
                    float(row.last_ask_price) if row.last_ask_price is not None else None
                ),
                price_complete=bool(row.price_complete),
            )
            for row in rows
        }

    async def fetch_identity_observations(
        self,
        *,
        exchange: str,
        symbols: Sequence[str],
        since: datetime,
        until: datetime,
    ) -> tuple[IdentityObservation, ...]:
        if not symbols:
            return ()
        async with self._engine.connect() as connection:
            result = await connection.execute(
                _IDENTITY_STATUS_SQL,
                {
                    "exchange": exchange,
                    "symbols": list(dict.fromkeys(symbols)),
                    "since": since,
                    "until": until,
                },
            )
            rows = result.all()
        return tuple(
            IdentityObservation(
                native_market_id=str(row.native_market_id),
                identity_status=str(row.identity_status),
                identity_key=str(row.identity_key) if row.identity_key is not None else None,
                onboarded_at=row.onboarded_at,
                captured_at=row.captured_at,
            )
            for row in rows
        )

    async def close(self) -> None:
        await self._engine.dispose()
