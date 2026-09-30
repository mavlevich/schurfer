"""Real-Postgres checks for the shadow-attempt store (migration 0055)."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import timedelta
from decimal import Decimal

import psycopg
import pytest
from schurfer_execution import source_lead_shadow as sh
from schurfer_journal.testing_database import integration_database_url

TEST_DATABASE_URL = integration_database_url()
REGISTRY_V4 = "source_lead_identity_registry_v4"
FINGERPRINT_V4 = "7d5f635a4ed02013ad3bd5fb7bd118f5b80979427bf059a130279fa2c3bee189"
ENTRY = sh.COHORT_START + timedelta(days=1)
_COSTS_SQL = (
    "SELECT send_cost_capture_version, send_spread_bps, "
    "send_notional_ask_impact_bps, send_qty_ask_impact_bps "
    "FROM app.source_lead_shadow_attempts WHERE id = %s"
)


def _connect_or_skip() -> psycopg.Connection:
    try:
        conn = psycopg.connect(TEST_DATABASE_URL, autocommit=True)
        row = conn.execute("SELECT to_regclass('app.source_lead_shadow_attempts')").fetchone()
        if not row or row[0] is None:
            conn.close()
            pytest.skip("migration 0055 is not applied")
        return conn
    except psycopg.OperationalError as exc:
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise
        pytest.skip(f"no local postgres: {exc}")


def _seed(conn: psycopg.Connection, base: str) -> int:
    event_id = conn.execute(
        "INSERT INTO app.pump_events (base, peak_pct, last_pct, exchanges) "
        "VALUES (%s, 25, 20, '[]'::jsonb) RETURNING id",
        (base,),
    ).fetchone()[0]  # type: ignore[index]
    capture_id = conn.execute(
        """
        INSERT INTO app.source_lead_captures (
            event_id, capture_version, source_exchange, base, source_symbol,
            source_first_observed_at, collector_started_at, capture_started_at,
            capture_completed_at, status, eligibility_reason, source_change_pct,
            first_sources, source_payload
        ) VALUES (%s, 'test_shadow', 'gate', %s, %s, %s, %s, %s, %s, 'complete',
                  'eligible', 20, '[]'::jsonb, '{}'::jsonb)
        RETURNING id
        """,
        (event_id, base, f"{base}_USDT", ENTRY, ENTRY, ENTRY, ENTRY),
    ).fetchone()[0]  # type: ignore[index]
    conn.execute(
        """
        INSERT INTO app.source_lead_target_observations (
            capture_id, target_exchange, status, eligibility_reason,
            identity_match_method, identity_verified, observed_at, latency_ms,
            requested_notional_usd, instrument, ticker, liquidity
        ) VALUES (%s, 'bybit', 'sampled', 'identity_verified', 'registry_exact_v2', true,
                  %s, 10, 50, %s::jsonb, '{}'::jsonb, '{"ask_vwap": 2.0}'::jsonb)
        """,
        (capture_id, ENTRY, f'{{"identity_key": "bybit:swap:{base}USDT:1"}}'),
    )
    conn.execute(
        """
        INSERT INTO app.source_lead_qualifications (
            capture_id, qualification_version, identity_registry_version,
            identity_registry_fingerprint, venue_selector_version, status, reason,
            canonical_asset_id, selected_target_exchange, selected_round_trip_impact_bps,
            requested_notional_usd, qualified_at, details
        ) VALUES (%s, %s, %s, %s, 'lowest_round_trip_impact_tradable_v2', 'qualified',
                  'lowest_round_trip_impact', %s, 'bybit', 4.0, 50, %s, '{}'::jsonb)
        """,
        (capture_id, sh.QUALIFICATION_VERSION, REGISTRY_V4, FINGERPRINT_V4, f"asset:{base}", ENTRY),
    )
    return int(capture_id)


def test_due_claim_finalize_and_recovery() -> None:
    conn = _connect_or_skip()
    base = f"S{uuid.uuid4().hex[:10].upper()}"
    store = sh.ShadowStore(TEST_DATABASE_URL)
    try:
        capture_id = _seed(conn, base)

        async def scenario() -> None:
            due = [e for e in await store.due() if e.capture_id == capture_id]
            assert len(due) == 1 and due[0].capture_ask_vwap == Decimal("2.0")
            seen = ENTRY + timedelta(seconds=40)
            row_id = await store.claim(due[0], seen)
            assert row_id is not None
            assert await store.claim(due[0], seen) is None
            assert capture_id not in {e.capture_id for e in await store.due()}
            row = conn.execute(
                "SELECT late, detect_latency_ms FROM app.source_lead_shadow_attempts WHERE id = %s",
                (row_id,),
            ).fetchone()
            assert row == (True, 40_000)
            # Crash window: decision_id saved, outbox not yet in trade_decisions.
            decision_id = str(uuid.uuid4())
            await store.save_decision_id(row_id, decision_id)
            await store.recover_claims(stale_after=timedelta(minutes=10))
            assert conn.execute(
                "SELECT outcome FROM app.source_lead_shadow_attempts WHERE id = %s", (row_id,)
            ).fetchone() == ("claimed",)  # too fresh: the outbox may still deliver
            # The window passes with no delivery: delivery_unknown, not a crash.
            conn.execute(
                "UPDATE app.source_lead_shadow_attempts "
                "SET updated_at = now() - interval '11 minutes' WHERE id = %s",
                (row_id,),
            )
            await store.recover_claims(stale_after=timedelta(minutes=10))
            assert conn.execute(
                "SELECT outcome FROM app.source_lead_shadow_attempts WHERE id = %s", (row_id,)
            ).fetchone() == ("delivery_unknown",)
            # Review: the decision arrives AFTER the window; reconciliation still fixes it.
            conn.execute(
                "INSERT INTO app.trade_decisions (decision_id, base, exchange, action, reason) "
                "VALUES (%s, %s, 'bybit', 'shadow_recorded', 'test')",
                (decision_id, base),
            )
            await store.recover_claims(stale_after=timedelta(minutes=10))
            assert conn.execute(
                "SELECT outcome FROM app.source_lead_shadow_attempts WHERE id = %s", (row_id,)
            ).fetchone() == ("shadow_recorded",)
            assert not await store.finalize(row_id, {"outcome": "fetch_failed"})

            # Without a decision in trade_decisions, a stale claim is a crash.
            conn.execute(
                "UPDATE app.source_lead_shadow_attempts SET outcome = 'claimed', "
                "decision_id = NULL WHERE id = %s",
                (row_id,),
            )
            await store.recover_claims()
            assert conn.execute(
                "SELECT outcome FROM app.source_lead_shadow_attempts WHERE id = %s", (row_id,)
            ).fetchone() == ("crashed_after_claim",)

        asyncio.run(scenario())
    finally:
        conn.execute("DELETE FROM app.trade_decisions WHERE base = %s", (base,))
        conn.execute("DELETE FROM app.pump_events WHERE base = %s", (base,))
        conn.close()


def test_send_book_cost_fields_are_nullable_and_persisted_with_the_attempt() -> None:
    conn = _connect_or_skip()
    base = f"S{uuid.uuid4().hex[:10].upper()}"
    store = sh.ShadowStore(TEST_DATABASE_URL)
    try:
        capture_id = _seed(conn, base)

        async def scenario() -> None:
            episode = next(e for e in await store.due() if e.capture_id == capture_id)
            row_id = await store.claim(episode, ENTRY + timedelta(seconds=5))
            assert row_id is not None
            assert conn.execute(_COSTS_SQL, (row_id,)).fetchone() == (
                sh.SEND_COST_CAPTURE_VERSION,
                None,
                None,
                None,
            )
            assert await store.finalize(
                row_id,
                {
                    "outcome": "shadow_recorded",
                    "send_spread_bps": Decimal("11.25"),
                    "send_notional_ask_impact_bps": Decimal("6.5"),
                    "send_qty_ask_impact_bps": Decimal("6.75"),
                },
            )
            assert conn.execute(_COSTS_SQL, (row_id,)).fetchone() == (
                sh.SEND_COST_CAPTURE_VERSION,
                Decimal("11.2500"),
                Decimal("6.5000"),
                Decimal("6.7500"),
            )

        asyncio.run(scenario())
    finally:
        conn.execute("DELETE FROM app.pump_events WHERE base = %s", (base,))
        conn.close()
