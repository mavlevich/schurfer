"""Real-Postgres invariants for migration 0064 (probe emergency close reasons): a task
may carry the two probe reasons, and downgrade refuses while one exists.
"""

from __future__ import annotations

import os
import uuid
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


def _connect_or_skip() -> psycopg.Connection[Any]:
    try:
        conn = psycopg.connect(TEST_DATABASE_URL, autocommit=True)
    except psycopg.OperationalError as exc:
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise RuntimeError(
                f"REQUIRE_INTEGRATION_DB=1 but Postgres is unavailable: {exc}"
            ) from exc
        pytest.skip(f"no local postgres reachable: {exc}")
    if conn.execute("SELECT to_regclass('app.live_probe_authorizations')").fetchone() == (None,):
        conn.close()
        pytest.skip("migration 0064 is not applied")
    return conn


def _alembic_config() -> Config:
    config = Config(str(ALEMBIC_INI))
    url = TEST_DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture(autouse=True)
def _cleanup() -> Any:
    """Test-only cleanup on the disposable database (the guards forbid deletes)."""
    yield
    try:
        conn = psycopg.connect(TEST_DATABASE_URL, autocommit=True)
    except psycopg.OperationalError:
        return
    with conn, conn.transaction():
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute("DELETE FROM app.live_emergency_closes WHERE exchange = 'm0064'")
        conn.execute("DELETE FROM app.live_order_attempts WHERE exchange = 'm0064'")


def _task(conn: psycopg.Connection[Any], reason: str) -> None:
    row = conn.execute(
        "INSERT INTO app.live_order_attempts (operation, client_order_id, exchange, base, "
        "symbol, native_market_id, market_type, side, size_usd, requested_amount, leverage, "
        "exit_params, setup_context, status) VALUES ('entry', %s, 'm0064', 'X', 'X/USDT:USDT', "
        "%s, 'swap', 'long', 10, 1, 1, '{}'::jsonb, '{}'::jsonb, 'completed') RETURNING id",
        (uuid.uuid4().hex, uuid.uuid4().hex[:12]),
    ).fetchone()
    assert row is not None
    conn.execute(
        "INSERT INTO app.live_emergency_closes (source_attempt_id, entry_client_order_id, "
        "exchange, base, symbol, native_market_id, market_type, side, owned_quantity, "
        "owned_remainder, reason, status) VALUES (%s, %s, 'm0064', 'X', 'X/USDT:USDT', %s, "
        "'swap', 'long', 1, 1, %s, 'requested')",
        (row[0], uuid.uuid4().hex, uuid.uuid4().hex[:12], reason),
    )


def test_the_probe_reasons_are_allowed_and_others_are_not() -> None:
    with _connect_or_skip() as conn:
        _task(conn, "probe_loss_threshold")
        _task(conn, "probe_recovery")
        with pytest.raises(psycopg.errors.CheckViolation):
            _task(conn, "probe_whatever")


def test_downgrade_refuses_while_a_probe_task_exists() -> None:
    with _connect_or_skip() as conn:
        _task(conn, "probe_recovery")

    with pytest.raises(Exception, match="probe emergency close tasks"):
        command.downgrade(_alembic_config(), "0063")

    with _connect_or_skip() as conn:
        assert conn.execute(
            "SELECT count(*) FROM app.live_emergency_closes WHERE exchange = 'm0064'"
        ).fetchone() == (1,)
