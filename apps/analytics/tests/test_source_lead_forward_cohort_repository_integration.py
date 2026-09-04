"""Real-Postgres coverage for SourceLeadForwardCohortRepository
(research/source-lead-forward-cohort-plumbing-v1).

Only a real query against the actual three-table join (source_lead_
qualifications -> source_lead_captures -> source_lead_target_observations)
proves the join keys, status/version filters, and since-bound actually
select the intended row -- none of that is exercised by
`test_source_lead_forward_cohort_report.py`'s synthetic `RawQualifiedEpisode`
fixtures, which bypass the database entirely by design.

Matches infra/docker/docker-compose.dev.yml's local dev Postgres, same
convention as this package's other real-Postgres tests. Skips (not fails)
when no Postgres is reachable. Seed helper mirrors
packages/journal/tests/test_migration_0042_integration.py's own
`_insert_capture`/`_insert_target` (a source_lead_captures row's capture_id
FK requires a real app.pump_events row first; ON DELETE CASCADE from
pump_events cleans up both captures and target observations together).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.source_lead_forward_cohort_repository import (
    SourceLeadForwardCohortRepository,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

TEST_DATABASE_URL = "postgresql+psycopg://schurfer:schurfer_dev@localhost:5432/schurfer"

_QUALIFICATION_VERSION = "test_source_lead_forward_cohort_v1"
_IDENTITY_REGISTRY_FINGERPRINT = "ab" * 32  # matches ck_..._registry_fingerprint's ^[0-9a-f]{64}$


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
                        :event_id, :capture_version, 'gate', :base, :symbol,
                        :observed_at, :observed_at, :observed_at,
                        :observed_at, 'complete', 'eligible', 20.0,
                        '[]'::jsonb, '{}'::jsonb
                    )
                    RETURNING id
                """),
                {
                    "event_id": event_id,
                    "capture_version": "test_capture_v1",
                    "base": base,
                    "symbol": f"{base}_USDT",
                    "observed_at": observed_at,
                },
            )
        ).scalar_one()
    return int(capture_id)


async def _insert_qualification(
    engine: AsyncEngine,
    *,
    capture_id: int,
    status: str,
    qualification_version: str = _QUALIFICATION_VERSION,
    selected_target_exchange: str | None = "binance",
) -> None:
    is_qualified = status == "qualified"
    async with engine.begin() as connection:
        await connection.execute(
            text("""
                INSERT INTO app.source_lead_qualifications (
                    capture_id, qualification_version, identity_registry_version,
                    identity_registry_fingerprint, venue_selector_version, status,
                    reason, canonical_asset_id, selected_target_exchange,
                    selected_round_trip_impact_bps, requested_notional_usd,
                    qualified_at, details
                ) VALUES (
                    :capture_id, :qualification_version, 'test_registry_v1',
                    :fingerprint, 'lowest_round_trip_impact_v1', :status,
                    :reason, :canonical_asset_id, :selected_target_exchange,
                    :impact, 50.0,
                    now(), '{}'::jsonb
                )
            """),
            {
                "capture_id": capture_id,
                "qualification_version": qualification_version,
                "fingerprint": _IDENTITY_REGISTRY_FINGERPRINT,
                "status": status,
                "reason": "lowest_round_trip_impact" if is_qualified else "test",
                "canonical_asset_id": f"canonical:{capture_id}" if is_qualified else None,
                "selected_target_exchange": selected_target_exchange if is_qualified else None,
                "impact": 5.0 if is_qualified else None,
            },
        )


async def _insert_target_observation(
    engine: AsyncEngine,
    *,
    capture_id: int,
    target_exchange: str,
    status: str,
    observed_at: datetime,
    ask_vwap: float = 2.0,
    requested_notional_usd: float = 50.0,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("""
                INSERT INTO app.source_lead_target_observations (
                    capture_id, target_exchange, status, eligibility_reason,
                    identity_match_method, identity_verified, observed_at,
                    latency_ms, requested_notional_usd, instrument, ticker, liquidity
                ) VALUES (
                    :capture_id, :target_exchange, :status, 'eligible',
                    'registry_exact_v2', true, :observed_at,
                    50, :requested_notional_usd, :instrument, '{}'::jsonb, :liquidity
                )
            """),
            {
                "capture_id": capture_id,
                "target_exchange": target_exchange,
                "status": status,
                "observed_at": observed_at,
                "requested_notional_usd": requested_notional_usd,
                "instrument": '{"unified_symbol": "ABC/USDT:USDT"}',
                "liquidity": f'{{"ask_vwap": {ask_vwap}}}',
            },
        )


async def _cleanup(engine: AsyncEngine, *, bases: tuple[str, ...]) -> None:
    async with engine.begin() as connection:
        for base in bases:
            await connection.execute(
                text("DELETE FROM app.pump_events WHERE base = :base"), {"base": base}
            )


