"""Real-Postgres tests of closing a LIVE_PROBE position: the L-reached transaction and
the recovery at every start (docs/engineering/live-probe-loss-threshold-design-v1.md,
sections 4 and 5)."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import ccxt
import psycopg
import pytest
from schurfer_execution import (
    emergency_accounting,
    emergency_close,
    journal,
    live_probe,
    monitor,
    order_ids,
    orders,
    probe_close,
)
from schurfer_execution.emergency_close import PostgresEmergencyCloseStore

from .test_emergency_close import FakeExchange
from .test_emergency_close import _rdb as _executor_rdb
from .test_live_probe_integration import (
    INPUTS,
    TEST_DATABASE_URL,
    _cleanup,
    _issue,
    _query,
    _trade,
)

D = Decimal
# Each test trades its own instrument: an active task holds its exact identity.
INSTRUMENT_PREFIX = "TLP"
SYMBOL = "TLP/USDT:USDT"
ORDER_LOCK = "lock:order:bybit:TLP"

__all__ = ["_cleanup"]


class LockRedis:
    """The order lock's Redis calls: SET NX, and the owner-checked renew and release."""

    def __init__(self) -> None:
        self.keys: dict[str, str] = {}

    async def set(self, key: str, token: str, *, nx: bool = False, px: int | None = None) -> Any:
        if nx and key in self.keys:
            return None
        self.keys[key] = token
        return True

    async def eval(self, script: str, _count: int, key: str, token: str, *_args: Any) -> int:
        if self.keys.get(key) != token:
            return 0
        if '"del"' in script:
            del self.keys[key]
        return 1

    async def get(self, key: str) -> str | None:
        return self.keys.get(key)

    async def delete(self, *keys: str) -> int:
        return sum(self.keys.pop(key, None) is not None for key in keys)


@pytest.fixture
def rdb() -> LockRedis:
    return LockRedis()


@pytest.fixture(autouse=True)
def _cleanup_tasks() -> Any:
    """Test-only cleanup of the tasks, incidents and close rows these tests create."""
    yield
    try:
        conn = psycopg.connect(TEST_DATABASE_URL, autocommit=True)
    except psycopg.OperationalError:
        return
    with conn, conn.transaction():
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute("DELETE FROM app.trade_close_fills WHERE order_id LIKE 'tlp-%%'")
        conn.execute(
            "DELETE FROM app.live_order_attempts WHERE native_market_id LIKE 'TLP%%' "
            "AND operation = 'close'"
        )
        conn.execute("DELETE FROM app.live_emergency_closes WHERE native_market_id LIKE 'TLP%%'")
        conn.execute(
            "DELETE FROM app.live_reconciliation_incidents WHERE native_market_id LIKE 'TLP%%'"
        )


def _instrument() -> str:
    return f"{INSTRUMENT_PREFIX}{uuid.uuid4().hex[:8].upper()}USDT"


async def _open_position(
    *,
    filled: str | None = "10",
    trade: bool = True,
    stage: str = "linked",
    status: str = "completed",
    **issue: Any,
) -> tuple[str, live_probe.Reservation, int, int | None]:
    """A probe with one reservation, cut at ``stage``: ``written`` (the entry attempt
    exists, not linked), ``sent`` (linked, still sending), ``placed`` (the trade on the
    attempt only) or ``linked`` (the whole chain). The attempt is ``status`` with
    ``filled``; with ``trade`` it carries an open journalled trade. Returns probe,
    reservation, attempt, trade."""
    instrument = _instrument()
    probe_id = await _issue(instruments=(instrument,), loss_threshold_usd=D(50), **issue)
    reservation = await live_probe.reserve(
        TEST_DATABASE_URL,
        live_probe.EntryRequest(
            probe_id=probe_id,
            decision_id=uuid.uuid4().hex,
            environment="testnet",
            account_key="probe-sub-1",
            native_market_id=instrument,
            side="long",
            reserve_inputs=INPUTS,
        ),
    )
    assert isinstance(reservation, live_probe.Reservation), reservation
    await live_probe.begin_sending(TEST_DATABASE_URL, reservation.id)
    trade_id = await _trade(status="open", pnl=None) if trade else None
    rows = await _query(
        "INSERT INTO app.live_order_attempts (operation, client_order_id, exchange, base, "
        "symbol, native_market_id, market_type, side, size_usd, requested_amount, "
        "filled_amount, leverage, exit_params, setup_context, status, trade_id) VALUES "
        "('entry', %s, 'bybit', 'TLP', 'TLP/USDT:USDT', %s, 'swap', 'long', 10, 10, %s, 1, "
        "'{}'::jsonb, '{\"live_probe_test\": true}'::jsonb, %s, %s) RETURNING id",
        (reservation.client_order_id, instrument, filled, status, trade_id),
    )
    attempt_id = int(rows[0]["id"])
    if stage == "written":
        return probe_id, reservation, attempt_id, trade_id
    await live_probe.link_attempt(TEST_DATABASE_URL, reservation.id, attempt_id)
    if stage == "sent":
        return probe_id, reservation, attempt_id, trade_id
    await live_probe.mark_placed(TEST_DATABASE_URL, reservation.id)
    if trade_id is not None and stage == "linked":
        await live_probe.link_trade(TEST_DATABASE_URL, reservation.id, trade_id)
    return probe_id, reservation, attempt_id, trade_id


async def _close_row(
    reservation: live_probe.Reservation, trade_id: int | None, *, status: str, filled: str | None
) -> int:
    """A close order of the trade sent before any task (a monitor exit)."""
    rows = await _query(
        "INSERT INTO app.live_order_attempts (operation, client_order_id, exchange, base, "
        "symbol, native_market_id, market_type, side, size_usd, requested_amount, "
        "filled_amount, leverage, exit_params, setup_context, status, trade_id) VALUES "
        "('close', %s, 'bybit', 'TLP', 'TLP/USDT:USDT', %s, 'swap', 'long', 3, 3, %s, 1, "
        "'{}'::jsonb, '{\"live_probe_test\": true}'::jsonb, %s, %s) RETURNING id",
        (str(uuid.uuid4()), reservation.native_market_id, filled, status, trade_id),
    )
    return int(rows[0]["id"])


