"""Postgres adapter for analysis/momentum-flow-bidirectional-burst-study-v1.

See momentum_flow_bidirectional_burst_study.py's own module doc comment for
why this exists and what it fixes versus the first-pass
docs/analysis/momentum_flow_volume_burst_screen.sql. The one fact worth
repeating here: `fetch_candidate_extreme_minutes` uses Postgres RANGE-based
window frames (keyed on the actual bucket_start timestamp), never
ROWS-based ones -- ROWS BETWEEN N PRECEDING silently spans more than N real
minutes once a gap has been filtered out by `complete = true`, which is
exactly what corrupted the first-pass screen's own top burst bucket.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .momentum_flow_bidirectional_burst_study import BurstMinute
from .momentum_flow_capture_contract import BYBIT_MOMENTUM_MARKET_TYPE
from .outcome_repository import async_database_url

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

# The table is named `bybit_momentum_bars_1m` for historical reasons only --
# it is the shared multi-exchange bars table (an `exchange` column
# distinguishes bybit/binance rows), not a bybit-only one. Both exchanges
# share the same "linear" market_type (see momentum_flow_watch_contract.py's
# BINANCE_WATCH_CONTRACT, which only overrides watch_version/source_exchange).
_CANDIDATE_EXTREME_MINUTES_SQL = text("""
    WITH bars AS (
        -- Loaded from (since - 24h) so the trailing 24h/5m windows below
        -- have real history to look back over even for rows right at the
        -- start of [since, until) -- filtering this CTE itself to
        -- [since, until) would silently truncate every window's lookback
        -- at `since`, undercounting volume for no real reason. The output
        -- is filtered to the caller's actual [since, until) later, in the
        -- final SELECT.
        SELECT symbol, bucket_start, close_price, buy_total_notional_usd, sell_total_notional_usd
        FROM timeseries.bybit_momentum_bars_1m
        WHERE exchange = :exchange AND market_type = :market_type
          AND capture_version = :capture_version
          AND complete = true AND close_price > 0
          AND bucket_start >= :since - INTERVAL '24 hours' AND bucket_start < :until
    ),
    windowed AS (
        SELECT symbol, bucket_start, close_price,
               SUM(buy_total_notional_usd) OVER w5 AS buy_notional_5m,
               SUM(sell_total_notional_usd) OVER w5 AS sell_notional_5m,
               SUM(buy_total_notional_usd + sell_total_notional_usd) OVER w24h AS total_volume_24h,
               count(*) OVER w5 AS observed_bars_5m,
               count(*) OVER w24h AS observed_bars_24h
        FROM bars
        WINDOW
            w5 AS (
                PARTITION BY symbol ORDER BY bucket_start
                -- Five completed one-minute buckets: t-4m, ..., t.  The
                -- previous INTERVAL '5 minutes' bound included t-5m too
                -- and therefore measured six buckets at minute-aligned
                -- timestamps.
                RANGE BETWEEN INTERVAL '4 minutes' PRECEDING AND CURRENT ROW
            ),
            w24h AS (
                PARTITION BY symbol ORDER BY bucket_start
                -- Same inclusive-frame correction: 1,440 one-minute
                -- buckets, not 1,441.
                RANGE BETWEEN INTERVAL '1439 minutes' PRECEDING AND CURRENT ROW
            )
    ),
    scored AS (
        SELECT symbol, bucket_start, close_price,
               100.0 * buy_notional_5m / NULLIF(total_volume_24h, 0) AS buy_burst_pct_5m,
               100.0 * sell_notional_5m / NULLIF(total_volume_24h, 0) AS sell_burst_pct_5m
        FROM windowed
        WHERE bucket_start >= :since
          AND observed_bars_5m = 5
          AND observed_bars_24h = 1440
          AND total_volume_24h > :min_volume_24h_usd
    )
    SELECT symbol, bucket_start, close_price, buy_burst_pct_5m, sell_burst_pct_5m
    FROM scored
    WHERE buy_burst_pct_5m >= :extreme_threshold_pct OR sell_burst_pct_5m >= :extreme_threshold_pct
    ORDER BY symbol, bucket_start