async def test_fetch_qualified_episodes_returns_a_genuine_joined_row() -> None:
    engine = await _connect_or_skip()
    base = "FWDCOHORTQUALIFIED"
    observed_at = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
    try:
        capture_id = await _insert_capture(engine, base=base, observed_at=observed_at)
        await _insert_qualification(engine, capture_id=capture_id, status="qualified")
        await _insert_target_observation(
            engine,
            capture_id=capture_id,
            target_exchange="binance",
            status="sampled",
            observed_at=observed_at,
            ask_vwap=3.5,
        )

        repository = SourceLeadForwardCohortRepository(engine)
        episodes = await repository.fetch_qualified_episodes(
            qualification_version=_QUALIFICATION_VERSION,
            since=observed_at - timedelta(minutes=1),
            limit=100,
        )
        matching = [episode for episode in episodes if episode.base == base]
        assert len(matching) == 1
        (episode,) = matching
        assert episode.capture_id == capture_id
        assert episode.canonical_asset_id == f"canonical:{capture_id}"
        assert episode.target_exchange == "binance"
        assert episode.observed_at == observed_at
        assert episode.requested_notional_usd == pytest.approx(50.0)
        assert episode.liquidity["ask_vwap"] == pytest.approx(3.5)
        assert episode.instrument["unified_symbol"] == "ABC/USDT:USDT"
    finally:
        await _cleanup(engine, bases=(base,))
        await engine.dispose()


async def test_fetch_qualified_episodes_excludes_excluded_status() -> None:
    engine = await _connect_or_skip()
    base = "FWDCOHORTEXCLUDED"
    observed_at = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
    try:
        capture_id = await _insert_capture(engine, base=base, observed_at=observed_at)
        await _insert_qualification(engine, capture_id=capture_id, status="excluded")

        repository = SourceLeadForwardCohortRepository(engine)
        episodes = await repository.fetch_qualified_episodes(
            qualification_version=_QUALIFICATION_VERSION,
            since=observed_at - timedelta(minutes=1),
            limit=100,
        )
        assert not any(episode.base == base for episode in episodes)
    finally:
        await _cleanup(engine, bases=(base,))
        await engine.dispose()


async def test_fetch_qualified_episodes_excludes_a_non_sampled_target_observation() -> None:
    engine = await _connect_or_skip()
    base = "FWDCOHORTNOTSAMPLED"
    observed_at = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
    try:
        capture_id = await _insert_capture(engine, base=base, observed_at=observed_at)
        await _insert_qualification(engine, capture_id=capture_id, status="qualified")
        await _insert_target_observation(
            engine,
            capture_id=capture_id,
            target_exchange="binance",
            status="fetch_failed",
            observed_at=observed_at,
        )

        repository = SourceLeadForwardCohortRepository(engine)
        episodes = await repository.fetch_qualified_episodes(
            qualification_version=_QUALIFICATION_VERSION,
            since=observed_at - timedelta(minutes=1),
            limit=100,
        )
        # The qualification row exists and is 'qualified', but its own
        # selected target observation never resolved to a 'sampled' row --
        # the INNER JOIN must produce no episode here, not a partial one
        # with missing liquidity/observed_at.
        assert not any(episode.base == base for episode in episodes)
    finally:
        await _cleanup(engine, bases=(base,))
        await engine.dispose()


async def test_fetch_qualified_episodes_respects_since_bound() -> None:
    engine = await _connect_or_skip()
    base = "FWDCOHORTTOOOLD"
    observed_at = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
    try:
        capture_id = await _insert_capture(engine, base=base, observed_at=observed_at)
        await _insert_qualification(engine, capture_id=capture_id, status="qualified")
        await _insert_target_observation(
            engine,
            capture_id=capture_id,
            target_exchange="binance",
            status="sampled",
            observed_at=observed_at,
        )

        repository = SourceLeadForwardCohortRepository(engine)
        episodes = await repository.fetch_qualified_episodes(
            qualification_version=_QUALIFICATION_VERSION,
            since=observed_at + timedelta(minutes=1),  # strictly after the seeded capture
            limit=100,
        )
        assert not any(episode.base == base for episode in episodes)
    finally:
        await _cleanup(engine, bases=(base,))
        await engine.dispose()


async def test_fetch_qualified_episodes_rejects_non_positive_limit() -> None:
    engine = await _connect_or_skip()
    try:
        repository = SourceLeadForwardCohortRepository(engine)
        with pytest.raises(ValueError, match="limit must be positive"):
            await repository.fetch_qualified_episodes(
                qualification_version=_QUALIFICATION_VERSION,
                since=datetime.now(UTC),
                limit=0,
            )
    finally:
        await engine.dispose()