async def _tasks(native_market_id: str) -> list[dict[str, Any]]:
    return await _query(
        "SELECT id, reason, status, owned_quantity, trade_id, source_attempt_id, last_error "
        "FROM app.live_emergency_closes WHERE native_market_id = %s ORDER BY id",
        (native_market_id,),
    )


async def _reservation(reservation_id: int) -> dict[str, Any]:
    rows = await _query(
        "SELECT state, attempt_id, trade_id, state_reason FROM app.live_probe_reservations "
        "WHERE id = %s",
        (reservation_id,),
    )
    return rows[0]


def _executor(position: str) -> tuple[emergency_close.EmergencyCloseExecutor, FakeExchange]:
    exchange = FakeExchange(position)
    exchange.positions[0]["symbol"] = SYMBOL
    exchange.markets[SYMBOL] = {"contractSize": 1}
    executor = emergency_close.EmergencyCloseExecutor(
        {"bybit": exchange}, PostgresEmergencyCloseStore(TEST_DATABASE_URL), _executor_rdb()
    )
    return executor, exchange


# --- L reached (section 4) ----------------------------------------------------------


async def test_l_reached_records_and_creates_the_close_task(
    rdb: LockRedis,
) -> None:
    probe_id, reservation, attempt_id, trade_id = await _open_position()

    result = await probe_close.record_l_reached(
        TEST_DATABASE_URL, rdb, probe_id, loss_usd=D("5.01"), inputs={"mark": "0.9"}
    )

    assert result.recorded
    [close] = result.closes
    assert close.created and close.reservation_id == reservation.id
    [task] = await _tasks(reservation.native_market_id)
    assert task["reason"] == emergency_close.REASON_PROBE_LOSS_THRESHOLD
    assert task["status"] == "requested"
    assert task["owned_quantity"] == D(10), "the full confirmed entry fill"
    assert (task["trade_id"], task["source_attempt_id"]) == (trade_id, attempt_id)
    record = (
        await _query(
            "SELECT l_reached_at, l_reached_loss_usd, l_reached_inputs "
            "FROM app.live_probe_authorizations WHERE probe_id = %s",
            (probe_id,),
        )
    )[0]
    assert record["l_reached_loss_usd"] == D("5.01")
    assert record["l_reached_inputs"] == {"mark": "0.9"}
    refused = await live_probe.reserve(
        TEST_DATABASE_URL,
        live_probe.EntryRequest(
            probe_id=probe_id,
            decision_id=uuid.uuid4().hex,
            environment="testnet",
            account_key="probe-sub-1",
            native_market_id=reservation.native_market_id,
            side="long",
            reserve_inputs=INPUTS,
        ),
    )
    assert isinstance(refused, live_probe.Refused)
    assert refused.reason == live_probe.REFUSED_L_REACHED


async def test_l_reached_again_keeps_the_first_inputs_and_one_task(rdb: LockRedis) -> None:
    probe_id, reservation, *_ = await _open_position()

    first = await probe_close.record_l_reached(
        TEST_DATABASE_URL, rdb, probe_id, loss_usd=D(6), inputs={"tick": 1}
    )
    again = await probe_close.record_l_reached(
        TEST_DATABASE_URL, rdb, probe_id, loss_usd=D(9), inputs={"tick": 2}
    )

    assert first.recorded and not again.recorded
    assert not again.closes[0].created
    assert again.closes[0].task_id == first.closes[0].task_id
    assert len(await _tasks(reservation.native_market_id)) == 1
    events = await _query(
        "SELECT event FROM app.live_probe_authorization_events WHERE probe_id = %s",
        (probe_id,),
    )
    assert [e["event"] for e in events].count("l_reached") == 1


async def test_a_monitor_exit_filled_before_the_task_is_linked_not_inserted(rdb: LockRedis) -> None:
    """Monitor exit filled 3 before the task: its row is linked, remainder 7."""
    probe_id, reservation, _, trade_id = await _open_position()
    assert trade_id is not None
    filled_exit = await _close_row(reservation, trade_id, status="completed", filled="3")
    in_flight = await _close_row(reservation, trade_id, status="accepted", filled=None)

    result = await probe_close.record_l_reached(
        TEST_DATABASE_URL, rdb, probe_id, loss_usd=D(6), inputs={}
    )

    task_id = result.closes[0].task_id
    assert task_id is not None
    closes = await PostgresEmergencyCloseStore(TEST_DATABASE_URL).load_closes(task_id)
    assert [c.attempt_id for c in closes] == [filled_exit, in_flight]
    assert [c.status for c in closes] == ["completed", "accepted"], "followed, never resent"
    task = (await _tasks(reservation.native_market_id))[0]
    filled = sum((c.filled_amount or D(0) for c in closes if c.status == "completed"), D(0))
    assert task["owned_quantity"] - filled == D(7)
    count = await _query(
        "SELECT count(*) AS n FROM app.live_order_attempts WHERE trade_id = %s "
        "AND operation = 'close'",
        (trade_id,),
    )
    assert count[0]["n"] == 2, "no close row inserted twice"


