"""An entry fill found late by reconciliation (PR 4b of
docs/engineering/live-entry-failure-paths-design-v1.md, section 4): on a fake venue,
then the locked reads and the task on Postgres.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import replace
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg
import pytest
from schurfer_execution import emergency_accounting as ea
from schurfer_execution import emergency_close as ec
from schurfer_execution import entry_recovery, late_fill
from schurfer_execution import reconciliation as rec
from schurfer_execution.exchange_orders import attempt_state_from_order
from schurfer_execution.reconciliation_worker import ReconciliationWorker
from schurfer_journal.testing_database import (
    assert_active_test_database_url,
    integration_database_url,
)

ENTRY = "0123456789abcdef0123456789abcdef"
SYMBOL = "BEAT/USDT:USDT"


@pytest.fixture(autouse=True)
def _no_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entry_recovery, "CANCEL_CONFIRM_AT_SECONDS", (0.0, 0.0, 0.0, 0.0))


class Venue:
    """The entry order and the stops of a late entry. ``after_cancel`` is what the
    entry becomes once its rest is cancelled."""

    def __init__(
        self,
        entry: dict[str, Any] | None,
        *,
        after_cancel: dict[str, Any] | None = None,
        stops_unreadable: bool = False,
    ) -> None:
        self.has = {"fetchOpenOrders": True, "fetchClosedOrders": True}
        self.entry = (
            {"id": "entry-1", "clientOrderId": ENTRY, "amount": 100.0, **entry} if entry else None
        )
        self.after_cancel = after_cancel
        self.stops_unreadable = stops_unreadable
        self.stops: list[dict[str, Any]] = []
        self.closes: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.lookups = 0

    def add_stop(self, sequence: int, amount: float, **fields: Any) -> None:
        self.stops.append(
            {
                "id": f"stop-{sequence}",
                "clientOrderId": f"{ENTRY}s{sequence}",
                "status": "open",
                "filled": 0.0,
                "amount": amount,
                **fields,
            }
        )

    def add_close(self, sequence: int, amount: float, **fields: Any) -> None:
        """A ``<entry>cN`` close, as the database-down path sends one."""
        self.closes.append(
            {
                "id": f"close-{sequence}",
                "clientOrderId": f"{ENTRY}c{sequence}",
                "status": "closed",
                "filled": amount,
                "amount": amount,
                "average": 1.4,
                **fields,
            }
        )

    def price_to_precision(self, symbol: str, price: float) -> str:
        return f"{price:.6f}"

    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return {"last": 1.0}

    async def create_stop_market_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        trigger_price: float,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        sequence = int(params["clientOrderId"].removeprefix(f"{ENTRY}s"))
        self.add_stop(sequence, amount, side=side, triggerPrice=trigger_price)
        return dict(self.stops[-1])

    async def cancel_order(
        self, order_id: str, symbol: str, params: dict[str, Any] | None = None
    ) -> None:
        self.cancelled.append(order_id)
        if self.entry is not None and order_id == self.entry["id"] and self.after_cancel:
            self.entry.update(self.after_cancel)

    def _orders(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        if params.get("trigger"):
            if self.stops_unreadable:
                raise RuntimeError("listing failed")
            return self.stops
        self.lookups += 1
        return ([self.entry] if self.entry is not None else []) + self.closes

    async def fetch_open_orders(self, symbol: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return [dict(order) for order in self._orders(params) if order["status"] == "open"]

    async def fetch_closed_orders(
        self, symbol: str, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return [dict(order) for order in self._orders(params) if order["status"] != "open"]


def _rdb(*, lock_free: bool = True) -> MagicMock:
    rdb = MagicMock()
    rdb.set = AsyncMock(return_value=True if lock_free else None)
    rdb.eval = AsyncMock(return_value=1)
    rdb.delete = AsyncMock(return_value=1)
    return rdb


def _attempt(**changes: Any) -> rec.OrderAttemptSnapshot:
    attempt = rec.OrderAttemptSnapshot(
        identity=rec.FullIdentity(
            exchange="bybit",
            symbol=SYMBOL,
            native_market_id="BEATUSDT",
            market_type="swap",
            side="long",
        ),
        attempt_id=5,
        base="BEAT",
        client_order_id=ENTRY,
        order_id=None,
        status="submission_unknown",
        requested_amount=Decimal(100),
        filled_amount=None,
        trade_id=None,
    )
    return replace(attempt, **changes)


def _locked(**changes: Any) -> late_fill._Locked:
    locked = late_fill._Locked(
        status="submission_unknown", trade_id=None, initial_sl_pct=10.0, competitor=None
    )
    return replace(locked, **changes)


class Recorder:
    """Stands in for the worker's attempt update: records what an order shows."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Decimal | None]] = []

    async def __call__(
        self, attempt: rec.OrderAttemptSnapshot, order: dict[str, Any]
    ) -> rec.OrderAttemptSnapshot:
        status, filled, _error = attempt_state_from_order(order)
        self.calls.append((status, filled))
        return replace(attempt, status=status, filled_amount=filled)


