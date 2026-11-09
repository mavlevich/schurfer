"""The live cost worker: its decisions on a fake store, then the whole settlement on
Postgres (docs/engineering/live-net-accounting-design-v1.md, section 5)."""

from __future__ import annotations

import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg
import pytest
from schurfer_execution import cost_worker as cw
from schurfer_execution import journal
from schurfer_execution import live_costs as lc
from schurfer_journal.testing_database import (
    assert_active_test_database_url,
    integration_database_url,
)

ENTRY = "c" * 32
T_ENTRY = datetime(2026, 11, 3, 7, 10, tzinfo=UTC)
T_EXIT = datetime(2026, 11, 3, 8, 30, tzinfo=UTC)
T_SETTLEMENT = datetime(2026, 11, 3, 8, 0, tzinfo=UTC)


def _rdb() -> MagicMock:
    rdb = MagicMock()
    rdb.delete = AsyncMock(return_value=1)
    return rdb


def _execution(
    exec_id: str,
    *,
    order_id: str | None,
    link: str | None,
    side: str,
    qty: str,
    price: str,
    at: datetime,
    fee: str,
    exec_type: str = lc.EXEC_TRADE,
) -> lc.Execution:
    quantity = Decimal(qty)
    return lc.Execution(
        exec_id=exec_id,
        exec_type=exec_type,
        order_id=order_id,
        order_link_id=link,
        side=side,
        quantity=quantity,
        price=Decimal(price) if exec_type == lc.EXEC_TRADE else None,
        value=quantity * Decimal(price) if exec_type == lc.EXEC_TRADE else None,
        fee=Decimal(fee),
        fee_currency="USDT",
        executed_at=at,
        raw={"execId": exec_id},
    )


def _round_trip(entry_cid: str = ENTRY, prefix: str = "") -> list[lc.Execution]:
    return [
        _execution(
            f"{prefix}e1",
            order_id=f"{prefix}o-entry",
            link=entry_cid,
            side="Buy",
            qty="10",
            price="100",
            at=T_ENTRY,
            fee="0.55",
        ),
        _execution(
            f"{prefix}f1",
            order_id=None,
            link=None,
            side="Buy",
            qty="10",
            price="0",
            at=T_SETTLEMENT,
            fee="0.10",
            exec_type=lc.EXEC_FUNDING,
        ),
        _execution(
            f"{prefix}x1",
            order_id=f"{prefix}o-exit",
            link=f"{entry_cid}s1",
            side="Sell",
            qty="10",
            price="101",
            at=T_EXIT,
            fee="0.5555",
        ),
    ]


# The history has published the settlement after every test trade's exit.
_PUBLISHED_TO = T_SETTLEMENT + timedelta(hours=8)


class FakeSource:
    def __init__(
        self,
        executions: list[lc.Execution],
        *,
        fail: bool = False,
        published_to: datetime | None = _PUBLISHED_TO,
    ) -> None:
        self.rows = executions
        self.fail = fail
        self.newest = published_to

    async def executions(self, symbol: str, start: datetime, end: datetime) -> list[lc.Execution]:
        if self.fail:
            raise RuntimeError("execution list unavailable")
        return [row for row in self.rows if start <= row.executed_at <= end]

    async def settlements(self, symbol: str, start: datetime, end: datetime) -> list[datetime]:
        return [at for at in (T_SETTLEMENT, _PUBLISHED_TO) if start <= at <= end]

    async def published_to(self, symbol: str) -> datetime | None:
        return self.newest

    async def order(self, symbol: str, order_id: str) -> lc.OrderState | None:
        filled = sum((row.quantity for row in self.rows if row.order_id == order_id), Decimal(0))
        return lc.OrderState(order_id, terminal=True, filled=filled)


def _facts(**changes: Any) -> lc.TradeFacts:
    return replace(
        lc.TradeFacts(
            trade_id=5,
            exchange="bybit",
            native_market_id="BTCUSDT",
            side="long",
            entry_client_order_id=ENTRY,
            entry_order_id="o-entry",
            entry_filled=Decimal(10),
            contract_size=Decimal(1),
            order_ids=frozenset({"o-entry"}),
            client_order_ids=frozenset(),
            sent_at=T_ENTRY - timedelta(seconds=1),
            exit_at=T_EXIT,
        ),
        **changes,
    )


