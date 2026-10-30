"""Real-Postgres gate for the reconciliation incident upsert's previous status."""

from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg
import pytest
from schurfer_execution import reconciliation as rec
from schurfer_execution.reconciliation_worker import ReconciliationWorker
from schurfer_execution.supervisor import WorkerReadinessGate
from schurfer_journal.testing_database import (
    assert_active_test_database_url,
    integration_database_url,
)

TEST_DATABASE_URL = integration_database_url()


async def _connect_or_skip() -> psycopg.AsyncConnection:
    assert_active_test_database_url(TEST_DATABASE_URL)
    try:
        connection = await psycopg.AsyncConnection.connect(TEST_DATABASE_URL, autocommit=True)
        async with connection.cursor() as cursor:
            await cursor.execute("SELECT to_regclass('app.live_reconciliation_incidents')")
            row = await cursor.fetchone()
        if row is None or row[0] is None:
            await connection.close()
            pytest.skip("live_reconciliation_incidents is not migrated")
        return connection
    except Exception as exc:
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise RuntimeError(
                f"REQUIRE_INTEGRATION_DB=1 but PostgreSQL/head is unavailable: {exc}"
            ) from exc
        pytest.skip(f"no local postgres/head reachable: {exc}")


async def test_incident_alerts_once_per_sighting_and_again_after_resolution() -> None:
    connection = await _connect_or_skip()
    # A unique exchange name keeps this test's rows apart from any other test.
    exchange = f"itest-{uuid.uuid4().hex[:12]}"
    identity = rec.FullIdentity(exchange, "BTC/USDT:USDT", "BTCUSDT", "swap", "long")
    decision = rec.classify_position_state(identity, None, None, [], None)
    worker = ReconciliationWorker(
        {},
        TEST_DATABASE_URL,
        MagicMock(),
        WorkerReadinessGate(set()),
        alert_credentials=("token", "chat"),
    )
    notify_alert = AsyncMock(return_value=True)
    try:
        with patch("schurfer_execution.reconciliation_worker.notify.notify_alert", notify_alert):
            key = await worker._upsert_incident(decision, identity, None, [], None)
            await worker._upsert_incident(decision, identity, None, [], None)
            assert notify_alert.await_count == 1

            async with connection.cursor() as cursor:
                await cursor.execute(
                    "UPDATE app.live_reconciliation_incidents SET status = 'resolved' "
                    "WHERE incident_key = %s",
                    (key,),
                )
            await worker._upsert_incident(decision, identity, None, [], None)
            assert notify_alert.await_count == 2

            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT status FROM app.live_reconciliation_incidents WHERE incident_key = %s",
                    (key,),
                )
                row = await cursor.fetchone()
            assert row == ("open",)
    finally:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "DELETE FROM app.live_reconciliation_incidents WHERE exchange = %s", (exchange,)
            )
        await connection.close()
