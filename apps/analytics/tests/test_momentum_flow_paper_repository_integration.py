"""Real-Postgres coverage for momentum PAPER freshness and health queries.

The test follows the repository integration-test convention in this package:
it skips when the local migrated development Postgres is unavailable, while CI
and developer environments with that service exercise the actual constraints.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from schurfer_analytics.momentum_flow_paper_contract import PaperContract
from schurfer_analytics.momentum_flow_paper_repository import (
    MomentumFlowPaperRepository,
    _outcomes,
    _probes,
    _runs,
    _watch_evaluations,
)
from sqlalchemy import delete, insert, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

TEST_DATABASE_URL = "postgresql+psycopg://schurfer:schurfer_dev@localhost:5432/schurfer"


async def _connect_or_skip() -> AsyncEngine:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"no local postgres reachable: {exc}")
    return engine


_INSERT_WATCH = text("""
    INSERT INTO timeseries.momentum_flow_watch_evaluations_1m (
        exchange,
        market_type,
        symbol,
        capture_version,
        watch_version,
        bucket_start,
        universe_version,
        quality_ready,
        raw_qualified,
        decision_status,
        reason_codes,
        cross_section_size,
        evaluator_started_at,
        evaluator_completed_at,
        decision_at,
        episode_id,
        watch_id,
        state_active_after,
        state_clear_streak_after,
        state_last_watch_at_after,
        input_hash
    ) VALUES (
        :exchange,
        :market_type,
        :symbol,
        :capture_version,
        :watch_version,
        :bucket_start,
        :universe_version,
        true,
        true,
        'watch',
        ARRAY[]::text[],
        1,
        :evaluator_started_at,
        :evaluator_completed_at,
        :decision_at,
        :episode_id,
        :watch_id,
        true,
        0,
        :decision_at,
        :input_hash
    )