async def _resolve(
    venue: Venue,
    *,
    locked: late_fill._Locked | None = None,
    lock_free: bool = True,
    create_task: AsyncMock | None = None,
    recovery: late_fill.LateFillRecovery | None = None,
    attempt: rec.OrderAttemptSnapshot | None = None,
) -> tuple[rec.OrderAttemptSnapshot | None, Recorder, AsyncMock, AsyncMock]:
    store = MagicMock()
    store.create_task = create_task or AsyncMock(return_value=(41, True))
    recovery = recovery or late_fill.LateFillRecovery(
        {"bybit": venue},
        "postgresql://x",
        _rdb(lock_free=lock_free),
        alert_credentials=("token", "chat"),
        store=store,
    )
    recovery.store = store
    record = Recorder()
    alert = AsyncMock(return_value=True)
    with (
        patch.object(
            late_fill.LateFillRecovery,
            "_read_locked",
            AsyncMock(return_value=locked or _locked()),
        ),
        patch("schurfer_execution.late_fill.notify.notify_alert", alert),
    ):
        result = await recovery.resolve(attempt or _attempt(), record)
    return result, record, store.create_task, alert


def test_only_an_unresolved_entry_with_a_derived_id_and_no_trade_is_a_candidate() -> None:
    assert late_fill.is_candidate(_attempt())
    assert late_fill.is_candidate(_attempt(status="accepted"))
    assert late_fill.is_candidate(_attempt(status="pending"))
    # An unresolved fill price or a failed journal write: protected, own recovery.
    assert not late_fill.is_candidate(_attempt(status="completed"))
    assert not late_fill.is_candidate(_attempt(status="manual_required"))
    assert not late_fill.is_candidate(_attempt(trade_id=3))
    assert not late_fill.is_candidate(_attempt(client_order_id="legacy-id"))
    incomplete = replace(_attempt().identity, native_market_id="")
    assert not late_fill.is_candidate(_attempt(identity=incomplete))


async def test_a_busy_lock_leaves_the_attempt_to_its_holder() -> None:
    venue = Venue({"status": "closed", "filled": 100.0, "average": 1.5})

    result, record, create_task, _alert = await _resolve(venue, lock_free=False)

    assert result is None and venue.lookups == 0
    assert record.calls == [] and not create_task.await_count


async def test_an_attempt_resolved_before_the_lock_is_left_alone() -> None:
    venue = Venue({"status": "closed", "filled": 100.0, "average": 1.5})

    result, record, create_task, _alert = await _resolve(
        venue, locked=_locked(status="completed", trade_id=9)
    )

    assert result is None and venue.lookups == 0
    assert record.calls == [] and not create_task.await_count


async def test_nothing_found_changes_nothing() -> None:
    result, record, create_task, _alert = await _resolve(Venue(None))

    assert result is None and record.calls == [] and not create_task.await_count