class FakeStore:
    def __init__(self, trade: cw.PendingTrade | None) -> None:
        self.trade = trade
        self.settled: list[lc.CostResult] = []
        self.incomplete: list[str] = []

    async def pending_trade_ids(self, settled_before: datetime) -> list[int]:
        return [5]

    async def load(self, trade_id: int) -> cw.PendingTrade | None:
        return self.trade

    async def settle(self, trade_id: int, result: lc.CostResult) -> bool:
        self.settled.append(result)
        return True

    async def mark_incomplete(self, trade_id: int, error: str) -> bool:
        first = not self.incomplete
        self.incomplete.append(error)
        return first


NO_NEIGHBOURS = lc.Neighbours(frozenset(), frozenset(), frozenset(), ())
_BOTH_ORDERS = {
    "o-entry": lc.OrderState("o-entry", True, Decimal(10)),
    "o-exit": lc.OrderState("o-exit", True, Decimal(10)),
}


def _worker(
    store: FakeStore, source: FakeSource | None, *, now: datetime
) -> tuple[cw.LiveCostWorker, MagicMock]:
    rdb = _rdb()
    sources: dict[str, Any] = {"bybit": source} if source is not None else {}
    return (
        cw.LiveCostWorker(sources, store, rdb, alert_credentials=("t", "c"), now=lambda: now),
        rdb,
    )


async def test_a_settled_trade_is_written_and_readiness_revoked() -> None:
    store = FakeStore(cw.PendingTrade(_facts(), NO_NEIGHBOURS))
    worker, rdb = _worker(store, FakeSource(_round_trip()), now=T_EXIT + timedelta(minutes=6))

    assert await worker.settle(5) == lc.STATUS_COMPLETE
    assert store.settled[0].net_pnl_usd == Decimal("10") - Decimal("1.1055") - Decimal("0.10")
    rdb.delete.assert_awaited_with("risk:pnl_ready")


async def test_incomplete_alerts_once_and_entries_stay_blocked() -> None:
    stranger = _execution(
        "u1",
        order_id="o-unknown",
        link=None,
        side="Buy",
        qty="1",
        price="100",
        at=T_ENTRY + timedelta(minutes=5),
        fee="0.05",
    )
    store = FakeStore(cw.PendingTrade(_facts(), NO_NEIGHBOURS))
    worker, _ = _worker(
        store, FakeSource([*_round_trip(), stranger]), now=T_EXIT + timedelta(minutes=6)
    )

    with patch("schurfer_execution.cost_worker.notify.notify_alert", AsyncMock()) as alert:
        assert await worker.settle(5) == lc.STATUS_INCOMPLETE
        assert await worker.settle(5) == lc.STATUS_INCOMPLETE

    assert alert.await_count == 1
    assert "unknown order" in store.incomplete[0]
    assert store.settled == []


async def test_a_failed_read_stays_pending_then_becomes_incomplete_after_24_h() -> None:
    store = FakeStore(cw.PendingTrade(_facts(), NO_NEIGHBOURS))
    worker, _ = _worker(store, FakeSource([], fail=True), now=T_EXIT + timedelta(hours=1))
    assert await worker.settle(5) == lc.STATUS_PENDING
    assert store.incomplete == []

    late, _ = _worker(store, FakeSource([], fail=True), now=T_EXIT + timedelta(hours=24))
    with patch("schurfer_execution.cost_worker.notify.notify_alert", AsyncMock()):
        assert await late.settle(5) == lc.STATUS_INCOMPLETE
    assert "24 h" in store.incomplete[0]
    assert "execution list unavailable" in store.incomplete[0]


async def test_exit_rows_short_by_one_become_incomplete_at_the_deadline() -> None:
    rows = _round_trip()
    rows[-1] = replace(rows[-1], quantity=Decimal(9), value=Decimal(909))
    store = FakeStore(cw.PendingTrade(_facts(), NO_NEIGHBOURS))
    worker, _ = _worker(store, FakeSource(rows), now=T_EXIT + timedelta(hours=2))
    assert await worker.settle(5) == lc.STATUS_PENDING

    late, _ = _worker(store, FakeSource(rows), now=T_EXIT + timedelta(hours=25))
    with patch("schurfer_execution.cost_worker.notify.notify_alert", AsyncMock()):
        assert await late.settle(5) == lc.STATUS_INCOMPLETE


