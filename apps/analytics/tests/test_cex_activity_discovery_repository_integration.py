"""Real-Postgres coverage for the exact 24-hour CEX activity path."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.cex_activity_discovery import OUTCOME_HORIZON_MINUTES, PathRequest
from schurfer_analytics.cex_activity_discovery_repository import (
    PATH_BATCH_SIZE,
    CexActivityDiscoveryRepository,
    report_maturity_at,
)
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


async def test_fetch_exact_paths_rejects_over_wall_time_budget() -> None:
    """Colleague review, 2026-09-03 (research/cex-activity-discovery-
    completion-v1 planning): mirrors momentum_flow_bidirectional_burst_
    offline_repository.py's own wall-time-budget test -- max_wall_seconds=0.0
    means the deadline is already in the past by the time the very first
    per-batch check runs."""
    engine = await _connect_or_skip()
    try:
        repository = CexActivityDiscoveryRepository(engine)
        request = PathRequest("signal:1", _SYMBOL, _TRIGGER_AT, _TRIGGER_AT + timedelta(minutes=1))
        with pytest.raises(TimeoutError, match="max_wall_seconds"):
            await repository.fetch_exact_paths(
                exchange=_TEST_EXCHANGE,
                market_type=_TEST_MARKET_TYPE,
                capture_version=_TEST_CAPTURE_VERSION,
                requests=(request,),
                max_wall_seconds=0.0,
            )
    finally:
        await engine.dispose()


async def test_fetch_exact_paths_enforces_the_deadline_across_multiple_batches() -> None:
    """The same deadline persists across several batches, not a fresh
    per-batch budget -- a fake `_now` clock (the function's own private
    testing seam) lets the first few pre-batch checks pass with small,
    real-looking values, then jumps far past the deadline, proving the
    check fires at a LATER batch than the very first one without racing
    real wall-clock time. Three batches' worth of requests forces at least
    3 pre-batch checks (PATH_BATCH_SIZE=200)."""
    engine = await _connect_or_skip()
    try:
        repository = CexActivityDiscoveryRepository(engine)
        requests = tuple(
            PathRequest(f"signal:{i}", _SYMBOL, _TRIGGER_AT, _TRIGGER_AT + timedelta(minutes=1))
            for i in range(2 * PATH_BATCH_SIZE + 1)  # forces 3 batches
        )

        real_start = time.monotonic()
        call_count = 0

        def fake_now() -> float:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return real_start + call_count * 0.001
            return real_start + 10_000.0

        with pytest.raises(TimeoutError, match="max_wall_seconds"):
            await repository.fetch_exact_paths(
                exchange=_TEST_EXCHANGE,
                market_type=_TEST_MARKET_TYPE,
                capture_version=_TEST_CAPTURE_VERSION,
                requests=requests,
                max_wall_seconds=5.0,
                _now=fake_now,
            )
        # Sanity: the fake clock's early, real-looking values were actually
        # consumed (proving this exercised real batch processing before
        # the jump, not an immediate pre-first-batch failure).
        assert call_count > 1
    finally:
        await engine.dispose()


async def test_fetch_exact_paths_holds_one_snapshot_across_batches() -> None:
    """The exact gap colleague review, 2026-09-04 found: the previous
    per-batch connection/transaction meant a request landing in a LATER
    batch could observe a write committed WHILE an earlier batch was
    already running, even within one Python-level fetch_exact_paths call.
    Forces exactly 2 batches (PATH_BATCH_SIZE filler requests + 1 more),
    and uses the private `_after_batch` testing seam to commit a brand-new
    entry bar via a SEPARATE connection right after batch 1 finishes, for
    a symbol whose own request only appears in batch 2. If the two batches
    genuinely share one REPEATABLE READ snapshot (established at batch 1's
    own first query), batch 2's query -- issued strictly after the write
    was committed -- must still not see it."""
    engine = await _connect_or_skip()
    other_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        await _cleanup(engine)
        late_symbol = "LATESNAPUSDT"
        late_request = PathRequest(
            "signal:late", late_symbol, _TRIGGER_AT, _TRIGGER_AT + timedelta(minutes=1)
        )
        # PATH_BATCH_SIZE filler requests (batch 1, indices 0..199) + the
        # late request (batch 2, index 200) -- exactly 2 batches.
        requests = (
            *(
                PathRequest(f"signal:{i}", _SYMBOL, _TRIGGER_AT, _TRIGGER_AT + timedelta(minutes=1))
                for i in range(PATH_BATCH_SIZE)
            ),
            late_request,
        )

        async def _insert_late_entry_bar() -> None:
            async with other_engine.begin() as connection:
                await connection.execute(
                    _INSERT_BAR_SQL,
                    {
                        "exchange": _TEST_EXCHANGE,
                        "market_type": _TEST_MARKET_TYPE,
                        "symbol": late_symbol,
                        "capture_version": _TEST_CAPTURE_VERSION,
                        "bucket_start": _TRIGGER_AT + timedelta(minutes=1),
                        "open_price": 100.0,
                        "high_price": 101.0,
                        "low_price": 99.0,
                        "close_price": 100.0,
                    },
                )

        repository = CexActivityDiscoveryRepository(engine)
        paths = await repository.fetch_exact_paths(
            exchange=_TEST_EXCHANGE,
            market_type=_TEST_MARKET_TYPE,
            capture_version=_TEST_CAPTURE_VERSION,
            requests=requests,
            _after_batch=_insert_late_entry_bar,
        )
        late_path = paths[late_request.request_id]
        assert late_path.entry_price is None
        assert late_path.unresolved_reason == "missing_entry_bar"

        # Sanity: the bar really was committed and IS visible to a fresh
        # query outside that transaction -- proving the assertion above is
        # about snapshot isolation, not a seeding mistake.
        fresh_paths = await repository.fetch_exact_paths(
            exchange=_TEST_EXCHANGE,
            market_type=_TEST_MARKET_TYPE,
            capture_version=_TEST_CAPTURE_VERSION,
            requests=(late_request,),
        )
        assert fresh_paths[late_request.request_id].entry_price == 100.0
    finally:
        await _cleanup(engine)
        await engine.dispose()
        await other_engine.dispose()


# --- report_maturity_at (colleague review, 2026-09-03) ---------------------


def test_report_maturity_at_adds_horizon_and_one_minute_to_the_latest_entry() -> None:
    latest_entry_at = datetime(2026, 8, 27, 0, 1, tzinfo=UTC)
    maturity = report_maturity_at(latest_entry_at)
    assert maturity == latest_entry_at + timedelta(minutes=OUTCOME_HORIZON_MINUTES + 1)


def test_report_maturity_at_reflects_a_control_offset_past_the_window() -> None:
    """The exact scenario this fix closes: a control request offset up to
    CONTROL_SEARCH_DAYS forward of an episode near the discovery window's
    own end lands well past `until` -- report_maturity_at must be computed
    from THAT entry_at, not from `until` itself."""
    until = datetime(2026, 8, 27, tzinfo=UTC)
    control_entry_at = until + timedelta(days=6, minutes=1)  # a real, in-window-search offset
    old_naive_maturity = until + timedelta(minutes=OUTCOME_HORIZON_MINUTES + 1)
    real_maturity = report_maturity_at(control_entry_at)
    assert real_maturity > old_naive_maturity
