"""Real-Postgres gate for the reconciliation incident upsert's previous status."""

from __future__ import annotations

import asyncio
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg
import pytest
from schurfer_execution import reconciliation as rec
from schurfer_execution.reconciliation_worker import ReconciliationWorker, _incident_key
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


def _worker() -> ReconciliationWorker:
    return ReconciliationWorker(
        {},
        TEST_DATABASE_URL,
        MagicMock(),
        WorkerReadinessGate(set()),
        alert_credentials=("token", "chat"),
    )


async def _wait_for_lock_waiters(connection: psycopg.AsyncConnection, count: int) -> None:
    for _ in range(200):
        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
            row = await cursor.fetchone()
        if row is not None and row[0] >= count:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"expected {count} sessions waiting on a lock")


async def test_concurrent_sightings_of_one_incident_alert_once() -> None:
    connection = await _connect_or_skip()
    exchange = f"itest-{uuid.uuid4().hex[:12]}"
    identity = rec.FullIdentity(exchange, "BTC/USDT:USDT", "BTCUSDT", "swap", "long")
    decision = rec.classify_position_state(identity, None, None, [], None)
    key = _incident_key(identity, decision.discrepancy)
    # An uncommitted insert of the same incident holds both scans at their
    # statements until it commits, the interleaving that made a statement
    # snapshot report a first sighting to both of them.
    blocker = await psycopg.AsyncConnection.connect(TEST_DATABASE_URL)
    notify_alert = AsyncMock(return_value=True)
    try:
        async with blocker.cursor() as cursor:
            await cursor.execute(
                "INSERT INTO app.live_reconciliation_incidents ("
                "incident_key, exchange, symbol, native_market_id, market_type, side, "
                "contracts, discrepancy_type, status, evidence_json"
                ") VALUES (%s, %s, %s, %s, %s, %s, NULL, %s, 'open', '{}'::jsonb)",
                (
                    key,
                    exchange,
                    identity.symbol,
                    identity.native_market_id,
                    identity.market_type,
                    identity.side,
                    decision.discrepancy.value,
                ),
            )
        with patch("schurfer_execution.reconciliation_worker.notify.notify_alert", notify_alert):
            scans = asyncio.gather(
                _worker()._upsert_incident(decision, identity, None, [], None),
                _worker()._upsert_incident(decision, identity, None, [], None),
            )
            await _wait_for_lock_waiters(connection, 2)
            await blocker.commit()
            await asyncio.wait_for(scans, timeout=10)

        assert notify_alert.await_count == 1
    finally:
        await blocker.close()
        async with connection.cursor() as cursor:
            await cursor.execute(
                "DELETE FROM app.live_reconciliation_incidents WHERE exchange = %s", (exchange,)
            )
        await connection.close()