async def test_a_terminal_fill_is_flattened_through_a_task() -> None:
    venue = Venue({"status": "closed", "filled": 100.0, "average": 1.5})

    result, record, create_task, alert = await _resolve(venue)

    spec, adopted = create_task.await_args.args
    assert spec.reason == ec.REASON_LATE_FILL_FLATTENED and adopted == ()
    assert spec.owned_quantity == Decimal("100.0") and spec.trade_id is None
    assert (spec.source_attempt_id, spec.entry_client_order_id) == (5, ENTRY)
    assert record.calls == [("completed", Decimal("100.0"))]
    assert result is not None and result.status == "completed"
    assert "task 41 flattens it" in alert.await_args.kwargs["text"]
    assert venue.stops == [] and venue.cancelled == []


async def test_a_close_sent_while_the_database_was_down_is_adopted_by_the_task() -> None:
    """The entry path sent ``c1`` for 40 of the 100 without a database; the task
    owns the 100 with ``c1`` among its closes, so its remainder is 60."""
    venue = Venue({"status": "closed", "filled": 100.0, "average": 1.5})
    venue.add_close(1, 100.0, status="canceled", filled=40.0)

    _result, record, create_task, alert = await _resolve(venue)

    spec, adopted = create_task.await_args.args
    assert spec.owned_quantity == Decimal("100.0")
    assert [(close.client_order_id, close.order_id, close.filled_amount) for close in adopted] == [
        (f"{ENTRY}c1", "close-1", Decimal("40.0"))
    ]
    assert record.calls == [("completed", Decimal("100.0"))]
    assert "1 close order(s) already sent" in alert.await_args.kwargs["text"]


async def test_closes_that_cannot_be_listed_create_no_task() -> None:
    venue = Venue({"status": "closed", "filled": 100.0, "average": 1.5})

    with patch(
        "schurfer_execution.late_fill.exchange_orders.orders_with_prefix",
        AsyncMock(side_effect=RuntimeError("listing failed")),
    ):
        result, record, create_task, _alert = await _resolve(venue)

    assert result is None and record.calls == []
    assert not create_task.await_count


async def test_a_task_found_again_after_a_crash_is_not_alerted_twice() -> None:
    venue = Venue({"status": "closed", "filled": 100.0, "average": 1.5})

    _result, record, _create_task, alert = await _resolve(
        venue, create_task=AsyncMock(return_value=(41, False))
    )

    assert record.calls == [("completed", Decimal("100.0"))]
    assert not alert.await_count


async def test_a_competitor_on_the_instrument_leaves_it_to_an_operator() -> None:
    venue = Venue({"status": "closed", "filled": 100.0, "average": 1.5})

    result, record, create_task, _alert = await _resolve(
        venue, locked=_locked(competitor="an open trade holds the instrument")
    )

    assert not create_task.await_count
    # Recorded completed without a trade: the classifier quarantines it, manual.
    assert record.calls == [("completed", Decimal("100.0"))]
    assert result is not None and result.status == "completed"


async def test_a_terminal_entry_with_no_fill_is_no_position() -> None:
    venue = Venue({"status": "canceled", "filled": 0.0, "average": None})

    _result, record, create_task, _alert = await _resolve(venue)

    assert record.calls == [("no_fill", Decimal("0.0"))]
    assert not create_task.await_count


async def test_a_terminal_entry_without_a_filled_amount_needs_an_operator() -> None:
    venue = Venue({"status": "closed", "filled": None, "average": 1.5})

    _result, record, create_task, _alert = await _resolve(venue)

    assert record.calls == [("manual_required", None)]
    assert not create_task.await_count


async def test_an_active_entry_keeps_the_stop_the_entry_path_left_then_is_flattened() -> None:
    """The entry path left s1 on 40 and an unconfirmed cancel. Reconciliation takes
    s1 over, places no second stop for the same fill, and flattens once the
    entry is terminal."""
    venue = Venue(
        {"status": "open", "filled": 40.0, "average": 1.5}, after_cancel={"status": "canceled"}
    )
    venue.add_stop(1, 40.0)

    _result, record, create_task, _alert = await _resolve(venue, locked=_locked(status="accepted"))

    assert [stop["clientOrderId"][-2:] for stop in venue.stops] == ["s1"]
    assert venue.cancelled == ["entry-1"]
    assert create_task.await_args.args[0].owned_quantity == Decimal("40.0")
    assert record.calls == [("completed", Decimal("40.0"))]


