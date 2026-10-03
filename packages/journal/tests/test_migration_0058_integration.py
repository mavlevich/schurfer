"""Real-Postgres invariants for migration 0058 (history archive catalog and fence).

Catalog rows move only forward with their evidence, keep their content, are never
deleted, and one live revision exists per range. Fences only move up and are never
deleted. Downgrade refuses while the catalog holds rows.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from schurfer_journal.testing_database import (
    assert_active_test_database_url,
    integration_database_url,
)

TEST_DATABASE_URL = integration_database_url()
ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
assert_active_test_database_url(TEST_DATABASE_URL)

START = datetime(2001, 1, 4, tzinfo=UTC)
END = datetime(2001, 1, 11, tzinfo=UTC)
SHA = "a" * 64
FP = "hafp_v1:" + "b" * 64


def _connect_or_skip() -> psycopg.Connection[Any]:
    try:
        conn = psycopg.connect(TEST_DATABASE_URL, autocommit=True)
    except psycopg.OperationalError as exc:
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise RuntimeError(
                f"REQUIRE_INTEGRATION_DB=1 but Postgres is unavailable: {exc}"
            ) from exc
        pytest.skip(f"no local postgres reachable: {exc}")
    if conn.execute("SELECT to_regclass('app.history_archive_datasets')").fetchone() == (None,):
        conn.close()
        pytest.skip("migration 0058 is not applied")
    return conn


def _insert(conn: psycopg.Connection[Any], dataset: str, *, revision: int = 1) -> int:
    row = conn.execute(
        "INSERT INTO app.history_archive_datasets (dataset, contract_version, source_table, "
        "chunk_name, range_start, range_end, revision, state, row_count, file_name, file_bytes, "
        "file_sha256, content_fingerprint, manifest_sha256, snapshot_at, code_revision) "
        "VALUES (%s, 'v1', 'app.live_long_short_ratio', 'c', %s, %s, %s, 'exported', 10, 'f', "
        "100, %s, %s, %s, now(), 'rev') RETURNING id",
        (dataset, START, END, revision, SHA, FP, SHA),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _dataset() -> str:
    return f"m0058_{uuid.uuid4().hex[:10]}"


@pytest.fixture(autouse=True)
def _forget_test_rows() -> Any:
    """Test-only cleanup: the guards forbid deleting catalog or fence rows, so they are
    bypassed for this session on the disposable database, keeping later migration
    round-trip tests free of archive rows."""
    yield
    try:
        conn = psycopg.connect(TEST_DATABASE_URL, autocommit=True)
    except psycopg.OperationalError:
        return
    with conn:
        if conn.execute("SELECT to_regclass('app.history_archive_datasets')").fetchone() == (None,):
            return
        with conn.transaction():
            conn.execute("SET LOCAL session_replication_role = replica")
            conn.execute("DELETE FROM app.history_archive_datasets WHERE dataset LIKE 'm0058\\_%'")
            conn.execute("DELETE FROM app.history_archive_fences WHERE dataset LIKE 'm0058\\_%'")


def test_rows_move_forward_only_with_their_evidence() -> None:
    with _connect_or_skip() as conn:
        row_id = _insert(conn, _dataset())
        # No skipping a state, and each state needs its evidence.
        with pytest.raises(
            psycopg.errors.RaiseException, match="cannot move from exported to verified"
        ):
            conn.execute(
                "UPDATE app.history_archive_datasets SET state = 'verified', borg_archive = 'a', "
                "archived_at = now(), verified_at = now(), verified_sha256 = file_sha256, "
                "verified_fingerprint = content_fingerprint WHERE id = %s",
                (row_id,),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "UPDATE app.history_archive_datasets SET state = 'archived' WHERE id = %s",
                (row_id,),
            )
        conn.execute(
            "UPDATE app.history_archive_datasets SET state = 'archived', borg_archive = 'a1', "
            "archived_at = now() WHERE id = %s",
            (row_id,),
        )
        with pytest.raises(psycopg.errors.RaiseException, match="already names its archive"):
            conn.execute(
                "UPDATE app.history_archive_datasets SET borg_archive = 'a2' WHERE id = %s",
                (row_id,),
            )
        with pytest.raises(psycopg.errors.CheckViolation):  # extracted hash must match
            conn.execute(
                "UPDATE app.history_archive_datasets SET state = 'verified', verified_at = now(), "
                "verified_sha256 = %s, verified_fingerprint = content_fingerprint WHERE id = %s",
                ("c" * 64, row_id),
            )
        conn.execute(
            "UPDATE app.history_archive_datasets SET state = 'verified', verified_at = now(), "
            "verified_sha256 = file_sha256, verified_fingerprint = content_fingerprint "
            "WHERE id = %s",
            (row_id,),
        )
        with pytest.raises(psycopg.errors.RaiseException, match="cannot move from verified"):
            conn.execute(
                "UPDATE app.history_archive_datasets SET verified_at = now() WHERE id = %s",
                (row_id,),
            )
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
            conn.execute(
                "UPDATE app.history_archive_datasets SET row_count = 11, state = 'superseded', "
                "superseded_reason = 'x' WHERE id = %s",
                (row_id,),
            )
        conn.execute(
            "UPDATE app.history_archive_datasets SET state = 'superseded', "
            "superseded_reason = 'source changed' WHERE id = %s",
            (row_id,),
        )
        with pytest.raises(psycopg.errors.RaiseException, match="superseded and final"):
            conn.execute(
                "UPDATE app.history_archive_datasets SET superseded_reason = 'y' WHERE id = %s",
                (row_id,),
            )
        with pytest.raises(psycopg.errors.RaiseException, match="never deleted"):
            conn.execute("DELETE FROM app.history_archive_datasets WHERE id = %s", (row_id,))


@pytest.mark.parametrize(
    "proof",
    [
        "verified_sha256 = NULL, verified_fingerprint = content_fingerprint",
        "verified_sha256 = file_sha256, verified_fingerprint = NULL",
        "verified_sha256 = NULL, verified_fingerprint = NULL",
    ],
)
def test_verified_needs_both_extraction_proofs(proof: str) -> None:
    """A CHECK that evaluates to NULL passes, so each proof is required explicitly."""
    with _connect_or_skip() as conn:
        row_id = _insert(conn, _dataset())
        conn.execute(
            "UPDATE app.history_archive_datasets SET state = 'archived', borg_archive = 'a', "
            "archived_at = now() WHERE id = %s",
            (row_id,),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "UPDATE app.history_archive_datasets SET state = 'verified', "  # noqa: S608
                f"verified_at = now(), {proof} WHERE id = %s",
                (row_id,),
            )


def test_one_live_revision_per_range() -> None:
    with _connect_or_skip() as conn:
        dataset = _dataset()
        first = _insert(conn, dataset)
        with pytest.raises(psycopg.errors.UniqueViolation):
            _insert(conn, dataset, revision=2)
        conn.execute(
            "UPDATE app.history_archive_datasets SET state = 'superseded', "
            "superseded_reason = 'lost' WHERE id = %s",
            (first,),
        )
        _insert(conn, dataset, revision=2)
        with pytest.raises(psycopg.errors.UniqueViolation):
            _insert(conn, dataset, revision=2)


def test_fences_only_move_up_and_stay() -> None:
    with _connect_or_skip() as conn:
        dataset = _dataset()
        conn.execute(
            "INSERT INTO app.history_archive_fences (dataset, source_table, closed_before) "
            "VALUES (%s, 'x', %s)",
            (dataset, START),
        )
        conn.execute(
            "UPDATE app.history_archive_fences SET closed_before = %s WHERE dataset = %s",
            (END, dataset),
        )
        with pytest.raises(psycopg.errors.RaiseException, match="never moves back"):
            conn.execute(
                "UPDATE app.history_archive_fences SET closed_before = %s WHERE dataset = %s",
                (START, dataset),
            )
        with pytest.raises(psycopg.errors.RaiseException, match="changes only its instant"):
            conn.execute(
                "UPDATE app.history_archive_fences SET source_table = 'y' WHERE dataset = %s",
                (dataset,),
            )
        with pytest.raises(psycopg.errors.RaiseException, match="never deleted"):
            conn.execute("DELETE FROM app.history_archive_fences WHERE dataset = %s", (dataset,))
        assert conn.execute(
            "SELECT count(*) FROM app.history_archive_fences "
            "WHERE dataset = 'lsr_history' AND source_table = 'app.live_long_short_ratio'"
        ).fetchone() == (1,)


def _alembic_config() -> Config:
    config = Config(str(ALEMBIC_INI))
    url = TEST_DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_downgrade_refuses_while_the_catalog_holds_rows() -> None:
    with _connect_or_skip() as conn:
        _insert(conn, _dataset())
    with pytest.raises(Exception, match="refusing to drop"):
        command.downgrade(_alembic_config(), "0057")
    with _connect_or_skip() as conn:
        assert conn.execute(
            "SELECT count(*) FROM pg_trigger WHERE tgname = 'history_archive_fence' "
            "AND tgrelid = 'app.live_long_short_ratio'::regclass"
        ).fetchone() == (1,)
