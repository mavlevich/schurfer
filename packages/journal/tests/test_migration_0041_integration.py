"""Real-Postgres invariants for source-lead qualification registry v2
migration 0041.

Exercises the actual ck_source_lead_qualification_v2_registry_contract CHECK
CONSTRAINT against a real source_lead_qualifications row, not just the
migration source text: a v2-tagged row must carry exactly the pinned
registry_v2 version and fingerprint, the existing v1 constraint (migration
0022) must be untouched, and dropping the constraint on downgrade must
actually remove the gate (then upgrade must restore it).
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

_V1_VERSION = "source_lead_qualified_capture_v1"
_V1_REGISTRY_VERSION = "source_lead_identity_registry_v1"
_V1_FINGERPRINT = "31604214fa148d3f86562a212fdc935029c82a7a4959a7b5001b6bd5637ff7f8"
_V2_VERSION = "source_lead_qualified_capture_v2"
_V2_REGISTRY_VERSION = "source_lead_identity_registry_v2"
_V2_FINGERPRINT = "757fd1327593d07ca27efe17a031ae0eab95bf6998aecc1ec26f0df38667dca0"
_WRONG_FINGERPRINT = "0" * 64


def _connect_or_skip() -> psycopg.Connection:
    try:
        connection = psycopg.connect(TEST_DATABASE_URL)
        # Must run inside an explicit transaction() that actually commits: a
        # bare cursor.execute() implicitly opens a transaction (psycopg3
        # default autocommit=False) that otherwise stays open on the
        # returned connection, turning any later `connection.transaction()`
        # block in the caller into a nested savepoint rather than a real
        # commit -- the row locks it takes then outlive that block and can
        # deadlock a concurrent DDL connection (same class of bug this
        # blocked in test_migration_0040_integration.py during development).
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM pg_constraint "
                "WHERE conname = 'ck_source_lead_qualification_v2_registry_contract'"
            )
            found = cursor.fetchone() is not None
        if not found:
            connection.close()
            pytest.skip("migration 0041 is not applied")
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
    """Creates the minimal pump_events + source_lead_captures row a
    qualification row's capture_id FK requires, and returns the capture id.
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
            %s, 'test_migration_0041', 'gate', %s, %s,
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


def _insert_qualification(
    cursor: psycopg.Cursor,
    *,
    capture_id: int,
    qualification_version: str,
    identity_registry_version: str,
    identity_registry_fingerprint: str,
) -> None:
    cursor.execute(
        """
        INSERT INTO app.source_lead_qualifications (
            capture_id, qualification_version, identity_registry_version,
            identity_registry_fingerprint, venue_selector_version, status,
            reason, canonical_asset_id, selected_target_exchange,
            selected_round_trip_impact_bps, requested_notional_usd,
            qualified_at, details
        ) VALUES (
            %s, %s, %s,
            %s, 'lowest_round_trip_impact_v1', 'excluded',
            'test', NULL, NULL,
            NULL, 50.0,
            now(), '{}'::jsonb
        )
        """,
        (
            capture_id,
            qualification_version,
            identity_registry_version,
            identity_registry_fingerprint,
        ),
    )


