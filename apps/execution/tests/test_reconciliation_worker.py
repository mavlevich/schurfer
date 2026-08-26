from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, call

from schurfer_execution import reconciliation as rec
from schurfer_execution.reconciliation_worker import (
    _OPEN_TRADES_SQL,
    ReconciliationWorker,
    _attempt_state_from_order,
)
from schurfer_execution.supervisor import SUBMISSION_UNKNOWN_BLOCKER, WorkerReadinessGate


def _identity() -> rec.FullIdentity:
    return rec.FullIdentity("bybit", "BTC/USDT:USDT", "BTCUSDT", "swap", "long")


def _attempt(*, status: str) -> rec.OrderAttemptSnapshot:
    return rec.OrderAttemptSnapshot(
        identity=_identity(),
        attempt_id=7,
        base="BTC",
        client_order_id="client-7",
        order_id=None,
        status=status,
        requested_amount=Decimal("1"),
        filled_amount=None,
        trade_id=None,
    )


async def test_client_order_id_match_is_exact() -> None:
    exchange = MagicMock()
    exchange.has = {"fetchOpenOrders": True, "fetchClosedOrders": True}
    exchange.fetch_open_orders = AsyncMock(
        return_value=[{"id": "wrong", "clientOrderId": "someone-else"}]
    )
    expected = {"id": "order-7", "info": {"orderLinkId": "client-7"}}
    exchange.fetch_closed_orders = AsyncMock(return_value=[expected])
    worker = ReconciliationWorker(
        {"bybit": exchange}, "postgresql://test", MagicMock(), WorkerReadinessGate(set())
    )

    order = await worker._find_exact_order(exchange, _attempt(status="submission_unknown"))

    assert order == expected


def test_terminal_order_without_filled_amount_stays_manual() -> None:
    assert _attempt_state_from_order({"status": "closed"}) == (
        "manual_required",
        None,
        "terminal exchange order omitted filled amount",
    )
    assert _attempt_state_from_order({"status": "closed", "filled": 0}) == (
        "no_fill",
        Decimal(0),
        None,
    )
    assert _attempt_state_from_order({"status": "closed", "filled": "0.25"}) == (
        "completed",
        Decimal("0.25"),
        None,
    )


async def test_opened_at_without_trade_pointer_is_quarantinable_redis_state() -> None:
    rdb = MagicMock()
    rdb.scan = AsyncMock(
        side_effect=[
            (0, []),
            (0, [b"position:opened_at:bybit:BTC"]),
        ]
    )
    rdb.get = AsyncMock(return_value=None)
    worker = ReconciliationWorker(
        {"bybit": MagicMock()},
        "postgresql://test",
        rdb,
        WorkerReadinessGate(set()),
    )

    snapshots = await worker._fetch_redis_snapshots({})

    assert len(snapshots) == 1
    snapshot = next(iter(snapshots.values()))
    assert snapshot.base == "BTC"
    assert not snapshot.identity.complete
    assert rdb.scan.await_args_list == [
        call(0, match="trade:id:bybit:*"),
        call(0, match="position:opened_at:bybit:*"),
    ]


def test_open_trade_query_excludes_paper_ledger_rows() -> None:
    assert "setup_context->>'paper'" in _OPEN_TRADES_SQL


async def test_successful_clean_scan_clears_startup_and_unknown_blockers() -> None:
    identity = _identity()
    exchange_snap = rec.ExchangePositionSnapshot(identity, Decimal("1"), Decimal("100"), {})
    trade = rec.OwnedTradeSnapshot(identity, 9, 7, "BTC", Decimal("1"), Decimal("100"))
    attempt = _attempt(status="completed")
    attempt = rec.OrderAttemptSnapshot(**{**attempt.__dict__, "trade_id": 9})
    redis = rec.RedisPositionSnapshot(identity, 9, "bybit", "BTC")
    gate = WorkerReadinessGate(set())
    gate.set_safety_blocker(rec.STARTUP_BLOCKER)
    gate.set_safety_blocker(SUBMISSION_UNKNOWN_BLOCKER)
    worker = ReconciliationWorker({}, "postgresql://test", MagicMock(), gate)
    worker._fetch_exchange_snapshots = AsyncMock(return_value={identity: exchange_snap})
    worker._fetch_db_snapshots = AsyncMock(
        return_value=({identity: trade}, {identity: [attempt]}, {9: trade})
    )
    worker._fetch_redis_snapshots = AsyncMock(return_value={identity: redis})
    worker._resolve_attempts = AsyncMock()
    worker._resolve_absent_incidents = AsyncMock(return_value=0)

    assert await worker._run_scan()
    assert gate.is_open()[0]
    assert gate.get_reasons() == []


async def test_source_failure_keeps_startup_closed() -> None:
    gate = WorkerReadinessGate(set())
    gate.set_safety_blocker(rec.STARTUP_BLOCKER)
    worker = ReconciliationWorker({}, "postgresql://test", MagicMock(), gate)
    worker._fetch_exchange_snapshots = AsyncMock(side_effect=OSError("exchange unavailable"))

    assert not await worker._run_scan()
    assert not gate.is_open()[0]
    assert gate.get_reasons() == [rec.SOURCE_BLOCKER, rec.STARTUP_BLOCKER]


async def test_submission_unknown_to_no_fill_clears_blocker() -> None:
    identity = _identity()
    attempt = _attempt(status="submission_unknown")
    gate = WorkerReadinessGate(set())
    gate.set_safety_blocker(rec.STARTUP_BLOCKER)

    exchange = MagicMock()
    exchange.has = {"fetchOpenOrders": True, "fetchClosedOrders": True}
    exchange.fetch_open_orders = AsyncMock(return_value=[])
    # Return a terminal order with 0 filled to prove the timeout resulted in no position
    terminal_order = {
        "id": "order-7",
        "status": "closed",
        "filled": 0,
        "info": {"orderLinkId": "client-7"},
    }
    exchange.fetch_closed_orders = AsyncMock(return_value=[terminal_order])

    worker = ReconciliationWorker({"bybit": exchange}, "postgresql://test", MagicMock(), gate)

    # Mock network fetching methods
    worker._fetch_exchange_snapshots = AsyncMock(return_value={})
    # DB snapshot yields the unresolved attempt initially
    worker._fetch_db_snapshots = AsyncMock(return_value=({}, {identity: [attempt]}, {}))
    worker._fetch_redis_snapshots = AsyncMock(return_value={})

    # Needs to bypass actual DB UPDATE since we're not running integration DB test
    worker._update_attempt_from_order = AsyncMock(
        return_value=rec.OrderAttemptSnapshot(
            **{**attempt.__dict__, "status": "no_fill", "filled_amount": Decimal("0")}
        )
    )
    worker._resolve_absent_incidents = AsyncMock(return_value=0)

    # 1. Run scan
    success = await worker._run_scan()

    assert success is True
    # Worker looked for the order
    exchange.fetch_closed_orders.assert_called_once()
    worker._update_attempt_from_order.assert_called_once()

    # Gate should be fully open: STARTUP and SUBMISSION_UNKNOWN cleared
    is_open, _ = gate.is_open()
    assert is_open is True
    assert gate.get_reasons() == []
