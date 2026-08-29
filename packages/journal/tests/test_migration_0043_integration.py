"""Real-Postgres invariants for source-lead qualification registry v3
migration 0043.

Exercises the actual ck_source_lead_qualification_v3_registry_contract CHECK
CONSTRAINT against a real source_lead_qualifications row, not just the
migration source text: a v3-tagged row must carry exactly the pinned
registry_v3 version and fingerprint, the existing v1/v2 constraints
(migrations 0022/0041) must be untouched, and dropping the constraint on
downgrade must actually remove the gate (then upgrade must restore it).

This constraint is deliberately inert in production while this PR is live
(research/gate-source-lead-registry-activation-v3, PR 2 of 3):
source_lead_qualification.py's QUALIFICATION_VERSION still writes v2-tagged
rows, and no v3-tagged row exists until PR 3. This test exercises the gate
directly at the database level regardless of what application code
currently writes.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
import pytest
from alembic import command
from alembic.config import Config

# Single source of truth for which database this test operates on --
# TEST_DATABASE_URL (the raw psycopg connection _connect_or_skip uses for
# every INSERT/assert) and _alembic_config() (which drives command.upgrade/
# command.downgrade) must never be able to disagree. An earlier version
# read DATABASE_URL only inside _alembic_config(), independently of this
# constant: if DATABASE_URL was set to a different database than the
# hardcoded default, the test's own assertions ran against one database
# while its downgrade/upgrade commands ran DDL against a different one --
# a misleading pass/fail signal at best, and a real risk of running
# alembic downgrade against an unintended database at worst (colleague
# review, 2026-08-29, PR 2 review round).
TEST_DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://schurfer:schurfer_dev@localhost:5432/schurfer"
)
ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"

# Deriving TEST_DATABASE_URL from DATABASE_URL (above) closed the "two
# different databases" gap, but opened a worse one: this test runs real DDL
# (command.downgrade, DROP/ADD CONSTRAINT) and INSERT/DELETE, and would
# blindly trust whatever DATABASE_URL happens to be set to -- including this
# repo's own documented production tunnel (`ssh -f -N -L
# 15432:127.0.0.1:5432 schurfer`), if a developer's shell still had
# DATABASE_URL exported from an earlier prod session (colleague review,
# 2026-08-29, PR 2 second review round). Refuses to proceed unless the
# resolved host/port is exactly the local dev Postgres or CI's own isolated
# service container -- both this project's Makefile and .github/workflows/
# ci.yml always use localhost:5432/127.0.0.1:5432 for that; the prod tunnel
# is deliberately mapped to the non-standard 15432 specifically so the two
# can never collide.
_ALLOWED_TEST_HOSTS = {"localhost", "127.0.0.1"}
_ALLOWED_TEST_PORT = 5432


def _refuse_unless_local_test_database(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.hostname not in _ALLOWED_TEST_HOSTS or parsed.port != _ALLOWED_TEST_PORT:
        raise RuntimeError(
            f"refusing to run destructive migration-test DDL/DML against "
            f"{parsed.hostname}:{parsed.port} -- only localhost/127.0.0.1:{_ALLOWED_TEST_PORT} "
            "(the local dev or CI Postgres) is permitted. DATABASE_URL is pointed somewhere "
            "else -- if this is genuinely a safe, isolated test database, point it at port "
            f"{_ALLOWED_TEST_PORT} rather than changing this guard."
        )


_refuse_unless_local_test_database(TEST_DATABASE_URL)


def test_refuses_the_documented_production_tunnel_port() -> None:
    """The exact scenario the guard exists for: DATABASE_URL left pointed
    at this repo's own documented production tunnel
    (`ssh -f -N -L 15432:127.0.0.1:5432 schurfer`) from an earlier session."""
    with pytest.raises(RuntimeError, match="refusing to run destructive"):
        _refuse_unless_local_test_database("postgresql://schurfer:x@127.0.0.1:15432/schurfer")


def test_refuses_a_remote_host_even_on_the_expected_port() -> None:
    with pytest.raises(RuntimeError, match="refusing to run destructive"):
        _refuse_unless_local_test_database("postgresql://schurfer:x@db.example.com:5432/schurfer")


def test_accepts_localhost_and_loopback_on_the_local_dev_port() -> None:
    _refuse_unless_local_test_database("postgresql://schurfer:x@localhost:5432/schurfer")
    _refuse_unless_local_test_database("postgresql://schurfer:x@127.0.0.1:5432/schurfer")


_V2_VERSION = "source_lead_qualified_capture_v2"
_V2_REGISTRY_VERSION = "source_lead_identity_registry_v2"
_V2_FINGERPRINT = "757fd1327593d07ca27efe17a031ae0eab95bf6998aecc1ec26f0df38667dca0"
_V3_VERSION = "source_lead_qualified_capture_v3"
_V3_REGISTRY_VERSION = "source_lead_identity_registry_v3"
_V3_FINGERPRINT = "9d36c41442261cfe4e608342378e2d83f96c78afd537de682698796e77733236"
_WRONG_FINGERPRINT = "0" * 64


def _connect_or_skip() -> psycopg.Connection:
    try:
        connection = psycopg.connect(TEST_DATABASE_URL)
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM pg_constraint "
                "WHERE conname = 'ck_source_lead_qualification_v3_registry_contract'"
            )
            found = cursor.fetchone() is not None
        if not found:
            connection.close()
            pytest.skip("migration 0043 is not applied")
        return connection
    except Exception as exc:
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise RuntimeError(
                f"REQUIRE_INTEGRATION_DB=1 but Postgres/head is unavailable: {exc}"
            ) from exc
        pytest.skip(f"no local postgres/head reachable: {exc}")


def _alembic_config() -> Config:
    """Sets sqlalchemy.url from TEST_DATABASE_URL directly, normalizing a
    plain postgresql:// scheme to postgresql+psycopg:// the same way
    migrations/env.py does -- env.py only applies that normalization when
    it reads DATABASE_URL from the OS environment itself, which it does
    NOT do when the variable is unset, leaving a plain postgresql:// URL
    to reach SQLAlchemy's engine_from_config and default to the psycopg2
    driver (not installed here). Must stay independent of whether
    DATABASE_URL happens to be set in the ambient shell, not just work when
    it is."""
    config = Config(str(ALEMBIC_INI))
    url = TEST_DATABASE_URL
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg://" + url[len(prefix) :]
            break
    config.set_main_option("sqlalchemy.url", url)
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
            %s, 'test_migration_0043', 'gate', %s, %s,
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


def test_v3_row_requires_the_pinned_registry_contract_and_v2_is_untouched() -> None:
    connection = _connect_or_skip()
    base = f"T{uuid.uuid4().hex[:16].upper()}"
    try:
        with connection.transaction(), connection.cursor() as cursor:
            capture_id = _insert_capture(cursor, base=base)

            # v2 rows are governed by the pre-existing (migration 0041)
            # constraint, untouched by 0043: a correct v2 row still inserts.
            _insert_qualification(
                cursor,
                capture_id=capture_id,
                qualification_version=_V2_VERSION,
                identity_registry_version=_V2_REGISTRY_VERSION,
                identity_registry_fingerprint=_V2_FINGERPRINT,
            )

            # ...and a v2 row with a wrong fingerprint is still rejected,
            # proving 0043 did not loosen or replace the v2 constraint.
            with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
                _insert_qualification(
                    cursor,
                    capture_id=capture_id,
                    qualification_version=_V2_VERSION,
                    identity_registry_version=_V2_REGISTRY_VERSION,
                    identity_registry_fingerprint=_WRONG_FINGERPRINT,
                )

            # A v3 row with a wrong fingerprint is rejected by 0043's new
            # constraint.
            with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
                _insert_qualification(
                    cursor,
                    capture_id=capture_id,
                    qualification_version=_V3_VERSION,
                    identity_registry_version=_V3_REGISTRY_VERSION,
                    identity_registry_fingerprint=_WRONG_FINGERPRINT,
                )

            # A v3 row claiming the wrong registry_version string (even with
            # the correct fingerprint) is rejected too -- both sides of the
            # AND must match.
            with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
                _insert_qualification(
                    cursor,
                    capture_id=capture_id,
                    qualification_version=_V3_VERSION,
                    identity_registry_version="source_lead_identity_registry_bogus",
                    identity_registry_fingerprint=_V3_FINGERPRINT,
                )

            # The correct v3 row is accepted.
            _insert_qualification(
                cursor,
                capture_id=capture_id,
                qualification_version=_V3_VERSION,
                identity_registry_version=_V3_REGISTRY_VERSION,
                identity_registry_fingerprint=_V3_FINGERPRINT,
            )

            cursor.execute(
                "SELECT count(*) FROM app.source_lead_qualifications WHERE capture_id = %s",
                (capture_id,),
            )
            row = cursor.fetchone()
            assert row == (2,), f"expected exactly the v2 and v3 rows to persist, got {row}"
    finally:
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute("DELETE FROM app.pump_events WHERE base = %s", (base,))
        connection.close()


def test_downgrade_removes_the_v3_gate_then_upgrade_restores_it() -> None:
    connection = _connect_or_skip()
    base = f"T{uuid.uuid4().hex[:16].upper()}"
    config = _alembic_config()
    try:
        with connection.transaction(), connection.cursor() as cursor:
            capture_id = _insert_capture(cursor, base=base)

        command.downgrade(config, "0042")
        try:
            # With the constraint dropped, a wrong-fingerprint v3 row is no
            # longer rejected -- proving the downgrade actually removes the
            # gate, not just that it runs without error. Deleted again
            # before the constraint comes back: ADD CONSTRAINT validates
            # every existing row, so leaving this one in place would make
            # the upgrade below fail for an unrelated reason.
            with connection.transaction(), connection.cursor() as cursor:
                _insert_qualification(
                    cursor,
                    capture_id=capture_id,
                    qualification_version=_V3_VERSION,
                    identity_registry_version=_V3_REGISTRY_VERSION,
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
                    qualification_version=_V3_VERSION,
                    identity_registry_version=_V3_REGISTRY_VERSION,
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