def test_v2_row_requires_the_pinned_registry_contract_and_v1_is_untouched() -> None:
    connection = _connect_or_skip()
    base = f"T{uuid.uuid4().hex[:16].upper()}"
    try:
        with connection.transaction(), connection.cursor() as cursor:
            capture_id = _insert_capture(cursor, base=base)

            # v1 rows are governed by the pre-existing (migration 0022)
            # constraint, untouched by 0041: a correct v1 row still inserts.
            _insert_qualification(
                cursor,
                capture_id=capture_id,
                qualification_version=_V1_VERSION,
                identity_registry_version=_V1_REGISTRY_VERSION,
                identity_registry_fingerprint=_V1_FINGERPRINT,
            )

            # ...and a v1 row with a wrong fingerprint is still rejected,
            # proving 0041 did not loosen or replace the v1 constraint.
            with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
                _insert_qualification(
                    cursor,
                    capture_id=capture_id,
                    qualification_version=_V1_VERSION,
                    identity_registry_version=_V1_REGISTRY_VERSION,
                    identity_registry_fingerprint=_WRONG_FINGERPRINT,
                )

            # A v2 row with a wrong fingerprint is rejected by 0041's new
            # constraint.
            with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
                _insert_qualification(
                    cursor,
                    capture_id=capture_id,
                    qualification_version=_V2_VERSION,
                    identity_registry_version=_V2_REGISTRY_VERSION,
                    identity_registry_fingerprint=_WRONG_FINGERPRINT,
                )

            # A v2 row claiming the wrong registry_version string (even with
            # the correct fingerprint) is rejected too -- both sides of the
            # AND must match.
            with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
                _insert_qualification(
                    cursor,
                    capture_id=capture_id,
                    qualification_version=_V2_VERSION,
                    identity_registry_version="source_lead_identity_registry_bogus",
                    identity_registry_fingerprint=_V2_FINGERPRINT,
                )

            # The correct v2 row is accepted.
            _insert_qualification(
                cursor,
                capture_id=capture_id,
                qualification_version=_V2_VERSION,
                identity_registry_version=_V2_REGISTRY_VERSION,
                identity_registry_fingerprint=_V2_FINGERPRINT,
            )

            cursor.execute(
                "SELECT count(*) FROM app.source_lead_qualifications WHERE capture_id = %s",
                (capture_id,),
            )
            row = cursor.fetchone()
            assert row == (2,), f"expected exactly the v1 and v2 rows to persist, got {row}"
    finally:
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute("DELETE FROM app.pump_events WHERE base = %s", (base,))
        connection.close()


def test_downgrade_removes_the_v2_gate_then_upgrade_restores_it() -> None:
    connection = _connect_or_skip()
    base = f"T{uuid.uuid4().hex[:16].upper()}"
    config = _alembic_config()
    try:
        with connection.transaction(), connection.cursor() as cursor:
            capture_id = _insert_capture(cursor, base=base)

        command.downgrade(config, "0040")
        try:
            # With the constraint dropped, a wrong-fingerprint v2 row is no
            # longer rejected -- proving the downgrade actually removes the
            # gate, not just that it runs without error. Deleted again
            # before the constraint comes back: ADD CONSTRAINT validates
            # every existing row, so leaving this one in place would make
            # the upgrade below fail for an unrelated reason.
            with connection.transaction(), connection.cursor() as cursor:
                _insert_qualification(
                    cursor,
                    capture_id=capture_id,
                    qualification_version=_V2_VERSION,
                    identity_registry_version=_V2_REGISTRY_VERSION,
                    identity_registry_fingerprint=_WRONG_FINGERPRINT,
                )
                cursor.execute(
                    "SELECT count(*) FROM app.source_lead_qualifications WHERE capture_id = %s",
                    (capture_id,),
                )
                assert cursor.fetchone() == (1,)
                cursor.execute(
                    "DELETE FROM app.source_lead_qualifications WHERE capture_id = %s",
                    (capture_id,),
                )
        finally:
            command.upgrade(config, "head")

        # Back at head, a second, distinct capture must have the gate
        # enforced again.
        with connection.transaction(), connection.cursor() as cursor:
            second_capture_id = _insert_capture(cursor, base=f"{base}B")
            with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
                _insert_qualification(
                    cursor,
                    capture_id=second_capture_id,
                    qualification_version=_V2_VERSION,
                    identity_registry_version=_V2_REGISTRY_VERSION,
                    identity_registry_fingerprint=_WRONG_FINGERPRINT,
                )
    finally:
        # Restore head unconditionally, even if an assertion above failed --
        # this test must never leave the shared dev database migrated
        # backward for whatever test runs next.
        command.upgrade(config, "head")
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute("DELETE FROM app.pump_events WHERE base LIKE %s", (f"{base}%",))
        connection.close()
