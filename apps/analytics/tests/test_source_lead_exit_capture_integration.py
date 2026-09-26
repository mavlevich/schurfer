"""Real-Postgres checks for the exit-capture store (migration 0052)."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import timedelta
from decimal import Decimal

import psycopg
import pytest
from schurfer_analytics import source_lead_exit_capture as ex
from schurfer_analytics.source_lead_contract import IDENTITY_REGISTRY_V4_START
from schurfer_analytics.source_lead_qualification import (
    EXPECTED_REGISTRY_FINGERPRINT,
    EXPECTED_REGISTRY_VERSION,
    QUALIFICATION_VERSION,
)

TEST_DATABASE_URL = "postgresql://schurfer:schurfer_dev@localhost:5432/schurfer"
ENTRY = IDENTITY_REGISTRY_V4_START + timedelta(days=1, seconds=20)


def _connect_or_skip() -> psycopg.Connection:
    try:
        connection = psycopg.connect(TEST_DATABASE_URL, autocommit=True)
        row = connection.execute(
            "SELECT to_regclass('app.source_lead_exit_observations')"
        ).fetchone()
        if not row or row[0] is None:
            connection.close()
            pytest.skip("migration 0052 is not applied")
        return connection
    except psycopg.OperationalError as exc:
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise
        pytest.skip(f"no local postgres: {exc}")


def _seed(connection: psycopg.Connection, base: str) -> int:
    event_id = connection.execute(
        "INSERT INTO app.pump_events (base, peak_pct, last_pct, exchanges) "
        "VALUES (%s, 25, 20, '[]'::jsonb) RETURNING id",
        (base,),
    ).fetchone()[0]  # type: ignore[index]
    capture_id = connection.execute(
        """
        INSERT INTO app.source_lead_captures (
            event_id, capture_version, source_exchange, base, source_symbol,
            source_first_observed_at, collector_started_at, capture_started_at,
            capture_completed_at, status, eligibility_reason, source_change_pct,
            first_sources, source_payload
        ) VALUES (%s, 'test_exit', 'gate', %s, %s, %s, %s, %s, %s, 'complete',
                  'eligible', 20, '[]'::jsonb, '{}'::jsonb)
        RETURNING id
        """,
        (event_id, base, f"{base}_USDT", ENTRY, ENTRY, ENTRY, ENTRY),
    ).fetchone()[0]  # type: ignore[index]
    connection.execute(
        """
        INSERT INTO app.source_lead_target_observations (
            capture_id, target_exchange, status, eligibility_reason,
            identity_match_method, identity_verified, observed_at, latency_ms,
            requested_notional_usd, instrument, ticker, liquidity
        ) VALUES (%s, 'bybit', 'sampled', 'identity_verified', 'registry_exact_v2', true,
                  %s, 10, 50, %s::jsonb, '{}'::jsonb, '{"ask_vwap": 2.01}'::jsonb)
        """,
        (capture_id, ENTRY, f'{{"identity_key": "bybit:swap:{base}USDT:1"}}'),
    )
    connection.execute(
        """
        INSERT INTO app.source_lead_qualifications (
            capture_id, qualification_version, identity_registry_version,
            identity_registry_fingerprint, venue_selector_version, status, reason,
            canonical_asset_id, selected_target_exchange, selected_round_trip_impact_bps,
            requested_notional_usd, qualified_at, details
        ) VALUES (%s, %s, %s, %s, 'lowest_round_trip_impact_tradable_v2', 'qualified',
                  'lowest_round_trip_impact', %s, 'bybit', 4.0, 50, now(), '{}'::jsonb)
        """,
        (
            capture_id,
            QUALIFICATION_VERSION,
            EXPECTED_REGISTRY_VERSION,
            EXPECTED_REGISTRY_FINGERPRINT,
            f"asset:{base.lower()}",
        ),
    )
    return int(capture_id)


def test_due_claim_finalize_and_crash_recovery() -> None:
    connection = _connect_or_skip()
    base = f"X{uuid.uuid4().hex[:10].upper()}"
    store = ex.ExitStore(TEST_DATABASE_URL)
    try:
        capture_id = _seed(connection, base)

        async def scenario() -> None:
            due = [e for e in await store.due_episodes() if e.capture_id == capture_id]
            assert len(due) == 1
            episode = due[0]
            assert episode.ask_vwap == Decimal("2.01")
            assert ex.native_symbol(episode.identity_key) == f"{base}USDT"

            row_id = await store.claim(episode, outcome="claimed", timeliness=None)
            assert row_id is not None
            # A second claim for the same episode never succeeds.
            assert await store.claim(episode, outcome="claimed", timeliness=None) is None
            # A claimed episode is no longer due.
            assert capture_id not in {e.capture_id for e in await store.due_episodes()}

            # Crash between claim and write: recovery marks it, never re-requests.
            await store.recover_claims()
            outcome = connection.execute(
                "SELECT outcome, timeliness FROM app.source_lead_exit_observations WHERE id = %s",
                (row_id,),
            ).fetchone()
            assert outcome == ("crashed_after_claim", "missed")

            await store.finalize(
                row_id,
                {
                    "outcome": "sampled",
                    "timeliness": "on_time",
                    "attempts": 1,
                    "book_snapshot": {"b": [["1.99", "1"]]},
                    "bid_vwap": Decimal("1.99"),
                },
            )
            stored = connection.execute(
                "SELECT outcome, book_snapshot, bid_vwap FROM app.source_lead_exit_observations "
                "WHERE id = %s",
                (row_id,),
            ).fetchone()
            assert stored == ("sampled", {"b": [["1.99", "1"]]}, Decimal("1.99"))

            with pytest.raises(ValueError, match="unknown exit columns"):
                await store.finalize(row_id, {"capture_id": 1})

        asyncio.run(scenario())

        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "UPDATE app.source_lead_exit_observations SET outcome = 'bogus' "
                "WHERE capture_id = %s",
                (capture_id,),
            )
    finally:
        connection.execute("DELETE FROM app.pump_events WHERE base = %s", (base,))
        connection.close()