async def test_a_close_of_the_trade_owned_by_another_task_needs_the_owner(rdb: LockRedis) -> None:
    probe_id, reservation, attempt_id, trade_id = await _open_position()
    assert trade_id is not None
    other_entry = await _query(
        "INSERT INTO app.live_order_attempts (operation, client_order_id, exchange, base, "
        "symbol, native_market_id, market_type, side, size_usd, requested_amount, leverage, "
        "exit_params, setup_context, status) VALUES ('entry', %s, 'bybit', 'TLP', "
        "'TLP/USDT:USDT', %s, 'swap', 'short', 10, 10, 1, '{}'::jsonb, "
        "'{\"live_probe_test\": true}'::jsonb, 'completed') RETURNING id",
        (uuid.uuid4().hex, reservation.native_market_id),
    )
    other_task = await _query(
        "INSERT INTO app.live_emergency_closes (source_attempt_id, entry_client_order_id, "
        "exchange, base, symbol, native_market_id, market_type, side, owned_quantity, "
        "owned_remainder, reason, status) VALUES (%s, %s, 'bybit', 'TLP', 'TLP/USDT:USDT', "
        "%s, 'swap', 'short', 1, 0, 'stop_placement_failed', 'accounted') RETURNING id",
        (other_entry[0]["id"], uuid.uuid4().hex, reservation.native_market_id),
    )
    foreign = await _close_row(reservation, trade_id, status="completed", filled="1")
    await _query(
        "UPDATE app.live_order_attempts SET task_id = %s WHERE id = %s",
        (other_task[0]["id"], foreign),
    )

    result = await probe_close.record_l_reached(
        TEST_DATABASE_URL, rdb, probe_id, loss_usd=D(6), inputs={}
    )

    task = [
        t
        for t in await _tasks(reservation.native_market_id)
        if t["source_attempt_id"] == attempt_id
    ]
    assert result.closes[0].created
    assert task[0]["status"] == "manual_required"


async def test_a_position_already_held_by_a_task_gets_no_second_one(rdb: LockRedis) -> None:
    probe_id, reservation, attempt_id, trade_id = await _open_position()
    store = PostgresEmergencyCloseStore(TEST_DATABASE_URL)
    existing, _ = await store.create_task(
        emergency_close.TaskSpec(
            source_attempt_id=attempt_id,
            entry_client_order_id=reservation.client_order_id,
            exchange="bybit",
            base="TLP",
            symbol="TLP/USDT:USDT",
            native_market_id=reservation.native_market_id,
            market_type="swap",
            side="long",
            owned_quantity=D(10),
            reason=emergency_close.REASON_STOP_PLACEMENT_FAILED,
            trade_id=trade_id,
        )
    )

    result = await probe_close.record_l_reached(
        TEST_DATABASE_URL, rdb, probe_id, loss_usd=D(6), inputs={}
    )

    assert result.recorded
    assert result.closes[0].task_id == existing and not result.closes[0].created
    assert len(await _tasks(reservation.native_market_id)) == 1


async def test_an_exchange_position_without_a_probe_link_is_never_closed_by_l(
    rdb: LockRedis,
) -> None:
    probe_id, *_ = await _open_position()
    foreign = _instrument()
    await _query(
        "INSERT INTO app.live_order_attempts (operation, client_order_id, exchange, base, "
        "symbol, native_market_id, market_type, side, size_usd, requested_amount, "
        "filled_amount, leverage, exit_params, setup_context, status) VALUES ('entry', %s, "
        "'bybit', 'TLP', 'TLP/USDT:USDT', %s, 'swap', 'long', 10, 10, 10, 1, '{}'::jsonb, "
        "'{\"live_probe_test\": true}'::jsonb, 'completed')",
        (uuid.uuid4().hex, foreign),
    )

    await probe_close.record_l_reached(TEST_DATABASE_URL, rdb, probe_id, loss_usd=D(6), inputs={})

    assert await _tasks(foreign) == []


# --- Restart recovery (section 5) ---------------------------------------------------


async def test_a_start_suspends_and_closes_an_open_probe_position(rdb: LockRedis) -> None:
    probe_id, reservation, *_ = await _open_position()
    idle = await _issue()

    recovery = await probe_close.recover_at_start(TEST_DATABASE_URL, rdb)

    assert {probe_id, idle} <= set(recovery.suspended)
    [close] = [c for c in recovery.closes if c.probe_id == probe_id]
    assert close.created
    [task] = await _tasks(reservation.native_market_id)
    assert task["reason"] == emergency_close.REASON_PROBE_RECOVERY
    assert not [c for c in recovery.closes if c.probe_id == idle], "no position, no close"
    alerts = probe_close.recovery_alerts(recovery)
    assert any(probe_id in a and "recovery close task" in a for a in alerts)
    assert any(idle in a and "suspended" in a and "close" not in a for a in alerts)


async def test_a_start_with_a_task_for_the_position_creates_no_second_one(rdb: LockRedis) -> None:
    probe_id, reservation, *_ = await _open_position()
    first = await probe_close.recover_at_start(TEST_DATABASE_URL, rdb)
    await live_probe.resume(TEST_DATABASE_URL, probe_id, actor="owner")

    again = await probe_close.recover_at_start(TEST_DATABASE_URL, rdb)

    [close] = [c for c in again.closes if c.probe_id == probe_id]
    assert not close.created
    assert close.task_id == next(c.task_id for c in first.closes if c.probe_id == probe_id)
    assert probe_id in again.suspended
    assert len(await _tasks(reservation.native_market_id)) == 1


async def test_a_revoked_probe_with_an_open_position_is_still_closed(rdb: LockRedis) -> None:
    probe_id, *_ = await _open_position()
    await live_probe.revoke(TEST_DATABASE_URL, probe_id, actor="owner", reason="stop")

    recovery = await probe_close.recover_at_start(TEST_DATABASE_URL, rdb)

    assert probe_id not in recovery.suspended
    assert [c.created for c in recovery.closes if c.probe_id == probe_id] == [True]


