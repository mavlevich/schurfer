"""Real-Postgres coverage for freeze_dataset's offline-denominator wiring
(research/cex-activity-discovery-completion-v1, colleague review 2026-09-03
planning: "wiring this into cex_activity_discovery_report.py itself" was
explicitly called out as the one piece the offline-denominator PR (#327)
itself deliberately left undone -- infra only, no caller).

This is the one thing the offline repository's own already-merged test
suite cannot prove by itself: that freeze_dataset actually constructs
OfflineBarsExtractRepository, calls extract_bars_to_parquet then
fetch_candidate_extreme_minutes_offline with the right arguments, and
threads the resulting candidates through episode-building, path-fetching,
and the dataset-artifact freeze -- against REAL Postgres, not a mock.
Equivalence between the offline and live 5m/24h computations themselves is
already proven by
test_offline_query_matches_live_query_on_identical_seeded_data in that
suite; this file does not re-prove that, only the wiring around it.

exchange/market_type/capture_version are frozen CLI defaults (see
test_cex_activity_discovery.py's own
test_cex_v1_primary_threshold_cannot_be_overridden_from_the_cli) -- not
overridable via build_parser()'s own CLI surface. This file constructs the
argparse.Namespace freeze_dataset actually consumes directly, rather than
through build_parser().parse_args(), so a test-only exchange/market_type/
capture_version can scope seeded rows away from any other data in this
Postgres.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from schurfer_analytics import cex_activity_discovery_report as report_module
from schurfer_analytics.cex_activity_discovery_dataset_artifact import (
    read as read_artifact,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

if TYPE_CHECKING:
    from pathlib import Path

TEST_DATABASE_URL = "postgresql+psycopg://schurfer:schurfer_dev@localhost:5432/schurfer"

_TEST_EXCHANGE = "test_cex_activity_report_freeze"
_TEST_CAPTURE_VERSION = "test_capture_v1"
_TEST_MARKET_TYPE = "linear"
_TARGET = datetime(2026, 8, 17, 0, 0, tzinfo=UTC)

_INSERT_BAR_SQL = text("""
    INSERT INTO timeseries.bybit_momentum_bars_1m
        (exchange, market_type, symbol, capture_version, bucket_start,
         universe_version, open_price, high_price, low_price, close_price,
         buy_total_notional_usd, sell_total_notional_usd,
         buy_hist_counts, buy_hist_notional, sell_hist_counts, sell_hist_notional,
         price_complete, ticker_complete, trades_complete, complete, payload_hash)
    VALUES
        (:exchange, :market_type, :symbol, :capture_version, :bucket_start,
         'universe-v1', :close_price, :close_price, :close_price, :close_price,
         :buy_notional, :sell_notional, '{}', '{}', '{}', '{}',
         true, true, true, true, decode(repeat('cd', 32), 'hex'))
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


async def _seed_gapless_burst_window(engine: AsyncEngine, *, symbol: str, target: datetime) -> None:
    """Same shape as momentum_flow_bidirectional_burst_offline_repository's
    own test suite: 1435 flat baseline minutes, then a 5-minute one-sided
    buy burst ending at `target` -- easily crosses the report's own
    DEFAULT_EXTREME_THRESHOLD_PCT/DEFAULT_MIN_VOLUME_24H_USD defaults."""
    baseline_minutes = 1435
    burst_minutes = 5
    rows = [
        {
            "exchange": _TEST_EXCHANGE,
            "market_type": _TEST_MARKET_TYPE,
            "symbol": symbol,
            "capture_version": _TEST_CAPTURE_VERSION,
            "bucket_start": target - timedelta(minutes=baseline_minutes + burst_minutes - 1 - i),
            "close_price": 1.0,
            "buy_notional": 100.0,
            "sell_notional": 100.0,
        }
        for i in range(baseline_minutes)
    ]
    rows.extend(
        {
            "exchange": _TEST_EXCHANGE,
            "market_type": _TEST_MARKET_TYPE,
            "symbol": symbol,
            "capture_version": _TEST_CAPTURE_VERSION,
            "bucket_start": target - timedelta(minutes=burst_minutes - 1 - i),
            "close_price": 2.0,
            "buy_notional": 50_000.0,
            "sell_notional": 0.0,
        }
        for i in range(burst_minutes)
    )
    async with engine.begin() as connection:
        await connection.execute(_INSERT_BAR_SQL, rows)


def _freeze_args(
    *, since: datetime, until: datetime, artifact_directory: Path
) -> argparse.Namespace:
    return argparse.Namespace(
        since=since,
        until=until,
        exchange=_TEST_EXCHANGE,
        market_type=_TEST_MARKET_TYPE,
        capture_version=_TEST_CAPTURE_VERSION,
        extreme_threshold_pct=10.0,
        refractory_minutes=60,
        min_volume_24h_usd=50_000.0,
        max_candidate_minutes=200_000,
        max_path_requests=200_000,
        code_revision="deadbeef",
        working_tree_dirty=False,
        artifact_directory=str(artifact_directory),
        # None -- exercises the common-case path: freeze_dataset opens its
        # own temp scratch directory for the Parquet extract and deletes
        # it once the candidate query has run, never leaving a disposable
        # extract file behind next to the immutable artifact.
        extract_directory=None,
    )


async def test_freeze_dataset_writes_a_real_artifact_via_the_offline_extract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = await _connect_or_skip()
    try:
        await _cleanup(engine)
        symbol = "REPORTFREEZEUSDT"
        await _seed_gapless_burst_window(engine, symbol=symbol, target=_TARGET)

        since = _TARGET
        until = _TARGET + timedelta(minutes=1)
        monkeypatch.setattr(report_module, "DISCOVERY_SINCE", since)
        monkeypatch.setattr(report_module, "DISCOVERY_UNTIL", until)
        monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)

        args = _freeze_args(since=since, until=until, artifact_directory=tmp_path / "artifacts")

        manifest = await report_module.freeze_dataset(args)

        assert manifest.row_count == 1
        assert manifest.cohort["exchange"] == _TEST_EXCHANGE
        assert manifest.extra["candidate_extreme_minutes"] == 1
        # Extract provenance actually came from a real extract_bars_to_
        # parquet run, not a stub -- the burst's own 1440 rows plus the
        # 5 already-counted burst rows share bucket_start values, so
        # row_count should be at least the 1435 baseline + 5 burst rows.
        assert manifest.extra["extract_row_count"] >= 1440
        assert manifest.extra["extract_symbol_count"] == 1
        assert manifest.extra["extract_query_version"]
        assert manifest.extra["extract_parquet_sha256"]

        read_manifest, episodes, _signal_paths, _controls, _control_paths = read_artifact(
            manifest.fingerprint, directory=str(tmp_path / "artifacts")
        )
        assert read_manifest.fingerprint == manifest.fingerprint
        assert len(episodes) == 1
        assert episodes[0].symbol == symbol
    finally:
        await _cleanup(engine)
        await engine.dispose()
