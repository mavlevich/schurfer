"""Real-Postgres coverage for MomentumFlowBidirectionalBurstRepository.

Only a real query proves the RANGE-window burst math, the exact-timestamp
price lookup, and the exchange/market_type/capture_version scoping actually
hold against the real `timeseries.bybit_momentum_bars_1m` table -- not just
that the SQLAlchemy text() statements parse. In particular this is the
regression coverage for the ROWS-vs-RANGE bug the 2026-08-17 colleague
review found in the first-pass screens: a seeded gap (a missing minute)
must NOT silently widen the 5-minute burst window past 5 real minutes.

Matches infra/docker/docker-compose.dev.yml's local dev Postgres, same
convention as this session's other real-Postgres tests. Skips (not fails)
when no Postgres is reachable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.momentum_flow_bidirectional_burst_repository import (
    MomentumFlowBidirectionalBurstRepository,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

TEST_DATABASE_URL = "postgresql+psycopg://schurfer:schurfer_dev@localhost:5432/schurfer"

_TEST_EXCHANGE = "test_bidir_burst"
_TEST_CAPTURE_VERSION = "test_capture_v1"
_TEST_MARKET_TYPE = "linear"
_START = datetime(2026, 8, 17, 0, 0, tzinfo=UTC)

_INSERT_BAR_SQL = text("""
    INSERT INTO timeseries.bybit_momentum_bars_1m
        (exchange, market_type, symbol, capture_version, bucket_start,
         universe_version, close_price, buy_total_notional_usd, sell_total_notional_usd,
         buy_hist_counts, buy_hist_notional, sell_hist_counts, sell_hist_notional,
         ticker_complete, trades_complete, complete, payload_hash)
    VALUES
        (:exchange, :market_type, :symbol, :capture_version, :bucket_start,
         'universe-v1', :close_price, :buy_total_notional_usd, :sell_total_notional_usd,
         '{}', '{}', '{}', '{}',
         true, true, true, decode(repeat('ef', 32), 'hex'))
    ON CONFLICT DO NOTHING