async def test_an_active_entry_whose_stop_fired_gets_a_new_stop_for_what_is_held() -> None:
    venue = Venue({"status": "open", "filled": 40.0, "average": 1.5})
    venue.add_stop(1, 40.0, status="canceled", filled=20.0)

    _result, record, create_task, alert = await _resolve(venue, locked=_locked(status="accepted"))

    assert [(stop["clientOrderId"][-2:], stop["amount"]) for stop in venue.stops] == [
        ("s1", 40.0),
        ("s2", 20.0),
    ]
    assert not create_task.await_count
    assert record.calls == [("accepted", Decimal("40.0"))]
    assert (
        "20.0 of the 40.0 filled is still held, all of it covered"
        in (alert.await_args.kwargs["text"])
    )


async def test_stops_that_cannot_be_read_leave_an_active_entry_untouched() -> None:
    venue = Venue({"status": "open", "filled": 40.0, "average": 1.5}, stops_unreadable=True)

    result, record, create_task, _alert = await _resolve(venue)

    assert result is None and record.calls == []
    assert venue.cancelled == [] and venue.stops == []
    assert not create_task.await_count


async def test_an_unconfirmed_cancel_is_alerted_once_and_stays_unresolved() -> None:
    venue = Venue(
        {"status": "open", "filled": 40.0, "average": 1.5}, after_cancel={"filled": 100.0}
    )
    store = MagicMock()
    recovery = late_fill.LateFillRecovery(
        {"bybit": venue}, "postgresql://x", _rdb(), alert_credentials=("t", "c"), store=store
    )

    _r1, record1, _c1, alert1 = await _resolve(venue, recovery=recovery)
    _r2, record2, _c2, alert2 = await _resolve(venue, recovery=recovery)

    assert [stop["amount"] for stop in venue.stops] == [40.0, 60.0]
    assert record1.calls == record2.calls == [("accepted", Decimal("100.0"))]
    assert alert1.await_count == 1 and not alert2.await_count
    assert (
        "100.0 of the 100.0 filled is still held, all of it covered"
        in (alert1.await_args.kwargs["text"])
    )


async def test_a_failure_leaves_the_attempt_unresolved() -> None:
    venue = Venue({"status": "closed", "filled": 100.0, "average": 1.5})

    result, record, _create_task, _alert = await _resolve(
        venue, create_task=AsyncMock(side_effect=ec.TaskConflictError("held"))
    )

    assert result is None and record.calls == []


# --- Postgres -------------------------------------------------------------------

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


async def _insert_attempt(
    connection: psycopg.AsyncConnection,
    exchange_name: str,
    *,
    entry: str,
    status: str,
    operation: str = "entry",
    task_id: int | None = None,
) -> int:
    async with connection.cursor() as cursor:
        await cursor.execute(
            "INSERT INTO app.live_order_attempts ("
            "operation, client_order_id, exchange, base, symbol, native_market_id, "
            "market_type, side, size_usd, requested_amount, leverage, contract_size, "
            "exit_params, setup_context, status, task_id"
            ") VALUES (%s, %s, %s, 'BEAT', %s, 'BEATUSDT', 'swap', 'long', 100, 100, 1, 1, "
            "'{\"initial_sl_pct\": 7.5}'::jsonb, '{}'::jsonb, %s, %s) RETURNING id",
            (operation, entry, exchange_name, SYMBOL, status, task_id),
        )
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


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


def _postgres_attempt(exchange_name: str, attempt_id: int) -> rec.OrderAttemptSnapshot:
    return _attempt(
        identity=replace(_attempt().identity, exchange=exchange_name), attempt_id=attempt_id
    )