""")


def _watch_row(
    *,
    symbol: str,
    watch_version: str,
    watch_id: UUID,
    decision_at: datetime,
) -> dict[str, object]:
    return {
        "exchange": "bybit",
        "market_type": "linear",
        "symbol": symbol,
        "capture_version": "test_capture_v1",
        "watch_version": watch_version,
        "bucket_start": decision_at.replace(second=0, microsecond=0) - timedelta(minutes=1),
        "universe_version": "test_universe_v1",
        "evaluator_started_at": decision_at - timedelta(milliseconds=2),
        "evaluator_completed_at": decision_at + timedelta(milliseconds=2),
        "decision_at": decision_at,
        "episode_id": uuid4(),
        "watch_id": watch_id,
        "input_hash": b"x" * 32,
    }


async def test_paper_repository_live_freshness_queries() -> None:
    engine = await _connect_or_skip()
    repository = MomentumFlowPaperRepository(engine)
    now = datetime.now(UTC)
    cohort_started_at = now - timedelta(hours=1)
    test_watch_version = f"test_watch_{uuid4()}"
    test_paper_version = f"test_paper_{uuid4()}"
    watch_id_fresh = uuid4()
    watch_id_boundary = uuid4()
    watch_id_expired = uuid4()
    watch_id_future = uuid4()
    watch_id_out_of_cohort = uuid4()
    probe_id_interrupted = uuid4()
    probe_id_closed = uuid4()

    contract = PaperContract(
        paper_version=test_paper_version,
        watch_version=test_watch_version,
        source_exchange="bybit",
        market_type="linear",
        max_hold_minutes=15,
        outcome_horizons_minutes=(5, 15),
        max_watch_to_quote_seconds=30,
    )

    try:
        await repository.register_run(
            contract=contract,
            contract_sha256=contract.sha256_hex(),
            now=cohort_started_at,
        )
        async with engine.begin() as connection:
            await connection.execute(
                _INSERT_WATCH,
                [
                    _watch_row(
                        symbol="BTCUSDT",
                        watch_version=test_watch_version,
                        watch_id=watch_id_fresh,
                        decision_at=now - timedelta(seconds=5),
                    ),
                    _watch_row(
                        symbol="ETHUSDT",
                        watch_version=test_watch_version,
                        watch_id=watch_id_boundary,
                        decision_at=now - timedelta(seconds=30),
                    ),
                    _watch_row(
                        symbol="SOLUSDT",
                        watch_version=test_watch_version,
                        watch_id=watch_id_expired,
                        decision_at=now - timedelta(seconds=30, microseconds=1),
                    ),
                    _watch_row(
                        symbol="XRPUSDT",
                        watch_version=test_watch_version,
                        watch_id=watch_id_future,
                        decision_at=now + timedelta(seconds=1),
                    ),
                    _watch_row(
                        symbol="ADAUSDT",
                        watch_version=test_watch_version,
                        watch_id=watch_id_out_of_cohort,
                        decision_at=cohort_started_at - timedelta(seconds=1),
                    ),
                ],
            )

        fresh = await repository.due_fresh_watches(
            contract=contract,
            cohort_started_at=cohort_started_at,
            now=now,
            limit=100,
        )
        assert {candidate.watch_id for candidate in fresh} == {
            watch_id_fresh,
            watch_id_boundary,
        }

        expired = await repository.due_expired_watches(
            contract=contract,
            cohort_started_at=cohort_started_at,
            now=now,
            limit=100,
        )
        assert tuple(candidate.watch_id for candidate in expired) == (watch_id_expired,)
        assert await repository.bulk_reject_stale_watches(expired, contract=contract, now=now) == 1
        assert await repository.bulk_reject_stale_watches(expired, contract=contract, now=now) == 0

        async with engine.begin() as connection:
            await connection.execute(
                insert(_probes).values(
                    paper_id=probe_id_interrupted,
                    paper_version=test_paper_version,
                    watch_version=test_watch_version,
                    watch_id=watch_id_boundary,
                    episode_id=uuid4(),
                    exchange="bybit",
                    market_type="linear",
                    symbol="ETHUSDT",
                    watch_bucket_start=now - timedelta(minutes=1),
                    watch_decision_at=now - timedelta(seconds=30),
                    claimed_at=now,
                    entry_status="unresolved_interrupted",
                    entry_reason="worker_interrupted_after_claim",
                    position_status="not_open",
                )
            )
            await connection.execute(
                insert(_probes).values(
                    paper_id=probe_id_closed,
                    paper_version=test_paper_version,
                    watch_version=test_watch_version,
                    watch_id=uuid4(),
                    episode_id=uuid4(),
                    exchange="bybit",
                    market_type="linear",
                    symbol="DOGEUSDT",
                    watch_bucket_start=now - timedelta(minutes=2),
                    watch_decision_at=now - timedelta(seconds=1),
                    claimed_at=now,
                    entry_status="opened",
                    entry_reason="exact_venue_executable_ask",
                    entry_at=now,
                    entry_vwap=100.0,
                    entry_filled_notional_usd=50.0,
                    position_status="closed",
                    exit_reason="max_hold",
                    exit_at=now + timedelta(minutes=15),
                    exit_vwap=101.0,
                    accounting_status="complete",
                )
            )
            await connection.execute(
                insert(_outcomes).values(
                    [
                        {
                            "paper_id": probe_id_closed,
                            "horizon_minutes": 5,
                            "due_at": now + timedelta(minutes=5),
                            "status": "complete",
                            "quote_observed_at": now + timedelta(minutes=5),
                            "bid_vwap": 101.0,
                            "error": None,
                        },
                        {
                            "paper_id": probe_id_closed,
                            "horizon_minutes": 15,
                            "due_at": now + timedelta(minutes=15),
                            "status": "missed_deadline",
                            "quote_observed_at": None,
                            "bid_vwap": None,
                            "error": "test deadline",
                        },
                    ]
                )
            )

        health = await repository.health(
            contract=contract,
            cohort_started_at=cohort_started_at,
        )
        assert health.rejected_stale_count == 1
        assert health.interrupted_count == 1
        assert health.completed_probes_count == 1
        assert health.complete_outcomes == 1
        assert health.missed_outcomes == 1
        assert health.fresh_unclaimed_watches == 1
        assert health.expired_unclaimed_watches == 0
        assert health.claim_delay_p99 is not None
        assert health.claim_delay_p99 <= 30
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                delete(_outcomes).where(
                    _outcomes.c.paper_id.in_((probe_id_interrupted, probe_id_closed))
                )
            )
            await connection.execute(
                delete(_probes).where(_probes.c.paper_version == test_paper_version)
            )
            await connection.execute(
                delete(_runs).where(_runs.c.paper_version == test_paper_version)
            )
            await connection.execute(
                delete(_watch_evaluations).where(
                    _watch_evaluations.c.watch_version == test_watch_version
                )
            )
        await engine.dispose()
