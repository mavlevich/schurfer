"""Emergency close executor transitions on a fake exchange and an in-memory store,
then the Postgres store and migration 0061 on a real database.

Each case maps to a row of the tests table in
docs/engineering/live-entry-failure-paths-design-v1.md that PR 2 covers.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import ccxt
import psycopg
import pytest
from schurfer_execution import emergency_close as ec
from schurfer_execution import order_attempts
from schurfer_journal.testing_database import (
    assert_active_test_database_url,
    integration_database_url,
)

ENTRY = "0123456789abcdef0123456789abcdef"
SYMBOL = "BTC/USDT:USDT"
T0 = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


class MemoryStore:
    def __init__(self, task: ec.EmergencyCloseTask) -> None:
        self.task = task
        self.closes: list[ec.CloseOrder] = []
        self.claim_owner: str | None = None
        self.now = T0

    async def create_task(self, spec: ec.TaskSpec) -> tuple[int, bool]:
        raise NotImplementedError

    async def runnable_task_ids(self) -> list[int]:
        return [self.task.id] if self.task.status in ec.RUNNABLE_STATUSES else []

    async def claim(
        self, task_id: int, owner: str, lease_seconds: float
    ) -> ec.EmergencyCloseTask | None:
        if self.task.status not in ec.RUNNABLE_STATUSES:
            return None
        if self.claim_owner not in (None, owner):
            return None
        self.claim_owner = owner
        return self.task

    async def load_closes(self, task_id: int) -> list[ec.CloseOrder]:
        return list(self.closes)

    async def record_close_intent(
        self,
        task: ec.EmergencyCloseTask,
        *,
        client_order_id: str,
        amount: Decimal,
        size_usd: Decimal,
        contract_size: Decimal,
    ) -> int:
        attempt_id = len(self.closes) + 1
        self.closes.append(
            ec.CloseOrder(attempt_id, client_order_id, "pending", None, amount, None, self.now)
        )
        return attempt_id

    async def mark_close(
        self,
        attempt_id: int,
        *,
        status: str,
        order_id: str | None,
        filled_amount: Decimal | None,
        error: str | None,
    ) -> None:
        index = attempt_id - 1
        close = self.closes[index]
        self.closes[index] = replace(
            close,
            status=status,
            order_id=order_id or close.order_id,
            filled_amount=filled_amount if filled_amount is not None else close.filled_amount,
        )

    async def update_task(
        self,
        task: ec.EmergencyCloseTask,
        owner: str,
        *,
        status: str,
        owned_remainder: Decimal,
        rejected_sends: int,
        last_error: str | None,
        release: bool,
    ) -> None:
        assert owner == self.claim_owner
        self.task = replace(
            task, status=status, owned_remainder=owned_remainder, rejected_sends=rejected_sends
        )
        if release:
            self.claim_owner = None


class FakeExchange:
    """Positions and orders keyed by id; a close fills as configured."""

    def __init__(self, position: str | None, *, side: str = "long") -> None:
        self.has = {"fetchOpenOrders": True, "fetchClosedOrders": True}
        self.markets = {SYMBOL: {"contractSize": 1}}
        self.positions: list[dict[str, Any]] = []
        if position is not None:
            self.set_position(position, side=side)
        self.orders: dict[str, dict[str, Any]] = {}
        self.sent: list[tuple[str, float, dict[str, Any]]] = []
        self.cancelled: list[str] = []
        self.on_create: Any = None

    def set_position(self, contracts: str, *, side: str = "long") -> None:
        self.positions = [
            {"symbol": SYMBOL, "contracts": float(contracts), "side": side, "markPrice": 100.0}
        ]

    def add_order(self, client_order_id: str, *, status: str, filled: float, stop: bool = False):
        order_id = f"o{len(self.orders) + 1}"
        self.orders[order_id] = {
            "id": order_id,
            "clientOrderId": client_order_id,
            "status": status,
            "filled": filled,
            "stop": stop,
        }
        return self.orders[order_id]

    async def fetch_positions(self, symbols: list[str]) -> list[dict[str, Any]]:
        return list(self.positions)

    def _listing(self, open_orders: bool, params: dict[str, Any]) -> list[dict[str, Any]]:
        trigger = bool(params.get("trigger"))
        return [
            dict(order)
            for order in self.orders.values()
            if order["stop"] == trigger and (order["status"] == "open") == open_orders
        ]

    async def fetch_open_orders(self, symbol: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return self._listing(True, params)

    async def fetch_closed_orders(
        self, symbol: str, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return self._listing(False, params)

    async def cancel_order(self, order_id: str, symbol: str, params: dict[str, Any]) -> None:
        self.cancelled.append(order_id)
        self.orders[order_id]["status"] = "canceled"

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        return f"{amount:.8f}".rstrip("0").rstrip(".")

    async def create_market_order(
        self, symbol: str, side: str, amount: float, params: dict[str, Any]
    ) -> dict[str, Any]:
        self.sent.append((side, amount, params))
        if self.on_create is not None:
            return self.on_create(amount, params)
        order = self.add_order(params["clientOrderId"], status="closed", filled=amount)
        if amount >= self.positions[0]["contracts"]:
            self.set_position("0")
        return dict(order)


def _task(owned: str = "10", *, side: str = "long") -> ec.EmergencyCloseTask:
    return ec.EmergencyCloseTask(
        id=1,
        source_attempt_id=7,
        entry_client_order_id=ENTRY,
        exchange="bybit",
        base="BTC",
        symbol=SYMBOL,
        native_market_id="BTCUSDT",
        market_type="swap",
        side=side,
        trade_id=None,
        owned_quantity=Decimal(owned),
        owned_remainder=Decimal(owned),
        reason=ec.REASON_STOP_PLACEMENT_FAILED,
        status=ec.STATUS_REQUESTED,
        rejected_sends=0,
    )


def _rdb() -> MagicMock:
    rdb = MagicMock()
    rdb.set = AsyncMock(return_value=True)
    rdb.eval = AsyncMock(return_value=1)
    return rdb


def _executor(
    exchange: FakeExchange, store: MemoryStore, **kwargs: Any
) -> tuple[ec.EmergencyCloseExecutor, AsyncMock]:
    notify_alert = AsyncMock(return_value=True)
    executor = ec.EmergencyCloseExecutor(
        {"bybit": exchange},
        store,
        _rdb(),
        alert_credentials=("token", "chat"),
        owner="executor-a",
        now=lambda: store.now,
        **kwargs,
    )
    return executor, notify_alert


async def _run(executor: ec.EmergencyCloseExecutor, notify_alert: AsyncMock) -> str:
    with patch("schurfer_execution.emergency_close.notify.notify_alert", notify_alert):
        return await executor.run_pass(1)


async def test_close_row_is_durable_before_the_send_and_a_fill_confirms_flat() -> None:
    exchange = FakeExchange("10")
    store = MemoryStore(_task())
    executor, notify_alert = _executor(exchange, store)

    def created(amount: float, params: dict[str, Any]) -> dict[str, Any]:
        assert [close.client_order_id for close in store.closes] == [f"{ENTRY}c1"]
        assert store.task.status == ec.STATUS_SENDING
        exchange.set_position("0")
        return dict(exchange.add_order(params["clientOrderId"], status="closed", filled=amount))

    exchange.on_create = created

    assert await _run(executor, notify_alert) == ec.STATUS_SENDING
    assert exchange.sent == [("sell", 10.0, {"reduceOnly": True, "clientOrderId": f"{ENTRY}c1"})]
    assert store.closes[0].status == "completed"

    assert await _run(executor, notify_alert) == ec.STATUS_CONFIRMED_FLAT
    assert len(exchange.sent) == 1
    assert store.task.owned_remainder == 0
    assert "confirmed flat" in notify_alert.await_args.kwargs["text"]


async def test_short_task_closes_with_a_buy() -> None:
    exchange = FakeExchange("3", side="short")
    store = MemoryStore(_task("3", side="short"))
    executor, notify_alert = _executor(exchange, store)

    await _run(executor, notify_alert)

    assert exchange.sent[0][:2] == ("buy", 3.0)


async def test_unknown_close_is_looked_up_never_resent_then_escalated() -> None:
    exchange = FakeExchange("10")
    store = MemoryStore(_task())
    executor, notify_alert = _executor(exchange, store, unknown_deadline_seconds=60)

    def timed_out(amount: float, params: dict[str, Any]) -> dict[str, Any]:
        raise ccxt.RequestTimeout("timed out")

    exchange.on_create = timed_out

    assert await _run(executor, notify_alert) == ec.STATUS_UNKNOWN
    assert store.closes[0].status == "submission_unknown"

    store.now = T0 + timedelta(seconds=30)
    assert await _run(executor, notify_alert) == ec.STATUS_UNKNOWN
    store.now = T0 + timedelta(seconds=61)
    assert await _run(executor, notify_alert) == ec.STATUS_MANUAL_REQUIRED
    assert len(exchange.sent) == 1
    assert "needs an operator" in notify_alert.await_args.kwargs["text"]


async def test_position_absent_while_a_close_is_unknown_is_not_done() -> None:
    exchange = FakeExchange("10")
    store = MemoryStore(_task())
    executor, notify_alert = _executor(exchange, store)

    def timed_out(amount: float, params: dict[str, Any]) -> dict[str, Any]:
        exchange.set_position("0")
        raise ccxt.NetworkError("connection reset")

    exchange.on_create = timed_out
    await _run(executor, notify_alert)

    assert await _run(executor, notify_alert) == ec.STATUS_UNKNOWN


async def test_unknown_close_found_filled_is_counted_and_confirms_flat() -> None:
    exchange = FakeExchange("10")
    store = MemoryStore(_task())
    executor, notify_alert = _executor(exchange, store)

    def lost_response(amount: float, params: dict[str, Any]) -> dict[str, Any]:
        exchange.add_order(params["clientOrderId"], status="closed", filled=amount)
        exchange.set_position("0")
        raise ccxt.RequestTimeout("response lost")

    exchange.on_create = lost_response
    assert await _run(executor, notify_alert) == ec.STATUS_UNKNOWN

    assert await _run(executor, notify_alert) == ec.STATUS_CONFIRMED_FLAT
    assert store.closes[0].status == "completed"
    assert store.closes[0].filled_amount == Decimal(10)
    assert len(exchange.sent) == 1


async def test_active_close_is_followed_without_a_new_send() -> None:
    exchange = FakeExchange("10")
    store = MemoryStore(_task())
    executor, notify_alert = _executor(exchange, store)
    exchange.on_create = lambda amount, params: dict(
        exchange.add_order(params["clientOrderId"], status="open", filled=0)
    )

    assert await _run(executor, notify_alert) == ec.STATUS_SENDING
    assert await _run(executor, notify_alert) == ec.STATUS_SENDING
    assert len(exchange.sent) == 1


async def test_partial_ioc_fill_is_counted_and_the_remainder_is_sent() -> None:
    exchange = FakeExchange("10")
    store = MemoryStore(_task())
    executor, notify_alert = _executor(exchange, store)

    def partial(amount: float, params: dict[str, Any]) -> dict[str, Any]:
        exchange.set_position("6")
        return dict(exchange.add_order(params["clientOrderId"], status="canceled", filled=4))

    exchange.on_create = partial
    await _run(executor, notify_alert)
    exchange.on_create = None

    await _run(executor, notify_alert)

    assert exchange.sent[1] == ("sell", 6.0, {"reduceOnly": True, "clientOrderId": f"{ENTRY}c2"})
    assert store.task.owned_remainder == 6


async def test_foreign_volume_after_a_partial_close_sends_nothing() -> None:
    exchange = FakeExchange("10")
    store = MemoryStore(_task())
    executor, notify_alert = _executor(exchange, store)

    def partial(amount: float, params: dict[str, Any]) -> dict[str, Any]:
        exchange.set_position("8")  # ours 10, closed 4, someone added 2
        return dict(exchange.add_order(params["clientOrderId"], status="canceled", filled=4))

    exchange.on_create = partial
    await _run(executor, notify_alert)

    assert await _run(executor, notify_alert) == ec.STATUS_MANUAL_REQUIRED
    assert len(exchange.sent) == 1


async def test_explicit_rejection_allows_a_new_id_until_the_limit() -> None:
    exchange = FakeExchange("10")
    store = MemoryStore(_task())
    executor, notify_alert = _executor(exchange, store, max_rejected_sends=3)

    def rejected(amount: float, params: dict[str, Any]) -> dict[str, Any]:
        raise ccxt.InvalidOrder("reduce-only rejected")

    exchange.on_create = rejected

    assert await _run(executor, notify_alert) == ec.STATUS_REQUESTED
    assert await _run(executor, notify_alert) == ec.STATUS_REQUESTED
    assert await _run(executor, notify_alert) == ec.STATUS_MANUAL_REQUIRED
    assert [params["clientOrderId"] for _side, _amount, params in exchange.sent] == [
        f"{ENTRY}c1",
        f"{ENTRY}c2",
        f"{ENTRY}c3",
    ]
    assert [close.status for close in store.closes] == ["failed"] * 3


async def test_resting_protective_stop_is_cancelled_before_flat() -> None:
    exchange = FakeExchange("0")
    store = MemoryStore(_task())
    stop = exchange.add_order(f"{ENTRY}s1", status="open", filled=0, stop=True)
    executor, notify_alert = _executor(exchange, store)

    assert await _run(executor, notify_alert) == ec.STATUS_REQUESTED
    assert exchange.cancelled == [stop["id"]]

    assert await _run(executor, notify_alert) == ec.STATUS_CONFIRMED_FLAT
    assert exchange.sent == []


async def test_triggered_stop_fill_counts_toward_the_remainder() -> None:
    exchange = FakeExchange("6")
    store = MemoryStore(_task())
    exchange.add_order(f"{ENTRY}s1", status="closed", filled=4, stop=True)
    executor, notify_alert = _executor(exchange, store)

    await _run(executor, notify_alert)

    assert exchange.sent[0][:2] == ("sell", 6.0)


async def test_opposite_side_position_sends_nothing() -> None:
    exchange = FakeExchange("10", side="short")
    store = MemoryStore(_task())
    executor, notify_alert = _executor(exchange, store)

    assert await _run(executor, notify_alert) == ec.STATUS_MANUAL_REQUIRED
    assert exchange.sent == []


async def test_claim_held_elsewhere_or_busy_lock_sends_nothing() -> None:
    exchange = FakeExchange("10")
    store = MemoryStore(_task())
    executor, notify_alert = _executor(exchange, store)

    store.claim_owner = "executor-b"
    assert await _run(executor, notify_alert) == "claimed_elsewhere"

    store.claim_owner = None
    executor.rdb.set = AsyncMock(return_value=False)
    assert await _run(executor, notify_alert) == "lock_busy"
    assert exchange.sent == []
    assert store.claim_owner is None


async def test_inline_pass_uses_the_callers_lock() -> None:
    exchange = FakeExchange("10")
    store = MemoryStore(_task())
    executor, notify_alert = _executor(exchange, store)
    executor.rdb.set = AsyncMock(return_value=False)

    with patch("schurfer_execution.emergency_close.notify.notify_alert", notify_alert):
        assert await executor.run_pass(1, held_lease=MagicMock()) == ec.STATUS_SENDING
    assert len(exchange.sent) == 1


async def test_worker_reports_a_failed_pass() -> None:
    store = MemoryStore(_task())
    executor = MagicMock()
    executor.run_pass = AsyncMock(side_effect=RuntimeError("db down"))
    worker = ec.EmergencyCloseWorker(executor, store)

    assert not await worker.run_once()


async def test_a_finished_pass_releases_its_claim_but_keeps_it_across_the_send() -> None:
    exchange = FakeExchange("10")
    store = MemoryStore(_task())
    executor, notify_alert = _executor(exchange, store)

    def created(amount: float, params: dict[str, Any]) -> dict[str, Any]:
        assert store.claim_owner == "executor-a"
        return dict(exchange.add_order(params["clientOrderId"], status="open", filled=0))

    exchange.on_create = created
    await _run(executor, notify_alert)

    assert store.claim_owner is None


async def test_create_response_without_a_fill_is_followed_not_trusted() -> None:
    exchange = FakeExchange("10")
    store = MemoryStore(_task())
    executor, notify_alert = _executor(exchange, store)

    def unindexed(amount: float, params: dict[str, Any]) -> dict[str, Any]:
        exchange.add_order(params["clientOrderId"], status="closed", filled=amount)
        exchange.set_position("0")
        return {"id": "o1", "clientOrderId": params["clientOrderId"], "status": "closed"}

    exchange.on_create = unindexed

    assert await _run(executor, notify_alert) == ec.STATUS_SENDING
    assert store.closes[0].status == "accepted"
    assert await _run(executor, notify_alert) == ec.STATUS_CONFIRMED_FLAT
    assert store.closes[0].filled_amount == Decimal(10)


async def test_triggered_stop_still_filling_waits_without_a_send() -> None:
    exchange = FakeExchange("7")
    store = MemoryStore(_task())
    exchange.add_order(f"{ENTRY}s1", status="open", filled=3, stop=True)
    executor, notify_alert = _executor(exchange, store)

    assert await _run(executor, notify_alert) == ec.STATUS_REQUESTED
    assert exchange.sent == []
    assert exchange.cancelled == []


# Postgres store and migration 0061.

TEST_DATABASE_URL = integration_database_url()


async def _connect_or_skip() -> psycopg.AsyncConnection:
    assert_active_test_database_url(TEST_DATABASE_URL)
    try:
        connection = await psycopg.AsyncConnection.connect(TEST_DATABASE_URL, autocommit=True)
        async with connection.cursor() as cursor:
            await cursor.execute("SELECT to_regclass('app.live_emergency_closes')")
            row = await cursor.fetchone()
        if row is None or row[0] is None:
            await connection.close()
            pytest.skip("live_emergency_closes is not migrated")
        return connection
    except Exception as exc:
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise RuntimeError(
                f"REQUIRE_INTEGRATION_DB=1 but PostgreSQL/head is unavailable: {exc}"
            ) from exc
        pytest.skip(f"no local postgres/head reachable: {exc}")


async def _entry_attempt(connection: psycopg.AsyncConnection, exchange: str) -> tuple[int, str]:
    entry = uuid.uuid4().hex
    async with connection.cursor() as cursor:
        await cursor.execute(
            "INSERT INTO app.live_order_attempts ("
            "client_order_id, exchange, base, symbol, native_market_id, market_type, side, "
            "size_usd, requested_amount, leverage, exit_params, setup_context, status"
            ") VALUES (%s, %s, 'BTC', %s, 'BTCUSDT', 'swap', 'long', 1000, 10, 1, "
            "'{}'::jsonb, '{}'::jsonb, 'completed') RETURNING id",
            (entry, exchange, SYMBOL),
        )
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0]), entry


def _spec(exchange: str, attempt_id: int, entry: str) -> ec.TaskSpec:
    return ec.TaskSpec(
        source_attempt_id=attempt_id,
        entry_client_order_id=entry,
        exchange=exchange,
        base="btc",
        symbol=SYMBOL,
        native_market_id="BTCUSDT",
        market_type="swap",
        side="long",
        owned_quantity=Decimal(10),
        reason=ec.REASON_STOP_PLACEMENT_FAILED,
    )


async def _cleanup(connection: psycopg.AsyncConnection, exchange: str) -> None:
    async with connection.cursor() as cursor:
        await cursor.execute(
            "DELETE FROM app.live_order_attempts WHERE exchange = %s AND task_id IS NOT NULL",
            (exchange,),
        )
        await cursor.execute(
            "DELETE FROM app.live_emergency_closes WHERE exchange = %s", (exchange,)
        )
        await cursor.execute("DELETE FROM app.live_order_attempts WHERE exchange = %s", (exchange,))
        await cursor.execute(
            "DELETE FROM app.live_reconciliation_incidents WHERE exchange = %s", (exchange,)
        )
        await connection.close()


async def test_task_is_unique_per_attempt_and_identity_and_blocks_entries() -> None:
    connection = await _connect_or_skip()
    exchange = f"itest-{uuid.uuid4().hex[:12]}"
    store = ec.PostgresEmergencyCloseStore(TEST_DATABASE_URL)
    try:
        attempt_id, entry = await _entry_attempt(connection, exchange)
        task_id, created = await store.create_task(_spec(exchange, attempt_id, entry))
        assert created
        assert await store.create_task(_spec(exchange, attempt_id, entry)) == (task_id, False)

        other_attempt, other_entry = await _entry_attempt(connection, exchange)
        with pytest.raises(ec.TaskConflictError):
            await store.create_task(_spec(exchange, other_attempt, other_entry))

        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT status, discrepancy_type FROM app.live_reconciliation_incidents "
                "WHERE incident_key = %s",
                (ec.incident_key(task_id),),
            )
            assert await cursor.fetchone() == ("manual_required", ec.INCIDENT_DISCREPANCY)
        assert await ec.has_active_task(TEST_DATABASE_URL, exchange=exchange, base="BTC")
    finally:
        await _cleanup(connection, exchange)


async def test_claim_excludes_a_second_owner_until_it_expires() -> None:
    connection = await _connect_or_skip()
    exchange = f"itest-{uuid.uuid4().hex[:12]}"
    store = ec.PostgresEmergencyCloseStore(TEST_DATABASE_URL)
    try:
        attempt_id, entry = await _entry_attempt(connection, exchange)
        task_id, _ = await store.create_task(_spec(exchange, attempt_id, entry))

        assert await store.claim(task_id, "owner-a", 60) is not None
        assert await store.claim(task_id, "owner-b", 60) is None
        assert await store.claim(task_id, "owner-a", 60) is not None

        async with connection.cursor() as cursor:
            await cursor.execute(
                "UPDATE app.live_emergency_closes "
                "SET claim_expires_at = now() - interval '1 second' WHERE id = %s",
                (task_id,),
            )
        assert await store.claim(task_id, "owner-b", 60) is not None
    finally:
        await _cleanup(connection, exchange)


async def test_task_link_is_allowed_only_on_close_rows() -> None:
    connection = await _connect_or_skip()
    exchange = f"itest-{uuid.uuid4().hex[:12]}"
    store = ec.PostgresEmergencyCloseStore(TEST_DATABASE_URL)
    try:
        attempt_id, entry = await _entry_attempt(connection, exchange)
        task_id, _ = await store.create_task(_spec(exchange, attempt_id, entry))
        async with connection.cursor() as cursor:
            with pytest.raises(psycopg.errors.CheckViolation):
                await cursor.execute(
                    "UPDATE app.live_order_attempts SET task_id = %s WHERE id = %s",
                    (task_id, attempt_id),
                )
    finally:
        await _cleanup(connection, exchange)


async def test_executor_flattens_through_linked_close_rows() -> None:
    connection = await _connect_or_skip()
    exchange_name = f"itest-{uuid.uuid4().hex[:12]}"
    store = ec.PostgresEmergencyCloseStore(TEST_DATABASE_URL)
    exchange = FakeExchange("10")
    executor = ec.EmergencyCloseExecutor({exchange_name: exchange}, store, _rdb())
    try:
        attempt_id, entry = await _entry_attempt(connection, exchange_name)
        task_id, _ = await store.create_task(_spec(exchange_name, attempt_id, entry))

        with patch("schurfer_execution.emergency_close.notify.notify_alert", AsyncMock()):
            assert await executor.run_pass(task_id) == ec.STATUS_SENDING
            assert await executor.run_pass(task_id) == ec.STATUS_CONFIRMED_FLAT

        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT operation, client_order_id, status, filled_amount "
                "FROM app.live_order_attempts WHERE task_id = %s",
                (task_id,),
            )
            assert await cursor.fetchall() == [("close", f"{entry}c1", "completed", Decimal(10))]
            await cursor.execute(
                "SELECT status, owned_remainder, confirmed_flat_at IS NOT NULL "
                "FROM app.live_emergency_closes WHERE id = %s",
                (task_id,),
            )
            assert await cursor.fetchone() == ("confirmed_flat", Decimal(0), True)
        assert task_id not in await store.runnable_task_ids()
        assert not await ec.has_active_task(TEST_DATABASE_URL, exchange=exchange_name, base="BTC")
    finally:
        await _cleanup(connection, exchange_name)


async def test_monitor_close_recovery_skips_task_close_rows() -> None:
    connection = await _connect_or_skip()
    exchange_name = f"itest-{uuid.uuid4().hex[:12]}"
    store = ec.PostgresEmergencyCloseStore(TEST_DATABASE_URL)
    exchange = FakeExchange("10")
    exchange.on_create = lambda amount, params: dict(
        exchange.add_order(params["clientOrderId"], status="open", filled=0)
    )
    executor = ec.EmergencyCloseExecutor({exchange_name: exchange}, store, _rdb())
    try:
        attempt_id, entry = await _entry_attempt(connection, exchange_name)
        task_id, _ = await store.create_task(_spec(exchange_name, attempt_id, entry))
        with patch("schurfer_execution.emergency_close.notify.notify_alert", AsyncMock()):
            assert await executor.run_pass(task_id) == ec.STATUS_SENDING

        recoverable = await order_attempts.load_recoverable_close_attempts(TEST_DATABASE_URL)
        assert [a for a in recoverable if a.exchange == exchange_name] == []
        # The pass ended, so another executor may run the next one at once.
        assert await store.claim(task_id, "owner-b", 60) is not None
    finally:
        await _cleanup(connection, exchange_name)
