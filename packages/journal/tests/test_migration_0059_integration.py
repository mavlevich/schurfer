"""Real-Postgres invariants for migration 0059 (history archive snapshots and sets).

Chunk and snapshot rows each need their own shape; a snapshot may be empty; live
snapshots are unique per dataset and set; `unit` and `snapshot_set` never change. A set
is verified only with every required member verified, gives way only to a newer verified
set of its purpose, and protects its members. Nothing is deleted. Downgrade refuses while
snapshots exist.
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

SHA = "a" * 64
FP = "hafp_v1:" + "b" * 64
T0 = datetime(2026, 10, 5, tzinfo=UTC)


def _connect_or_skip() -> psycopg.Connection[Any]:
    try:
        conn = psycopg.connect(TEST_DATABASE_URL, autocommit=True)
    except psycopg.OperationalError as exc:
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise RuntimeError(
                f"REQUIRE_INTEGRATION_DB=1 but Postgres is unavailable: {exc}"
            ) from exc
        pytest.skip(f"no local postgres reachable: {exc}")
    if conn.execute("SELECT to_regclass('app.history_archive_snapshot_sets')").fetchone() == (
        None,
    ):
        conn.close()
        pytest.skip("migration 0059 is not applied")
    return conn


@pytest.fixture(autouse=True)
def _forget_test_rows() -> Any:
    """Test-only cleanup on the disposable database (the guards forbid deletes)."""
    yield
    try:
        conn = psycopg.connect(TEST_DATABASE_URL, autocommit=True)
    except psycopg.OperationalError:
        return
    with conn:
        if conn.execute("SELECT to_regclass('app.history_archive_snapshot_sets')").fetchone() == (
            None,
        ):
            return
        with conn.transaction():
            conn.execute("SET LOCAL session_replication_role = replica")
            conn.execute("DELETE FROM app.history_archive_datasets WHERE dataset LIKE 'm0059\\_%'")
            conn.execute(
                "DELETE FROM app.history_archive_snapshot_sets WHERE purpose LIKE 'm0059\\_%'"
            )


def _name() -> str:
    return f"m0059_{uuid.uuid4().hex[:10]}"


def _snapshot_row(
    conn: psycopg.Connection[Any], dataset: str, set_id: str, *, rows: int = 5, revision: int = 1
) -> int:
    row = conn.execute(
        "INSERT INTO app.history_archive_datasets (dataset, contract_version, source_table, "
        "revision, state, row_count, file_name, file_bytes, file_sha256, content_fingerprint, "
        "manifest_sha256, snapshot_at, code_revision, unit, snapshot_set) VALUES (%s, 'v1', "
        "'app.t', %s, 'exported', %s, 'f', 100, %s, %s, %s, now(), 'rev', 'snapshot', %s) "
        "RETURNING id",
        (dataset, revision, rows, SHA, FP, SHA, set_id),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _verify_row(conn: psycopg.Connection[Any], row_id: int) -> None:
    conn.execute(
        "UPDATE app.history_archive_datasets SET state = 'archived', borg_archive = 'a', "
        "archived_at = now() WHERE id = %s",
        (row_id,),
    )
    conn.execute(
        "UPDATE app.history_archive_datasets SET state = 'verified', verified_at = now(), "
        "verified_sha256 = file_sha256, verified_fingerprint = content_fingerprint WHERE id = %s",
        (row_id,),
    )


def _set(conn: psycopg.Connection[Any], purpose: str, members: list[str], at: datetime) -> str:
    set_id = _name()
    conn.execute(
        "INSERT INTO app.history_archive_snapshot_sets (set_id, purpose, required_datasets, "
        "snapshot_at, code_revision, pinned_chunks, reference, reference_sha256, state) "
        "VALUES (%s, %s, %s, %s, 'rev', '[]', '{}', %s, 'building')",
        (set_id, purpose, members, at, SHA),
    )
    return set_id


def test_chunk_and_snapshot_rows_have_their_own_shape() -> None:
    with _connect_or_skip() as conn:
        dataset = _name()
        set_id = _name()
        assert _snapshot_row(conn, dataset, set_id, rows=0)  # an empty snapshot is allowed
        with pytest.raises(psycopg.errors.CheckViolation):  # a snapshot with a range
            conn.execute(
                "INSERT INTO app.history_archive_datasets (dataset, contract_version, "
                "source_table, range_start, range_end, revision, state, row_count, file_name, "
                "file_bytes, file_sha256, content_fingerprint, manifest_sha256, snapshot_at, "
                "code_revision, unit, snapshot_set) VALUES (%s, 'v1', 'app.t', %s, %s, 2, "
                "'exported', 1, 'f', 1, %s, %s, %s, now(), 'r', 'snapshot', %s)",
                (dataset, T0, T0.replace(day=6), SHA, FP, SHA, _name()),
            )
        with pytest.raises(psycopg.errors.CheckViolation):  # a chunk without a range
            conn.execute(
                "INSERT INTO app.history_archive_datasets (dataset, contract_version, "
                "source_table, chunk_name, revision, state, row_count, file_name, file_bytes, "
                "file_sha256, content_fingerprint, manifest_sha256, snapshot_at, code_revision) "
                "VALUES (%s, 'v1', 'app.t', 'c', 1, 'exported', 1, 'f', 1, %s, %s, %s, now(), 'r')",
                (dataset, SHA, FP, SHA),
            )
        with pytest.raises(psycopg.errors.CheckViolation):  # an empty chunk
            conn.execute(
                "INSERT INTO app.history_archive_datasets (dataset, contract_version, "
                "source_table, chunk_name, range_start, range_end, revision, state, row_count, "
                "file_name, file_bytes, file_sha256, content_fingerprint, manifest_sha256, "
                "snapshot_at, code_revision) VALUES (%s, 'v1', 'app.t', 'c', %s, %s, 1, "
                "'exported', 0, 'f', 1, %s, %s, %s, now(), 'r')",
                (dataset, T0, T0.replace(day=6), SHA, FP, SHA),
            )


def test_live_snapshots_are_unique_per_dataset_and_set_and_keep_unit_and_set() -> None:
    with _connect_or_skip() as conn:
        dataset, set_id = _name(), _name()
        first = _snapshot_row(conn, dataset, set_id)
        with pytest.raises(psycopg.errors.UniqueViolation):
            _snapshot_row(conn, dataset, set_id, revision=2)
        _snapshot_row(conn, dataset, _name())  # another set is fine
        for change in ("unit = 'chunk'", "snapshot_set = 'other'"):
            with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
                conn.execute(
                    f"UPDATE app.history_archive_datasets SET {change}, state = 'superseded', "  # noqa: S608
                    "superseded_reason = 'x' WHERE id = %s",
                    (first,),
                )
        conn.execute(
            "UPDATE app.history_archive_datasets SET state = 'superseded', "
            "superseded_reason = 'redo' WHERE id = %s",
            (first,),
        )
        _snapshot_row(conn, dataset, set_id, revision=2)


def test_set_lifecycle() -> None:
    with _connect_or_skip() as conn:
        purpose = _name()
        a_ds, b_ds = _name(), _name()
        old = _set(conn, purpose, [a_ds, b_ds], T0)
        a_row = _snapshot_row(conn, a_ds, old)
        _verify_row(conn, a_row)
        with pytest.raises(psycopg.errors.RaiseException, match="lacks verified members"):
            conn.execute(
                "UPDATE app.history_archive_snapshot_sets SET state = 'verified', "
                "verified_at = now() WHERE set_id = %s",
                (old,),
            )
        b_row = _snapshot_row(conn, b_ds, old)
        _verify_row(conn, b_row)
        conn.execute(
            "UPDATE app.history_archive_snapshot_sets SET state = 'verified', verified_at = now() "
            "WHERE set_id = %s",
            (old,),
        )
        with pytest.raises(psycopg.errors.RaiseException, match="belongs to verified set"):
            conn.execute(
                "UPDATE app.history_archive_datasets SET state = 'superseded', "
                "superseded_reason = 'x' WHERE id = %s",
                (a_row,),
            )
        building = _set(conn, purpose, [a_ds], T0.replace(day=6))
        with pytest.raises(psycopg.errors.RaiseException, match="newer verified set"):
            conn.execute(
                "UPDATE app.history_archive_snapshot_sets SET state = 'superseded', "
                "superseded_by = %s WHERE set_id = %s",
                (building, old),
            )
        with pytest.raises(psycopg.errors.CheckViolation):  # abandoning needs a reason
            conn.execute(
                "UPDATE app.history_archive_snapshot_sets SET state = 'abandoned' "
                "WHERE set_id = %s",
                (building,),
            )
        conn.execute(
            "UPDATE app.history_archive_snapshot_sets SET state = 'abandoned', "
            "closed_reason = 'incomplete inputs' WHERE set_id = %s",
            (building,),
        )
        newer = _set(conn, purpose, [a_ds], T0.replace(day=7))
        _verify_row(conn, _snapshot_row(conn, a_ds, newer))
        conn.execute(
            "UPDATE app.history_archive_snapshot_sets SET state = 'verified', verified_at = now() "
            "WHERE set_id = %s",
            (newer,),
        )
        conn.execute(
            "UPDATE app.history_archive_snapshot_sets SET state = 'superseded', "
            "superseded_by = %s WHERE set_id = %s",
            (newer, old),
        )
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
            conn.execute(
                "UPDATE app.history_archive_snapshot_sets SET reference = '{\"x\": 1}' "
                "WHERE set_id = %s",
                (newer,),
            )
        with pytest.raises(psycopg.errors.RaiseException, match="never deleted"):
            conn.execute("DELETE FROM app.history_archive_snapshot_sets WHERE set_id = %s", (old,))


def _alembic_config() -> Config:
    config = Config(str(ALEMBIC_INI))
    url = TEST_DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_downgrade_refuses_while_snapshots_exist() -> None:
    with _connect_or_skip() as conn:
        _snapshot_row(conn, _name(), _name())
    with pytest.raises(Exception, match="snapshots exist"):
        command.downgrade(_alembic_config(), "0058")
    with _connect_or_skip() as conn:
        assert conn.execute(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'app' "
            "AND table_name = 'history_archive_datasets' AND column_name = 'unit'"
        ).fetchone() == (1,)
