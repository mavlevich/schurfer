"""Real-Postgres invariants for notification-deliveries warning-severity
migration 0040."""

from __future__ import annotations

import os
import uuid
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
        # Must run inside an explicit transaction() that actually commits: a
        # bare cursor.execute() implicitly opens a transaction (psycopg3
        # default autocommit=False) that otherwise stays open on the
        # returned connection, turning any later `connection.transaction()`
        # block in the caller into a nested savepoint rather than a real
        # commit -- the row locks it takes then outlive that block and can
        # deadlock a concurrent DDL connection (confirmed: this blocked
        # migration 0040's own downgrade() call in this exact test file
        # during development).
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM pg_constraint "
                "WHERE conname = 'ck_notification_deliveries_severity' "
                "AND pg_get_constraintdef(oid) LIKE '%warning%'"
            )
            found = cursor.fetchone() is not None
        if not found:
            connection.close()
            pytest.skip("migration 0040 is not applied")
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


def _insert_delivery(
    cursor: psycopg.Cursor, *, notification_id: str, dedup_key: str, severity: str
) -> None:
    cursor.execute(
        """
        INSERT INTO app.notification_deliveries (
            notification_id, envelope_version, producer, kind, severity,
            dedup_key, channel, stream_entry_id, status, attempt_count,
            first_enqueued_at, payload_hash
        ) VALUES (
            %s, 1, 'test', 'test.kind', %s, %s, 'telegram', 'test-entry',
            'pending', 0, now(), decode(repeat('00', 32), 'hex')
        )
        """,
        (notification_id, severity, dedup_key),
    )


def test_warning_severity_is_accepted_after_upgrade() -> None:
    connection = _connect_or_skip()
    dedup_key = f"test-0040-{uuid.uuid4().hex}"
    try:
        with connection.transaction(), connection.cursor() as cursor:
            _insert_delivery(
                cursor,
                notification_id=str(uuid.uuid4()),
                dedup_key=dedup_key,
                severity="warning",
            )
    finally:
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM app.notification_deliveries WHERE dedup_key = %s", (dedup_key,)
            )
        connection.close()


def test_unknown_severity_is_rejected() -> None:
    connection = _connect_or_skip()
    dedup_key = f"test-0040-{uuid.uuid4().hex}"
    try:
        with (
            pytest.raises(psycopg.errors.CheckViolation),
            connection.transaction(),
            connection.cursor() as cursor,
        ):
            _insert_delivery(
                cursor,
                notification_id=str(uuid.uuid4()),
                dedup_key=dedup_key,
                severity="bogus",
            )
    finally:
        connection.close()


def test_downgrade_folds_existing_warning_rows_to_info_then_upgrade_restores() -> None:
    """The colleague-review regression this test guards: re-adding the
    narrower pre-0040 constraint validates every existing row, so a
    'warning' row present at downgrade time must be folded back to 'info'
    first, not left to fail the ALTER outright."""
    connection = _connect_or_skip()
    dedup_key = f"test-0040-{uuid.uuid4().hex}"
    config = _alembic_config()
    try:
        with connection.transaction(), connection.cursor() as cursor:
            _insert_delivery(
                cursor,
                notification_id=str(uuid.uuid4()),
                dedup_key=dedup_key,
                severity="warning",
            )

        # Target migration 0040's own downgrade() explicitly by revision id,
        # not "-1" -- "-1" means "one below current head", which silently
        # stopped exercising 0040's fold-to-info logic once 0041 was added
        # on top of it and became the new head.
        command.downgrade(config, "0039")
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT severity FROM app.notification_deliveries WHERE dedup_key = %s",
                (dedup_key,),
            )
            assert cursor.fetchone() == ("info",)

        # Downgraded schema must reject 'warning' again.
        with (
            pytest.raises(psycopg.errors.CheckViolation),
            connection.transaction(),
            connection.cursor() as cursor,
        ):
            _insert_delivery(
                cursor,
                notification_id=str(uuid.uuid4()),
                dedup_key=f"{dedup_key}-rejected",
                severity="warning",
            )
    finally:
        # Restore head unconditionally, even if an assertion above failed --
        # this test must never leave the shared dev database migrated
        # backward for whatever test runs next.
        command.upgrade(config, "head")
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM app.notification_deliveries WHERE dedup_key LIKE %s",
                (f"{dedup_key}%",),
            )
        connection.close()