async def test_a_history_not_published_past_the_exit_becomes_incomplete_at_the_deadline() -> None:
    """The history never lists a settlement after the exit: never a confirmed zero."""
    store = FakeStore(cw.PendingTrade(_facts(), NO_NEIGHBOURS))
    source = FakeSource(_round_trip(), published_to=T_SETTLEMENT)
    worker, _ = _worker(store, source, now=T_EXIT + timedelta(hours=2))
    assert await worker.settle(5) == lc.STATUS_PENDING

    late, _ = _worker(store, source, now=T_EXIT + timedelta(hours=25))
    with patch("schurfer_execution.cost_worker.notify.notify_alert", AsyncMock()):
        assert await late.settle(5) == lc.STATUS_INCOMPLETE
    assert "published to" in store.incomplete[0]
    assert store.settled == []


class FailingStore(FakeStore):
    def __init__(self, trade: cw.PendingTrade, error: Exception) -> None:
        super().__init__(trade)
        self.error = error

    async def settle(self, trade_id: int, result: lc.CostResult) -> bool:
        raise self.error


async def test_a_failing_write_is_retried_then_needs_an_operator_after_24_h() -> None:
    """Colleague review (P2): the read succeeds but the write keeps failing. The
    pass fails until the deadline, then the trade goes to an operator."""
    store = FailingStore(cw.PendingTrade(_facts(), NO_NEIGHBOURS), RuntimeError("db down"))
    worker, _ = _worker(store, FakeSource(_round_trip()), now=T_EXIT + timedelta(hours=1))
    with pytest.raises(RuntimeError, match="db down"):
        await worker.settle(5)
    assert store.incomplete == []

    late, _ = _worker(store, FakeSource(_round_trip()), now=T_EXIT + timedelta(hours=24))
    with patch("schurfer_execution.cost_worker.notify.notify_alert", AsyncMock()) as alert:
        assert await late.settle(5) == lc.STATUS_INCOMPLETE
    assert "writing failed" in store.incomplete[0]
    alert.assert_awaited_once()


async def test_an_execution_of_another_trade_goes_to_an_operator_at_once() -> None:
    claimed = cw.ExecutionClaimedError("execution e1 is already trade 4's")
    store = FailingStore(cw.PendingTrade(_facts(), NO_NEIGHBOURS), claimed)
    worker, _ = _worker(store, FakeSource(_round_trip()), now=T_EXIT + timedelta(minutes=6))
    with patch("schurfer_execution.cost_worker.notify.notify_alert", AsyncMock()):
        assert await worker.settle(5) == lc.STATUS_INCOMPLETE
    assert "already trade 4's" in store.incomplete[0]


async def test_a_trade_without_an_entry_attempt_or_a_source_needs_an_operator() -> None:
    with patch("schurfer_execution.cost_worker.notify.notify_alert", AsyncMock()):
        worker, _ = _worker(FakeStore(None), FakeSource([]), now=T_EXIT)
        assert await worker.settle(5) == lc.STATUS_INCOMPLETE

        store = FakeStore(cw.PendingTrade(_facts(exchange="bingx"), NO_NEIGHBOURS))
        worker, _ = _worker(store, FakeSource([]), now=T_EXIT)
        assert await worker.settle(5) == lc.STATUS_INCOMPLETE
        assert "bingx" in store.incomplete[0]


# --- Postgres ----------------------------------------------------------------------

TEST_DATABASE_URL = integration_database_url()


async def _connect_or_skip() -> psycopg.AsyncConnection:
    assert_active_test_database_url(TEST_DATABASE_URL)
    try:
        connection = await psycopg.AsyncConnection.connect(TEST_DATABASE_URL, autocommit=True)
        cursor = await connection.execute("SELECT to_regclass('app.trade_cost_legs')")
        row = await cursor.fetchone()
        if row is None or row[0] is None:
            await connection.close()
            pytest.skip("trade_cost_legs is not migrated")
        return connection
    except Exception as exc:
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise RuntimeError(
                f"REQUIRE_INTEGRATION_DB=1 but PostgreSQL/head is unavailable: {exc}"
            ) from exc
        pytest.skip(f"no local postgres/head reachable: {exc}")


