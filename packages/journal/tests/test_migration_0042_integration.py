"""Real-Postgres invariants for source-lead target identity pairing
migration 0042.

Exercises the actual ck_source_lead_target_v2_identity_pairing CHECK
CONSTRAINT: identity_match_method='registry_exact_v2' must always carry
identity_verified=true, identity_match_method='registry_lookup_v2' must
always carry identity_verified=false, and the pre-existing base_symbol_v1
constraint (migration prior to this PR) must stay untouched.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config

TEST_DATABASE_URL = "postgresql://schurfer:schurfer_dev@localhost:5432/schurfer"
ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def _connect_or_skip() -> psycopg.Connection:
    try:
        connection = psycopg.connect(TEST_DATABASE_URL)
        # See test_migration_0040/0041_integration.py's identical comment:
        # a bare cursor.execute() implicitly opens a transaction that must
        # be committed via connection.transaction(), or its row locks
        # outlive this check and can deadlock a concurrent DDL connection.
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM pg_constraint "
                "WHERE conname = 'ck_source_lead_target_v2_identity_pairing'"
            )
            found = cursor.fetchone() is not None
        if not found:
            connection.close()
            pytest.skip("migration 0042 is not applied")
        return connection
    except Exception as exc:
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise RuntimeError(
                f"REQUIRE_INTEGRATION_DB=1 but Postgres/head is unavailable: {exc}"
            ) from exc
        pytest.skip(f"no local postgres/head reachable: {exc}")


def _alembic_config() -> Config:
    config = Config(str(ALEMBIC_INI))
    db_url = os.getenv("DATABASE_URL")
    if db_url:
        config.set_main_option("sqlalchemy.url", db_url)
    return config


def _insert_capture(cursor: psycopg.Cursor, *, base: str) -> int:
    """Creates the minimal pump_events + source_lead_captures row a target
    observation's capture_id FK requires, and returns the capture id.
    Deleting the pump_events row cascades through both (ON DELETE CASCADE)."""
    now = datetime.now(UTC)
    cursor.execute(
        """
        INSERT INTO app.pump_events (base, peak_pct, last_pct, exchanges)
        VALUES (%s, 25.0, 20.0, '[]'::jsonb)
        RETURNING id
        """,
        (base,),
    )
    row = cursor.fetchone()
    assert row is not None
    event_id = row[0]
    cursor.execute(
        """
        INSERT INTO app.source_lead_captures (
            event_id, capture_version, source_exchange, base, source_symbol,
            source_first_observed_at, collector_started_at, capture_started_at,
            capture_completed_at, status, eligibility_reason, source_change_pct,
            first_sources, source_payload
        ) VALUES (
            %s, 'test_migration_0042', 'gate', %s, %s,
            %s, %s, %s,
            %s, 'complete', 'eligible', 20.0,
            '[]'::jsonb, '{}'::jsonb
        )
        RETURNING id
        """,
        (event_id, base, f"{base}_USDT", now, now, now, now),
    )
    row = cursor.fetchone()
    assert row is not None
    return int(row[0])


def _insert_target(
    cursor: psycopg.Cursor,
    *,
    capture_id: int,
    target_exchange: str,
    identity_match_method: str,
    identity_verified: bool,
) -> None:
    now = datetime.now(UTC)
    cursor.execute(
        """
        INSERT INTO app.source_lead_target_observations (
            capture_id, target_exchange, status, eligibility_reason,
            identity_match_method, identity_verified, observed_at,
            latency_ms, requested_notional_usd, instrument, ticker, liquidity
        ) VALUES (
            %s, %s, 'excluded', 'test',
            %s, %s, %s,
            0, 50.0, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb
        )
        """,
        (capture_id, target_exchange, identity_match_method, identity_verified, now),
    )


def test_v2_methods_require_matching_identity_verified() -> None:
    connection = _connect_or_skip()
    base = f"T{uuid.uuid4().hex[:16].upper()}"
    try:
        with connection.transaction(), connection.cursor() as cursor:
            capture_id = _insert_capture(cursor, base=base)

            # registry_exact_v2 + verified=true: accepted.
            _insert_target(
                cursor,
                capture_id=capture_id,
                target_exchange="binance",
                identity_match_method="registry_exact_v2",
                identity_verified=True,
            )
            # registry_lookup_v2 + verified=false: accepted.
            _insert_target(
                cursor,
                capture_id=capture_id,
                target_exchange="bybit",
                identity_match_method="registry_lookup_v2",
                identity_verified=False,
            )

            # registry_exact_v2 claiming verified=false is rejected -- a
            # confirmed route can never be tagged unconfirmed.
            with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
                _insert_target(
                    cursor,
                    capture_id=capture_id,
                    target_exchange="okx",
                    identity_match_method="registry_exact_v2",
                    identity_verified=False,
                )

            # registry_lookup_v2 claiming verified=true is rejected -- an
            # unresolved route can never be tagged confirmed.
            with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
                _insert_target(
                    cursor,
                    capture_id=capture_id,
                    target_exchange="okx",
                    identity_match_method="registry_lookup_v2",
                    identity_verified=True,
                )

            # base_symbol_v1 (the pre-existing, migration-prior constraint)
            # is untouched: verified=false still accepted, verified=true
            # still rejected.
            _insert_target(
                cursor,
                capture_id=capture_id,
                target_exchange="mexc",
                identity_match_method="base_symbol_v1",
                identity_verified=False,
            )
            with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
                _insert_target(
                    cursor,
                    capture_id=capture_id,
                    target_exchange="gate",
                    identity_match_method="base_symbol_v1",
                    identity_verified=True,
                )

            cursor.execute(
                "SELECT count(*) FROM app.source_lead_target_observations WHERE capture_id = %s",
                (capture_id,),
            )
            row = cursor.fetchone()
            assert row == (3,), f"expected exactly the three accepted rows to persist, got {row}"
    finally:
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute("DELETE FROM app.pump_events WHERE base = %s", (base,))
        connection.close()


def test_downgrade_removes_the_pairing_gate_then_upgrade_restores_it() -> None:
    connection = _connect_or_skip()
    base = f"T{uuid.uuid4().hex[:16].upper()}"
    config = _alembic_config()
    try:
        with connection.transaction(), connection.cursor() as cursor:
            capture_id = _insert_capture(cursor, base=base)

        command.downgrade(config, "0041")
        try:
            # With the constraint dropped, a mismatched pairing is no
            # longer rejected -- proving the downgrade actually removes the
            # gate. Deleted again before the constraint comes back: ADD
            # CONSTRAINT validates every existing row.
            with connection.transaction(), connection.cursor() as cursor:
                _insert_target(
                    cursor,
                    capture_id=capture_id,
                    target_exchange="binance",
                    identity_match_method="registry_exact_v2",
                    identity_verified=False,
                )
                cursor.execute(
                    "SELECT count(*) FROM app.source_lead_target_observations "
                    "WHERE capture_id = %s",
                    (capture_id,),
                )
                assert cursor.fetchone() == (1,)
                cursor.execute(
                    "DELETE FROM app.source_lead_target_observations WHERE capture_id = %s",
                    (capture_id,),
                )
        finally:
            command.upgrade(config, "head")

        # Back at head, a second, distinct capture must have the gate
        # enforced again.
        with connection.transaction(), connection.cursor() as cursor:
            second_capture_id = _insert_capture(cursor, base=f"{base}B")
            with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
                _insert_target(
                    cursor,
                    capture_id=second_capture_id,
                    target_exchange="binance",
                    identity_match_method="registry_exact_v2",
                    identity_verified=False,
                )
    finally:
        # Restore head unconditionally, even if an assertion above failed --
        # this test must never leave the shared dev database migrated
        # backward for whatever test runs next.
        command.upgrade(config, "head")
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute("DELETE FROM app.pump_events WHERE base LIKE %s", (f"{base}%",))
        connection.close()