async def test_a_closed_probe_trade_needs_no_recovery_close(rdb: LockRedis) -> None:
    probe_id, reservation, _, trade_id = await _open_position()
    await _query(
        "UPDATE app.trades SET status = 'closed', exit_at = now(), pnl_usd = 0 WHERE id = %s",
        (trade_id,),
    )

    recovery = await probe_close.recover_at_start(TEST_DATABASE_URL, rdb)

    assert not [c for c in recovery.closes if c.probe_id == probe_id]
    assert await _tasks(reservation.native_market_id) == []


async def test_the_recovery_worker_retries_until_the_database_answers() -> None:
    calls: list[str] = []

    async def flaky(db_url: str, rdb: Any) -> probe_close.Recovery:
        calls.append(db_url)
        if len(calls) == 1:
            raise psycopg.OperationalError("database down")
        return probe_close.Recovery(suspended=["tlp-x"], closes=[])

    worker = probe_close.ProbeRecoveryWorker(
        "postgresql://unused", LockRedis(), alert_credentials=None, recover=flaky
    )

    assert not await worker.run_once()
    assert not worker.recovered.is_set()
    assert await worker.run_once()
    assert worker.recovered.is_set()
    assert await worker.run_once()
    assert len(calls) == 2, "once per start"


async def test_the_recovery_worker_waits_out_a_pending_position_alerting_once() -> None:
    waiting = probe_close.CloseOutcome("tlp-x", 1, None, False, "entry attempt 7 is accepted", True)
    results = [
        probe_close.Recovery(suspended=["tlp-x"], closes=[waiting]),
        probe_close.Recovery(suspended=[], closes=[waiting]),
        probe_close.Recovery(suspended=[], closes=[]),
    ]

    async def recover(db_url: str, rdb: Any) -> probe_close.Recovery:
        return results.pop(0)

    worker = probe_close.ProbeRecoveryWorker(
        "postgresql://unused", LockRedis(), alert_credentials=("t", "c"), recover=recover
    )
    sent: list[str] = []

    async def notify_alert(*_credentials: str, text: str) -> bool:
        sent.append(text)
        return True

    with patch("schurfer_execution.probe_close.notify.notify_alert", notify_alert):
        assert not await worker.run_once()
        assert not await worker.run_once()
        assert not worker.recovered.is_set()
        assert await worker.run_once()

    assert worker.recovered.is_set()
    assert len(sent) == 2, sent
    assert "recovery waits: entry attempt 7 is accepted" in sent[0]


# --- A restart cut the chain sending -> placed -> trade (review round 1) ------------


async def test_a_start_recovers_a_filled_entry_still_sending(rdb: LockRedis) -> None:
    for stage in ("written", "sent"):
        probe_id, reservation, attempt_id, trade_id = await _open_position(stage=stage)

        recovery = await probe_close.recover_at_start(TEST_DATABASE_URL, rdb)

        [close] = [c for c in recovery.closes if c.probe_id == probe_id]
        assert close.created, (stage, close)
        [task] = await _tasks(reservation.native_market_id)
        assert (task["trade_id"], task["owned_quantity"]) == (trade_id, D(10))
        row = await _reservation(reservation.id)
        assert (row["state"], row["attempt_id"], row["trade_id"]) == (
            "placed",
            attempt_id,
            trade_id,
        ), "the chain the restart cut is completed"


async def test_the_trade_of_the_attempt_is_used_before_the_reservation_links_it(
    rdb: LockRedis,
) -> None:
    probe_id, reservation, _, trade_id = await _open_position(stage="placed")
    partial = await _close_row(reservation, trade_id, status="completed", filled="3")

    result = await probe_close.record_l_reached(
        TEST_DATABASE_URL, rdb, probe_id, loss_usd=D(60), inputs={}
    )

    [task] = await _tasks(reservation.native_market_id)
    assert task["trade_id"] == trade_id
    task_id = result.closes[0].task_id
    assert task_id is not None
    closes = await PostgresEmergencyCloseStore(TEST_DATABASE_URL).load_closes(task_id)
    assert [c.attempt_id for c in closes] == [partial]
    assert (await _reservation(reservation.id))["trade_id"] == trade_id


async def test_a_trade_closed_before_the_link_is_linked_and_needs_no_close(
    rdb: LockRedis,
) -> None:
    probe_id, reservation, _, trade_id = await _open_position(stage="placed")
    await _query(
        "UPDATE app.trades SET status = 'closed', exit_at = now(), pnl_usd = 0 WHERE id = %s",
        (trade_id,),
    )

    recovery = await probe_close.recover_at_start(TEST_DATABASE_URL, rdb)

    assert not [c for c in recovery.closes if c.probe_id == probe_id]
    assert await _tasks(reservation.native_market_id) == []
    assert (await _reservation(reservation.id))["trade_id"] == trade_id, "the ledger counts it"


async def test_an_entry_whose_outcome_is_not_final_keeps_the_recovery_pending(
    rdb: LockRedis,
) -> None:
    probe_id, reservation, *_ = await _open_position(
        stage="sent", status="submission_unknown", filled=None, trade=False
    )

    recovery = await probe_close.recover_at_start(TEST_DATABASE_URL, rdb)

    [close] = [c for c in recovery.closes if c.probe_id == probe_id]
    assert close.pending and close.task_id is None
    assert probe_id in recovery.suspended, "the suspension commits regardless"
    assert (await _reservation(reservation.id))["state"] == "sending"