async def test_the_locked_read_names_whatever_else_holds_the_instrument_on_postgres() -> None:
    connection = await _connect_or_skip()
    exchange_name = f"itest-{uuid.uuid4().hex[:12]}"
    recovery = late_fill.LateFillRecovery({}, TEST_DATABASE_URL, _rdb())
    try:
        attempt_id = await _insert_attempt(
            connection, exchange_name, entry=ENTRY, status="submission_unknown"
        )
        attempt = _postgres_attempt(exchange_name, attempt_id)

        locked = await recovery._read_locked(attempt)
        assert locked == late_fill._Locked(
            status="submission_unknown", trade_id=None, initial_sl_pct=7.5, competitor=None
        )

        # The closes of this attempt's own task are not a competitor.
        task_id, _ = await ec.PostgresEmergencyCloseStore(TEST_DATABASE_URL).create_task(
            ec.TaskSpec(
                source_attempt_id=attempt_id,
                entry_client_order_id=ENTRY,
                exchange=exchange_name,
                base="BEAT",
                symbol=SYMBOL,
                native_market_id="BEATUSDT",
                market_type="swap",
                side="long",
                owned_quantity=Decimal(40),
                reason=ec.REASON_LATE_FILL_FLATTENED,
            )
        )
        await _insert_attempt(
            connection,
            exchange_name,
            entry=f"{ENTRY}c1",
            status="accepted",
            operation="close",
            task_id=task_id,
        )
        assert (await recovery._read_locked(attempt)).competitor is None  # type: ignore[union-attr]

        other = await _insert_attempt(
            connection, exchange_name, entry=uuid.uuid4().hex, status="pending"
        )
        assert (await recovery._read_locked(attempt)).competitor == (  # type: ignore[union-attr]
            "another unresolved attempt holds the instrument"
        )
        async with connection.cursor() as cursor:
            await cursor.execute(
                "UPDATE app.live_order_attempts SET status = 'failed' WHERE id = %s", (other,)
            )
            await cursor.execute(
                "INSERT INTO app.strategies (name, version, description) "
                "VALUES ('late_fill_itest', '1', 'late fill test') "
                "ON CONFLICT (name, version) DO UPDATE SET updated_at = now() RETURNING id"
            )
            strategy = await cursor.fetchone()
            assert strategy is not None
            await cursor.execute(
                "INSERT INTO app.trades (strategy_id, symbol, exchange, market_type, side, "
                "size_usd, leverage, entry_price, entry_at, status, accounting_version, "
                "accounting_status, setup_context) VALUES (%s, %s, %s, 'perp', 'short', 10, 1, "
                "1, now(), 'open', 'legacy_price_only_v1', 'legacy', '{}'::jsonb)",
                (strategy[0], SYMBOL, exchange_name),
            )
        assert (await recovery._read_locked(attempt)).competitor == (  # type: ignore[union-attr]
            "an open trade holds the instrument"
        )
    finally:
        await _cleanup(connection, exchange_name)


async def test_reconciliation_flattens_a_late_fill_once_on_postgres() -> None:
    """The worker's attempt resolution creates one task, records the attempt, and
    after a crash before the record finds the same task again."""
    connection = await _connect_or_skip()
    exchange_name = f"itest-{uuid.uuid4().hex[:12]}"
    venue = Venue({"status": "closed", "filled": 40.0, "average": 1.5})
    venue.markets = {SYMBOL: {"id": "BEATUSDT", "type": "swap"}}  # type: ignore[attr-defined]
    worker = ReconciliationWorker({exchange_name: venue}, TEST_DATABASE_URL, _rdb(), MagicMock())
    try:
        attempt_id = await _insert_attempt(
            connection, exchange_name, entry=ENTRY, status="submission_unknown"
        )
        attempt = _postgres_attempt(exchange_name, attempt_id)

        for _crash in range(2):
            attempts = {attempt.identity: [attempt]}
            await worker._resolve_attempts(attempts)
            assert attempts[attempt.identity][0].status == "completed"
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT status, filled_amount, trade_id FROM app.live_order_attempts "
                    "WHERE id = %s",
                    (attempt_id,),
                )
                assert await cursor.fetchone() == ("completed", Decimal(40), None)
                await cursor.execute(
                    "SELECT reason, owned_quantity, status FROM app.live_emergency_closes "
                    "WHERE source_attempt_id = %s",
                    (attempt_id,),
                )
                assert await cursor.fetchall() == [
                    (ec.REASON_LATE_FILL_FLATTENED, Decimal(40), ec.STATUS_REQUESTED)
                ]
                # As if the record were lost: the next scan finds the same task.
                await cursor.execute(
                    "UPDATE app.live_order_attempts SET status = 'accepted' WHERE id = %s",
                    (attempt_id,),
                )
    finally:
        await _cleanup(connection, exchange_name)


