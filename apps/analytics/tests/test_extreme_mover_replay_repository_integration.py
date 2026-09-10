"""Real-PostgreSQL selection/outcome boundary for extreme-mover replay."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.extreme_mover_replay import DISCOVERY_END, DISCOVERY_START, build_report
from schurfer_analytics.extreme_mover_replay_repository import ExtremeMoverReplayRepository
from schurfer_analytics.replay import ReplayFilters
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

TEST_DATABASE_URL = "postgresql+psycopg://schurfer:schurfer_dev@localhost:5432/schurfer"
T0 = datetime(2026, 9, 1, 12, tzinfo=UTC)


async def _connect_or_skip() -> AsyncEngine:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise RuntimeError(
                f"REQUIRE_INTEGRATION_DB=1 but PostgreSQL is unreachable: {exc}"
            ) from exc
        pytest.skip(f"no local PostgreSQL reachable: {exc}")
    return engine


async def test_repository_preserves_selected_decisions_partial_and_resolver_version() -> None:
    engine = await _connect_or_skip()
    base = f"XMR{uuid.uuid4().hex[:6]}".upper()
    first_id = str(uuid.uuid4())
    quality_id = str(uuid.uuid4())
    try:
        async with engine.begin() as connection:
            event_id = (
                await connection.execute(
                    text("""
                        INSERT INTO app.pump_events
                            (base, episode, miss_count, first_seen_at, last_seen_at,
                             peak_pct, last_pct, exchanges, closed_at)
                        VALUES (:base, 1, 0, :ts, :ts, 50, 40, '[]'::jsonb,
                                :closed_at)
                        RETURNING id
                    """),
                    {"base": base, "ts": T0, "closed_at": T0 + timedelta(hours=2)},
                )
            ).scalar_one()
            for decision_id, offset, allowed in (
                (first_id, 0, False),
                (quality_id, 1, True),
            ):
                await connection.execute(
                    text("""
                        INSERT INTO app.trade_decisions (
                            ts, base, exchange, action, reason, decision_id,
                            pump_event_id, strategy_version, features, liquidity,
                            price, created_at
                        ) VALUES (
                            :ts, :base, 'binance', 'skipped', 'integration test',
                            :decision_id, :event_id, 'pump_short_measurement_v1',
                            jsonb_build_object(
                                'signal', jsonb_build_object(
                                    'computed_at', extract(epoch from CAST(:ts AS timestamptz))
                                ),
                                'config', '{}'::jsonb
                            ),
                            jsonb_build_object(
                                'status', 'sampled',
                                'quality', jsonb_build_object('allowed', :allowed),
                                'bid_impact_bps', jsonb_build_object('100', 4),
                                'ask_impact_bps', jsonb_build_object('100', 6)
                            ),
                            100, :ts
                        )
                    """),
                    {
                        "ts": T0 + timedelta(minutes=offset),
                        "base": base,
                        "decision_id": decision_id,
                        "event_id": event_id,
                        "allowed": allowed,
                    },
                )
            for decision_id, resolver, status in (
                (first_id, "forward_v1", "partial"),
                (first_id, "alternate_v1", "complete"),
                (quality_id, "forward_v1", "complete"),
            ):
                await connection.execute(
                    text("""
                        INSERT INTO app.trade_decision_outcomes (
                            decision_id, horizon_minutes, resolver_version,
                            anchor_exchange, source_exchange, timeframe_minutes,
                            entry_price, forward_price, mfe_pct, mae_pct,
                            short_return_pct, bars_count, expected_bars,
                            coverage_ratio, status, attempt_count, resolved_at,
                            created_at, updated_at
                        ) VALUES (
                            :decision_id, 60, :resolver, 'binance', 'binance', 1,
                            100, 110, 5, 12, -10, 60, 60, 1, :status, 1, :ts,
                            :ts, :ts
                        )
                    """),
                    {
                        "decision_id": decision_id,
                        "resolver": resolver,
                        "status": status,
                        "ts": T0 + timedelta(hours=1),
                    },
                )

        filters = ReplayFilters(
            since=DISCOVERY_START,
            until=DISCOVERY_END,
            strategy_versions=("pump_short_measurement_v1",),
            resolver_version="forward_v1",
            required_horizons=(60,),
        )
        repository = ExtremeMoverReplayRepository(engine)
        snapshot, decisions = await repository.load(filters)
        mine = tuple(row for row in decisions if row.base == base)
        report = build_report(
            mine,
            dataset_since=DISCOVERY_START,
            dataset_until_exclusive=DISCOVERY_END,
            database_snapshot_at=snapshot,
            generated_at=snapshot,
            code_revision="integration",
            working_tree_dirty=False,
            bootstrap_iterations=100,
        )

        first = next(
            row
            for row in report.episode_results
            if row.anchor == "first_decision"
            and row.direction == "long"
            and row.horizon_minutes == 60
        )
        quality = next(
            row
            for row in report.episode_results
            if row.anchor == "first_quality"
            and row.direction == "long"
            and row.horizon_minutes == 60
        )
        assert first.decision_id == first_id
        assert first.status == "unresolved"
        assert first.reason == "outcome_status:partial"
        assert quality.decision_id == quality_id
        assert quality.status == "complete"
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "DELETE FROM app.trade_decision_outcomes "
                    "WHERE decision_id IN (:first, :quality)"
                ),
                {"first": first_id, "quality": quality_id},
            )
            await connection.execute(
                text("DELETE FROM app.trade_decisions WHERE base = :base"),
                {"base": base},
            )
            await connection.execute(
                text("DELETE FROM app.pump_events WHERE base = :base"),
                {"base": base},
            )
        await engine.dispose()