async def test_a_start_releases_a_reservation_whose_attempt_was_never_written(
    rdb: LockRedis,
) -> None:
    instrument = _instrument()
    probe_id = await _issue(instruments=(instrument,), loss_threshold_usd=D(50))
    reservation = await live_probe.reserve(
        TEST_DATABASE_URL,
        live_probe.EntryRequest(
            probe_id=probe_id,
            decision_id=uuid.uuid4().hex,
            environment="testnet",
            account_key="probe-sub-1",
            native_market_id=instrument,
            side="long",
            reserve_inputs=INPUTS,
        ),
    )
    assert isinstance(reservation, live_probe.Reservation)
    await live_probe.begin_sending(TEST_DATABASE_URL, reservation.id)

    recovery = await probe_close.recover_at_start(TEST_DATABASE_URL, rdb)

    [close] = [c for c in recovery.closes if c.probe_id == probe_id]
    assert not close.pending and close.task_id is None
    assert (await _reservation(reservation.id))["state"] == "released"


async def test_a_task_accounted_while_the_position_is_open_opens_an_incident(
    rdb: LockRedis,
) -> None:
    probe_id, reservation, attempt_id, trade_id = await _open_position()
    await _query(
        "INSERT INTO app.live_emergency_closes (source_attempt_id, entry_client_order_id, "
        "exchange, base, symbol, native_market_id, market_type, side, owned_quantity, "
        "owned_remainder, reason, status, trade_id) VALUES (%s, %s, 'bybit', 'TLP', "
        "'TLP/USDT:USDT', %s, 'swap', 'long', 10, 0, 'probe_loss_threshold', 'accounted', %s) "
        "RETURNING id",
        (attempt_id, reservation.client_order_id, reservation.native_market_id, trade_id),
    )

    recovery = await probe_close.recover_at_start(TEST_DATABASE_URL, rdb)

    [close] = [c for c in recovery.closes if c.probe_id == probe_id]
    assert not close.created and "incident" in (close.note or "")
    incidents = await _query(
        "SELECT discrepancy_type, status FROM app.live_reconciliation_incidents "
        "WHERE native_market_id = %s",
        (reservation.native_market_id,),
    )
    assert incidents == [
        {"discrepancy_type": probe_close.INCIDENT_ACCOUNTED_OPEN, "status": "manual_required"}
    ]


# --- Handoff under the order lock (review round 1) -----------------------------------


async def test_l_waits_for_a_monitor_exit_holding_the_order_lock(rdb: LockRedis) -> None:
    """The monitor checked for a task under the lock and is still closing: L is
    recorded at once, the task only once the monitor's close row is final."""
    for status in ("completed", "partial"):
        probe_id, reservation, _, trade_id = await _open_position()
        rdb.keys[ORDER_LOCK] = "monitor"

        with pytest.raises(probe_close.OrderLockBusyError):
            await probe_close.record_l_reached(
                TEST_DATABASE_URL, rdb, probe_id, loss_usd=D(60), inputs={}
            )

        assert await _tasks(reservation.native_market_id) == []
        record = await _query(
            "SELECT l_reached_at FROM app.live_probe_authorizations WHERE probe_id = %s",
            (probe_id,),
        )
        assert record[0]["l_reached_at"] is not None, "entries stop at once"
        await _close_row(reservation, trade_id, status=status, filled="3")
        del rdb.keys[ORDER_LOCK]

        result = await probe_close.record_l_reached(
            TEST_DATABASE_URL, rdb, probe_id, loss_usd=D(60), inputs={}
        )
        task_id = result.closes[0].task_id
        assert task_id is not None and not result.recorded
        assert rdb.keys == {}, "the lock is released after the commit"
        executor, exchange = _executor("7")

        assert await executor.run_pass(task_id) == emergency_close.STATUS_SENDING, status
        assert [amount for _, amount, _ in exchange.sent] == [7.0]
        await _query(
            "UPDATE app.live_emergency_closes SET status = 'accounted' WHERE id = %s", (task_id,)
        )


async def test_a_monitor_close_of_unknown_outcome_is_followed_not_resent(
    rdb: LockRedis,
) -> None:
    probe_id, reservation, _, trade_id = await _open_position()
    await _close_row(reservation, trade_id, status="submission_unknown", filled=None)

    result = await probe_close.record_l_reached(
        TEST_DATABASE_URL, rdb, probe_id, loss_usd=D(60), inputs={}
    )
    task_id = result.closes[0].task_id
    assert task_id is not None
    executor, exchange = _executor("10")

    assert await executor.run_pass(task_id) == emergency_close.STATUS_UNKNOWN
    assert exchange.sent == []


async def test_a_close_of_the_instrument_linked_to_no_trade_needs_the_owner(
    rdb: LockRedis,
) -> None:
    probe_id, reservation, *_ = await _open_position()
    await _close_row(reservation, None, status="submission_unknown", filled=None)

    result = await probe_close.record_l_reached(
        TEST_DATABASE_URL, rdb, probe_id, loss_usd=D(60), inputs={}
    )

    assert result.closes[0].created
    [task] = await _tasks(reservation.native_market_id)
    assert task["status"] == "manual_required"
    assert "linked to no task" in task["last_error"]


async def test_a_start_with_a_busy_order_lock_suspends_and_writes_no_task(
    rdb: LockRedis,
) -> None:
    probe_id, reservation, *_ = await _open_position()
    rdb.keys[ORDER_LOCK] = "monitor"

    recovery = await probe_close.recover_at_start(TEST_DATABASE_URL, rdb)

    assert probe_id in recovery.suspended
    assert recovery.close_error is not None and "busy" in recovery.close_error
    record = await _query(
        "SELECT suspended_at FROM app.live_probe_authorizations WHERE probe_id = %s",
        (probe_id,),
    )
    assert record[0]["suspended_at"] is not None
    assert await _tasks(reservation.native_market_id) == []


