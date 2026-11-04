"""Real database + real Bybit read adapter + loss decision + durable close task."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from psycopg.types.json import Jsonb
from schurfer_execution import live_probe
from schurfer_execution import probe_evaluator as pe

from .test_live_probe_integration import INPUTS, TEST_DATABASE_URL, _cleanup, _issue, _query, _trade
from .test_probe_close_integration import LockRedis, _cleanup_tasks

__all__ = ["_cleanup", "_cleanup_tasks"]
D = Decimal


async def setup_probe():
    native = "TLP" + uuid.uuid4().hex[:8].upper() + "USDT"
    symbol = "TLP/USDT:USDT"
    probe = await _issue(instruments=(native,), loss_threshold_usd=D(1))
    reservation = await live_probe.reserve(
        TEST_DATABASE_URL,
        live_probe.EntryRequest(
            probe_id=probe,
            decision_id=uuid.uuid4().hex,
            environment="testnet",
            account_key="probe-sub-1",
            native_market_id=native,
            side="long",
            reserve_inputs=INPUTS,
        ),
    )
    assert isinstance(reservation, live_probe.Reservation)
    await live_probe.begin_sending(TEST_DATABASE_URL, reservation.id)
    trade = await _trade(status="open", pnl=None)
    now = datetime.now(tz=UTC)
    entered = now - timedelta(minutes=1)
    order = "tlp-entry-" + uuid.uuid4().hex
    await _query(
        "UPDATE app.trades SET exchange='bybit', symbol=%s, entry_price=1, "
        "entry_at=%s, entry_order_id=%s WHERE id=%s",
        (symbol, entered, order, trade),
    )
    rows = await _query(
        "INSERT INTO app.live_order_attempts (operation, client_order_id, exchange, base, "
        "symbol, native_market_id, market_type, side, size_usd, requested_amount, filled_amount, "
        "contract_size, leverage, exit_params, setup_context, status, trade_id, "
        "order_id, created_at) "
        "VALUES ('entry', %s, 'bybit', 'TLP', %s, %s, 'swap', 'long', 10, 10, 10, 1, 1, "
        "'{}'::jsonb, %s, 'completed', %s, %s, %s) RETURNING id",
        (
            reservation.client_order_id,
            symbol,
            native,
            Jsonb({"live_probe_test": True, "live_probe_preflight": {"taker_rate": "0.001"}}),
            trade,
            order,
            entered,
        ),
    )
    attempt = int(rows[0]["id"])
    await live_probe.link_attempt(TEST_DATABASE_URL, reservation.id, attempt)
    await live_probe.mark_placed(TEST_DATABASE_URL, reservation.id)
    await live_probe.link_trade(TEST_DATABASE_URL, reservation.id, trade)
    old = now - timedelta(hours=1)
    next_at = now + timedelta(hours=1)
    ex = SimpleNamespace(
        id="bybit",
        markets={
            symbol: {
                "id": native,
                "symbol": symbol,
                "base": "TLP",
                "quote": "USDT",
                "settle": "USDT",
                "type": "swap",
            }
        },
        fetch_positions=AsyncMock(
            return_value=[{"symbol": symbol, "side": "long", "contracts": "10", "markPrice": ".89"}]
        ),
        publicGetV5MarketTickers=AsyncMock(
            return_value={
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "symbol": native,
                            "nextFundingTime": str(int(next_at.timestamp() * 1000)),
                            "fundingRate": ".001",
                        }
                    ]
                },
            }
        ),
        publicGetV5MarketFundingHistory=AsyncMock(
            side_effect=lambda params: {
                "retCode": 0,
                "result": {
                    "list": []
                    if "startTime" in params
                    else [
                        {
                            "fundingRateTimestamp": str(int(old.timestamp() * 1000)),
                            "fundingRate": ".001",
                        }
                    ]
                },
            }
        ),
        privateGetV5ExecutionList=AsyncMock(
            return_value={
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "execId": uuid.uuid4().hex,
                            "execType": "Trade",
                            "orderId": order,
                            "orderLinkId": reservation.client_order_id,
                            "side": "Buy",
                            "execQty": "10",
                            "execPrice": "1",
                            "execValue": "10",
                            "execFee": ".01",
                            "feeCurrency": "USDT",
                            "execTime": str(int(entered.timestamp() * 1000)),
                        }
                    ]
                },
            }
        ),
        privateGetV5OrderHistory=AsyncMock(
            return_value={
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "orderId": order,
                            "orderStatus": "Filled",
                            "cumExecQty": "10",
                        }
                    ]
                },
            }
        ),
        create_order=AsyncMock(),
    )
    recovered = asyncio.Event()
    recovered.set()
    rdb = LockRedis()
    gate = Mock()
    worker = pe.ProbeLossEvaluator(
        TEST_DATABASE_URL,
        {"bybit": ex},
        pe.PostgresProbeStore(TEST_DATABASE_URL),
        rdb,
        gate,
        recovered,
        now=lambda: now,
    )
    return probe, trade, attempt, worker, ex, rdb, gate


@pytest.mark.asyncio
async def test_verified_open_loss_creates_one_task_without_an_exchange_send():
    probe, trade, _attempt, worker, ex, _rdb, gate = await setup_probe()
    assert await worker.run_once()
    rows = await _query(
        "SELECT l_reached_at, l_reached_loss_usd, l_reached_inputs "
        "FROM app.live_probe_authorizations WHERE probe_id=%s",
        (probe,),
    )
    assert rows[0]["l_reached_at"] is not None
    assert rows[0]["l_reached_loss_usd"] == D("1.1189")
    assert rows[0]["l_reached_inputs"]["mark"] == "0.89"
    tasks = await _query(
        "SELECT reason, owned_quantity, trade_id FROM app.live_emergency_closes WHERE trade_id=%s",
        (trade,),
    )
    assert tasks == [{"reason": "probe_loss_threshold", "owned_quantity": D(10), "trade_id": trade}]
    assert await worker.run_once()
    assert ex.fetch_positions.await_count == 1  # standing request doesn't re-evaluate price
    assert (
        len(await _query("SELECT id FROM app.live_emergency_closes WHERE trade_id=%s", (trade,)))
        == 1
    )
    ex.create_order.assert_not_called()
    gate.clear_safety_blocker.assert_called_with(pe.LATCH_BLOCKER)


@pytest.mark.asyncio
async def test_busy_lock_then_new_evaluator_recovers_the_standing_request():
    probe, trade, _attempt, worker, ex, rdb, gate = await setup_probe()
    rdb.keys["lock:order:bybit:TLP"] = "another-owner"
    assert not await worker.run_once()
    gate.clear_safety_blocker.assert_not_called()
    assert probe in worker.pending
    rows = await _query(
        "SELECT l_reached_loss_usd, l_reached_inputs "
        "FROM app.live_probe_authorizations WHERE probe_id=%s",
        (probe,),
    )
    original = rows[0]
    assert original["l_reached_loss_usd"] > 1
    assert not await _query("SELECT id FROM app.live_emergency_closes WHERE trade_id=%s", (trade,))
    rdb.keys.clear()
    worker = pe.ProbeLossEvaluator(
        TEST_DATABASE_URL,
        {"bybit": ex},
        pe.PostgresProbeStore(TEST_DATABASE_URL),
        rdb,
        gate,
        worker.recovered,
    )
    ex.fetch_positions.side_effect = RuntimeError("must not read a recovered price")
    assert await worker.run_once()
    assert await _query("SELECT id FROM app.live_emergency_closes WHERE trade_id=%s", (trade,))
    assert (
        await _query(
            "SELECT l_reached_loss_usd, l_reached_inputs "
            "FROM app.live_probe_authorizations WHERE probe_id=%s",
            (probe,),
        )
    )[0] == original
    ex.create_order.assert_not_called()


@pytest.mark.asyncio
async def test_quantity_mismatch_is_a_single_persistent_incident_never_a_close():
    probe, trade, _attempt, worker, ex, _rdb, _gate = await setup_probe()
    ex.fetch_positions.return_value[0]["contracts"] = "9"
    assert await worker.run_once()
    assert await worker.run_once()
    rows = await _query(
        "SELECT costs_incident_at, l_reached_at "
        "FROM app.live_probe_authorizations WHERE probe_id=%s",
        (probe,),
    )
    assert rows[0]["costs_incident_at"] is not None
    assert rows[0]["l_reached_at"] is None
    events = await _query(
        "SELECT event FROM app.live_probe_authorization_events "
        "WHERE probe_id=%s AND event='costs_incident'",
        (probe,),
    )
    assert len(events) == 1
    assert not await _query("SELECT id FROM app.live_emergency_closes WHERE trade_id=%s", (trade,))
    ex.create_order.assert_not_called()


@pytest.mark.asyncio
async def test_a_recovery_task_owns_the_position_until_accounting_finishes():
    from schurfer_execution import probe_close

    _probe, trade, _attempt, worker, ex, rdb, _gate = await setup_probe()
    result = await probe_close.recover_at_start(TEST_DATABASE_URL, rdb)
    assert not result.close_error
    assert any(c.task_id for c in result.closes)
    assert await worker.run_once()
    ex.fetch_positions.assert_not_called()
    ex.create_order.assert_not_called()
    assert (
        len(await _query("SELECT id FROM app.live_emergency_closes WHERE trade_id=%s", (trade,)))
        == 1
    )
