"""Real-Postgres coverage for LiquidationMakerUpperBoundRepository
(research/liquidation-maker-upper-bound-v1).

Only a real query against `timeseries.liquidation_events` proves the
RANGE-window trailing-notional math and the capture_version/estimated_
liquidation_notional-not-null scoping actually hold -- not just that the
SQLAlchemy text() statement parses.

Matches infra/docker/docker-compose.dev.yml's local dev Postgres, same
convention as this package's other real-Postgres tests. Skips (not fails)
when no Postgres is reachable.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.liquidation_maker_upper_bound_repository import (
    LiquidationMakerUpperBoundRepository,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

TEST_DATABASE_URL = "postgresql+psycopg://schurfer:schurfer_dev@localhost:5432/schurfer"

_TEST_EXCHANGE = "test_liq_maker_upper_bound"
_MARKET = "TESTUSDT"
_START = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)

_INSERT_EVENT_SQL = text("""
    INSERT INTO timeseries.liquidation_events (
        capture_version, exchange, market_type, native_market_id, universe_version,
        source_contract_variant, coverage_kind, position_side, event_at,
        exchange_published_at, received_at, source_session_id, source_event_key,
        payload_hash, quantity, quantity_unit, estimated_liquidation_notional, raw_payload
    ) VALUES (
        'liquidation_event_v1', :exchange, 'linear', :native_market_id, 'universe-v1',
        :source_contract_variant, :coverage_kind, :position_side, :event_at,
        :event_at, :event_at, :session_id, :event_key, :payload_hash,
        :quantity, 'contracts', :notional, '{}'::jsonb
    )
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


async def _seed_event(
    engine: AsyncEngine,
    *,
    event_at: datetime,
    notional: float,
    position_side: str = "long",
    native_market_id: str = _MARKET,
    coverage_kind: str = "complete_stream",
    source_contract_variant: str = "bybit_all_liquidation_v1",
    event_seq: int,
) -> None:
    key_source = f"{native_market_id}:{coverage_kind}:{event_at.isoformat()}:{event_seq}"
    event_key = hashlib.sha256(key_source.encode()).digest()
    payload_hash = hashlib.sha256(event_key).digest()
    async with engine.begin() as connection:
        await connection.execute(
            _INSERT_EVENT_SQL,
            {
                "exchange": _TEST_EXCHANGE,
                "native_market_id": native_market_id,
                "position_side": position_side,
                "coverage_kind": coverage_kind,
                "source_contract_variant": source_contract_variant,
                "event_at": event_at,
                "session_id": "test-session",
                "event_key": event_key,
                "payload_hash": payload_hash,
                "quantity": 1.0,
                "notional": notional,
            },
        )


async def _cleanup(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM timeseries.liquidation_events WHERE exchange = :exchange"),
            {"exchange": _TEST_EXCHANGE},
        )


