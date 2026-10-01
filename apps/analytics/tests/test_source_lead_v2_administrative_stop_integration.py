"""Real-Postgres coverage for the HYP-012 v2 administrative stop.

- A stop and a formal claim exclude each other atomically under one unique key.
- A started or completed read is never rewritten; a stopped cohort refuses every claim;
  a rerun of the same stop is idempotent and a different one is refused.
- The accrual snapshot reproduces the state at the checkpoint from the real join.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from schurfer_analytics.formal_read_claims import (
    AdministrativeStop,
    FormalReadAdministrativelyStoppedError,
    FormalReadAlreadyClaimedError,
    FormalReadClaim,
    FormalReadLeaseHeldError,
    complete_claim,
    open_claim,
    record_administrative_stop,
)
from schurfer_analytics.source_lead_forward_cohort_repository import (
    SourceLeadForwardCohortRepository,
)
from schurfer_journal.testing_database import integration_database_url
from sqlalchemy.ext.asyncio import create_async_engine

TEST_DATABASE_URL = integration_database_url()
START = datetime(2026, 9, 29, tzinfo=UTC)


def _db_or_skip() -> None:
    try:
        with psycopg.connect(TEST_DATABASE_URL) as conn:
            row = conn.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = 'app' "
                "AND table_name = 'formal_read_claims' AND column_name = 'terminal_reason'"
            ).fetchone()
            if row is None:
                pytest.skip("migration 0057 is not applied")
    except psycopg.OperationalError as exc:
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise
        pytest.skip(f"no local postgres: {exc}")


def _cleanup(prefix: str) -> None:
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("DELETE FROM app.formal_read_claims WHERE study_id LIKE %s", (prefix + "%",))


def _key(study: str) -> dict[str, Any]:
    return {"study_id": study, "contract_version": "v2", "cohort_start": START}


def _claim(study: str, ids: list[int]) -> Any:
    return open_claim(
        TEST_DATABASE_URL,
        **_key(study),
        database_now=START + timedelta(days=40),
        candidate_ids=ids,
        code_revision="abc",
        working_tree_dirty=False,
    )


def _stop(study: str, ids: list[int], reason: str = "accrual_below_rule:2026-10-31") -> Any:
    return record_administrative_stop(
        TEST_DATABASE_URL,
        **_key(study),
        database_now=START + timedelta(days=33),
        closed_ids=ids,
        terminal_reason=reason,
        result_fingerprint="f" * 64,
        code_revision="abc",
        working_tree_dirty=False,
    )


def _status(study: str) -> tuple[str, int]:
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT status, candidate_count FROM app.formal_read_claims WHERE study_id = %s",
            (study,),
        ).fetchone()
    assert row is not None
    return str(row[0]), int(row[1])


def test_a_stop_and_a_claim_never_both_win() -> None:
    _db_or_skip()
    prefix = f"TSTOP-{uuid.uuid4().hex[:6]}-"

    async def race(study: str) -> list[Any]:
        return list(
            await asyncio.gather(
                _claim(study, [1, 2, 3]), _stop(study, [1, 2, 3, 4]), return_exceptions=True
            )
        )

    try:
        winners: set[str] = set()
        for i in range(12):
            study = f"{prefix}{i}"
            claim, stop = asyncio.run(race(study))
            if isinstance(claim, FormalReadClaim):
                assert isinstance(stop, FormalReadAlreadyClaimedError)
                assert _status(study) == ("claimed", 3)
                winners.add("claim")
            else:
                assert isinstance(claim, FormalReadAdministrativelyStoppedError)
                assert isinstance(stop, AdministrativeStop) and stop.newly_recorded
                assert _status(study) == ("admin_stopped", 4)
                winners.add("stop")
        assert winners  # each round had exactly one winner, checked above
    finally:
        _cleanup(prefix)


def test_a_started_or_completed_read_is_never_rewritten() -> None:
    _db_or_skip()
    prefix = f"TSTOP-{uuid.uuid4().hex[:6]}-"
    try:
        open_study, done_study = f"{prefix}open", f"{prefix}done"
        claim = asyncio.run(_claim(open_study, [5, 6]))
        with pytest.raises(FormalReadAlreadyClaimedError, match="never rewritten"):
            asyncio.run(_stop(open_study, [5, 6, 7]))
        assert _status(open_study) == ("claimed", 2)

        done = asyncio.run(_claim(done_study, [8]))
        asyncio.run(complete_claim(TEST_DATABASE_URL, done.id, "a" * 64, owner=done.owner))
        with pytest.raises(FormalReadAlreadyClaimedError, match="never rewritten"):
            asyncio.run(_stop(done_study, [8]))
        assert _status(done_study) == ("completed", 1)
        assert claim.status == "claimed"
    finally:
        _cleanup(prefix)


def test_a_stopped_cohort_refuses_claims_and_reruns_are_idempotent() -> None:
    _db_or_skip()
    prefix = f"TSTOP-{uuid.uuid4().hex[:6]}-"
    study = f"{prefix}s"
    try:
        first = asyncio.run(_stop(study, [3, 1, 2]))
        assert first.newly_recorded and first.candidate_ids == (3, 1, 2)
        again = asyncio.run(_stop(study, [3, 1, 2]))
        assert not again.newly_recorded and again.id == first.id
        assert again.candidate_ids == (3, 1, 2)
        with pytest.raises(ValueError, match="never moves"):
            asyncio.run(_stop(study, [3, 1, 2], reason="accrual_below_rule:2026-11-30"))
        with pytest.raises(FormalReadAdministrativelyStoppedError):
            asyncio.run(_claim(study, [1]))
        # Even with an expired lease nothing is taken over: the row is terminal.
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            row = conn.execute(
                "SELECT lease_expires_at < now() FROM app.formal_read_claims WHERE study_id = %s",
                (study,),
            ).fetchone()
        assert row is not None and row[0]
        with pytest.raises(FormalReadAdministrativelyStoppedError):
            asyncio.run(_claim(study, [1]))
        assert _status(study) == ("admin_stopped", 3)
    finally:
        _cleanup(prefix)


def test_lease_held_claims_still_refuse_as_before() -> None:
    _db_or_skip()
    prefix = f"TSTOP-{uuid.uuid4().hex[:6]}-"
    try:
        asyncio.run(_claim(f"{prefix}l", [1]))
        with pytest.raises(FormalReadLeaseHeldError):
            asyncio.run(_claim(f"{prefix}l", [1]))
    finally:
        _cleanup(prefix)


# --- accrual snapshot ------------------------------------------------------------------

_VERSION = "test_v2_administrative_stop_snapshot"
_FINGERPRINT = "cd" * 32


def _seed(
    conn: psycopg.Connection,
    base: str,
    *,
    source_at: datetime,
    qualified_created_at: datetime,
    status: str = "qualified",
    target_status: str = "sampled",
) -> int:
    event = conn.execute(
        "INSERT INTO app.pump_events (base, peak_pct, last_pct, exchanges) "
        "VALUES (%s, 25.0, 20.0, '[]'::jsonb) RETURNING id",
        (base,),
    ).fetchone()
    assert event is not None
    capture = conn.execute(
        """
        INSERT INTO app.source_lead_captures (
            event_id, capture_version, source_exchange, base, source_symbol,
            source_first_observed_at, collector_started_at, capture_started_at,
            capture_completed_at, status, eligibility_reason, source_change_pct,
            first_sources, source_payload
        ) VALUES (%s, 'test_capture_v1', 'gate', %s, %s, %s, %s, %s, %s, 'complete',
                  'eligible', 20.0, '[]'::jsonb, '{}'::jsonb)
        RETURNING id
        """,
        (event[0], base, f"{base}_USDT", source_at, source_at, source_at, source_at),
    ).fetchone()
    assert capture is not None
    qualified = status == "qualified"
    conn.execute(
        """
        INSERT INTO app.source_lead_qualifications (
            capture_id, qualification_version, identity_registry_version,
            identity_registry_fingerprint, venue_selector_version, status, reason,
            canonical_asset_id, selected_target_exchange, selected_round_trip_impact_bps,
            requested_notional_usd, qualified_at, details, created_at, updated_at
        ) VALUES (%s, %s, 'test_registry', %s, 'lowest_round_trip_impact_v1', %s, 'test',
                  %s, %s, %s, 50.0, %s, '{}'::jsonb, %s, %s)
        """,
        (
            capture[0],
            _VERSION,
            _FINGERPRINT,
            status,
            f"asset:{base}" if qualified else None,
            "bybit" if qualified else None,
            5.0 if qualified else None,
            qualified_created_at,
            qualified_created_at,
            qualified_created_at,
        ),
    )
    conn.execute(
        """
        INSERT INTO app.source_lead_target_observations (
            capture_id, target_exchange, status, eligibility_reason, identity_match_method,
            identity_verified, observed_at, latency_ms, requested_notional_usd, instrument,
            ticker, liquidity
        ) VALUES (%s, 'bybit', %s, 'eligible', 'registry_exact_v2', true, %s, 50, 50.0,
                  '{}'::jsonb, '{}'::jsonb, '{"ask_vwap": 2.0}'::jsonb)
        """,
        (capture[0], target_status, source_at + timedelta(seconds=20)),
    )
    return int(capture[0])


def test_the_snapshot_reproduces_the_state_at_the_checkpoint() -> None:
    _db_or_skip()
    tag = uuid.uuid4().hex[:8].upper()
    checkpoint = START + timedelta(days=32)
    before = checkpoint - timedelta(days=1)
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        try:
            kept = _seed(conn, f"SNAPA{tag}", source_at=before, qualified_created_at=before)
            _seed(  # qualified only after the checkpoint: not in the snapshot
                conn, f"SNAPB{tag}", source_at=before, qualified_created_at=checkpoint
            )
            _seed(  # captured at the checkpoint: outside [start, C)
                conn, f"SNAPC{tag}", source_at=checkpoint, qualified_created_at=checkpoint
            )
            _seed(  # before the cohort start
                conn,
                f"SNAPD{tag}",
                source_at=START - timedelta(minutes=1),
                qualified_created_at=START,
            )
            _seed(
                conn,
                f"SNAPE{tag}",
                source_at=before,
                qualified_created_at=before,
                status="excluded",
            )
            _seed(
                conn,
                f"SNAPF{tag}",
                source_at=before,
                qualified_created_at=before,
                target_status="fetch_failed",
            )

            async def snapshot() -> Any:
                engine = create_async_engine(integration_database_url(sqlalchemy=True))
                repository = SourceLeadForwardCohortRepository(engine)
                try:
                    return await repository.fetch_accrual_snapshot(
                        qualification_version=_VERSION, since=START, as_of=checkpoint, limit=100
                    )
                finally:
                    await repository.close()

            rows = asyncio.run(snapshot())
            assert [r.capture_id for r in rows] == [kept]
            (row,) = rows
            assert row.canonical_asset_id == f"asset:SNAPA{tag}"
            assert row.target_exchange == "bybit"
            assert row.observed_at == before + timedelta(seconds=20)
        finally:
            conn.execute("DELETE FROM app.pump_events WHERE base LIKE %s", (f"SNAP_{tag}",))
