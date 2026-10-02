"""Real-Postgres invariants for migration 0057 (formal-read administrative stop).

`admin_stopped` is a third terminal state with a reason and an artifact fingerprint; a
trigger keeps terminal rows terminal and forbids turning an open claim into a stop;
downgrade to 0056 refuses while a stop exists and otherwise restores the old schema.
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
from schurfer_journal.testing_database import (
    assert_active_test_database_url,
    integration_database_url,
)

TEST_DATABASE_URL = integration_database_url()
ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def _refuse_unless_local_test_database(url: str) -> None:
    try:
        assert_active_test_database_url(url)
    except RuntimeError as exc:
        raise RuntimeError("refusing to run destructive migration-test DDL/DML") from exc


_refuse_unless_local_test_database(TEST_DATABASE_URL)


def _connect_or_skip() -> psycopg.Connection:
    try:
        connection = psycopg.connect(TEST_DATABASE_URL, autocommit=True)
        found = connection.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = 'app' "
            "AND table_name = 'formal_read_claims' AND column_name = 'terminal_reason'"
        ).fetchone()
        if found is None:
            connection.close()
            pytest.skip("migration 0057 is not applied")
        return connection
    except psycopg.OperationalError as exc:
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise RuntimeError(
                f"REQUIRE_INTEGRATION_DB=1 but Postgres is unavailable: {exc}"
            ) from exc
        pytest.skip(f"no local postgres reachable: {exc}")


def _alembic_config() -> Config:
    config = Config(str(ALEMBIC_INI))
    url = TEST_DATABASE_URL
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg://" + url[len(prefix) :]
            break
    config.set_main_option("sqlalchemy.url", url)
    return config


_START = datetime(2026, 9, 29, tzinfo=UTC)


def _insert(
    connection: psycopg.Connection,
    study: str,
    *,
    status: str = "claimed",
    completed: bool = False,
    fingerprint: str | None = None,
    reason: str | None = None,
) -> int:
    row = connection.execute(
        """
        INSERT INTO app.formal_read_claims (
            study_id, contract_version, cohort_start, database_now, candidate_count,
            candidate_ids, candidate_ids_sha256, code_revision, working_tree_dirty, status,
            lease_owner, lease_expires_at, completed_at, result_fingerprint, terminal_reason
        ) VALUES (%s, 'v2', %s, now(), 0, '[]'::jsonb, %s, 'abc', false, %s,
                  'owner', now(), CASE WHEN %s THEN now() END, %s, %s)
        RETURNING id
        """,
        (study, _START, "0" * 64, status, completed, fingerprint, reason),
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_admin_stopped_needs_a_reason_and_an_artifact() -> None:
    connection = _connect_or_skip()
    study = f"TEST0057-{uuid.uuid4().hex[:8]}"
    try:
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert(connection, study, status="admin_stopped", completed=True, fingerprint="f")
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert(connection, study, status="admin_stopped", completed=True, reason="r")
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert(connection, study, reason="a claim carries no terminal reason")
        _insert(
            connection, study, status="admin_stopped", completed=True, fingerprint="f", reason="r"
        )
    finally:
        connection.execute("DELETE FROM app.formal_read_claims WHERE study_id LIKE 'TEST0057-%'")
        connection.close()


def test_terminal_rows_stay_terminal_and_a_claim_never_becomes_a_stop() -> None:
    connection = _connect_or_skip()
    tag = uuid.uuid4().hex[:8]
    try:
        claim = _insert(connection, f"TEST0057-{tag}-a")
        # The lease takeover and the completion still work on an open claim.
        connection.execute(
            "UPDATE app.formal_read_claims SET lease_owner = 'other' WHERE id = %s", (claim,)
        )
        with pytest.raises(psycopg.errors.RaiseException, match="cannot become admin_stopped"):
            connection.execute(
                "UPDATE app.formal_read_claims SET status = 'admin_stopped', "
                "completed_at = now(), result_fingerprint = 'f', terminal_reason = 'r' "
                "WHERE id = %s",
                (claim,),
            )
        connection.execute(
            "UPDATE app.formal_read_claims SET status = 'completed', completed_at = now(), "
            "result_fingerprint = 'f' WHERE id = %s",
            (claim,),
        )
        with pytest.raises(psycopg.errors.RaiseException, match="is terminal"):
            connection.execute(
                "UPDATE app.formal_read_claims SET result_fingerprint = 'g' WHERE id = %s",
                (claim,),
            )
        stopped = _insert(
            connection,
            f"TEST0057-{tag}-b",
            status="admin_stopped",
            completed=True,
            fingerprint="f",
            reason="r",
        )
        with pytest.raises(psycopg.errors.RaiseException, match="is terminal"):
            connection.execute(
                "UPDATE app.formal_read_claims SET status = 'claimed', completed_at = NULL, "
                "result_fingerprint = NULL, terminal_reason = NULL WHERE id = %s",
                (stopped,),
            )
    finally:
        connection.execute("DELETE FROM app.formal_read_claims WHERE study_id LIKE 'TEST0057-%'")
        connection.close()


def test_downgrade_refuses_while_a_stop_exists_and_round_trips_without_one() -> None:
    connection = _connect_or_skip()
    study = f"TEST0057-{uuid.uuid4().hex[:8]}"
    config = _alembic_config()
    try:
        _insert(
            connection, study, status="admin_stopped", completed=True, fingerprint="f", reason="r"
        )
        with pytest.raises(Exception, match="administrative stops exist"):
            command.downgrade(config, "0056")
        connection.execute("DELETE FROM app.formal_read_claims WHERE study_id = %s", (study,))
        # Only test rows may hold a stop in the test database.
        remaining = connection.execute(
            "SELECT count(*) FROM app.formal_read_claims WHERE status = 'admin_stopped'"
        ).fetchone()
        if remaining is None or remaining[0]:
            pytest.skip("another administrative stop exists in the test database")
        command.downgrade(config, "0056")
        command.upgrade(config, "head")
        _connect_or_skip().close()
    finally:
        connection.execute("DELETE FROM app.formal_read_claims WHERE study_id LIKE 'TEST0057-%'")
        connection.close()