""")


async def _connect_or_skip() -> AsyncEngine:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"no local postgres reachable: {exc}")
    return engine


async def _seed_bar(
    engine: AsyncEngine,
    *,
    symbol: str,
    bucket_start: datetime,
    close_price: float,
    buy_notional: float = 0.0,
    sell_notional: float = 0.0,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            _INSERT_BAR_SQL,
            {
                "exchange": _TEST_EXCHANGE,
                "market_type": _TEST_MARKET_TYPE,
                "symbol": symbol,
                "capture_version": _TEST_CAPTURE_VERSION,
                "bucket_start": bucket_start,
                "close_price": close_price,
                "buy_total_notional_usd": buy_notional,
                "sell_total_notional_usd": sell_notional,
            },
        )


async def _cleanup(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM timeseries.bybit_momentum_bars_1m WHERE exchange = :exchange"),
            {"exchange": _TEST_EXCHANGE},
        )


async def test_fetch_candidate_extreme_minutes_uses_a_real_5_minute_range_not_5_rows() -> None:
    # 24h of flat baseline volume, then a gap (bucket_start + 4..6 missing),
    # then a burst minute at +7. If the window were ROWS-based, the 5
    # "rows" preceding the burst minute would actually span several real
    # minutes further back than 5 -- exactly the bug the colleague review
    # found in the first-pass screen. RANGE must instead only see the
    # bars that fall within the real 5-minute wall-clock window.
    engine = await _connect_or_skip()
    try:
        symbol = "RANGEVSROWSUSDT"
        day_start = _START - timedelta(hours=24)
        for i in range(24 * 60):
            await _seed_bar(
                engine,
                symbol=symbol,
                bucket_start=day_start + timedelta(minutes=i),
                close_price=1.0,
                buy_notional=100.0,
                sell_notional=100.0,
            )
        # Gap: minutes _START+4 .. _START+6 (inclusive) are never written.
        await _seed_bar(
            engine,
            symbol=symbol,
            bucket_start=_START,
            close_price=1.0,
            buy_notional=100.0,
            sell_notional=100.0,
        )
        await _seed_bar(
            engine,
            symbol=symbol,
            bucket_start=_START + timedelta(minutes=1),
            close_price=1.0,
            buy_notional=100.0,
            sell_notional=100.0,
        )
        await _seed_bar(
            engine,
            symbol=symbol,
            bucket_start=_START + timedelta(minutes=2),
            close_price=1.0,
            buy_notional=100.0,
            sell_notional=100.0,
        )
        await _seed_bar(
            engine,
            symbol=symbol,
            bucket_start=_START + timedelta(minutes=3),
            close_price=1.0,
            buy_notional=100.0,
            sell_notional=100.0,
        )
        # burst minute: huge one-minute buy notional, right after the gap
        await _seed_bar(
            engine,
            symbol=symbol,
            bucket_start=_START + timedelta(minutes=7),
            close_price=1.0,
            buy_notional=50_000.0,
            sell_notional=0.0,
        )

        repository = MomentumFlowBidirectionalBurstRepository(engine)
        minutes = await repository.fetch_candidate_extreme_minutes(
            exchange=_TEST_EXCHANGE,
            capture_version=_TEST_CAPTURE_VERSION,
            market_type=_TEST_MARKET_TYPE,
            since=_START,
            until=_START + timedelta(minutes=10),
            min_volume_24h_usd=1.0,
            extreme_threshold_pct=1.0,
        )
        assert len(minutes) == 1
        (burst,) = minutes
        assert burst.bucket_start == _START + timedelta(minutes=7)

        # 5m numerator: RANGE 5min preceding the burst row (_START+2 ..
        # _START+7) sees only the bars that actually exist in that real
        # 5-minute span -- _START+2, _START+3, and the burst bar itself
        # (_START+4..6 are the seeded gap). A ROWS-based window would
        # instead pull in whatever the 5 preceding physical ROWS are
        # regardless of real elapsed time, reaching back to _START+0 and
        # spanning 7 real minutes, not 5.
        expected_buy_notional_5m = 100.0 + 100.0 + 50_000.0

        # 24h denominator: RANGE 24h preceding the burst row starts at
        # (_START+7 - 24h) = day_start + 7min, which excludes the first 7
        # of the 1440 seeded baseline bars (i=0..6, each i=1min after
        # day_start) -- 1433 baseline bars remain in-window.
        expected_24h_volume = (1433 * 200.0) + (4 * 200.0) + 50_000.0
        expected_buy_burst_pct = 100.0 * expected_buy_notional_5m / expected_24h_volume
        assert burst.buy_burst_pct_5m == pytest.approx(expected_buy_burst_pct, rel=1e-9)
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_fetch_candidate_extreme_minutes_rejects_since_after_until() -> None:
    engine = await _connect_or_skip()
    try:
        repository = MomentumFlowBidirectionalBurstRepository(engine)
        now = datetime.now(UTC)
        with pytest.raises(ValueError, match="since must be earlier"):
            await repository.fetch_candidate_extreme_minutes(
                exchange=_TEST_EXCHANGE,
                capture_version=_TEST_CAPTURE_VERSION,
                since=now,
                until=now - timedelta(minutes=1),
                min_volume_24h_usd=1.0,
                extreme_threshold_pct=1.0,
            )
    finally:
        await engine.dispose()


async def test_fetch_prices_at_returns_only_exact_timestamp_matches() -> None:
    engine = await _connect_or_skip()
    try:
        symbol = "EXACTPRICEUSDT"
        await _seed_bar(engine, symbol=symbol, bucket_start=_START, close_price=42.0)
        # A neighboring bar one minute off must never be substituted in.
        await _seed_bar(
            engine, symbol=symbol, bucket_start=_START + timedelta(minutes=1), close_price=99.0
        )

        repository = MomentumFlowBidirectionalBurstRepository(engine)
        prices = await repository.fetch_prices_at(
            exchange=_TEST_EXCHANGE,
            capture_version=_TEST_CAPTURE_VERSION,
            market_type=_TEST_MARKET_TYPE,
            symbol_timestamps=[
                (symbol, _START),
                (symbol, _START + timedelta(minutes=5)),  # no bar here
            ],
        )
        assert prices == {(symbol, _START): 42.0}
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_fetch_symbol_baseline_forward_returns_computes_unconditional_mean() -> None:
    engine = await _connect_or_skip()
    try:
        symbol = "BASELINEUSDT"
        await _seed_bar(engine, symbol=symbol, bucket_start=_START, close_price=100.0)
        await _seed_bar(
            engine, symbol=symbol, bucket_start=_START + timedelta(minutes=15), close_price=110.0
        )
        await _seed_bar(
            engine, symbol=symbol, bucket_start=_START + timedelta(minutes=1), close_price=200.0
        )
        await _seed_bar(
            engine, symbol=symbol, bucket_start=_START + timedelta(minutes=16), close_price=190.0
        )
        # No forward bar for this third base minute -- must simply be
        # excluded from the average, not treated as a zero return.
        await _seed_bar(
            engine, symbol=symbol, bucket_start=_START + timedelta(minutes=2), close_price=100.0
        )

        repository = MomentumFlowBidirectionalBurstRepository(engine)
        baseline = await repository.fetch_symbol_baseline_forward_returns(
            exchange=_TEST_EXCHANGE,
            capture_version=_TEST_CAPTURE_VERSION,
            market_type=_TEST_MARKET_TYPE,
            since=_START,
            until=_START + timedelta(minutes=20),
            symbols=[symbol],
            horizons_minutes=[15],
        )
        # (110/100 - 1)*100 = 10.0, (190/200 - 1)*100 = -5.0 -> mean 2.5
        assert baseline[symbol][15] == pytest.approx(2.5)
    finally:
        await _cleanup(engine)
        await engine.dispose()
