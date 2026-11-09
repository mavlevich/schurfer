"""Probe loss decisions, durable retries and monitor startup/exit boundaries."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from schurfer_execution import live_costs as lc
from schurfer_execution import probe_close, probe_loss
from schurfer_execution import probe_evaluator as pe

from .test_live_costs import NO_NEIGHBOURS, T_EXIT, _facts

D = Decimal
NOW = T_EXIT + timedelta(minutes=1)
POSITION = pe.ProbePosition(
    probe_id="probe",
    trade_id=1,
    native_market_id="BTCUSDT",
    symbol="BTC/USDT:USDT",
    market_type="swap",
    side="long",
    exchange="bybit",
    quantity=D(80),
    taker_rate=D(".001"),
    threshold=D(5),
    realized_closed=D(0),
)
OPEN = probe_loss.OpenPosition("long", D(1), D(80), D(-2), D(".1"), D(0))
GOOD = lc.OpenCostResult(
    lc.STATUS_UNPROVEN_TAIL, position=OPEN, published_to=NOW - timedelta(hours=1)
)


def evaluator(monkeypatch, *, position=POSITION, result=GOOD):
    recovery = asyncio.Event()
    recovery.set()
    ex = SimpleNamespace(
        id="bybit",
        markets={
            POSITION.symbol: {
                "id": POSITION.native_market_id,
                "symbol": POSITION.symbol,
                "base": "BTC",
                "quote": "USDT",
                "settle": "USDT",
                "type": "swap",
            }
        },
        fetch_positions=AsyncMock(
            return_value=[
                {"symbol": POSITION.symbol, "side": "long", "contracts": "80", "markPrice": ".9"}
            ]
        ),
        publicGetV5MarketTickers=AsyncMock(
            return_value={
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "symbol": POSITION.native_market_id,
                            "nextFundingTime": str(
                                int((NOW + timedelta(hours=1)).timestamp() * 1000)
                            ),
                            "fundingRate": ".001",
                        }
                    ]
                },
            }
        ),
    )
    store = SimpleNamespace(
        positions=AsyncMock(return_value=[position]),
        load=AsyncMock(
            return_value=SimpleNamespace(facts=_facts(exit_at=None), neighbours=NO_NEIGHBOURS)
        ),
        incident=AsyncMock(return_value=True),
    )
    clock = [NOW]
    record = AsyncMock(return_value=probe_close.LReached(True, []))
    read = AsyncMock(return_value=result)
    monkeypatch.setattr(lc, "read_open", read)
    gate = Mock()
    worker = pe.ProbeLossEvaluator(
        "db", {"bybit": ex}, store, Mock(), gate, recovery, now=lambda: clock[0], record=record
    )
    return worker, store, ex, gate, record, read, clock


@pytest.mark.asyncio
async def test_lower_bound_crossing_l_latches_before_persistence(monkeypatch):
    worker, store, _ex, gate, record, read, _clock = evaluator(monkeypatch)

    async def write(*args, **kwargs):
        gate.set_safety_blocker.assert_called_once_with(pe.LATCH_BLOCKER)
        assert kwargs["loss_usd"] == D("10.172")  # partial loss, mark and future exit fee
        assert kwargs["inputs"]["lower_bound"] is True
        assert kwargs["inputs"]["terms"]["realized_open_parts"] == "-2"
        return probe_close.LReached(True, [])

    record.side_effect = write
    assert await worker.run_once()
    gate.clear_safety_blocker.assert_called_once_with(pe.LATCH_BLOCKER)
    assert read.await_args.kwargs["journal_quantity"] == 80
    store.incident.assert_not_called()


@pytest.mark.asyncio
async def test_failed_decision_keeps_first_inputs_after_price_recovers(monkeypatch):
    worker, _store, ex, gate, record, _read, _clock = evaluator(monkeypatch)
    record.side_effect = [RuntimeError("DB down"), probe_close.LReached(True, [])]
    assert not await worker.run_once()
    first = record.await_args.kwargs.copy()
    assert "probe" in worker.pending
    gate.clear_safety_blocker.assert_not_called()
    ex.fetch_positions.return_value[0]["markPrice"] = "2"
    assert await worker.run_once()
    assert record.await_args.kwargs == first
    assert ex.fetch_positions.await_count == 1  # retry is independent of today's price
    assert not worker.pending


@pytest.mark.asyncio
async def test_standing_request_needs_neither_a_fresh_mark_nor_a_cost_read(monkeypatch):
    p = replace(POSITION, reached_loss=D(7), reached_inputs={"original": True})
    worker, _store, ex, _gate, record, read, _clock = evaluator(monkeypatch, position=p)
    assert await worker.run_once()
    record.assert_awaited_once()
    assert record.await_args.kwargs == {"loss_usd": D(7), "inputs": {"original": True}}
    read.assert_not_called()
    ex.fetch_positions.assert_not_called()


@pytest.mark.asyncio
async def test_busy_order_lock_keeps_request_for_next_tick(monkeypatch):
    p = replace(POSITION, reached_loss=D(7), reached_inputs={"mark": "old"})
    worker, _store, _ex, gate, record, _read, _clock = evaluator(monkeypatch, position=p)
    record.side_effect = [probe_close.OrderLockBusyError("busy"), probe_close.LReached(False, [])]
    assert not await worker.run_once()
    gate.clear_safety_blocker.assert_not_called()
    assert await worker.run_once()
    assert record.await_count == 2


@pytest.mark.asyncio
async def test_recovery_must_finish_before_any_probe_read(monkeypatch):
    worker, store, ex, _gate, record, read, _clock = evaluator(monkeypatch)
    worker.recovered.clear()
    assert await worker.run_once()
    store.positions.assert_not_called()
    ex.fetch_positions.assert_not_called()
    record.assert_not_called()
    read.assert_not_called()


@pytest.mark.asyncio
async def test_no_probe_link_means_no_exchange_reads_or_closes(monkeypatch):
    worker, store, ex, _gate, record, read, _clock = evaluator(monkeypatch)
    store.positions.return_value = []
    assert await worker.run_once()
    ex.fetch_positions.assert_not_called()
    record.assert_not_called()
    read.assert_not_called()


@pytest.mark.asyncio
async def test_pending_costs_and_missing_mark_alert_after_three_ticks(monkeypatch):
    worker, _store, ex, _gate, record, _read, _clock = evaluator(
        monkeypatch, result=lc.OpenCostResult(lc.STATUS_PENDING, error="rows late")
    )
    ex.fetch_positions.return_value[0]["markPrice"] = None
    alert = AsyncMock()
    worker._alert_once = alert
    for _ in range(2):
        assert await worker.run_once()
    alert.assert_not_called()
    assert await worker.run_once()
    assert alert.await_args.args[0] == "mark:1"
    record.assert_not_called()


@pytest.mark.asyncio
async def test_slow_collector_cannot_decide_from_a_stale_mark(monkeypatch):
    worker, _store, _ex, _gate, record, read, clock = evaluator(monkeypatch)

    async def slow(*args, **kwargs):
        clock[0] += timedelta(seconds=61)
        return GOOD

    read.side_effect = slow
    assert await worker.run_once()
    record.assert_not_called()
    assert worker.stale[1] == 1


@pytest.mark.asyncio
async def test_incomplete_evidence_is_an_incident_never_an_l_close(monkeypatch):
    worker, store, _ex, _gate, record, _read, _clock = evaluator(
        monkeypatch, result=lc.OpenCostResult(lc.STATUS_INCOMPLETE, error="foreign execution")
    )
    assert await worker.run_once()
    store.incident.assert_awaited_once_with("probe", "foreign execution")
    record.assert_not_called()


@pytest.mark.asyncio
async def test_quantity_mismatch_is_not_automatically_closed(monkeypatch):
    worker, store, ex, _gate, record, read, _clock = evaluator(monkeypatch)
    ex.fetch_positions.return_value[0]["contracts"] = "79"
    for _ in range(2):
        assert await worker.run_once()
        store.incident.assert_not_called()
    assert await worker.run_once()
    assert "quantity" in store.incident.await_args.args[1]
    record.assert_not_called()
    read.assert_not_called()


@pytest.mark.asyncio
async def test_costs_incident_failure_does_not_prevent_a_proven_l_decision(monkeypatch):
    result = replace(GOOD, published_to=NOW - timedelta(hours=9))
    worker, store, _ex, _gate, record, _read, _clock = evaluator(monkeypatch, result=result)
    store.incident.side_effect = RuntimeError("DB down for incident")
    assert not await worker.run_once()
    record.assert_awaited_once()
    assert not worker.pending  # the standing close task was already persisted


@pytest.mark.asyncio
async def test_funding_delay_is_reported_even_with_a_stale_mark(monkeypatch):
    settlement = probe_loss.UnlistedSettlement(NOW - timedelta(minutes=31), None, D(80), None)
    result = replace(GOOD, position=replace(OPEN, unlisted=(settlement,)))
    worker, store, ex, _gate, record, _read, _clock = evaluator(monkeypatch, result=result)
    ex.fetch_positions.return_value[0]["markPrice"] = "0"
    assert await worker.run_once()
    assert "30 minutes" in store.incident.await_args.args[1]
    record.assert_not_called()


@pytest.mark.asyncio
async def test_below_l_is_only_a_lower_bound_not_a_new_permission(monkeypatch):
    worker, _store, _ex, gate, record, _read, _clock = evaluator(
        monkeypatch, position=replace(POSITION, threshold=D(50))
    )
    assert await worker.run_once()
    record.assert_not_called()
    gate.clear_safety_blocker.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("rate", [None, D("-0.1"), D("NaN")])
async def test_missing_persisted_preflight_rate_does_not_use_the_reserve(monkeypatch, rate):
    worker, store, ex, _gate, record, read, _clock = evaluator(
        monkeypatch, position=replace(POSITION, taker_rate=rate)
    )
    assert await worker.run_once()
    assert "preflight" in store.incident.await_args.args[1]
    ex.fetch_positions.assert_not_called()
    record.assert_not_called()
    read.assert_not_called()


@pytest.mark.asyncio
async def test_the_preflight_observation_survives_the_first_tick_after_settlement(monkeypatch):
    before = lc.FundingSettlement(NOW - timedelta(seconds=10), D(".002"))
    worker, _store, _ex, _gate, _record, read, _clock = evaluator(
        monkeypatch, position=replace(POSITION, threshold=D(50), initial_funding=before)
    )
    assert await worker.run_once()
    assert before in read.await_args.kwargs["observed"]


@pytest.mark.asyncio
async def test_an_observation_after_settlement_cannot_overwrite_its_previous_rate(monkeypatch):
    worker, _store, ex, _gate, _record, read, clock = evaluator(
        monkeypatch, position=replace(POSITION, threshold=D(50))
    )
    assert await worker.run_once()
    at = next(iter(worker.observed[1]))
    clock[0] = at + timedelta(seconds=1)
    ex.publicGetV5MarketTickers.return_value["result"]["list"][0]["fundingRate"] = ".02"
    assert await worker.run_once()
    assert worker.observed[1][at].rate == D(".001")
    assert lc.FundingSettlement(at, D(".001")) in read.await_args.kwargs["observed"]


@pytest.mark.asyncio
async def test_an_incident_reopened_after_operator_clear_alerts_again(monkeypatch):
    worker, store, _ex, _gate, _record, _read, _clock = evaluator(
        monkeypatch, result=lc.OpenCostResult(lc.STATUS_INCOMPLETE, error="foreign execution")
    )
    store.incident.side_effect = [True, False, True]
    worker.alert_credentials = ("fake", "fake")
    alert = AsyncMock(return_value=True)
    monkeypatch.setattr(pe.notify, "notify_alert", alert)
    for _ in range(3):
        assert await worker.run_once()
    assert alert.await_count == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"taker_rate": "NaN"},
        {"taker_rate": "bad"},
        {},
    ],
)
def test_malformed_preflight_rates_are_never_used(changes):
    assert pe._preflight_taker(changes) is None


def test_only_an_observation_read_before_sending_and_before_settlement_is_seeded():
    at = NOW + timedelta(seconds=10)
    preflight = {
        "next_funding_at": at.isoformat(),
        "read_at": NOW.isoformat(),
        "funding_rate": ".001",
    }
    assert pe._preflight_funding(preflight, NOW) == lc.FundingSettlement(at, D(".001"))
    assert pe._preflight_funding(preflight, NOW - timedelta(seconds=1)) is None
    preflight["read_at"] = at.isoformat()
    assert pe._preflight_funding(preflight, at) is None


@pytest.mark.asyncio
async def test_collector_network_failure_is_visible_in_monitor_readiness(monkeypatch):
    worker, _store, _ex, _gate, record, read, _clock = evaluator(monkeypatch)
    read.side_effect = RuntimeError("funding endpoint unavailable")
    assert not await worker.run_once()
    record.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("in_memory", [False, True])
async def test_hung_close_request_is_bounded_and_keeps_the_first_decision(monkeypatch, in_memory):
    position = replace(POSITION, reached_loss=D(7), reached_inputs={"first_mark": "old"})
    worker, _store, ex, gate, record, _read, _clock = evaluator(monkeypatch, position=position)
    monkeypatch.setattr(pe, "_REQUEST_TIMEOUT_SECONDS", 0.01, raising=False)
    if in_memory:
        worker._latch("probe", D(7), {"first_mark": "old"})

    async def stuck(*args, **kwargs):
        await asyncio.Event().wait()

    record.side_effect = stuck
    assert not await asyncio.wait_for(worker.run_once(), timeout=0.2)
    assert worker.pending == {"probe": (D(7), {"first_mark": "old"})}
    gate.clear_safety_blocker.assert_not_called()
    ex.fetch_positions.assert_not_called()
    record.side_effect = None
    record.return_value = probe_close.LReached(False, [])
    assert await worker.run_once()
    assert record.await_args.kwargs == {"loss_usd": D(7), "inputs": {"first_mark": "old"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_read", ["ticker", "collector"])
async def test_timed_out_reads_still_count_missing_marks_and_alert_once(monkeypatch, blocked_read):
    worker, _store, ex, _gate, record, read, _clock = evaluator(monkeypatch)
    worker.alert_credentials = ("fake", "fake")
    alert = AsyncMock(return_value=True)
    monkeypatch.setattr(pe.notify, "notify_alert", alert)
    original_wait_for = asyncio.wait_for

    def short_evaluation(awaitable, timeout):
        return original_wait_for(awaitable, timeout=0.01 if timeout == 45 else timeout)

    monkeypatch.setattr(pe.asyncio, "wait_for", short_evaluation)

    async def stuck(*args, **kwargs):
        await asyncio.Event().wait()

    if blocked_read == "ticker":
        ex.publicGetV5MarketTickers.side_effect = stuck
    else:
        ex.fetch_positions.return_value[0]["markPrice"] = None
        read.side_effect = stuck
    for _ in range(4):
        assert not await worker.run_once()
    assert worker.stale[1] == 4
    alert.assert_awaited_once()
    assert "three monitor ticks" in alert.await_args.kwargs["text"]
    record.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot", ["flat", "smaller", "collector"])
async def test_review_one_tick_stop_race_then_closed_trade_is_not_an_incident(
    monkeypatch, snapshot
):
    worker, store, ex, _gate, record, read, _clock = evaluator(monkeypatch)
    if snapshot == "flat":
        ex.fetch_positions.return_value = []
    elif snapshot == "smaller":
        ex.fetch_positions.return_value[0]["contracts"] = "79"
    else:
        from .test_live_costs import _entry, _exit, _orders
        from .test_open_costs import collect

        read.return_value = collect(
            rows=[_entry(), _exit("21")],
            orders=_orders(("o-entry", "100"), ("o-exit", "21")),
            journal_quantity=D(80),
        )
    assert await worker.run_once()
    store.incident.assert_not_called()
    record.assert_not_called()
    store.positions.return_value = []
    assert await worker.run_once()
    store.incident.assert_not_called()


@pytest.mark.asyncio
async def test_review_failure_then_success_sends_both_phase_alerts(monkeypatch):
    worker, _store, _ex, _gate, record, _read, _clock = evaluator(monkeypatch)
    worker.alert_credentials = ("fake", "fake")
    alert = AsyncMock(return_value=True)
    monkeypatch.setattr(pe.notify, "notify_alert", alert)
    outcome = probe_close.CloseOutcome("probe", 1, 42, True)
    record.side_effect = [RuntimeError("DB unavailable"), probe_close.LReached(True, [outcome])]
    assert not await worker.run_once()
    assert await worker.run_once()
    texts = [call.kwargs["text"] for call in alert.await_args_list]
    assert len(texts) == 2
    assert "not confirmed" in texts[0]
    assert "recorded" in texts[1] and "42" in texts[1]


@pytest.mark.asyncio
async def test_review_pending_close_then_created_task_is_not_silently_deduplicated(monkeypatch):
    p = replace(POSITION, reached_loss=D(7), reached_inputs={"mark": "old"})
    worker, _store, _ex, _gate, record, _read, _clock = evaluator(monkeypatch, position=p)
    worker.alert_credentials = ("fake", "fake")
    alert = AsyncMock(return_value=True)
    monkeypatch.setattr(pe.notify, "notify_alert", alert)
    record.side_effect = [
        probe_close.LReached(
            False, [probe_close.CloseOutcome("probe", 1, None, False, "entry fill not final", True)]
        ),
        probe_close.LReached(False, [probe_close.CloseOutcome("probe", 1, 42, True)]),
    ]
    assert await worker.run_once()
    assert await worker.run_once()
    texts = [c.kwargs["text"] for c in alert.await_args_list]
    assert len(texts) == 2
    assert "pending" in texts[0] and "emergency tasks []" not in texts[0]
    assert "42" in texts[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "hang"])
async def test_review_loss_worker_failure_does_not_delay_or_restart_protection(
    monkeypatch, failure
):
    from schurfer_execution import monitor
    from schurfer_execution.supervisor import WorkerRestartPolicy, WorkerSpec, WorkerSupervisor

    from .test_supervisor import _wait_until

    worker, _store, _ex, _gate, _record, _read, _clock = evaluator(monkeypatch)
    worker.interval_seconds = 0.002
    monkeypatch.setattr(pe, "_PASS_TIMEOUT_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(monitor, "_INTERVAL_SECONDS", 0.002)
    tick = AsyncMock()
    monkeypatch.setattr(monitor, "_tick", tick)
    broken = False

    async def run():
        if not broken:
            return True
        if failure == "error":
            raise RuntimeError("cost endpoint unavailable")
        await asyncio.Event().wait()
        return False

    worker.run_once = run
    supervisor = WorkerSupervisor(
        [
            WorkerSpec(
                "position_monitor",
                lambda tr: monitor.run_position_monitor({}, Mock(), Mock(), tr),
                WorkerRestartPolicy.BOUNDED_FATAL,
                True,
                restart_budget=3,
                stale_timeout_seconds=2,
            ),
            WorkerSpec(
                "live_probe_loss",
                lambda tr: worker(tr),
                WorkerRestartPolicy.ALWAYS,
                True,
                stale_timeout_seconds=2,
            ),
        ],
        monitor_interval_seconds=0.002,
        restart_backoff_base_seconds=0.002,
        restart_backoff_max_seconds=0.01,
    )
    try:
        await supervisor.start()
        await _wait_until(lambda: supervisor.gate.is_open()[0], timeout=2)
        before = tick.await_count
        broken = True
        await _wait_until(
            lambda: len(supervisor.workers["live_probe_loss"].restarts) >= 1, timeout=2
        )
        assert not supervisor.gate.is_open()[0]
        assert tick.await_count > before + 2
        assert supervisor.workers["position_monitor"].restarts == []
        assert supervisor.workers["position_monitor"].tracker.consecutive_failures == 0
        broken = False
        await _wait_until(lambda: supervisor.gate.is_open()[0], timeout=2)
    finally:
        supervisor.stop()
        await supervisor.wait_stopped()


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot", ["flat", "smaller", "collector"])
async def test_review_persistent_quantity_difference_only_incidents_on_third_tick(
    monkeypatch, snapshot
):
    worker, store, ex, _gate, record, read, _clock = evaluator(monkeypatch)
    if snapshot == "flat":
        ex.fetch_positions.return_value = []
    elif snapshot == "smaller":
        ex.fetch_positions.return_value[0]["contracts"] = "79"
    else:
        from .test_live_costs import _entry, _exit, _orders
        from .test_open_costs import collect

        read.return_value = collect(
            rows=[_entry(), _exit("21")],
            orders=_orders(("o-entry", "100"), ("o-exit", "21")),
            journal_quantity=D(80),
        )
    for _ in range(2):
        assert await worker.run_once()
        store.incident.assert_not_called()
    assert await worker.run_once()
    store.incident.assert_awaited_once()
    assert "3 consecutive ticks" in store.incident.await_args.args[1]
    record.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", ["consistent", "pending", "scan_failure"])
async def test_review_quantity_streak_resets_after_an_intervening_pass(monkeypatch, interrupt):
    worker, store, ex, _gate, record, read, _clock = evaluator(
        monkeypatch, position=replace(POSITION, threshold=D(50))
    )
    ex.fetch_positions.return_value[0]["contracts"] = "79"
    assert await worker.run_once()
    ex.fetch_positions.return_value[0]["contracts"] = "80"
    if interrupt == "pending":
        read.return_value = lc.OpenCostResult(lc.STATUS_PENDING, error="fills pending")
    elif interrupt == "scan_failure":
        store.positions.side_effect = RuntimeError("DB read failed")
    assert await worker.run_once() is (interrupt != "scan_failure")
    store.positions.side_effect = None
    read.return_value = GOOD
    ex.fetch_positions.return_value[0]["contracts"] = "79"
    for _ in range(2):
        assert await worker.run_once()
        store.incident.assert_not_called()
    record.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["larger", "side", "route"])
async def test_review_unowned_exposure_is_an_immediate_incident(monkeypatch, problem):
    worker, store, ex, _gate, record, _read, _clock = evaluator(monkeypatch)
    if problem == "larger":
        ex.fetch_positions.return_value[0]["contracts"] = "81"
    elif problem == "side":
        ex.fetch_positions.return_value[0]["side"] = "short"
    else:
        ex.markets[POSITION.symbol]["symbol"] = "different-symbol"
    assert await worker.run_once()
    store.incident.assert_awaited_once()
    record.assert_not_called()


@pytest.mark.asyncio
async def test_review_moved_funding_does_not_create_a_thirty_minute_incident(monkeypatch):
    from .test_live_costs import SETTLEMENTS, _entry, _exit, _funding
    from .test_open_costs import collect

    at8, at9 = SETTLEMENTS[2:4]
    result = collect(
        rows=[_entry(), _exit("20"), _funding("f9", qty="100", at=at9, fee="0")],
        settlements=[lc.FundingSettlement(SETTLEMENTS[i], D(".001")) for i in (0, 1, 3)],
        published_to=at9,
        observed=(lc.FundingSettlement(at8, D(".01")),),
        marks={at8: D(1)},
    )
    worker, store, _ex, _gate, record, _read, _clock = evaluator(
        monkeypatch, position=replace(POSITION, threshold=D(50)), result=result
    )
    assert await worker.run_once()
    store.incident.assert_not_called()
    record.assert_not_called()


@pytest.mark.asyncio
async def test_review_already_closed_position_does_not_claim_a_pending_task(monkeypatch):
    worker, _store, _ex, _gate, record, _read, _clock = evaluator(monkeypatch)
    worker.alert_credentials = ("fake", "fake")
    alert = AsyncMock(return_value=True)
    monkeypatch.setattr(pe.notify, "notify_alert", alert)
    record.return_value = probe_close.LReached(True, [])
    assert await worker.run_once()
    text = alert.await_args.kwargs["text"]
    assert "no open probe position" in text
    assert "tasks []" not in text and "pending" not in text
