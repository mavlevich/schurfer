from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import Response
from schurfer_execution.routers.health import get_workers_health
from schurfer_execution.supervisor import (
    WorkerReadinessGate,
    WorkerRestartPolicy,
    WorkerSpec,
    WorkerState,
    WorkerSupervisor,
)


async def _wait_until(predicate: object, *, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if callable(predicate) and predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition was not met before timeout")


async def _block_forever() -> None:
    await asyncio.Event().wait()


def _spec(
    name: str,
    factory: object,
    *,
    policy: WorkerRestartPolicy = WorkerRestartPolicy.BOUNDED_DEGRADED,
    is_critical: bool = False,
    restart_budget: int = 3,
    stale_timeout_seconds: float = 1.0,
    max_consecutive_failures: int = 3,
    enabled: bool = True,
) -> WorkerSpec:
    return WorkerSpec(
        name=name,
        factory=factory,  # type: ignore[arg-type]
        policy=policy,
        is_critical=is_critical,
        restart_budget=restart_budget,
        stale_timeout_seconds=stale_timeout_seconds,
        max_consecutive_failures=max_consecutive_failures,
        enabled=enabled,
    )


def test_gate_starts_closed_until_every_critical_worker_is_ready() -> None:
    gate = WorkerReadinessGate({"critical_1", "critical_2"})
    assert gate.is_open() == (False, 0)

    gate.mark_ready("critical_1")
    assert not gate.is_open()[0]

    gate.mark_ready("critical_2")
    assert gate.is_open()[0]


def test_gate_with_no_critical_workers_starts_open() -> None:
    assert WorkerReadinessGate(set()).is_open()[0]


def test_worker_spec_rejects_zero_failure_threshold() -> None:
    async def worker(_tracker: object) -> None:
        return

    with pytest.raises(ValueError, match="max_consecutive_failures"):
        _spec("worker", worker, max_consecutive_failures=0)


def test_gate_close_invalidates_generation() -> None:
    gate = WorkerReadinessGate({"critical"})
    gate.mark_ready("critical")
    _, generation = gate.is_open()

    gate.close("failure")

    is_open, closed_generation = gate.is_open()
    assert not is_open
    assert closed_generation > generation
    assert gate.get_reasons() == ["failure"]


@pytest.mark.asyncio
async def test_disabled_worker_is_reported_stopped_and_never_spawned() -> None:
    factory = Mock()
    supervisor = WorkerSupervisor(
        [_spec("disabled", factory, enabled=False)], monitor_interval_seconds=0.01
    )

    await supervisor.start()
    await asyncio.sleep(0)

    assert supervisor.workers["disabled"].state == WorkerState.STOPPED_INTENTIONALLY
    assert supervisor.workers["disabled"].task is None
    factory.assert_not_called()
    supervisor.stop()
    await supervisor.wait_stopped()


def test_gate_membership_is_derived_from_enabled_critical_specs() -> None:
    async def worker(_tracker: object) -> None:
        await _block_forever()

    supervisor = WorkerSupervisor(
        [
            _spec("paper_monitor", worker, is_critical=True),
            _spec("disabled_critical", worker, is_critical=True, enabled=False),
        ]
    )

    supervisor.gate.mark_ready("paper_monitor")
    assert supervisor.gate.is_open()[0]


@pytest.mark.asyncio
async def test_unexpected_normal_return_is_restarted() -> None:
    run_count = 0

    async def worker(tracker: object) -> None:
        nonlocal run_count
        run_count += 1
        if run_count == 1:
            return
        tracker.tick_succeeded()  # type: ignore[attr-defined]
        await _block_forever()

    supervisor = WorkerSupervisor(
        [_spec("worker", worker)],
        monitor_interval_seconds=0.01,
        restart_backoff_base_seconds=0.01,
        restart_backoff_max_seconds=0.01,
    )
    await supervisor.start()
    await _wait_until(lambda: run_count == 2)

    assert len(supervisor.workers["worker"].restarts) == 1
    supervisor.stop()
    await supervisor.wait_stopped()


@pytest.mark.asyncio
async def test_worker_exception_is_restarted() -> None:
    run_count = 0

    async def worker(tracker: object) -> None:
        nonlocal run_count
        run_count += 1
        if run_count == 1:
            raise RuntimeError("boom")
        tracker.tick_succeeded()  # type: ignore[attr-defined]
        await _block_forever()

    supervisor = WorkerSupervisor(
        [_spec("worker", worker)],
        monitor_interval_seconds=0.01,
        restart_backoff_base_seconds=0.01,
        restart_backoff_max_seconds=0.01,
    )
    await supervisor.start()
    await _wait_until(lambda: run_count == 2)

    assert len(supervisor.workers["worker"].restarts) == 1
    supervisor.stop()
    await supervisor.wait_stopped()


@pytest.mark.asyncio
async def test_critical_never_policy_exits_process_without_in_process_restart() -> None:
    terminate = Mock()
    run_count = 0

    async def worker(_tracker: object) -> None:
        nonlocal run_count
        run_count += 1
        raise RuntimeError("boom")

    supervisor = WorkerSupervisor(
        [
            _spec(
                "signal_trader",
                worker,
                policy=WorkerRestartPolicy.NEVER,
                is_critical=True,
            )
        ],
        monitor_interval_seconds=0.01,
        terminate_process=terminate,
    )
    await supervisor.start()
    await _wait_until(lambda: terminate.called)

    assert run_count == 1
    assert supervisor.workers["signal_trader"].state == WorkerState.RESTART_EXHAUSTED
    terminate.assert_called_once_with("signal_trader failed with policy=NEVER")
    supervisor.stop()
    await supervisor.wait_stopped()


@pytest.mark.asyncio
async def test_any_critical_worker_exits_after_restart_budget_is_exhausted() -> None:
    terminate = Mock()

    async def worker(_tracker: object) -> None:
        raise RuntimeError("boom")

    supervisor = WorkerSupervisor(
        [
            _spec(
                "paper_monitor",
                worker,
                policy=WorkerRestartPolicy.BOUNDED_DEGRADED,
                is_critical=True,
                restart_budget=0,
            )
        ],
        monitor_interval_seconds=0.01,
        terminate_process=terminate,
    )
    await supervisor.start()
    await _wait_until(lambda: terminate.called)

    terminate.assert_called_once_with("paper_monitor exhausted restart budget")
    supervisor.stop()
    await supervisor.wait_stopped()


@pytest.mark.asyncio
async def test_persistent_tick_errors_trigger_restart() -> None:
    run_count = 0

    async def worker(tracker: object) -> None:
        nonlocal run_count
        run_count += 1
        if run_count > 1:
            tracker.tick_succeeded()  # type: ignore[attr-defined]
            await _block_forever()
        while True:
            tracker.tick_started()  # type: ignore[attr-defined]
            tracker.tick_failed(RuntimeError("dependency unavailable"))  # type: ignore[attr-defined]
            await asyncio.sleep(0.005)

    supervisor = WorkerSupervisor(
        [_spec("worker", worker, max_consecutive_failures=2)],
        monitor_interval_seconds=0.005,
        restart_backoff_base_seconds=0.005,
        restart_backoff_max_seconds=0.005,
    )
    await supervisor.start()
    await _wait_until(lambda: run_count == 2)

    assert len(supervisor.workers["worker"].restarts) == 1
    supervisor.stop()
    await supervisor.wait_stopped()


@pytest.mark.asyncio
async def test_missing_first_heartbeat_is_treated_as_stale() -> None:
    run_count = 0

    async def worker(tracker: object) -> None:
        nonlocal run_count
        run_count += 1
        if run_count > 1:
            tracker.tick_succeeded()  # type: ignore[attr-defined]
        await _block_forever()

    supervisor = WorkerSupervisor(
        [_spec("worker", worker, stale_timeout_seconds=0.02)],
        monitor_interval_seconds=0.005,
        restart_backoff_base_seconds=0.005,
        restart_backoff_max_seconds=0.005,
    )
    await supervisor.start()
    await _wait_until(lambda: run_count == 2)

    assert len(supervisor.workers["worker"].restarts) == 1
    supervisor.stop()
    await supervisor.wait_stopped()


@pytest.mark.asyncio
async def test_shutdown_does_not_restart_workers() -> None:
    run_count = 0

    async def worker(_tracker: object) -> None:
        nonlocal run_count
        run_count += 1
        await _block_forever()

    supervisor = WorkerSupervisor(
        [_spec("worker", worker)],
        monitor_interval_seconds=0.005,
        restart_backoff_base_seconds=0.005,
        restart_backoff_max_seconds=0.005,
    )
    await supervisor.start()
    await _wait_until(lambda: run_count == 1)
    supervisor.stop()
    await supervisor.wait_stopped()
    await asyncio.sleep(0.02)

    assert run_count == 1
    assert supervisor.workers["worker"].state == WorkerState.STOPPED_INTENTIONALLY


def test_restart_backoff_is_exponential_and_bounded() -> None:
    supervisor = WorkerSupervisor(
        [], restart_backoff_base_seconds=0.5, restart_backoff_max_seconds=2.0
    )
    assert [supervisor._restart_backoff(i) for i in range(5)] == [0.5, 1.0, 2.0, 2.0, 2.0]


@pytest.mark.asyncio
async def test_health_serializes_timestamps_and_degrades_on_noncritical_errors() -> None:
    async def worker(_tracker: object) -> None:
        await _block_forever()

    supervisor = WorkerSupervisor(
        [
            _spec("critical", worker, is_critical=True),
            _spec("writer", worker, policy=WorkerRestartPolicy.ALWAYS),
            _spec("disabled", worker, enabled=False),
        ]
    )
    critical = supervisor.workers["critical"]
    critical.tracker.tick_started()
    critical.tracker.tick_succeeded()
    supervisor.gate.mark_ready("critical")
    writer = supervisor.workers["writer"]
    writer.tracker.tick_started()
    writer.tracker.tick_failed(RuntimeError("db unavailable"))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(supervisor=supervisor)))
    response = Response()

    result = await get_workers_health(request, response)  # type: ignore[arg-type]

    assert response.status_code == 200
    assert result["status"] == "degraded"
    assert result["workers"]["critical"]["last_success_at"].endswith("+00:00")
    assert result["workers"]["writer"]["consecutive_failures"] == 1
    assert result["workers"]["disabled"]["state"] == "stopped_intentionally"


@pytest.mark.asyncio
async def test_health_without_supervisor_returns_503() -> None:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    response = Response()

    result = await get_workers_health(request, response)  # type: ignore[arg-type]

    assert response.status_code == 503
    assert result["status"] == "unknown"
