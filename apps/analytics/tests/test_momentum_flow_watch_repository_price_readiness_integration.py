"""Real-Postgres integration test for MomentumFlowWatchRepository.
has_any_recent_valid_price -- the exact query the whole readiness-gate
mechanism (momentum_flow_producer_readiness.py, wired into
run_watch_worker/run_paper_worker) depends on to catch a producer that
never populates close_price. This is the mechanism that would have caught
the 2026-08-15..2026-08-17 momentum_flow_watch_binance incident (see
docs/research/binance-watch-input-readiness-v1.md) at container startup
instead of 32+ hours of silently reporting "ok" -- worth a real-schema
check, not just a mocked one, given the stakes.

Matches infra/docker/docker-compose.dev.yml's local dev Postgres, same
convention as this session's other real-Postgres tests. Skips (not
fails) when no Postgres is reachable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.momentum_flow_watch_contract import WatchContract
from schurfer_analytics.momentum_flow_watch_repository import MomentumFlowWatchRepository
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

TEST_DATABASE_URL = "postgresql+psycopg://schurfer:schurfer_dev@localhost:5432/schurfer"

_TEST_EXCHANGE = "test_price_readiness"
_TEST_CAPTURE_VERSION = "test_capture_v1"
_TEST_MARKET_TYPE = "linear"


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
    bucket_start: datetime,
    close_price: float | None,
    complete: bool = True,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("""
                INSERT INTO timeseries.bybit_momentum_bars_1m
                    (exchange, market_type, symbol, capture_version, bucket_start,
                     universe_version, close_price, buy_total_notional_usd,
                     sell_total_notional_usd, buy_hist_counts, buy_hist_notional,
                     sell_hist_counts, sell_hist_notional, ticker_complete,
                     trades_complete, complete, payload_hash)
                VALUES
                    (:exchange, :market_type, 'TESTUSDT', :capture_version, :bucket_start,
                     'universe-v1', :close_price, 0, 0, '{}', '{}', '{}', '{}',
                     :complete, :complete, :complete, decode(repeat('ab', 32), 'hex'))
                ON CONFLICT DO NOTHING
            """),
            {
                "exchange": _TEST_EXCHANGE,
                "market_type": _TEST_MARKET_TYPE,
                "capture_version": _TEST_CAPTURE_VERSION,
                "bucket_start": bucket_start,
                "close_price": close_price,
                "complete": complete,
            },
        )


async def _cleanup(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM timeseries.bybit_momentum_bars_1m WHERE exchange = :exchange"),
            {"exchange": _TEST_EXCHANGE},
        )


def _contract() -> WatchContract:
    return WatchContract(
        source_exchange=_TEST_EXCHANGE,
        market_type=_TEST_MARKET_TYPE,
        capture_version=_TEST_CAPTURE_VERSION,
    )


async def test_true_when_a_recent_bar_has_close_price() -> None:
    engine = await _connect_or_skip()
    try:
        now = datetime.now(tz=UTC).replace(second=0, microsecond=0)
        await _seed_bar(engine, bucket_start=now - timedelta(minutes=2), close_price=42.5)

        repository = MomentumFlowWatchRepository(engine)
        ready = await repository.has_any_recent_valid_price(
            contract=_contract(), lookback_minutes=30
        )

        assert ready is True
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_false_when_recent_bars_never_populate_close_price() -> None:
    """The exact real-world shape of the incident: bars exist (OI/flow
    data is flowing fine), but close_price is NULL on every one of them."""
    engine = await _connect_or_skip()
    try:
        now = datetime.now(tz=UTC).replace(second=0, microsecond=0)
        for offset in range(5):
            await _seed_bar(engine, bucket_start=now - timedelta(minutes=offset), close_price=None)

        repository = MomentumFlowWatchRepository(engine)
        ready = await repository.has_any_recent_valid_price(
            contract=_contract(), lookback_minutes=30
        )

        assert ready is False
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_false_when_only_stale_bars_have_close_price() -> None:
    """A close_price existed once, long before the lookback window -- must
    not count as currently ready."""
    engine = await _connect_or_skip()
    try:
        now = datetime.now(tz=UTC).replace(second=0, microsecond=0)
        await _seed_bar(engine, bucket_start=now - timedelta(hours=3), close_price=42.5)
        await _seed_bar(engine, bucket_start=now - timedelta(minutes=1), close_price=None)

        repository = MomentumFlowWatchRepository(engine)
        ready = await repository.has_any_recent_valid_price(
            contract=_contract(), lookback_minutes=30
        )

        assert ready is False
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_false_when_close_price_is_zero() -> None:
    """A colleague review's own finding: a bare NOT NULL check would pass
    on a zero/garbage price. close_price > 0 is required, not just
    close_price IS NOT NULL."""
    engine = await _connect_or_skip()
    try:
        now = datetime.now(tz=UTC).replace(second=0, microsecond=0)
        await _seed_bar(engine, bucket_start=now - timedelta(minutes=1), close_price=0.0)

        repository = MomentumFlowWatchRepository(engine)
        ready = await repository.has_any_recent_valid_price(
            contract=_contract(), lookback_minutes=30
        )

        assert ready is False
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_false_when_only_incomplete_bars_have_a_valid_price() -> None:
    """A colleague review's own finding: a still-forming or gap-marked
    synthetic bar (complete=false) is not evidence the producer can feed
    a real decision, even if it happens to carry a positive close_price."""
    engine = await _connect_or_skip()
    try:
        now = datetime.now(tz=UTC).replace(second=0, microsecond=0)
        await _seed_bar(
            engine, bucket_start=now - timedelta(minutes=1), close_price=42.5, complete=False
        )

        repository = MomentumFlowWatchRepository(engine)
        ready = await repository.has_any_recent_valid_price(
            contract=_contract(), lookback_minutes=30
        )

        assert ready is False
    finally:
        await _cleanup(engine)
        await engine.dispose()
