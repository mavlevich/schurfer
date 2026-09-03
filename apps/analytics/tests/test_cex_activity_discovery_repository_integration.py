"""Real-Postgres coverage for the exact 24-hour CEX activity path."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.cex_activity_discovery import OUTCOME_HORIZON_MINUTES, PathRequest
from schurfer_analytics.cex_activity_discovery_repository import CexActivityDiscoveryRepository
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

TEST_DATABASE_URL = "postgresql+psycopg://schurfer:schurfer_dev@localhost:5432/schurfer"

_TEST_EXCHANGE = "test_cex_activity"
_TEST_CAPTURE_VERSION = "test_capture_v1"
_TEST_MARKET_TYPE = "linear"
_SYMBOL = "EXACTPATHUSDT"
_TRIGGER_AT = datetime(2026, 8, 17, 0, 0, tzinfo=UTC)

_INSERT_BAR_SQL = text("""
    INSERT INTO timeseries.bybit_momentum_bars_1m
        (exchange, market_type, symbol, capture_version, bucket_start,
         universe_version, open_price, high_price, low_price, close_price,
         buy_total_notional_usd, sell_total_notional_usd,
         buy_hist_counts, buy_hist_notional, sell_hist_counts, sell_hist_notional,
         price_complete, ticker_complete, trades_complete, complete, payload_hash)
    VALUES
        (:exchange, :market_type, :symbol, :capture_version, :bucket_start,
         'universe-v1', :open_price, :high_price, :low_price, :close_price,
         0, 0, '{}', '{}', '{}', '{}',
         true, true, true, true, decode(repeat('ac', 32), 'hex'))
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


async def _cleanup(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM timeseries.bybit_momentum_bars_1m WHERE exchange = :exchange"),
            {"exchange": _TEST_EXCHANGE},
        )


async def _seed_full_path(engine: AsyncEngine) -> None:
    rows = []
    for minute in range(1, OUTCOME_HORIZON_MINUTES + 1):
        bucket_start = _TRIGGER_AT + timedelta(minutes=minute)
        rows.append(
            {
                "exchange": _TEST_EXCHANGE,
                "market_type": _TEST_MARKET_TYPE,
                "symbol": _SYMBOL,
                "capture_version": _TEST_CAPTURE_VERSION,
                "bucket_start": bucket_start,
                "open_price": 100.0,
                "high_price": 126.0 if minute == 121 else 101.0,
                "low_price": 74.0 if minute == 241 else 99.0,
                "close_price": 100.0,
            }
        )
    async with engine.begin() as connection:
        await connection.execute(_INSERT_BAR_SQL, rows)


async def test_fetch_exact_paths_requires_all_1440_native_minutes() -> None:
    engine = await _connect_or_skip()
    try:
        await _cleanup(engine)
        await _seed_full_path(engine)
        repository = CexActivityDiscoveryRepository(engine)
        request = PathRequest("signal:1", _SYMBOL, _TRIGGER_AT, _TRIGGER_AT + timedelta(minutes=1))

        paths = await repository.fetch_exact_paths(
            exchange=_TEST_EXCHANGE,
            market_type=_TEST_MARKET_TYPE,
            capture_version=_TEST_CAPTURE_VERSION,
            requests=(request,),
        )
        path = paths[request.request_id]
        assert path.resolved
        assert path.entry_at == _TRIGGER_AT + timedelta(minutes=1)
        assert path.entry_price == 100.0
        assert path.observed_minutes == OUTCOME_HORIZON_MINUTES
        assert path.max_high == 126.0
        assert path.min_low == 74.0
        assert path.first_up_25_at == _TRIGGER_AT + timedelta(minutes=121)
        assert path.first_down_25_at == _TRIGGER_AT + timedelta(minutes=241)

        async with engine.begin() as connection:
            await connection.execute(
                text("""
                    DELETE FROM timeseries.bybit_momentum_bars_1m
                    WHERE exchange = :exchange AND symbol = :symbol
                      AND bucket_start = :bucket_start
                """),
                {
                    "exchange": _TEST_EXCHANGE,
                    "symbol": _SYMBOL,
                    "bucket_start": _TRIGGER_AT + timedelta(minutes=500),
                },
            )
        gapped = (
            await repository.fetch_exact_paths(
                exchange=_TEST_EXCHANGE,
                market_type=_TEST_MARKET_TYPE,
                capture_version=_TEST_CAPTURE_VERSION,
                requests=(request,),
            )
        )[request.request_id]
        assert gapped.observed_minutes == OUTCOME_HORIZON_MINUTES - 1
        assert not gapped.resolved
    finally:
        await _cleanup(engine)
        await engine.dispose()
