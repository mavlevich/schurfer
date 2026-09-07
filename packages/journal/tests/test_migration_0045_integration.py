"""Real-Postgres invariants for widening asset_class_evidence to TEXT, migration 0045.

Colleague review of ENG-018: this column is written from venue-controlled
values, and VARCHAR(256) turned an unexpectedly long venue payload into a
failed INSERT. persistence.upsert_pumps runs a whole scan batch in one
transaction and its contract forbids publishing a Redis snapshot when that
fails, so the blast radius was the scanner's output, not one row.

Exercised against a real database rather than the migration text: a value far
longer than the old limit is accepted and returned intact.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
import pytest
from alembic import command
from alembic.config import Config

TEST_DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://schurfer:schurfer_dev@localhost:5432/schurfer"
)
ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"

_ALLOWED_TEST_HOSTS = {"localhost", "127.0.0.1"}
_ALLOWED_TEST_PORT = 5432


def _refuse_unless_local_test_database(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.hostname not in _ALLOWED_TEST_HOSTS or parsed.port != _ALLOWED_TEST_PORT:
        raise RuntimeError(
            f"refusing to run destructive migration-test DDL/DML against "
            f"{parsed.hostname}:{parsed.port} -- only localhost/127.0.0.1:{_ALLOWED_TEST_PORT} "
            "(the local dev or CI Postgres) is permitted."
        )


_refuse_unless_local_test_database(TEST_DATABASE_URL)


def _connect_or_skip() -> psycopg.Connection:
    try:
        connection = psycopg.connect(TEST_DATABASE_URL)
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema = 'app' AND table_name = 'pump_event_sources' "
                "AND column_name = 'asset_class_evidence'"
            )
            row = cursor.fetchone()
        if row is None or row[0] != "text":
            connection.close()
            pytest.skip("migration 0045 is not applied")
        return connection
    except Exception as exc:
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise RuntimeError(
                f"REQUIRE_INTEGRATION_DB=1 but Postgres/head is unavailable: {exc}"
            ) from exc
        pytest.skip(f"no local postgres/head reachable: {exc}")


def _alembic_config() -> Config:
    config = Config(str(ALEMBIC_INI))
    url = TEST_DATABASE_URL
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg://" + url[len(prefix) :]
            break
    config.set_main_option("sqlalchemy.url", url)
    return config


def _insert_event(cursor: psycopg.Cursor, base: str) -> int:
    cursor.execute(
        "INSERT INTO app.pump_events (base, first_seen_at, last_seen_at, peak_pct, last_pct) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (base, datetime.now(UTC), datetime.now(UTC), 42.0, 42.0),
    )
    row = cursor.fetchone()
    assert row is not None
    return int(row[0])


def test_evidence_longer_than_the_old_limit_is_accepted_intact() -> None:
    """The exact write that used to raise "value too long for type character
    varying(256)" and take the whole batch down with it."""
    connection = _connect_or_skip()
    evidence = "xt.tags=" + ", ".join(f"VERY_LONG_TAG_{i:04d}" for i in range(40))
    assert len(evidence) > 256
    try:
        with connection.transaction(), connection.cursor() as cursor:
            event_id = _insert_event(cursor, "ENG18C")
            cursor.execute(
                "INSERT INTO app.pump_event_sources ("
                "  event_id, exchange, symbol, asset_class, asset_class_source,"
                "  asset_class_evidence, asset_class_version,"
                "  first_change_pct, last_change_pct, peak_change_pct"
                ") VALUES (%s, 'xt', 'BTCUSDT', 'unknown', 'venue_value_unmapped',"
                " %s, 'asset_class_v1', 42.0, 42.0, 42.0)",
                (event_id, evidence),
            )
            cursor.execute(
                "SELECT asset_class_evidence FROM app.pump_event_sources WHERE event_id = %s",
                (event_id,),
            )
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == evidence
            cursor.execute("DELETE FROM app.pump_events WHERE base = %s", ("ENG18C",))
    finally:
        connection.close()


def test_downgrade_narrows_the_column_and_upgrade_widens_it_again() -> None:
    connection = _connect_or_skip()
    config = _alembic_config()

    def _column_type() -> str:
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema = 'app' AND table_name = 'pump_event_sources' "
                "AND column_name = 'asset_class_evidence'"
            )
            row = cursor.fetchone()
            assert row is not None
            return str(row[0])

    try:
        command.downgrade(config, "0044")
        assert _column_type() == "character varying"
    finally:
        command.upgrade(config, "head")
        assert _column_type() == "text"
        connection.close()
