"""Real-Postgres coverage for MomentumFlowWatchRepository.
list_bucket_starts_in_window -- analysis/binance-watch-input-coverage-v1's
(PR4) own entry point for enumerating buckets to replay
prepare_symbol_evaluation against (see binance_watch_input_coverage_report.py).
Only a real query proves the exchange/market_type/capture_version scoping
and the half-open [since, until) bound actually hold, not just that the
SQLAlchemy statement builds without error.

Matches infra/docker/docker-compose.dev.yml's local dev Postgres, same
convention as this session's other real-Postgres tests. Skips (not
fails) when no Postgres is reachable.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.momentum_flow_watch_contract import WatchContract
from schurfer_analytics.momentum_flow_watch_repository import MomentumFlowWatchRepository
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

TEST_DATABASE_URL = "postgresql+psycopg://schurfer:schurfer_dev@localhost:5432/schurfer"

_TEST_EXCHANGE = "test_bucket_window"
_TEST_CAPTURE_VERSION = "test_capture_v1"
_TEST_MARKET_TYPE = "linear"
_START = datetime(2026, 8, 17, 0, 0, tzinfo=UTC)

_INSERT_BAR_SQL = text("""
    INSERT INTO timeseries.bybit_momentum_bars_1m
        (exchange, market_type, symbol, capture_version, bucket_start,
         universe_version, buy_hist_counts, buy_hist_notional,
         sell_hist_counts, sell_hist_notional,
         ticker_complete, trades_complete, complete, payload_hash)
    VALUES
        (:exchange, :market_type, :symbol, :capture_version, :bucket_start,
         'universe-v1', '{}', '{}', '{}', '{}',
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


async def _seed_bars(engine: AsyncEngine, bucket_starts: list[datetime]) -> None:
    rows = [
        {
            "exchange": _TEST_EXCHANGE,
            "market_type": _TEST_MARKET_TYPE,
            "symbol": "WINDOWTESTUSDT",
            "capture_version": _TEST_CAPTURE_VERSION,
            "bucket_start": bucket_start,
        }
        for bucket_start in bucket_starts
    ]
    async with engine.begin() as connection:
        await connection.execute(_INSERT_BAR_SQL, rows)


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


async def test_list_bucket_starts_in_window_scopes_to_the_given_contract_and_range() -> None:
    engine = await _connect_or_skip()
    try:
        bucket_starts = [_START + timedelta(minutes=i) for i in range(5)]
        await _seed_bars(engine, bucket_starts)

        repository = MomentumFlowWatchRepository(engine)
        contract = _contract()

        starts = await repository.list_bucket_starts_in_window(
            contract=contract,
            since=bucket_starts[0],
            until=bucket_starts[-1] + timedelta(minutes=1),
        )
        assert starts == tuple(bucket_starts)

        # Half-open upper bound: until == the last bucket_start must
        # exclude it.
        starts_excluding_last = await repository.list_bucket_starts_in_window(
            contract=contract, since=bucket_starts[0], until=bucket_starts[-1]
        )
        assert bucket_starts[-1] not in starts_excluding_last
        assert starts_excluding_last == tuple(bucket_starts[:-1])

        # A narrower [since, until) sees only the buckets inside it.
        narrowed = await repository.list_bucket_starts_in_window(
            contract=contract, since=bucket_starts[1], until=bucket_starts[3]
        )
        assert narrowed == tuple(bucket_starts[1:3])

        # A different capture_version must see nothing here -- the exact
        # scoping binance_watch_input_coverage_report.py depends on to
        # never accidentally replay Bybit's own bars against
        # BINANCE_WATCH_CONTRACT.
        other_contract = replace(contract, capture_version="a-different-capture-version")
        assert (
            await repository.list_bucket_starts_in_window(
                contract=other_contract,
                since=bucket_starts[0],
                until=bucket_starts[-1] + timedelta(minutes=1),
            )
            == ()
        )
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_list_bucket_starts_in_window_rejects_since_after_until() -> None:
    engine = await _connect_or_skip()
    try:
        repository = MomentumFlowWatchRepository(engine)
        contract = _contract()
        now = datetime.now(UTC)
        with pytest.raises(ValueError, match="since must be earlier"):
            await repository.list_bucket_starts_in_window(
                contract=contract, since=now, until=now - timedelta(minutes=1)
            )
    finally:
        await engine.dispose()
