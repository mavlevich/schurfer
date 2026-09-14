"""Real-Postgres coverage for load_readiness_inputs.

Proves the readiness funnel counts EXACTLY the formal candidate set (the same
three-table join the forward-cohort repository uses), that maturity keys off the
target-observation entry time rather than qualified_at, and that the exclusion-reason
diagnostic is cohort-scoped. Skips (not fails) when no local Postgres is reachable, the
same convention as this package's other repository integration tests. Seed helpers
mirror test_source_lead_forward_cohort_repository_integration.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.source_lead_forward_cohort_repository import (
    SourceLeadForwardCohortRepository,
)
from schurfer_analytics.source_lead_readiness import build_readiness
from schurfer_analytics.source_lead_readiness_repository import load_readiness_inputs
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

TEST_DATABASE_URL = "postgresql+psycopg://schurfer:schurfer_dev@localhost:5432/schurfer"
_RAW_DB_URL = "postgresql://schurfer:schurfer_dev@localhost:5432/schurfer"
_QV = "test_source_lead_readiness_v1"
_FINGERPRINT = "cd" * 32
_COHORT_START = datetime(2026, 9, 3, tzinfo=UTC)


async def _connect_or_skip() -> AsyncEngine:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"no local postgres reachable: {exc}")
    return engine


async def _insert_capture(engine: AsyncEngine, *, base: str, observed_at: datetime) -> int:
    async with engine.begin() as connection:
        event_id = (
            await connection.execute(
                text(
                    "INSERT INTO app.pump_events (base, peak_pct, last_pct, exchanges) "
                    "VALUES (:base, 25.0, 20.0, '[]'::jsonb) RETURNING id"
                ),
                {"base": base},
            )
        ).scalar_one()
        capture_id = (
            await connection.execute(
                text("""
                    INSERT INTO app.source_lead_captures (
                        event_id, capture_version, source_exchange, base, source_symbol,
                        source_first_observed_at, collector_started_at, capture_started_at,
                        capture_completed_at, status, eligibility_reason, source_change_pct,
                        first_sources, source_payload
                    ) VALUES (
                        :event_id, 'test_capture_v1', 'gate', :base, :symbol,
                        :observed_at, :observed_at, :observed_at,
                        :observed_at, 'complete', 'eligible', 20.0, '[]'::jsonb, '{}'::jsonb
                    ) RETURNING id
                """),
                {
                    "event_id": event_id,
                    "base": base,
                    "symbol": f"{base}_USDT",
                    "observed_at": observed_at,
                },
            )
        ).scalar_one()
    return int(capture_id)


async def _insert_qualification(
    engine: AsyncEngine, *, capture_id: int, status: str, qualification_version: str = _QV
) -> None:
    is_q = status == "qualified"
    async with engine.begin() as connection:
        await connection.execute(
            text("""
                INSERT INTO app.source_lead_qualifications (
                    capture_id, qualification_version, identity_registry_version,
                    identity_registry_fingerprint, venue_selector_version, status,
                    reason, canonical_asset_id, selected_target_exchange,
                    selected_round_trip_impact_bps, requested_notional_usd, qualified_at, details
                ) VALUES (
                    :capture_id, :qv, 'test_registry_v1', :fp, 'lowest_round_trip_impact_v1',
                    :status, :reason, :canonical, :target, :impact, 50.0, now(), '{}'::jsonb
                )
            """),
            {
                "capture_id": capture_id,
                "qv": qualification_version,
                "fp": _FINGERPRINT,
                "status": status,
                "reason": "lowest_round_trip_impact" if is_q else "source_identity_unapproved",
                "canonical": f"canonical:{capture_id}" if is_q else None,
                "target": "binance" if is_q else None,
                "impact": 5.0 if is_q else None,
            },
        )


async def _insert_target_observation(
    engine: AsyncEngine, *, capture_id: int, status: str, observed_at: datetime
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("""
                INSERT INTO app.source_lead_target_observations (
                    capture_id, target_exchange, status, eligibility_reason,
                    identity_match_method, identity_verified, observed_at,
                    latency_ms, requested_notional_usd, instrument, ticker, liquidity
                ) VALUES (
                    :capture_id, 'binance', :status, 'eligible', 'registry_exact_v2', true,
                    :observed_at, 50, 50.0, :instrument, '{}'::jsonb, :liquidity
                )
            """),
            {
                "capture_id": capture_id,
                "status": status,
                "observed_at": observed_at,
                "instrument": '{"unified_symbol": "ABC/USDT:USDT"}',
                "liquidity": '{"ask_vwap": 2.0}',
            },
        )


async def _seed_candidate(engine: AsyncEngine, *, base: str, observed_at: datetime) -> int:
    capture_id = await _insert_capture(engine, base=base, observed_at=observed_at)
    await _insert_qualification(engine, capture_id=capture_id, status="qualified")
    await _insert_target_observation(
        engine, capture_id=capture_id, status="sampled", observed_at=observed_at
    )
    return capture_id


async def _cleanup(engine: AsyncEngine, *, bases: tuple[str, ...]) -> None:
    async with engine.begin() as connection:
        for base in bases:
            await connection.execute(
                text("DELETE FROM app.pump_events WHERE base = :base"), {"base": base}
            )


async def test_readiness_candidate_set_matches_formal_repository() -> None:
    engine = await _connect_or_skip()
    old = datetime(2026, 9, 5, 0, 0, tzinfo=UTC)  # matured by entry time
    recent = datetime.now(UTC) - timedelta(minutes=5)  # exit bar not closed yet
    bases = (
        "READYGOOD1",  # qualified + sampled, matured
        "READYGOOD2",  # qualified + sampled, not matured (recent)
        "READYEXCL",  # excluded qualification
        "READYNOTSAMPLED",  # qualified but target observation not sampled
        "READYTOOOLD",  # qualified + sampled but before cohort start
    )
    try:
        await _seed_candidate(engine, base="READYGOOD1", observed_at=old)
        await _seed_candidate(engine, base="READYGOOD2", observed_at=recent)

        excl_id = await _insert_capture(engine, base="READYEXCL", observed_at=old)
        await _insert_qualification(engine, capture_id=excl_id, status="excluded")

        ns_id = await _insert_capture(engine, base="READYNOTSAMPLED", observed_at=old)
        await _insert_qualification(engine, capture_id=ns_id, status="qualified")
        await _insert_target_observation(
            engine, capture_id=ns_id, status="fetch_failed", observed_at=old
        )

        await _seed_candidate(
            engine, base="READYTOOOLD", observed_at=_COHORT_START - timedelta(days=1)
        )

        inputs = await load_readiness_inputs(
            _RAW_DB_URL, qualification_version=_QV, cohort_start=_COHORT_START
        )

        # Candidate set == formal repository's own fetch, and equals the two valid ones.
        forward = SourceLeadForwardCohortRepository.from_url(_RAW_DB_URL)
        try:
            formal = await forward.fetch_qualified_episodes(
                qualification_version=_QV, since=_COHORT_START, limit=1000
            )
        finally:
            await forward.close()
        assert inputs.captured_in_cohort >= 4  # all but the pre-cohort capture
        assert len(inputs.episodes) == len(formal) == 2

        report = build_readiness(inputs)
        assert report.candidates == 2
        # Maturity keys off observed_at (entry), not qualified_at (which is now()):
        # the old episode is matured, the 5-min-old one is not.
        assert report.matured == 1
        # The excluded qualification appears in the cohort-scoped diagnostic.
        assert report.excluded_by_reason.get("source_identity_unapproved") == 1
        # Accrual uses the fixed exposure window, not the event span.
        exposure_weeks = (inputs.database_now - _COHORT_START).total_seconds() / 86400.0 / 7.0
        assert report.qualified_per_week == pytest.approx(2 / exposure_weeks)
    finally:
        await _cleanup(engine, bases=bases)
        await engine.dispose()
