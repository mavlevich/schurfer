"""Accounting of a confirmed-flat emergency close task (PR 3b of
docs/engineering/live-entry-failure-paths-design-v1.md): on a fake exchange and an
in-memory store, then the whole round trip on Postgres.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg
import pytest
from schurfer_execution import emergency_accounting as ea
from schurfer_execution import emergency_close as ec
from schurfer_execution import journal
from schurfer_journal.testing_database import (
    assert_active_test_database_url,
    integration_database_url,
)

ENTRY = "fedcba9876543210fedcba9876543210"
SYMBOL = "BTC/USDT:USDT"
T0 = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


def _ms(minutes: float) -> int:
    return int((T0 + timedelta(minutes=minutes)).timestamp() * 1000)


class FakeExchange:
    """Orders keyed by id, with fill prices and times; a close fills at 95."""

    def __init__(self, position: str = "0") -> None:
        self.has = {"fetchOpenOrders": True, "fetchClosedOrders": True}
        self.markets = {SYMBOL: {"contractSize": 1}}
        self.contracts = float(position)
        self.orders: dict[str, dict[str, Any]] = {}

    def add_order(
        self,
        client_order_id: str,
        *,
        filled: float,
        average: float | None,
        minute: float | None,
        amount: float | None = None,
        status: str = "closed",
        stop: bool = False,
    ) -> dict[str, Any]:
        order_id = f"x{len(self.orders) + 1}"
        self.orders[order_id] = {
            "id": order_id,
            "clientOrderId": client_order_id,
            "status": status,
            "amount": amount if amount is not None else filled,
            "filled": filled,
            "average": average,
            "lastTradeTimestamp": _ms(minute) if minute is not None else None,
            "stop": stop,
        }
        return self.orders[order_id]

    async def fetch_positions(self, symbols: list[str]) -> list[dict[str, Any]]:
        return [{"symbol": SYMBOL, "contracts": self.contracts, "side": "long", "markPrice": 95.0}]

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

    async def fetch_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        return dict(self.orders[order_id])

    async def fetch_my_trades(self, symbol: str, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        return f"{amount:.8f}".rstrip("0").rstrip(".")

    async def create_market_order(
        self, symbol: str, side: str, amount: float, params: dict[str, Any]
    ) -> dict[str, Any]:
        order = self.add_order(params["clientOrderId"], filled=amount, average=95.0, minute=1)
        self.contracts = max(0.0, self.contracts - amount)
        return dict(order)


def _task(**changes: Any) -> ec.EmergencyCloseTask:
    task = ec.EmergencyCloseTask(
        id=1,
        source_attempt_id=7,
        entry_client_order_id=ENTRY,
        exchange="bybit",
        base="BTC",
        symbol=SYMBOL,
        native_market_id="BTCUSDT",
        market_type="swap",
        side="long",
        trade_id=None,
        owned_quantity=Decimal(10),
        owned_remainder=Decimal(0),
        reason=ec.REASON_STOP_PLACEMENT_FAILED,
        status=ec.STATUS_CONFIRMED_FLAT,
        rejected_sends=0,
        confirmed_flat_at=T0,
    )
    return replace(task, **changes)


def _entry(trade_id: int | None = None) -> ea.EntryAttempt:
    return ea.EntryAttempt(
        trade_id=trade_id,
        requested_amount=Decimal(10),
        leverage=1,
        contract_size=Decimal(1),
        setup_context={"strategy": "pump_short"},
        created_at=T0 - timedelta(minutes=1),
    )


def _close(attempt_id: int, client_order_id: str, filled: str) -> ec.CloseOrder:
    return ec.CloseOrder(
        attempt_id, client_order_id, "completed", None, Decimal(10), Decimal(filled), T0
    )


class MemoryStore:
    """Publishes a round trip whole or not at all, as the Postgres store does."""

    def __init__(
        self,
        task: ec.EmergencyCloseTask,
        closes: list[ec.CloseOrder],
        entry: ea.EntryAttempt | None = None,
    ) -> None:
        self.task = task
        self.closes = closes
        self.entry = entry or _entry()
        self.claim_owner: str | None = None
        self.last_error: str | None = None
        self.published: list[ea.RoundTrip] = []
        self.publish_failures: list[Exception] = []

    async def accountable_task_ids(self) -> list[int]:
        return [self.task.id] if self.task.status == ec.STATUS_CONFIRMED_FLAT else []

    async def claim_flat(
        self, task_id: int, owner: str, lease_seconds: float
    ) -> ec.EmergencyCloseTask | None:
        if self.task.status != ec.STATUS_CONFIRMED_FLAT:
            return None
        if self.claim_owner not in (None, owner):
            return None
        self.claim_owner = owner
        return self.task

    async def load_entry(self, attempt_id: int) -> ea.EntryAttempt:
        return self.entry

    async def load_closes(self, task_id: int) -> list[ec.CloseOrder]:
        return list(self.closes)

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
        self.task = replace(task, status=status)
        self.last_error = last_error
        if release:
            self.claim_owner = None

    async def publish(
        self, task: ec.EmergencyCloseTask, owner: str, round_trip: ea.RoundTrip
    ) -> int:
        assert owner == self.claim_owner
        if self.publish_failures:
            raise self.publish_failures.pop(0)
        self.published.append(round_trip)
        trade_id = round_trip.trade_id or 100
        self.task = replace(task, status=ec.STATUS_ACCOUNTED, trade_id=trade_id)
        self.claim_owner = None
        return trade_id


def _rdb() -> MagicMock:
    rdb = MagicMock()
    rdb.set = AsyncMock(return_value=True)
    rdb.eval = AsyncMock(return_value=1)
    rdb.delete = AsyncMock(return_value=1)
    return rdb


def _accountant(
    exchange: FakeExchange, store: Any, *, now: list[datetime] | None = None
) -> ea.EmergencyCloseAccountant:
    clock = now if now is not None else [T0 + timedelta(minutes=1)]
    return ea.EmergencyCloseAccountant(
        {"bybit": exchange},
        store,
        _rdb(),
        alert_credentials=("token", "chat"),
        owner="accountant-a",
        now=lambda: clock[0],
    )


async def _account(accountant: ea.EmergencyCloseAccountant, task_id: int = 1) -> str:
    notify_alert = AsyncMock(return_value=True)
    with patch("schurfer_execution.emergency_accounting.notify.notify_alert", notify_alert):
        return await accountant.account(task_id)


async def test_one_close_publishes_the_open_the_leg_and_the_close() -> None:
    exchange = FakeExchange()
    entry = exchange.add_order(ENTRY, filled=10, average=100.0, minute=-1)
    close = exchange.add_order(f"{ENTRY}c1", filled=10, average=95.0, minute=1)
    store = MemoryStore(_task(), [_close(1, f"{ENTRY}c1", "10")])
    accountant = _accountant(exchange, store)

    assert await _account(accountant) == ec.STATUS_ACCOUNTED

    (round_trip,) = store.published
    assert round_trip.trade_id is None and round_trip.opening is not None
    assert round_trip.opening.order_id == entry["id"]
    assert round_trip.opening.size_usd == 1000.0
    assert round_trip.opening.entry_price == 100.0
    assert round_trip.opening.entry_at == T0 - timedelta(minutes=1)
    assert round_trip.opening.setup_context["emergency_close_task_id"] == 1
    assert round_trip.opening.setup_context["strategy"] == "pump_short"
    (leg,) = round_trip.legs
    assert (leg.order_id, leg.price, leg.filled_amount) == (close["id"], 95.0, Decimal(10))
    assert (leg.remaining_amount, leg.terminal) == (Decimal(0), True)
    assert round_trip.reason == "emergency_close_stop_placement_failed"
    assert (store.task.status, store.task.trade_id, store.claim_owner) == (
        ec.STATUS_ACCOUNTED,
        100,
        None,
    )
    accountant.rdb.delete.assert_awaited_with("position:opened_at:bybit:BTC")


async def test_legs_are_in_execution_order_and_only_the_last_is_terminal() -> None:
    exchange = FakeExchange()
    exchange.add_order(ENTRY, filled=10, average=100.0, minute=-1)
    c2 = exchange.add_order(f"{ENTRY}c2", filled=4, average=95.0, minute=3)
    c1 = exchange.add_order(f"{ENTRY}c1", filled=4, amount=10, average=96.0, minute=1)
    s1 = exchange.add_order(f"{ENTRY}s1", filled=2, amount=10, average=94.0, minute=2, stop=True)
    exchange.add_order(f"{ENTRY}s2", filled=0, average=None, minute=2, status="canceled", stop=True)
    store = MemoryStore(_task(), [_close(1, f"{ENTRY}c1", "4"), _close(2, f"{ENTRY}c2", "4")])

    assert await _account(_accountant(exchange, store)) == ec.STATUS_ACCOUNTED

    (round_trip,) = store.published
    assert [leg.order_id for leg in round_trip.legs] == [c1["id"], s1["id"], c2["id"]]
    assert [leg.remaining_amount for leg in round_trip.legs] == [6, 4, 0]
    assert [leg.terminal for leg in round_trip.legs] == [False, False, True]
    assert round_trip.legs[0].requested_amount == 10


async def test_legs_short_of_the_owned_quantity_need_an_operator() -> None:
    exchange = FakeExchange()
    exchange.add_order(ENTRY, filled=10, average=100.0, minute=-1)
    exchange.add_order(f"{ENTRY}c1", filled=6, average=95.0, minute=1)
    store = MemoryStore(_task(), [_close(1, f"{ENTRY}c1", "6")])

    assert await _account(_accountant(exchange, store)) == ec.STATUS_MANUAL_REQUIRED

    assert store.published == []
    assert store.last_error is not None and "filled 6 of the owned 10" in store.last_error
    assert store.claim_owner is None


async def test_a_fill_amount_the_exchange_omits_is_unknown_not_zero() -> None:
    exchange = FakeExchange()
    exchange.add_order(ENTRY, filled=10, average=100.0, minute=-1)
    close = exchange.add_order(f"{ENTRY}c1", filled=10, average=95.0, minute=1)
    close["filled"] = None
    store = MemoryStore(_task(), [_close(1, f"{ENTRY}c1", "10")])
    accountant = _accountant(exchange, store)

    assert await _account(accountant) == ec.STATUS_CONFIRMED_FLAT
    assert store.published == []
    assert store.last_error is not None and "unknown" in store.last_error

    close["filled"] = 10
    assert await _account(accountant) == ec.STATUS_ACCOUNTED


async def test_a_stop_still_filling_waits() -> None:
    exchange = FakeExchange()
    exchange.add_order(ENTRY, filled=10, average=100.0, minute=-1)
    exchange.add_order(f"{ENTRY}c1", filled=6, average=95.0, minute=1)
    exchange.add_order(f"{ENTRY}s1", filled=4, average=94.0, minute=2, status="open", stop=True)
    store = MemoryStore(_task(), [_close(1, f"{ENTRY}c1", "6")])

    assert await _account(_accountant(exchange, store)) == ec.STATUS_CONFIRMED_FLAT
    assert store.published == []
    assert "not final" in (store.last_error or "")


async def test_an_unresolved_price_waits_then_needs_an_operator() -> None:
    exchange = FakeExchange()
    exchange.add_order(ENTRY, filled=10, average=100.0, minute=-1)
    exchange.add_order(f"{ENTRY}c1", filled=10, average=None, minute=1)
    store = MemoryStore(_task(), [_close(1, f"{ENTRY}c1", "10")])
    clock = [T0 + timedelta(minutes=1)]
    accountant = _accountant(exchange, store, now=clock)

    assert await _account(accountant) == ec.STATUS_CONFIRMED_FLAT
    assert store.published == []
    assert store.claim_owner is None
    assert store.last_error is not None and "unresolved" in store.last_error

    clock[0] = T0 + timedelta(minutes=11)
    assert await _account(accountant) == ec.STATUS_MANUAL_REQUIRED
    assert store.published == []
    assert "deadline" in (store.last_error or "")


async def test_a_failed_listing_waits_then_needs_an_operator() -> None:
    exchange = FakeExchange()
    exchange.fetch_closed_orders = AsyncMock(side_effect=RuntimeError("bybit 10002"))  # type: ignore[method-assign]
    store = MemoryStore(_task(), [_close(1, f"{ENTRY}c1", "10")])
    clock = [T0 + timedelta(minutes=1)]
    accountant = _accountant(exchange, store, now=clock)

    assert await _account(accountant) == ec.STATUS_CONFIRMED_FLAT
    assert "bybit 10002" in (store.last_error or "")

    clock[0] = T0 + timedelta(minutes=11)
    assert await _account(accountant) == ec.STATUS_MANUAL_REQUIRED
    assert "deadline" in (store.last_error or "")


async def test_a_failed_publish_wrote_nothing_and_is_retried() -> None:
    exchange = FakeExchange()
    exchange.add_order(ENTRY, filled=10, average=100.0, minute=-1)
    exchange.add_order(f"{ENTRY}c1", filled=10, average=95.0, minute=1)
    store = MemoryStore(_task(), [_close(1, f"{ENTRY}c1", "10")])
    store.publish_failures = [ea.JournalRefusedError("the journal did not close trade 100")]
    accountant = _accountant(exchange, store)

    assert await _account(accountant) == ec.STATUS_CONFIRMED_FLAT
    assert store.published == [] and store.claim_owner is None
    assert "did not close" in (store.last_error or "")
    assert await _account(accountant) == ec.STATUS_ACCOUNTED
    assert len(store.published) == 1


async def test_an_already_journalled_trade_is_closed_not_opened_again() -> None:
    exchange = FakeExchange()
    exchange.add_order(ENTRY, filled=10, average=100.0, minute=-1)
    exchange.add_order(f"{ENTRY}c1", filled=10, average=95.0, minute=1)
    store = MemoryStore(_task(), [_close(1, f"{ENTRY}c1", "10")], _entry(trade_id=42))

    assert await _account(_accountant(exchange, store)) == ec.STATUS_ACCOUNTED

    (round_trip,) = store.published
    assert (round_trip.trade_id, round_trip.opening) == (42, None)
    assert store.task.trade_id == 42


async def test_entry_not_yet_terminal_waits() -> None:
    exchange = FakeExchange()
    exchange.add_order(ENTRY, filled=10, average=100.0, minute=-1, status="open")
    exchange.add_order(f"{ENTRY}c1", filled=10, average=95.0, minute=1)
    store = MemoryStore(_task(), [_close(1, f"{ENTRY}c1", "10")])

    assert await _account(_accountant(exchange, store)) == ec.STATUS_CONFIRMED_FLAT
    assert store.published == []


async def test_a_claim_held_elsewhere_writes_nothing() -> None:
    exchange = FakeExchange()
    store = MemoryStore(_task(), [])
    store.claim_owner = "accountant-b"

    assert await _account(_accountant(exchange, store)) == "claimed_elsewhere"
    assert store.published == []


async def test_the_accounting_loop_reports_each_run_to_its_own_tracker() -> None:
    store = MagicMock()
    store.accountable_task_ids = AsyncMock(side_effect=[[], RuntimeError("db down")])
    accountant = ea.EmergencyCloseAccountant({}, store, _rdb(), interval_seconds=0)
    tracker = MagicMock()
    sleeps = AsyncMock(side_effect=[None, asyncio.CancelledError()])

    with (
        patch("schurfer_execution.emergency_accounting.asyncio.sleep", sleeps),
        pytest.raises(asyncio.CancelledError),
    ):
        await accountant(tracker)

    assert tracker.tick_succeeded.call_count == 1
    assert tracker.tick_failed.call_count == 1


# The whole round trip on Postgres.

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


async def _flat_task(
    connection: psycopg.AsyncConnection, exchange_name: str, exchange: FakeExchange
) -> tuple[int, int, str]:
    """An entry attempt, its task, and executor passes to confirmed_flat."""
    entry = uuid.uuid4().hex
    async with connection.cursor() as cursor:
        await cursor.execute(
            "INSERT INTO app.live_order_attempts ("
            "client_order_id, exchange, base, symbol, native_market_id, market_type, side, "
            "size_usd, requested_amount, leverage, contract_size, exit_params, setup_context, "
            "status, filled_amount"
            ") VALUES (%s, %s, 'BTC', %s, 'BTCUSDT', 'swap', 'long', 1000, 10, 1, 1, "
            "'{}'::jsonb, '{}'::jsonb, 'completed', 10) RETURNING id",
            (entry, exchange_name, SYMBOL),
        )
        row = await cursor.fetchone()
    assert row is not None
    attempt_id = int(row[0])
    exchange.add_order(entry, filled=10, average=100.0, minute=-1)
    store = ec.PostgresEmergencyCloseStore(TEST_DATABASE_URL)
    task_id, _ = await store.create_task(
        ec.TaskSpec(
            source_attempt_id=attempt_id,
            entry_client_order_id=entry,
            exchange=exchange_name,
            base="btc",
            symbol=SYMBOL,
            native_market_id="BTCUSDT",
            market_type="swap",
            side="long",
            owned_quantity=Decimal(10),
            reason=ec.REASON_STOP_PLACEMENT_FAILED,
        )
    )
    executor = ec.EmergencyCloseExecutor({exchange_name: exchange}, store, _rdb())
    with patch("schurfer_execution.emergency_close.notify.notify_alert", AsyncMock()):
        assert await executor.run_pass(task_id) == ec.STATUS_SENDING
        assert await executor.run_pass(task_id) == ec.STATUS_CONFIRMED_FLAT
    return task_id, attempt_id, entry


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
        await cursor.execute("DELETE FROM app.trades WHERE exchange = %s", (exchange,))
        await cursor.execute(
            "DELETE FROM app.live_reconciliation_incidents WHERE exchange = %s", (exchange,)
        )
        await connection.close()


async def test_round_trip_is_journalled_and_linked_on_postgres() -> None:
    connection = await _connect_or_skip()
    exchange_name = f"itest-{uuid.uuid4().hex[:12]}"
    exchange = FakeExchange("10")
    store = ea.PostgresAccountingStore(TEST_DATABASE_URL)
    try:
        task_id, attempt_id, _entry_id = await _flat_task(connection, exchange_name, exchange)
        assert task_id in await store.accountable_task_ids()
        accountant = ea.EmergencyCloseAccountant({exchange_name: exchange}, store, _rdb())

        assert await accountant.account(task_id) == ec.STATUS_ACCOUNTED

        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT id, status, entry_price, exit_price, size_usd, notes "
                "FROM app.trades WHERE exchange = %s",
                (exchange_name,),
            )
            (
                (trade_id, status, entry_price, exit_price, size_usd, notes),
            ) = await cursor.fetchall()
            assert (status, entry_price, exit_price, size_usd) == (
                "closed",
                Decimal(100),
                Decimal(95),
                Decimal(1000),
            )
            assert notes == "emergency_close_stop_placement_failed"
            await cursor.execute(
                "SELECT filled_amount, remaining_amount, terminal FROM app.trade_close_fills "
                "WHERE trade_id = %s",
                (trade_id,),
            )
            assert await cursor.fetchall() == [(Decimal(10), Decimal(0), True)]
            await cursor.execute(
                "SELECT status, trade_id, accounted_at IS NOT NULL, claim_owner "
                "FROM app.live_emergency_closes WHERE id = %s",
                (task_id,),
            )
            assert await cursor.fetchone() == ("accounted", trade_id, True, None)
            await cursor.execute(
                "SELECT operation, trade_id FROM app.live_order_attempts "
                "WHERE id = %s OR task_id = %s ORDER BY id",
                (attempt_id, task_id),
            )
            assert await cursor.fetchall() == [("entry", trade_id), ("close", trade_id)]
        assert task_id not in await store.accountable_task_ids()
    finally:
        await _cleanup(connection, exchange_name)


async def _accountable_task(
    connection: psycopg.AsyncConnection, exchange_name: str, closes: list[tuple[str, int]]
) -> tuple[int, int, str]:
    """An entry attempt and a confirmed-flat task whose closes are already completed."""
    entry = uuid.uuid4().hex
    async with connection.cursor() as cursor:
        await cursor.execute(
            "INSERT INTO app.live_order_attempts ("
            "client_order_id, exchange, base, symbol, native_market_id, market_type, side, "
            "size_usd, requested_amount, leverage, contract_size, exit_params, setup_context, "
            "status, filled_amount"
            ") VALUES (%s, %s, 'BTC', %s, 'BTCUSDT', 'swap', 'long', 1000, 10, 1, 1, "
            "'{}'::jsonb, '{}'::jsonb, 'completed', 10) RETURNING id",
            (entry, exchange_name, SYMBOL),
        )
        row = await cursor.fetchone()
    assert row is not None
    attempt_id = int(row[0])
    task_id, _ = await ec.PostgresEmergencyCloseStore(TEST_DATABASE_URL).create_task(
        ec.TaskSpec(
            source_attempt_id=attempt_id,
            entry_client_order_id=entry,
            exchange=exchange_name,
            base="btc",
            symbol=SYMBOL,
            native_market_id="BTCUSDT",
            market_type="swap",
            side="long",
            owned_quantity=Decimal(10),
            reason=ec.REASON_STOP_PLACEMENT_FAILED,
        )
    )
    async with connection.cursor() as cursor:
        for suffix, filled in closes:
            await cursor.execute(
                "INSERT INTO app.live_order_attempts ("
                "operation, client_order_id, exchange, base, symbol, native_market_id, "
                "market_type, side, size_usd, requested_amount, leverage, contract_size, "
                "exit_params, setup_context, status, filled_amount, task_id"
                ") VALUES ('close', %s, %s, 'BTC', %s, 'BTCUSDT', 'swap', 'sell', 0, %s, 1, 1, "
                "'{}'::jsonb, '{}'::jsonb, 'completed', %s, %s)",
                (f"{entry}{suffix}", exchange_name, SYMBOL, filled, filled, task_id),
            )
        await cursor.execute(
            "UPDATE app.live_emergency_closes SET status = 'confirmed_flat', "
            "owned_remainder = 0, confirmed_flat_at = now() WHERE id = %s",
            (task_id,),
        )
    return task_id, attempt_id, entry


async def test_a_failed_publish_leaves_nothing_and_a_reordered_retry_succeeds_on_postgres() -> None:
    """The colleague's case: a leg's time is missing, the close is refused, the time
    then appears and the legs reorder. Nothing was kept from the first attempt, so the
    retry has nothing to conflict with."""
    connection = await _connect_or_skip()
    exchange_name = f"itest-{uuid.uuid4().hex[:12]}"
    exchange = FakeExchange()
    store = ea.PostgresAccountingStore(TEST_DATABASE_URL)
    accountant = ea.EmergencyCloseAccountant({exchange_name: exchange}, store, _rdb())
    try:
        task_id, attempt_id, entry = await _accountable_task(connection, exchange_name, [("c1", 6)])
        exchange.add_order(entry, filled=10, average=100.0, minute=-1)
        c1 = exchange.add_order(f"{entry}c1", filled=6, average=96.0, minute=None)
        s1 = exchange.add_order(f"{entry}s1", filled=4, average=94.0, minute=2, stop=True)
        refused = AsyncMock(return_value=journal.CloseOutcome(committed=False))
        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT confirmed_flat_at FROM app.live_emergency_closes WHERE id = %s",
                (task_id,),
            )
            (confirmed_flat_at,) = await cursor.fetchone() or (None,)

        with patch("schurfer_execution.emergency_accounting.journal.close_trade_in", refused):
            assert await accountant.account(task_id) == ec.STATUS_CONFIRMED_FLAT

        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT count(*) FROM app.trades WHERE exchange = %s", (exchange_name,)
            )
            assert await cursor.fetchone() == (0,)
            await cursor.execute(
                "SELECT count(*) FROM app.trade_close_fills WHERE exchange = %s", (exchange_name,)
            )
            assert await cursor.fetchone() == (0,)
            await cursor.execute(
                "SELECT status, trade_id, claim_owner, confirmed_flat_at "
                "FROM app.live_emergency_closes WHERE id = %s",
                (task_id,),
            )
            # The deadline still counts from the first confirmation.
            assert await cursor.fetchone() == ("confirmed_flat", None, None, confirmed_flat_at)
            await cursor.execute(
                "SELECT trade_id FROM app.live_order_attempts WHERE id = %s", (attempt_id,)
            )
            assert await cursor.fetchone() == (None,)

        exchange.orders[c1["id"]]["lastTradeTimestamp"] = _ms(1)
        assert await accountant.account(task_id) == ec.STATUS_ACCOUNTED

        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT order_id, remaining_amount, terminal FROM app.trade_close_fills "
                "WHERE exchange = %s ORDER BY id",
                (exchange_name,),
            )
            assert await cursor.fetchall() == [
                (c1["id"], Decimal(4), False),
                (s1["id"], Decimal(0), True),
            ]
            await cursor.execute(
                "SELECT count(*), min(exit_price) FROM app.trades WHERE exchange = %s "
                "AND status = 'closed'",
                (exchange_name,),
            )
            assert await cursor.fetchone() == (1, Decimal("95.2"))
    finally:
        await _cleanup(connection, exchange_name)
