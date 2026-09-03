"""Real-Postgres coverage for OfflineBarsExtractRepository and the DuckDB
offline burst query (research/cex-activity-offline-denominator-v1).

The one thing worth proving here beyond "the SQL parses": the offline path
(Postgres extract -> Parquet -> DuckDB window query) computes EXACTLY the
same `BurstMinute` tuples as the existing live path
(`MomentumFlowBidirectionalBurstRepository.fetch_candidate_extreme_minutes`,
a single RANGE-window query against live Postgres) on identical seeded
input. An offline replica that quietly disagrees with the query it exists to
replace is worse than no replica at all -- it would make HYP-016 answerable
again while silently changing the answer.

Matches infra/docker/docker-compose.dev.yml's local dev Postgres, same
convention as the other real-Postgres tests in this package. Skips (not
fails) when no Postgres is reachable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from schurfer_analytics.momentum_flow_bidirectional_burst_offline_repository import (
    OfflineBarsExtractRepository,
    fetch_candidate_extreme_minutes_offline,
)
from schurfer_analytics.momentum_flow_bidirectional_burst_repository import (
    MomentumFlowBidirectionalBurstRepository,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

TEST_DATABASE_URL = "postgresql+psycopg://schurfer:schurfer_dev@localhost:5432/schurfer"

_TEST_EXCHANGE = "test_bidir_burst_offline"
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


async def _seed_gapless_burst_window(engine: AsyncEngine, *, symbol: str, target: datetime) -> None:
    """Same shape as the live-path positive test: 1435 flat baseline
    minutes, then a 5-minute one-sided buy burst ending at `target`."""
    baseline_minutes = 1435
    burst_minutes = 5
    for i in range(baseline_minutes):
        await _seed_bar(
            engine,
            symbol=symbol,
            bucket_start=target - timedelta(minutes=baseline_minutes + burst_minutes - 1 - i),
            close_price=1.0,
            buy_notional=100.0,
            sell_notional=100.0,
        )
    for i in range(burst_minutes):
        await _seed_bar(
            engine,
            symbol=symbol,
            bucket_start=target - timedelta(minutes=burst_minutes - 1 - i),
            close_price=2.0,
            buy_notional=50_000.0,
            sell_notional=0.0,
        )


async def test_extract_bars_to_parquet_writes_readable_deduped_rows(tmp_path: Path) -> None:
    engine = await _connect_or_skip()
    try:
        symbol = "EXTRACTUSDT"
        await _seed_bar(
            engine, symbol=symbol, bucket_start=_START, close_price=1.5, buy_notional=10.0
        )
        await _seed_bar(
            engine,
            symbol=symbol,
            bucket_start=_START + timedelta(minutes=1),
            close_price=1.6,
            buy_notional=20.0,
        )

        repository = OfflineBarsExtractRepository(engine)
        manifest = await repository.extract_bars_to_parquet(
            exchange=_TEST_EXCHANGE,
            capture_version=_TEST_CAPTURE_VERSION,
            market_type=_TEST_MARKET_TYPE,
            since=_START,
            until=_START + timedelta(minutes=2),
            output_path=tmp_path / "bars.parquet",
        )
        assert manifest.row_count == 2
        assert manifest.symbol_count == 1
        assert Path(manifest.parquet_path).exists()

        import duckdb

        connection = duckdb.connect(":memory:")
        try:
            rows = connection.execute(
                "SELECT symbol, bucket_start, close_price, buy_total_notional_usd "
                "FROM read_parquet(?) ORDER BY bucket_start",
                [manifest.parquet_path],
            ).fetchall()
        finally:
            connection.close()
        assert [row[0] for row in rows] == [symbol, symbol]
        assert rows[0][2] == pytest.approx(1.5)
        assert rows[1][3] == pytest.approx(20.0)
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_extract_bars_to_parquet_rejects_since_after_until(tmp_path: Path) -> None:
    engine = await _connect_or_skip()
    try:
        repository = OfflineBarsExtractRepository(engine)
        now = datetime.now(UTC)
        with pytest.raises(ValueError, match="since must be earlier"):
            await repository.extract_bars_to_parquet(
                exchange=_TEST_EXCHANGE,
                capture_version=_TEST_CAPTURE_VERSION,
                since=now,
                until=now - timedelta(minutes=1),
                output_path=tmp_path / "bars.parquet",
            )
    finally:
        await engine.dispose()


async def test_offline_query_matches_live_query_on_identical_seeded_data(tmp_path: Path) -> None:
    engine = await _connect_or_skip()
    try:
        symbol = "PARITYUSDT"
        target = _START
        await _seed_gapless_burst_window(engine, symbol=symbol, target=target)

        since = target
        until = target + timedelta(minutes=1)
        min_volume_24h_usd = 1.0
        extreme_threshold_pct = 10.0

        live_repository = MomentumFlowBidirectionalBurstRepository(engine)
        live_minutes = await live_repository.fetch_candidate_extreme_minutes(
            exchange=_TEST_EXCHANGE,
            capture_version=_TEST_CAPTURE_VERSION,
            market_type=_TEST_MARKET_TYPE,
            since=since,
            until=until,
            min_volume_24h_usd=min_volume_24h_usd,
            extreme_threshold_pct=extreme_threshold_pct,
        )
        assert len(live_minutes) == 1  # sanity: the seeded burst is real

        extract_repository = OfflineBarsExtractRepository(engine)
        manifest = await extract_repository.extract_bars_to_parquet(
            exchange=_TEST_EXCHANGE,
            capture_version=_TEST_CAPTURE_VERSION,
            market_type=_TEST_MARKET_TYPE,
            since=since,
            until=until,
            output_path=tmp_path / "parity.parquet",
        )
        offline_minutes = fetch_candidate_extreme_minutes_offline(
            Path(manifest.parquet_path),
            exchange=_TEST_EXCHANGE,
            since=since,
            until=until,
            min_volume_24h_usd=min_volume_24h_usd,
            extreme_threshold_pct=extreme_threshold_pct,
        )

        assert offline_minutes == live_minutes
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_offline_query_rejects_since_after_until(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="since must be earlier"):
        fetch_candidate_extreme_minutes_offline(
            tmp_path / "does-not-need-to-exist.parquet",
            exchange=_TEST_EXCHANGE,
            since=now,
            until=now - timedelta(minutes=1),
            min_volume_24h_usd=1.0,
            extreme_threshold_pct=1.0,
        )