async def _closed_live_trade(
    connection: psycopg.AsyncConnection, exchange: str, entry_cid: str, prefix: str = ""
) -> int:
    """A live trade opened and closed through the journal, its entry attempt linked."""
    trade_id = await journal.open_trade(
        TEST_DATABASE_URL,
        symbol="BTC/USDT:USDT",
        exchange=exchange,
        side="long",
        order_id=f"{prefix}o-entry",
        size_usd=1000.0,
        leverage=1,
        entry_price=100.0,
        setup_context={},
        entry_at=T_ENTRY,
    )
    assert trade_id is not None
    await connection.execute(
        "INSERT INTO app.live_order_attempts ("
        "client_order_id, exchange, base, symbol, native_market_id, market_type, side, "
        "size_usd, requested_amount, leverage, contract_size, exit_params, setup_context, "
        "status, order_id, filled_amount, trade_id, created_at"
        ") VALUES (%s, %s, 'BTC', 'BTC/USDT:USDT', 'BTCUSDT', 'swap', 'long', 1000, 10, 1, 1, "
        "'{}'::jsonb, '{}'::jsonb, 'completed', %s, 10, %s, %s)",
        (entry_cid, exchange, f"{prefix}o-entry", trade_id, T_ENTRY - timedelta(seconds=1)),
    )
    outcome = await journal.close_trade(
        TEST_DATABASE_URL,
        trade_id=trade_id,
        exit_order_id=f"{prefix}o-exit",
        exit_price=101.0,
        reason="test",
        executed_at=T_EXIT,
    )
    assert outcome.committed and outcome.accounting_status == "pending"
    return trade_id


async def _cleanup(connection: psycopg.AsyncConnection, exchange: str) -> None:
    for table in ("trade_cost_legs", "live_order_attempts", "trades"):
        await connection.execute(f"DELETE FROM app.{table} WHERE exchange = %s", (exchange,))  # noqa: S608
    await connection.close()


async def _blocks_entries(connection: psycopg.AsyncConnection, exchange: str) -> bool:
    """The tracker's own condition, scoped to this test's rows: other tests' rows in
    the shared database must not decide it."""
    snapshot = await journal.pnl_snapshot(TEST_DATABASE_URL)
    assert snapshot is not None
    cursor = await connection.execute(
        "SELECT 1 FROM app.trades WHERE exchange = %s AND status = 'closed' "
        "AND accounting_version = 'live_exchange_costs_v1' "
        "AND accounting_status IN ('pending', 'incomplete')",
        (exchange,),
    )
    mine = await cursor.fetchone() is not None
    if mine:
        assert snapshot.blocker is not None
    return mine


async def test_settlement_on_postgres_unblocks_entries_and_is_written_once() -> None:
    connection = await _connect_or_skip()
    exchange = f"itest-{uuid.uuid4().hex[:12]}"
    try:
        generation = await journal.pnl_generation(TEST_DATABASE_URL)
        assert generation is not None
        trade_id = await _closed_live_trade(connection, exchange, ENTRY)
        assert await journal.pnl_generation(TEST_DATABASE_URL) == generation + 1
        assert await _blocks_entries(connection, exchange)

        store = cw.PostgresCostStore(TEST_DATABASE_URL)
        now = T_EXIT + timedelta(minutes=6)
        assert trade_id in await store.pending_trade_ids(now - lc.SETTLE_DELAY)
        worker = cw.LiveCostWorker(
            {exchange: FakeSource(_round_trip())}, store, _rdb(), now=lambda: now
        )

        assert await worker.settle(trade_id) == lc.STATUS_COMPLETE

        cursor = await connection.execute(
            "SELECT accounting_status, gross_pnl_usd, fees_usd, funding_usd, net_pnl_usd, "
            "pnl_usd, net_pnl_pct, outcome_label, accounting_error FROM app.trades WHERE id = %s",
            (trade_id,),
        )
        assert await cursor.fetchone() == (
            "complete",
            Decimal("10.0000"),
            Decimal("1.1055"),
            Decimal("0.1000"),
            Decimal("8.7945"),
            Decimal("8.7945"),
            Decimal("0.8794"),  # 0.87945, rounded half to even
            "win",
            None,
        )
        cursor = await connection.execute(
            "SELECT exec_id, exec_type FROM app.trade_cost_legs WHERE trade_id = %s "
            "ORDER BY exec_id",
            (trade_id,),
        )
        assert await cursor.fetchall() == [("e1", "trade"), ("f1", "funding"), ("x1", "trade")]
        assert not await _blocks_entries(connection, exchange)
        assert await journal.pnl_generation(TEST_DATABASE_URL) == generation + 2

        # A rerun (a crash after the commit, before the worker moved on) writes nothing.
        result = lc.settle(
            (await store.load(trade_id)).facts,  # type: ignore[union-attr]
            _round_trip(),
            [T_SETTLEMENT],
            _BOTH_ORDERS,
            NO_NEIGHBOURS,
            published_to=_PUBLISHED_TO,
            now=now,
        )
        assert result.status == lc.STATUS_COMPLETE, result.error
        assert await store.settle(trade_id, result) is False
        cursor = await connection.execute(
            "SELECT count(*) FROM app.trade_cost_legs WHERE trade_id = %s", (trade_id,)
        )
        assert await cursor.fetchone() == (3,)
    finally:
        await _cleanup(connection, exchange)


