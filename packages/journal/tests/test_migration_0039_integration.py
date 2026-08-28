"""Real-Postgres invariants for live reconciliation migration 0039."""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest

TEST_DATABASE_URL = "postgresql://schurfer:schurfer_dev@localhost:5432/schurfer"


def _connect_or_skip() -> psycopg.Connection:
    try:
        connection = psycopg.connect(TEST_DATABASE_URL)
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('app.live_reconciliation_incidents')")
            if cursor.fetchone() != ("app.live_reconciliation_incidents",):
                connection.close()
                pytest.skip("migration 0039 is not applied")
        return connection
    except Exception as exc:
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise RuntimeError(
                f"REQUIRE_INTEGRATION_DB=1 but Postgres/head is unavailable: {exc}"
            ) from exc
        pytest.skip(f"no local postgres/head reachable: {exc}")


def test_incident_key_is_stable_and_statuses_fail_closed() -> None:
    connection = _connect_or_skip()
    incident_key = uuid.uuid4().hex
    try:
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO app.live_reconciliation_incidents (
                    incident_key, exchange, symbol, native_market_id,
                    market_type, side, discrepancy_type, status, evidence_json
                ) VALUES (%s, 'bybit', 'BTC/USDT:USDT', 'BTCUSDT',
                          'swap', 'long', 'test', 'open', '{}'::jsonb)
                """,
                (incident_key,),
            )
            with pytest.raises(psycopg.errors.UniqueViolation), connection.transaction():
                cursor.execute(
                    """
                    INSERT INTO app.live_reconciliation_incidents (
                        incident_key, exchange, symbol, native_market_id,
                        market_type, side, discrepancy_type, status, evidence_json
                    ) VALUES (%s, 'bybit', 'BTC/USDT:USDT', 'BTCUSDT',
                              'swap', 'long', 'test', 'resolved', '{}'::jsonb)
                    """,
                    (incident_key,),
                )
            with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
                cursor.execute(
                    "UPDATE app.live_reconciliation_incidents SET status='ignored' "
                    "WHERE incident_key=%s",
                    (incident_key,),
                )
    finally:
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM app.live_reconciliation_incidents WHERE incident_key=%s",
                (incident_key,),
            )
        connection.close()