class _Position(Venue):
    """The venue once the database-down ``c1`` closed ``closed`` of the 100."""

    def __init__(self, closed: float) -> None:
        super().__init__({"status": "closed", "filled": 100.0, "average": 1.5})
        self.markets = {SYMBOL: {"id": "BEATUSDT", "type": "swap", "contractSize": 1}}
        self.add_close(1, 100.0, status="closed" if closed == 100 else "canceled", filled=closed)
        self.closed = closed
        self.sent: list[tuple[float, str]] = []

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        return f"{amount:.6f}"

    async def fetch_positions(self, symbols: list[str]) -> list[dict[str, Any]]:
        held = 100.0 - self.closed
        return [{"symbol": SYMBOL, "side": "long", "contracts": held}] if held else []

    async def create_market_order(
        self, symbol: str, side: str, amount: float, params: dict[str, Any]
    ) -> dict[str, Any]:
        self.sent.append((amount, params["clientOrderId"]))
        return {"id": "close-sent", "clientOrderId": params["clientOrderId"], "status": "open"}


@pytest.mark.parametrize("closed", [40.0, 100.0])
async def test_reconciliation_adopts_a_database_down_close_on_postgres(closed: float) -> None:
    """``c1`` closed 40 of the 100: the task closes the 60 left instead of finding
    60 against an owned 100. ``c1`` closed all of it: the task is flat with ``c1``
    as its close, so the round trip has a close leg to journal."""
    connection = await _connect_or_skip()
    exchange_name = f"itest-{uuid.uuid4().hex[:12]}"
    venue = _Position(closed)
    store = ec.PostgresEmergencyCloseStore(TEST_DATABASE_URL)
    worker = ReconciliationWorker({exchange_name: venue}, TEST_DATABASE_URL, _rdb(), MagicMock())
    try:
        attempt_id = await _insert_attempt(
            connection, exchange_name, entry=ENTRY, status="accepted"
        )
        attempt = _postgres_attempt(exchange_name, attempt_id)

        await worker._resolve_attempts({attempt.identity: [attempt]})

        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT id FROM app.live_emergency_closes WHERE source_attempt_id = %s",
                (attempt_id,),
            )
            row = await cursor.fetchone()
        assert row is not None
        task_id = int(row[0])
        closes = await store.load_closes(task_id)
        assert [(close.client_order_id, close.filled_amount) for close in closes] == [
            (f"{ENTRY}c1", Decimal(str(closed)))
        ]

        executor = ec.EmergencyCloseExecutor({exchange_name: venue}, store, _rdb())
        status = await executor.run_pass(task_id)

        if closed == 40.0:
            assert status != ec.STATUS_MANUAL_REQUIRED
            assert venue.sent == [(60.0, f"{ENTRY}c2")]
        else:
            assert status == ec.STATUS_CONFIRMED_FLAT and venue.sent == []
            accountant = ea.EmergencyCloseAccountant(
                {exchange_name: venue}, ea.PostgresAccountingStore(TEST_DATABASE_URL), _rdb()
            )
            assert await accountant.account(task_id) == ec.STATUS_ACCOUNTED
    finally:
        await _cleanup(connection, exchange_name)