async def test_suspensions_are_alerted_when_the_close_step_of_the_start_fails(
    rdb: LockRedis,
) -> None:
    """The retry suspends nothing more: the first attempt's suspensions are alerted
    even though its close step failed."""
    await _open_position()
    idle = await _issue()
    rdb.keys[ORDER_LOCK] = "monitor"
    worker = probe_close.ProbeRecoveryWorker(TEST_DATABASE_URL, rdb, alert_credentials=("t", "c"))
    delivered = AsyncMock(return_value=True)

    with patch.object(probe_close.notify, "notify_alert", delivered):
        assert not await worker.run_once()
        assert not worker.recovered.is_set()
        del rdb.keys[ORDER_LOCK]
        assert await worker.run_once()

    texts = [call.kwargs["text"] for call in delivered.await_args_list]
    assert any(idle in text and "suspended" in text for text in texts), texts
    assert sum("could not check" in text for text in texts) == 1, texts


class _ListingExchange(FakeExchange):
    """``close_position`` lists every position, with no symbol filter."""

    async def fetch_positions(self, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        return list(self.positions)


async def test_a_monitor_close_sent_after_the_task_while_its_check_failed_open_is_followed(
    rdb: LockRedis,
) -> None:
    """The monitor's task check fails on a database error after the task exists, so
    its protective close goes out with an unknown outcome. The executor takes that
    close under the lock and sends nothing before it is resolved."""
    probe_id, reservation, _, trade_id = await _open_position()
    result = await probe_close.record_l_reached(
        TEST_DATABASE_URL, rdb, probe_id, loss_usd=D(60), inputs={}
    )
    task_id = result.closes[0].task_id
    assert task_id is not None
    exchange = _ListingExchange("10")
    exchange.positions[0]["symbol"] = SYMBOL
    exchange.markets[SYMBOL] = {
        "contractSize": 1,
        "id": reservation.native_market_id,
        "type": "swap",
    }

    def unknown(amount: Any, params: Any) -> None:
        raise ccxt.NetworkError("send result unknown")

    exchange.on_create = unknown
    rdb.get = AsyncMock(return_value=None)  # type: ignore[attr-defined]
    with (
        patch.object(
            emergency_close,
            "has_active_task",
            AsyncMock(side_effect=psycopg.OperationalError("query failed")),
        ),
        patch.object(orders, "_tracked_trade_id", AsyncMock(return_value=trade_id)),
    ):
        close = await orders.close_position(
            exchanges={"bybit": exchange},
            exchange="bybit",
            base="TLP",
            symbol=SYMBOL,
            reason="monitor exit",
            rdb=rdb,
            cfg=SimpleNamespace(db_url=TEST_DATABASE_URL),
        )
    assert "submission_unknown" in close["reason"], close
    assert len(exchange.sent) == 1 and rdb.keys == {}
    exchange.on_create = None
    executor = emergency_close.EmergencyCloseExecutor(
        {"bybit": exchange}, PostgresEmergencyCloseStore(TEST_DATABASE_URL), rdb
    )

    assert await executor.run_pass(task_id) == emergency_close.STATUS_UNKNOWN
    assert len(exchange.sent) == 1, exchange.sent
    closes = await PostgresEmergencyCloseStore(TEST_DATABASE_URL).load_closes(task_id)
    assert [c.status for c in closes] == ["submission_unknown"]


async def test_a_close_without_a_trade_sent_after_the_task_stops_it(rdb: LockRedis) -> None:
    probe_id, reservation, *_ = await _open_position()
    result = await probe_close.record_l_reached(
        TEST_DATABASE_URL, rdb, probe_id, loss_usd=D(60), inputs={}
    )
    task_id = result.closes[0].task_id
    assert task_id is not None
    await _close_row(reservation, None, status="completed", filled="3")
    executor, exchange = _executor("7")

    assert await executor.run_pass(task_id) == emergency_close.STATUS_MANUAL_REQUIRED
    assert exchange.sent == []
    [task] = await _tasks(reservation.native_market_id)
    assert "linked to no task" in task["last_error"]


async def test_a_close_of_another_exact_instrument_of_the_base_is_not_counted(
    rdb: LockRedis,
) -> None:
    """Same exchange and base, another native market: not this position's close."""
    probe_id, reservation, *_ = await _open_position()
    other_trade = await _trade(status="open", pnl=None)
    other = await _close_row(reservation, other_trade, status="completed", filled="3")
    await _query(
        "UPDATE app.live_order_attempts SET native_market_id = %s, symbol = 'TLP/USDC:USDC' "
        "WHERE id = %s",
        (reservation.native_market_id + "X", other),
    )

    await probe_close.record_l_reached(TEST_DATABASE_URL, rdb, probe_id, loss_usd=D(60), inputs={})

    [task] = await _tasks(reservation.native_market_id)
    assert task["status"] == "requested", task


async def test_a_close_of_the_base_whose_exact_instrument_is_unknown_needs_the_owner(
    rdb: LockRedis,
) -> None:
    probe_id, reservation, *_ = await _open_position()
    unknown = await _close_row(reservation, None, status="completed", filled="3")
    await _query(
        "UPDATE app.live_order_attempts SET native_market_id = NULL WHERE id = %s", (unknown,)
    )

    await probe_close.record_l_reached(TEST_DATABASE_URL, rdb, probe_id, loss_usd=D(60), inputs={})

    [task] = await _tasks(reservation.native_market_id)
    assert task["status"] == "manual_required" and "linked to no task" in task["last_error"]


# --- Accounting after L (live-entry-failure-paths-design-v1.md, section 1) -----------


class _Venue:
    """One long position and the market orders that close it, priced and timed, as
    ``close_position``, the executor and the accountant read them."""

    def __init__(self, contracts: str, native_market_id: str) -> None:
        self.has = {"fetchOpenOrders": True, "fetchClosedOrders": True}
        self.markets = {SYMBOL: {"contractSize": 1, "id": native_market_id, "type": "swap"}}
        self.contracts = float(contracts)
        self.orders: dict[str, dict[str, Any]] = {}
        self.fills: list[tuple[float, float]] = []
        self.sent: list[float] = []

    async def fetch_positions(self, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        return [{"symbol": SYMBOL, "contracts": self.contracts, "side": "long", "markPrice": 95.0}]

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        return f"{amount:.8f}".rstrip("0").rstrip(".")

    async def create_market_order(
        self, symbol: str, side: str, amount: float, params: dict[str, Any]
    ) -> dict[str, Any]:
        self.sent.append(amount)
        filled, price = self.fills.pop(0)
        order_id = f"tlp-{uuid.uuid4().hex}"
        self.orders[order_id] = {
            "id": order_id,
            "clientOrderId": params["clientOrderId"],
            "status": "closed",
            "amount": amount,
            "filled": filled,
            "average": price,
            "lastTradeTimestamp": 1_800_000_000_000 + len(self.orders) * 60_000,
            "stop": False,
        }
        self.contracts -= filled
        return dict(self.orders[order_id])

    def _listing(self, open_orders: bool, params: dict[str, Any] | None) -> list[dict[str, Any]]:
        trigger = bool((params or {}).get("trigger"))
        return [
            dict(order)
            for order in self.orders.values()
            if order["stop"] == trigger and (order["status"] == "open") == open_orders
        ]

    async def fetch_open_orders(
        self, symbol: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return self._listing(True, params)

    async def fetch_closed_orders(
        self, symbol: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return self._listing(False, params)

    async def fetch_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        return dict(self.orders[order_id])

    async def fetch_my_trades(self, symbol: str, **kwargs: Any) -> list[dict[str, Any]]:
        return []


async def _flat_after_a_partial_exit(
    rdb: LockRedis,
) -> tuple[int, int, _Venue, live_probe.Reservation]:
    """The monitor exit fills 3 of 10 and journals its leg; L closes the other 7 and
    the task is confirmed flat. Returns task, trade, venue, reservation."""
    probe_id, reservation, _, trade_id = await _open_position()
    assert trade_id is not None
    venue = _Venue("10", reservation.native_market_id)
    venue.fills = [(3.0, 95.0), (7.0, 94.0)]

    with patch.object(orders, "_tracked_trade_id", AsyncMock(return_value=trade_id)):
        exit_ = await orders.close_position(
            exchanges={"bybit": venue},
            exchange="bybit",
            base="TLP",
            symbol=SYMBOL,
            reason="monitor exit",
            rdb=rdb,
            cfg=SimpleNamespace(db_url=TEST_DATABASE_URL),
        )
    assert exit_["closed"] is False and exit_["filled_amount"] == 3.0, exit_
    monitor_rows = await _query(
        "SELECT status, filled_amount FROM app.live_order_attempts "
        "WHERE operation = 'close' AND trade_id = %s",
        (trade_id,),
    )
    assert [(r["status"], r["filled_amount"]) for r in monitor_rows] == [("partial", D(3))]

    result = await probe_close.record_l_reached(
        TEST_DATABASE_URL, rdb, probe_id, loss_usd=D(60), inputs={}
    )
    task_id = result.closes[0].task_id
    assert task_id is not None
    executor = emergency_close.EmergencyCloseExecutor(
        {"bybit": venue}, PostgresEmergencyCloseStore(TEST_DATABASE_URL), rdb
    )
    assert await executor.run_pass(task_id) == emergency_close.STATUS_SENDING
    assert await executor.run_pass(task_id) == emergency_close.STATUS_CONFIRMED_FLAT
    assert venue.sent == [10.0, 7.0]
    return task_id, trade_id, venue, reservation


def _accountant(venue: _Venue, rdb: LockRedis) -> emergency_accounting.EmergencyCloseAccountant:
    return emergency_accounting.EmergencyCloseAccountant(
        {"bybit": venue}, emergency_accounting.PostgresAccountingStore(TEST_DATABASE_URL), rdb
    )


async def _fills(trade_id: int) -> list[tuple[Decimal, bool]]:
    rows = await _query(
        "SELECT filled_amount, terminal FROM app.trade_close_fills WHERE trade_id = %s "
        "ORDER BY executed_at, id",
        (trade_id,),
    )
    return [(r["filled_amount"], r["terminal"]) for r in rows]


async def test_a_monitor_partial_exit_then_l_is_accounted_without_a_second_leg(
    rdb: LockRedis,
) -> None:
    """The accountant keeps the monitor's leg as journalled, writes the executor's as
    the terminal one, and closes the trade at the weighted price of both."""
    task_id, trade_id, venue, reservation = await _flat_after_a_partial_exit(rdb)

    assert await _accountant(venue, rdb).account(task_id) == emergency_close.STATUS_ACCOUNTED

    assert await _fills(trade_id) == [(D(3), False), (D(7), True)]
    [trade] = await _query("SELECT status, exit_price FROM app.trades WHERE id = %s", (trade_id,))
    assert trade["status"] == "closed"
    assert float(trade["exit_price"]) == pytest.approx((3 * 95 + 7 * 94) / 10)
    [task] = await _tasks(reservation.native_market_id)
    assert (task["status"], task["trade_id"]) == ("accounted", trade_id)


async def test_a_journalled_leg_the_task_does_not_explain_needs_the_owner(
    rdb: LockRedis,
) -> None:
    task_id, trade_id, venue, reservation = await _flat_after_a_partial_exit(rdb)
    await _query(
        "INSERT INTO app.trade_close_fills (trade_id, exchange, order_id, fill_price, "
        "filled_amount, requested_amount, remaining_amount, terminal, fill_source) "
        "VALUES (%s, 'bybit', %s, 95, 1, 1, 0, false, 'test') RETURNING id",
        (trade_id, f"tlp-{uuid.uuid4().hex}"),
    )

    assert await _accountant(venue, rdb).account(task_id) == emergency_close.STATUS_MANUAL_REQUIRED

    assert await _fills(trade_id) == [(D(3), False), (D(1), False)], "nothing written"
    [task] = await _tasks(reservation.native_market_id)
    assert "not one of the task's legs" in task["last_error"]


async def test_a_journalled_leg_with_another_amount_needs_the_owner(rdb: LockRedis) -> None:
    task_id, trade_id, venue, _ = await _flat_after_a_partial_exit(rdb)
    await _query(
        "UPDATE app.trade_close_fills SET filled_amount = 2 WHERE trade_id = %s",
        (trade_id,),
    )

    assert await _accountant(venue, rdb).account(task_id) == emergency_close.STATUS_MANUAL_REQUIRED

    assert await _fills(trade_id) == [(D(2), False)]
    [trade] = await _query("SELECT status FROM app.trades WHERE id = %s", (trade_id,))
    assert trade["status"] == "open"


async def test_a_journalled_leg_at_another_price_needs_the_owner(rdb: LockRedis) -> None:
    task_id, trade_id, venue, _ = await _flat_after_a_partial_exit(rdb)
    monitor_order = next(iter(venue.orders.values()))
    monitor_order["average"] = 96.0

    assert await _accountant(venue, rdb).account(task_id) == emergency_close.STATUS_MANUAL_REQUIRED

    assert await _fills(trade_id) == [(D(3), False)]
    [trade] = await _query("SELECT status FROM app.trades WHERE id = %s", (trade_id,))
    assert trade["status"] == "open"


async def test_a_leg_written_while_the_accountant_holds_the_trade_waits_and_is_refused(
    rdb: LockRedis,
) -> None:
    """Every leg writer holds the trade row: a delayed leg cannot slip in between the
    accountant's check and its writes, and once the trade is closed it is refused."""
    task_id, trade_id, venue, _ = await _flat_after_a_partial_exit(rdb)
    checked = emergency_accounting._journalled_legs
    late: list[asyncio.Task[float | None]] = []

    async def with_a_late_writer(*args: Any) -> dict[str, bool]:
        result = await checked(*args)
        late.append(
            asyncio.create_task(
                journal.record_close_fill(
                    TEST_DATABASE_URL,
                    trade_id=trade_id,
                    exchange="bybit",
                    order_id=f"tlp-{uuid.uuid4().hex}",
                    fill_price=80.0,
                    filled_amount=1.0,
                    requested_amount=1.0,
                    remaining_amount=0.0,
                    terminal=False,
                    fill_source="delayed_incident",
                )
            )
        )
        await asyncio.sleep(0.5)
        assert not late[0].done(), "the late writer waits for the trade row"
        return result

    with patch.object(emergency_accounting, "_journalled_legs", with_a_late_writer):
        status = await _accountant(venue, rdb).account(task_id)

    assert status == emergency_close.STATUS_ACCOUNTED
    assert await late[0] is None, "a closed trade takes no new leg"
    assert await _fills(trade_id) == [(D(3), False), (D(7), True)]


async def test_a_stop_the_monitor_journalled_and_closed_is_accounted_as_it_stands(
    rdb: LockRedis,
) -> None:
    """The monitor exit fills 3, the stop fills the other 7 and the monitor journals it
    and closes the trade before the accountant runs: nothing is written again."""
    probe_id, reservation, _, trade_id = await _open_position()
    assert trade_id is not None
    venue = _Venue("10", reservation.native_market_id)
    venue.fills = [(3.0, 95.0)]
    with patch.object(orders, "_tracked_trade_id", AsyncMock(return_value=trade_id)):
        await orders.close_position(
            exchanges={"bybit": venue},
            exchange="bybit",
            base="TLP",
            symbol=SYMBOL,
            reason="monitor exit",
            rdb=rdb,
            cfg=SimpleNamespace(db_url=TEST_DATABASE_URL),
        )
    reached = await probe_close.record_l_reached(
        TEST_DATABASE_URL, rdb, probe_id, loss_usd=D(60), inputs={}
    )
    task_id = reached.closes[0].task_id
    assert task_id is not None
    stop_id = f"tlp-{uuid.uuid4().hex}"
    venue.orders[stop_id] = {
        "id": stop_id,
        "clientOrderId": order_ids.stop_client_order_id(reservation.client_order_id, 1),
        "status": "closed",
        "amount": 10.0,
        "filled": 7.0,
        "average": 94.0,
        "lastTradeTimestamp": 1_800_000_060_000,
        "stop": True,
    }
    venue.contracts = 0.0
    executor = emergency_close.EmergencyCloseExecutor(
        {"bybit": venue}, PostgresEmergencyCloseStore(TEST_DATABASE_URL), rdb
    )
    assert await executor.run_pass(task_id) == emergency_close.STATUS_CONFIRMED_FLAT
    rdb.keys["position:sl_order_id:bybit:TLP"] = stop_id
    rdb.keys["trade:id:bybit:TLP"] = str(trade_id)
    with (
        patch.object(
            monitor.symbols,
            "resolve_execution_instrument",
            return_value=SimpleNamespace(symbol=SYMBOL),
        ),
        patch.object(monitor.notify, "credentials", return_value=None),
    ):
        await monitor._reconcile_one(
            "bybit", "TLP", {"bybit": venue}, rdb, SimpleNamespace(db_url=TEST_DATABASE_URL)
        )
    assert await _fills(trade_id) == [(D(3), False), (D(7), True)]

    assert await _accountant(venue, rdb).account(task_id) == emergency_close.STATUS_ACCOUNTED

    assert await _fills(trade_id) == [(D(3), False), (D(7), True)]
    [trade] = await _query("SELECT status, exit_price FROM app.trades WHERE id = %s", (trade_id,))
    assert trade["status"] == "closed"
    assert float(trade["exit_price"]) == pytest.approx((3 * 95 + 7 * 94) / 10)
