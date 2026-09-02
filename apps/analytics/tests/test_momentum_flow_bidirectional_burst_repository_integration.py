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
    candidate_query_windows,
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


def test_candidate_query_windows_are_half_open_daily_chunks() -> None:
    until = _START + timedelta(days=2, hours=3)
    assert candidate_query_windows(_START, until) == (
        (_START, _START + timedelta(days=1)),
        (_START + timedelta(days=1), _START + timedelta(days=2)),
        (_START + timedelta(days=2), until),
    )


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


async def test_fetch_candidate_extreme_minutes_rejects_a_gap_in_the_strict_windows() -> None:
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
        # A RANGE frame prevents old physical rows from being pulled across
        # the wall-clock gap. The stricter v2 contract additionally rejects
        # the row because neither the 5m nor 24h window is continuous.
        assert minutes == ()
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_fetch_candidate_extreme_minutes_detects_a_genuine_burst() -> None:
    # Colleague review, 2026-09-01: every existing test against this query
    # was a NEGATIVE case (rejects a gap, rejects since>until) -- none
    # actually proved the SQL's own burst-percentage arithmetic
    # (100.0 * buy_notional_5m / total_volume_24h) computes the right
    # number on a clean, gapless dataset where a burst genuinely SHOULD be
    # detected. A gapless 24h/1440-minute window: the first 1435 minutes
    # are flat baseline volume, the last 5 minutes (the query's own
    # candidate row and its 4 preceding minutes) carry a large one-sided
    # buy burst -- hand-computed expected percentages below.
    engine = await _connect_or_skip()
    try:
        symbol = "POSBURSTUSDT"
        target = _START
        baseline_minutes = 1435
        baseline_buy = 100.0
        baseline_sell = 100.0
        burst_minutes = 5
        burst_buy = 50_000.0
        for i in range(baseline_minutes):
            await _seed_bar(
                engine,
                symbol=symbol,
                bucket_start=target - timedelta(minutes=baseline_minutes + burst_minutes - 1 - i),
                close_price=1.0,
                buy_notional=baseline_buy,
                sell_notional=baseline_sell,
            )
        for i in range(burst_minutes):
            await _seed_bar(
                engine,
                symbol=symbol,
                bucket_start=target - timedelta(minutes=burst_minutes - 1 - i),
                close_price=2.0,
                buy_notional=burst_buy,
                sell_notional=0.0,
            )

        total_volume_24h = baseline_minutes * (baseline_buy + baseline_sell) + burst_minutes * (
            burst_buy
        )
        expected_buy_pct = 100.0 * (burst_minutes * burst_buy) / total_volume_24h

        repository = MomentumFlowBidirectionalBurstRepository(engine)
        minutes = await repository.fetch_candidate_extreme_minutes(
            exchange=_TEST_EXCHANGE,
            capture_version=_TEST_CAPTURE_VERSION,
            market_type=_TEST_MARKET_TYPE,
            since=target,
            until=target + timedelta(minutes=1),
            min_volume_24h_usd=1.0,
            extreme_threshold_pct=10.0,
        )
        assert len(minutes) == 1
        (minute,) = minutes
        assert minute.symbol == symbol
        assert minute.bucket_start == target
        assert minute.close_price == pytest.approx(2.0)
        assert minute.buy_burst_pct_5m == pytest.approx(expected_buy_pct)
        assert minute.sell_burst_pct_5m == pytest.approx(0.0)
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
