"""Real-Postgres coverage for point-in-time WATCH signal extraction."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from schurfer_analytics.radar_outcome_discovery_repository import (
    RadarOutcomeDiscoveryRepository,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

TEST_DATABASE_URL = "postgresql+psycopg://schurfer:schurfer_dev@localhost:5432/schurfer"
_EXCHANGE = "test_radar_outcome"
_MARKET_TYPE = "linear"
_CAPTURE_VERSION = "test_capture_v1"
_WATCH_VERSION = "test_radar_watch_v1"
_BUCKET_START = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)

_INSERT_WATCH = text("""
    INSERT INTO timeseries.momentum_flow_watch_evaluations_1m (
        exchange, market_type, symbol, capture_version, watch_version,
        bucket_start, universe_version, quality_ready, raw_qualified,
        decision_status, reason_codes, price_return_60m_pct,
        oi_growth_60m_pct, buy_imbalance_15m,
        flow_acceleration_15m_vs_prior_45m, cross_section_size,
        evaluator_started_at, evaluator_completed_at, decision_at,
        episode_id, watch_id, state_active_after, state_clear_streak_after,
        state_last_watch_at_after, input_hash
    ) VALUES (
        :exchange, :market_type, :symbol, :capture_version, :watch_version,
        :bucket_start, 'test_universe_v1', true, true,
        'watch', ARRAY[]::text[], 2.0,
        3.0, 0.4, 2.5, 100,
        :evaluator_started_at, :evaluator_completed_at, :decision_at,
        :episode_id, :watch_id, true, 0,
        :decision_at, :input_hash
    )
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


async def test_fetch_watch_signals_uses_decision_time_and_next_full_minute() -> None:
    engine = await _connect_or_skip()
    watch_id = uuid4()
    decision_at = _BUCKET_START + timedelta(minutes=1, seconds=12)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                _INSERT_WATCH,
                {
                    "exchange": _EXCHANGE,
                    "market_type": _MARKET_TYPE,
                    "symbol": "POINTINTIMEUSDT",
                    "capture_version": _CAPTURE_VERSION,
                    "watch_version": _WATCH_VERSION,
                    "bucket_start": _BUCKET_START,
                    "evaluator_started_at": decision_at - timedelta(milliseconds=2),
                    "evaluator_completed_at": decision_at + timedelta(milliseconds=2),
                    "decision_at": decision_at,
                    "episode_id": uuid4(),
                    "watch_id": watch_id,
                    "input_hash": b"r" * 32,
                },
            )

        repository = RadarOutcomeDiscoveryRepository(engine)
        signals = await repository.fetch_watch_signals(
            exchange=_EXCHANGE,
            market_type=_MARKET_TYPE,
            capture_version=_CAPTURE_VERSION,
            watch_version=_WATCH_VERSION,
            since=_BUCKET_START,
            until=_BUCKET_START + timedelta(minutes=1),
        )
        assert len(signals) == 1
        (signal,) = signals
        assert signal.watch_id == str(watch_id)
        assert signal.decision_at == decision_at
        assert signal.entry_at == _BUCKET_START + timedelta(minutes=2)
        assert signal.oi_growth_60m_pct == 3.0
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("""
                    DELETE FROM timeseries.momentum_flow_watch_evaluations_1m
                    WHERE exchange = :exchange AND watch_version = :watch_version
                """),
                {"exchange": _EXCHANGE, "watch_version": _WATCH_VERSION},
            )
        await engine.dispose()