""")

_REPORT_STATEMENT_TIMEOUT = "300s"


class MomentumFlowBidirectionalBurstRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, database_url: str) -> MomentumFlowBidirectionalBurstRepository:
        return cls(
            create_async_engine(
                async_database_url(database_url),
                pool_pre_ping=True,
                pool_size=2,
                max_overflow=0,
            )
        )

    async def fetch_candidate_extreme_minutes(
        self,
        *,
        exchange: str,
        capture_version: str,
        since: datetime,
        until: datetime,
        min_volume_24h_usd: float,
        extreme_threshold_pct: float,
        market_type: str = BYBIT_MOMENTUM_MARKET_TYPE,
    ) -> tuple[BurstMinute, ...]:
        if since >= until:
            raise ValueError("since must be earlier than until")
        async with self._engine.connect() as connection, connection.begin():
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            await connection.execute(
                text("SELECT set_config('statement_timeout', :timeout, true)"),
                {"timeout": _REPORT_STATEMENT_TIMEOUT},
            )
            result = await connection.execute(
                _CANDIDATE_EXTREME_MINUTES_SQL,
                {
                    "exchange": exchange,
                    "market_type": market_type,
                    "capture_version": capture_version,
                    "since": since,
                    "until": until,
                    "min_volume_24h_usd": min_volume_24h_usd,
                    "extreme_threshold_pct": extreme_threshold_pct,
                },
            )
            rows = result.all()
        return tuple(
            BurstMinute(
                exchange=exchange,
                symbol=str(row.symbol),
                bucket_start=row.bucket_start,
                close_price=float(row.close_price),
                buy_burst_pct_5m=float(row.buy_burst_pct_5m or 0.0),
                sell_burst_pct_5m=float(row.sell_burst_pct_5m or 0.0),
            )
            for row in rows
        )

    async def fetch_prices_at(
        self,
        *,
        exchange: str,
        capture_version: str,
        symbol_timestamps: Sequence[tuple[str, datetime]],
        market_type: str = BYBIT_MOMENTUM_MARKET_TYPE,
    ) -> dict[tuple[str, datetime], float]:
        """Exact-timestamp lookup, not a nearest-match/interpolated one: a
        (symbol, timestamp) pair with no exactly-matching bar is simply
        absent from the returned dict, the same fail-honest convention
        compute_episode_outcomes itself already documents."""
        if not symbol_timestamps:
            return {}
        symbols = sorted({symbol for symbol, _ in symbol_timestamps})
        timestamps = sorted({timestamp for _, timestamp in symbol_timestamps})
        async with self._engine.connect() as connection:
            result = await connection.execute(
                text("""
                    SELECT symbol, bucket_start, close_price
                    FROM timeseries.bybit_momentum_bars_1m
                    WHERE exchange = :exchange AND market_type = :market_type
                      AND capture_version = :capture_version
                      AND complete = true AND close_price > 0
                      AND symbol = ANY(:symbols) AND bucket_start = ANY(:timestamps)
                """),
                {
                    "exchange": exchange,
                    "market_type": market_type,
                    "capture_version": capture_version,
                    "symbols": symbols,
                    "timestamps": timestamps,
                },
            )
            rows = result.all()
        wanted = set(symbol_timestamps)
        return {
            (str(row.symbol), row.bucket_start): float(row.close_price)
            for row in rows
            if (str(row.symbol), row.bucket_start) in wanted
        }

    async def fetch_symbol_baseline_forward_returns(
        self,
        *,
        exchange: str,
        capture_version: str,
        since: datetime,
        until: datetime,
        symbols: Sequence[str],
        horizons_minutes: Sequence[int],
        market_type: str = BYBIT_MOMENTUM_MARKET_TYPE,
    ) -> dict[str, dict[int, float]]:
        """Each symbol's own unconditional mean forward %-return across
        EVERY bar in [since, until), not just its own burst episodes --
        the matched control: "what did holding this same asset for this
        same horizon look like on an unconditional draw," so a burst
        episode's own outcome is judged against its own asset's baseline
        drift/volatility, not a cross-asset average. Uses an exact-
        timestamp self-join (bucket_start + horizon), same discipline as
        fetch_prices_at -- never LEAD/LAG."""
        if not symbols or not horizons_minutes:
            return {}
        baseline: dict[str, dict[int, float]] = {symbol: {} for symbol in symbols}
        async with self._engine.connect() as connection:
            # unnest(:horizons_minutes) drives every horizon off one round
            # trip instead of looping the same query once per horizon in
            # Python -- the join condition below still keys off each row's
            # own horizon value, so this is exactly the same per-horizon
            # exact-timestamp self-join as before, just batched.
            result = await connection.execute(
                text("""
                    SELECT b.symbol, h.horizon,
                           avg(100.0 * (f.close_price / b.close_price - 1)) AS mean_return_pct,
                           count(*) AS n
                    FROM timeseries.bybit_momentum_bars_1m b
                    CROSS JOIN unnest(:horizons_minutes) AS h(horizon)
                    JOIN timeseries.bybit_momentum_bars_1m f
                      ON f.exchange = b.exchange AND f.market_type = b.market_type
                     AND f.capture_version = b.capture_version
                     AND f.symbol = b.symbol
                     AND f.bucket_start = b.bucket_start + make_interval(mins => h.horizon)
                     AND f.complete = true AND f.close_price > 0
                    WHERE b.exchange = :exchange AND b.market_type = :market_type
                      AND b.capture_version = :capture_version
                      AND b.complete = true AND b.close_price > 0
                      AND b.bucket_start >= :since AND b.bucket_start < :until
                      AND b.symbol = ANY(:symbols)
                    GROUP BY b.symbol, h.horizon
                """),
                {
                    "exchange": exchange,
                    "market_type": market_type,
                    "capture_version": capture_version,
                    "since": since,
                    "until": until,
                    "symbols": list(symbols),
                    "horizons_minutes": list(horizons_minutes),
                },
            )
            for row in result.all():
                if row.n and row.n > 0:
                    baseline[str(row.symbol)][int(row.horizon)] = float(row.mean_return_pct)
        return baseline

    async def close(self) -> None:
        await self._engine.dispose()
