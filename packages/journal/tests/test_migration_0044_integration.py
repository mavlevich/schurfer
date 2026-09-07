"""Real-Postgres invariants for the pump-event-source asset class, migration 0044.

Exercises the actual columns and partial index against a real
pump_event_sources row rather than the migration source text: the five class
columns accept and return a full classification, a row written before the
migration keeps NULL rather than being backfilled with a guess, and downgrade
actually removes them (then upgrade restores them).

ENG-018: the broad scanner records market_type='swap' for every admitted
ticker, which says the instrument is a perpetual and nothing about what it
tracks, so LBank 24H stock futures entered pump cohorts as crypto pumps.
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

# Same guard as the 0043 test: this file runs real DDL and DML, and must never
# do so against the documented production tunnel (port 15432) or any remote
# host, whatever DATABASE_URL happens to be exported to.
_ALLOWED_TEST_HOSTS = {"localhost", "127.0.0.1"}
_ALLOWED_TEST_PORT = 5432

_COLUMNS = (
    "asset_class",
    "asset_class_source",
    "asset_class_evidence",
    "asset_class_confidence",
    "asset_class_version",
)


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
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'app' AND table_name = 'pump_event_sources' "
                "AND column_name = 'asset_class'"
            )
            found = cursor.fetchone() is not None
        if not found:
            connection.close()
            pytest.skip("migration 0044 is not applied")
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


def _cleanup(cursor: psycopg.Cursor, base: str) -> None:
    """Sources cascade from the event, so deleting the event is enough."""
    cursor.execute("DELETE FROM app.pump_events WHERE base = %s", (base,))


def _insert_event(cursor: psycopg.Cursor, base: str) -> int:
    cursor.execute(
        "INSERT INTO app.pump_events (base, first_seen_at, last_seen_at, peak_pct, last_pct) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (base, datetime.now(UTC), datetime.now(UTC), 42.0, 42.0),
    )
    row = cursor.fetchone()
    assert row is not None
    return int(row[0])


def test_columns_round_trip_a_full_classification() -> None:
    connection = _connect_or_skip()
    try:
        with connection.transaction(), connection.cursor() as cursor:
            event_id = _insert_event(cursor, "ENG18A")
            cursor.execute(
                "INSERT INTO app.pump_event_sources ("
                "  event_id, exchange, symbol, market_type, base_asset,"
                "  asset_class, asset_class_source, asset_class_evidence,"
                "  asset_class_confidence, asset_class_version,"
                "  first_change_pct, last_change_pct, peak_change_pct"
                ") VALUES (%s, 'lbank', 'DJTUSDT', 'swap', 'DJT',"
                " 'tokenized_equity', 'curated', 'lbank:DJT', 'curated_reported',"
                " 'asset_class_v1', 42.0, 42.0, 42.0)",
                (event_id,),
            )
            cursor.execute(
                "SELECT asset_class, asset_class_source, asset_class_evidence,"
                " asset_class_confidence, asset_class_version"
                " FROM app.pump_event_sources WHERE event_id = %s",
                (event_id,),
            )
            assert cursor.fetchone() == (
                "tokenized_equity",
                "curated",
                "lbank:DJT",
                "curated_reported",
                "asset_class_v1",
            )
            _cleanup(cursor, "ENG18A")
    finally:
        connection.close()


def test_a_row_written_without_a_class_stays_null_rather_than_backfilled() -> None:
    """NULL means never classified, which must stay distinguishable from the
    classifier's own 'unknown', meaning classified and the venue exposes no
    usable evidence."""
    connection = _connect_or_skip()
    try:
        with connection.transaction(), connection.cursor() as cursor:
            event_id = _insert_event(cursor, "ENG18B")
            cursor.execute(
                "INSERT INTO app.pump_event_sources ("
                "  event_id, exchange, symbol, first_change_pct, last_change_pct,"
                "  peak_change_pct"
                ") VALUES (%s, 'lbank', 'BTCUSDT', 42.0, 42.0, 42.0)",
                (event_id,),
            )
            cursor.execute(
                "SELECT asset_class, asset_class_version FROM app.pump_event_sources"
                " WHERE event_id = %s",
                (event_id,),
            )
            assert cursor.fetchone() == (None, None)
            _cleanup(cursor, "ENG18B")
    finally:
        connection.close()


def test_partial_index_exists_and_covers_classified_rows_only() -> None:
    connection = _connect_or_skip()
    try:
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = 'app' AND indexname = 'ix_pump_event_sources_asset_class'"
            )
            row = cursor.fetchone()
            assert row is not None, "migration 0044 did not create the class index"
            indexdef = row[0]
            assert "asset_class" in indexdef
            assert "WHERE" in indexdef.upper()
    finally:
        connection.close()


def test_downgrade_removes_the_columns_and_upgrade_restores_them() -> None:
    connection = _connect_or_skip()
    config = _alembic_config()
    try:
        command.downgrade(config, "0043")
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'app' AND table_name = 'pump_event_sources' "
                "AND column_name = ANY(%s)",
                (list(_COLUMNS),),
            )
            assert cursor.fetchall() == []
    finally:
        command.upgrade(config, "0044")
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'app' AND table_name = 'pump_event_sources' "
                "AND column_name = ANY(%s)",
                (list(_COLUMNS),),
            )
            assert {row[0] for row in cursor.fetchall()} == set(_COLUMNS)
        connection.close()
