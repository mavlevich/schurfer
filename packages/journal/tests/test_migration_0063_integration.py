"""Real-Postgres invariant for migration 0063 (LIVE_PROBE records and reservations):
downgrade refuses while any record exists, so the evidence is never dropped with the
schema. The table invariants are tested with the code in
apps/execution/tests/test_live_probe_integration.py.
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
        pytest.skip("migration 0063 is not applied")
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
        conn.execute("DELETE FROM app.live_probe_authorizations WHERE probe_id LIKE 'm0063-%'")


def test_downgrade_refuses_while_a_record_exists() -> None:
    probe_id = f"m0063-{uuid.uuid4().hex[:10]}"
    with _connect_or_skip() as conn:
        conn.execute(
            "INSERT INTO app.live_probe_authorizations (probe_id, environment, account_key, "
            "exchange, strategy, contract_version, contract_sha256, instruments, max_entries, "
            "notional_usd, loss_threshold_usd, entries_until, flat_by, issued_by) VALUES "
            "(%s, 'testnet', 'k', 'bybit', 's', 'v', %s, '{BTCUSDT}', 1, 5, 1, "
            "now() + interval '1 day', now() + interval '2 days', 'owner')",
            (probe_id, "a" * 64),
        )

    with pytest.raises(Exception, match="LIVE_PROBE evidence"):
        command.downgrade(_alembic_config(), "0062")

    with _connect_or_skip() as conn:
        assert conn.execute(
            "SELECT count(*) FROM app.live_probe_authorizations WHERE probe_id = %s",
            (probe_id,),
        ).fetchone() == (1,)