async def test_fetch_trigger_minutes_computes_the_rolling_trailing_sum() -> None:
    engine = await _connect_or_skip()
    try:
        # Two $150k events one minute apart -> the second minute's trailing
        # 5-minute sum should be exactly $300k (both events fall inside the
        # trailing window), the first minute's own trailing sum only $150k.
        await _seed_event(engine, event_at=_START, notional=150_000.0, event_seq=1)
        await _seed_event(
            engine, event_at=_START + timedelta(minutes=1), notional=150_000.0, event_seq=2
        )

        repository = LiquidationMakerUpperBoundRepository(engine)
        rows = await repository.fetch_trigger_minutes(
            since=_START, until=_START + timedelta(minutes=2), limit=1000
        )
        by_minute = {row.bucket_start: row.trailing_notional_usd for row in rows}
        assert by_minute[_START] == pytest.approx(150_000.0)
        assert by_minute[_START + timedelta(minutes=1)] == pytest.approx(300_000.0)
        assert all(row.coverage_kind == "complete_stream" for row in rows)
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_fetch_trigger_minutes_never_blends_two_coverage_kinds_into_one_rolling_sum() -> None:
    """Colleague review, 2026-09-03: timeseries.liquidation_events captures
    Bybit as complete_stream and Binance as latest_per_symbol_1000ms --
    genuinely different measurement processes. Even on the SAME (exchange,
    native_market_id, position_side), two events under different
    coverage_kind values must produce two INDEPENDENT rolling sums, not one
    blended trailing_notional_usd -- proves the SQL's PARTITION BY now
    includes coverage_kind, not just exchange/market/side."""
    engine = await _connect_or_skip()
    try:
        await _seed_event(
            engine,
            event_at=_START,
            notional=150_000.0,
            coverage_kind="complete_stream",
            source_contract_variant="bybit_all_liquidation_v1",
            event_seq=1,
        )
        await _seed_event(
            engine,
            event_at=_START + timedelta(minutes=1),
            notional=150_000.0,
            coverage_kind="latest_per_symbol_1000ms",
            source_contract_variant="binance_merged_um_v1",
            event_seq=2,
        )

        repository = LiquidationMakerUpperBoundRepository(engine)
        rows = await repository.fetch_trigger_minutes(
            since=_START, until=_START + timedelta(minutes=2), limit=1000
        )
        by_coverage_and_minute = {(row.coverage_kind, row.bucket_start): row for row in rows}
        # If these were blended (the pre-fix behavior), the second minute's
        # trailing sum would be $300k, not $150k -- each coverage_kind's
        # own rolling sum must only ever see its own $150k event.
        assert by_coverage_and_minute[
            ("complete_stream", _START)
        ].trailing_notional_usd == pytest.approx(150_000.0)
        assert by_coverage_and_minute[
            ("latest_per_symbol_1000ms", _START + timedelta(minutes=1))
        ].trailing_notional_usd == pytest.approx(150_000.0)
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_fetch_trigger_minutes_excludes_events_outside_the_rolling_window() -> None:
    engine = await _connect_or_skip()
    try:
        await _seed_event(engine, event_at=_START, notional=200_000.0, event_seq=1)
        # 5 minutes later -- outside the (window - 1)-minute trailing range
        # of a minute another 5 minutes further still.
        far_minute = _START + timedelta(minutes=10)
        await _seed_event(engine, event_at=far_minute, notional=50_000.0, event_seq=2)

        repository = LiquidationMakerUpperBoundRepository(engine)
        rows = await repository.fetch_trigger_minutes(
            since=_START, until=far_minute + timedelta(minutes=1), limit=1000
        )
        by_minute = {row.bucket_start: row.trailing_notional_usd for row in rows}
        # far_minute's own trailing sum must NOT include the earlier
        # $200k event -- only its own $50k.
        assert by_minute[far_minute] == pytest.approx(50_000.0)
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_fetch_trigger_minutes_keeps_position_sides_separate() -> None:
    engine = await _connect_or_skip()
    try:
        await _seed_event(
            engine, event_at=_START, notional=100_000.0, position_side="long", event_seq=1
        )
        await _seed_event(
            engine, event_at=_START, notional=200_000.0, position_side="short", event_seq=2
        )

        repository = LiquidationMakerUpperBoundRepository(engine)
        rows = await repository.fetch_trigger_minutes(
            since=_START, until=_START + timedelta(minutes=1), limit=1000
        )
        by_side = {row.position_side: row.trailing_notional_usd for row in rows}
        assert by_side["long"] == pytest.approx(100_000.0)
        assert by_side["short"] == pytest.approx(200_000.0)
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_fetch_trigger_minutes_rejects_since_after_until() -> None:
    engine = await _connect_or_skip()
    try:
        repository = LiquidationMakerUpperBoundRepository(engine)
        now = datetime.now(UTC)
        with pytest.raises(ValueError, match="since must be earlier"):
            await repository.fetch_trigger_minutes(
                since=now, until=now - timedelta(minutes=1), limit=10
            )
    finally:
        await engine.dispose()


async def test_fetch_trigger_minutes_rejects_non_positive_limit() -> None:
    engine = await _connect_or_skip()
    try:
        repository = LiquidationMakerUpperBoundRepository(engine)
        now = datetime.now(UTC)
        with pytest.raises(ValueError, match="limit must be positive"):
            await repository.fetch_trigger_minutes(
                since=now, until=now + timedelta(minutes=1), limit=0
            )
    finally:
        await engine.dispose()