async def test_store_loads_the_trades_orders_and_its_neighbours() -> None:
    connection = await _connect_or_skip()
    exchange = f"itest-{uuid.uuid4().hex[:12]}"
    other = "d" * 32
    try:
        trade_id = await _closed_live_trade(connection, exchange, ENTRY)
        neighbour_id = await _closed_live_trade(connection, exchange, other, prefix="n-")

        loaded = await cw.PostgresCostStore(TEST_DATABASE_URL).load(trade_id)

        assert loaded is not None
        assert loaded.facts.entry_client_order_id == ENTRY
        assert loaded.facts.entry_filled == Decimal(10)
        assert loaded.facts.order_ids == frozenset({"o-entry", "o-exit"})
        assert loaded.neighbours.entry_client_order_ids == frozenset({other})
        assert loaded.neighbours.order_ids == frozenset({"n-o-entry", "n-o-exit"})
        assert len(loaded.neighbours.intervals) == 1
        assert neighbour_id != trade_id
    finally:
        await _cleanup(connection, exchange)


async def test_an_execution_claimed_by_another_trade_is_never_counted_twice() -> None:
    connection = await _connect_or_skip()
    exchange = f"itest-{uuid.uuid4().hex[:12]}"
    try:
        first = await _closed_live_trade(connection, exchange, ENTRY)
        store = cw.PostgresCostStore(TEST_DATABASE_URL)
        now = T_EXIT + timedelta(minutes=6)
        worker = cw.LiveCostWorker(
            {exchange: FakeSource(_round_trip())}, store, _rdb(), now=lambda: now
        )
        assert await worker.settle(first) == lc.STATUS_COMPLETE
        # Opened after the first settled: overlapping it, it would have been a second
        # candidate for the first trade's funding.
        second = await _closed_live_trade(connection, exchange, "e" * 32, prefix="s-")

        loaded = await store.load(first)
        assert loaded is not None
        result = lc.settle(
            loaded.facts,
            _round_trip(),
            [T_SETTLEMENT],
            _BOTH_ORDERS,
            NO_NEIGHBOURS,
            published_to=_PUBLISHED_TO,
            now=now,
        )
        assert result.status == lc.STATUS_COMPLETE, result.error
        with pytest.raises(cw.ExecutionClaimedError, match="already trade"):
            await store.settle(second, result)
        cursor = await connection.execute(
            "SELECT accounting_status FROM app.trades WHERE id = %s", (second,)
        )
        assert await cursor.fetchone() == ("pending",)
    finally:
        await _cleanup(connection, exchange)


async def test_operator_settlement_lifts_the_block() -> None:
    connection = await _connect_or_skip()
    exchange = f"itest-{uuid.uuid4().hex[:12]}"
    try:
        trade_id = await _closed_live_trade(connection, exchange, ENTRY)
        store = cw.PostgresCostStore(TEST_DATABASE_URL)
        assert await store.mark_incomplete(trade_id, "funding at a boundary") is True
        assert await store.mark_incomplete(trade_id, "again") is False
        assert await _blocks_entries(connection, exchange)

        parser = cw._build_arg_parser()
        command = [
            "settle",
            "--trade-id",
            str(trade_id),
            "--gross-usd",
            "10",
            "--fees-usd",
            "1.1",
            "--funding-usd",
            "0.1",
        ]
        with pytest.raises(SystemExit, match="--note"):
            await cw._run_settle(parser.parse_args([*command, "--note", " "]), TEST_DATABASE_URL)
        await cw._run_settle(
            parser.parse_args([*command, "--note", "checked against Bybit closed PnL"]),
            TEST_DATABASE_URL,
        )

        cursor = await connection.execute(
            "SELECT accounting_status, net_pnl_usd, pnl_usd, accounting_error "
            "FROM app.trades WHERE id = %s",
            (trade_id,),
        )
        assert await cursor.fetchone() == (
            "operator_settled",
            Decimal("8.8000"),
            Decimal("8.8000"),
            "settled by an operator: checked against Bybit closed PnL",
        )
        assert not await _blocks_entries(connection, exchange)
        assert (
            await store.operator_settle(
                trade_id,
                gross=Decimal(1),
                fees=Decimal(0),
                funding=Decimal(0),
                entry_value=Decimal(1000),
                note="twice",
            )
            is False
        )
    finally:
        await _cleanup(connection, exchange)
