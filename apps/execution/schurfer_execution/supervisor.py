from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

log = structlog.get_logger()


class WorkerRestartPolicy(Enum):
    NEVER = "never"
    BOUNDED_FATAL = "bounded_fatal"
    BOUNDED_DEGRADED = "bounded_degraded"
    ALWAYS = "always"


class WorkerState(Enum):
    PENDING = "pending"
    RUNNING = "running"
    FAILED = "failed"
    STOPPED_INTENTIONALLY = "stopped_intentionally"
    RESTART_EXHAUSTED = "restart_exhausted"


@dataclass
class InProcessTickTracker:
    name: str
    last_started_at: float | None = None
    last_success_at: float | None = None
    last_error_at: float | None = None
    last_started_at_utc: datetime | None = None
    last_success_at_utc: datetime | None = None
    last_error_at_utc: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    consecutive_idles: int = 0

    def tick_started(self) -> None:
        self.last_started_at = time.monotonic()
        self.last_started_at_utc = datetime.now(tz=UTC)

    def tick_succeeded(self) -> None:
        self.last_success_at = time.monotonic()
        self.last_success_at_utc = datetime.now(tz=UTC)
        self.consecutive_failures = 0
        self.consecutive_idles = 0

    def tick_idle(self) -> None:
        self.last_success_at = time.monotonic()
        self.last_success_at_utc = datetime.now(tz=UTC)
        self.consecutive_failures = 0
        self.consecutive_idles += 1

    def tick_failed(self, error: Exception) -> None:
        self.last_error_at = time.monotonic()
        self.last_error_at_utc = datetime.now(tz=UTC)
        self.last_error = str(error)
        self.consecutive_failures += 1

    def reset(self) -> None:
        self.last_started_at = None
        self.last_success_at = None
        self.last_error_at = None
        self.last_started_at_utc = None
        self.last_success_at_utc = None
        self.last_error_at_utc = None
        self.last_error = None
        self.consecutive_failures = 0
        self.consecutive_idles = 0


@dataclass
class WorkerSpec:
    name: str
    factory: Callable[[InProcessTickTracker], Coroutine[Any, Any, None]]
    policy: WorkerRestartPolicy
    is_critical: bool
    restart_budget: int = 0
    restart_window_seconds: float = 300.0
    stale_timeout_seconds: float = 60.0
    max_consecutive_failures: int = 3
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("worker name must not be empty")
        if self.restart_budget < 0:
            raise ValueError("restart_budget must be >= 0")
        if self.restart_window_seconds <= 0:
            raise ValueError("restart_window_seconds must be > 0")
        if self.stale_timeout_seconds <= 0:
            raise ValueError("stale_timeout_seconds must be > 0")
        if self.max_consecutive_failures <= 0:
            raise ValueError("max_consecutive_failures must be > 0")


class WorkerReadinessGate:
    def __init__(self, critical_worker_names: set[str]) -> None:
        self._critical_worker_names = critical_worker_names
        self._ready_workers: set[str] = set()
        self._is_open = not critical_worker_names
        self._generation = 0
        self._reasons: list[str] = [] if self._is_open else ["startup_pending"]

    def is_open(self) -> tuple[bool, int]:
        return self._is_open, self._generation

    def get_reasons(self) -> list[str]:
        return list(self._reasons)

    def mark_ready(self, worker_name: str) -> None:
        if worker_name in self._critical_worker_names:
            self._ready_workers.add(worker_name)
            if not self._is_open and self._ready_workers == self._critical_worker_names:
                self._is_open = True
                self._generation += 1
                self._reasons.clear()
                log.info("gate.opened", generation=self._generation)

    def close(self, reason: str) -> None:
        if self._is_open:
            self._is_open = False
            self._generation += 1
            log.warning("gate.closed", reason=reason, generation=self._generation)
        if reason not in self._reasons:
            self._reasons.append(reason)
        self._ready_workers.clear()

    def force_close_all(self, reason: str) -> None:
        self.close(reason)


@dataclass
class ManagedWorker:
    spec: WorkerSpec
    tracker: InProcessTickTracker
    state: WorkerState = WorkerState.PENDING
    task: asyncio.Task[None] | None = None
    restarts: list[float] = field(default_factory=list)
    next_restart_at: float | None = None
    spawned_at: float | None = None


class WorkerSupervisor:
    def __init__(
        self,
        specs: list[WorkerSpec],
        *,
        monitor_interval_seconds: float = 1.0,
        restart_backoff_base_seconds: float = 1.0,
        restart_backoff_max_seconds: float = 30.0,
        terminate_process: Callable[[str], None] | None = None,
    ) -> None:
        self.specs = {s.name: s for s in specs}
        if len(self.specs) != len(specs):
            raise ValueError("worker names must be unique")
        if monitor_interval_seconds <= 0:
            raise ValueError("monitor_interval_seconds must be > 0")
        if restart_backoff_base_seconds < 0:
            raise ValueError("restart_backoff_base_seconds must be >= 0")
        if restart_backoff_max_seconds < restart_backoff_base_seconds:
            raise ValueError("restart_backoff_max_seconds must be >= restart_backoff_base_seconds")

        critical_names = {s.name for s in specs if s.enabled and s.is_critical}
        self.gate = WorkerReadinessGate(critical_names)
        self._monitor_interval_seconds = monitor_interval_seconds
        self._restart_backoff_base_seconds = restart_backoff_base_seconds
        self._restart_backoff_max_seconds = restart_backoff_max_seconds
        self._terminate_process = terminate_process or self._default_terminate_process

        self.workers: dict[str, ManagedWorker] = {}
        for spec in specs:
            self.workers[spec.name] = ManagedWorker(
                spec=spec,
                tracker=InProcessTickTracker(name=spec.name),
                state=(WorkerState.PENDING if spec.enabled else WorkerState.STOPPED_INTENTIONALLY),
            )

        self._stop_event = asyncio.Event()
        self._monitor_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        for w in self.workers.values():
            self._spawn_worker(w)
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    def stop(self) -> None:
        self._stop_event.set()
        self.gate.force_close_all("shutdown")
        for w in self.workers.values():
            if w.spec.enabled and w.state != WorkerState.RESTART_EXHAUSTED:
                w.state = WorkerState.STOPPED_INTENTIONALLY
            if w.task and not w.task.done():
                w.task.cancel()

    async def wait_stopped(self) -> None:
        if self._monitor_task:
            self._monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._monitor_task
        tasks = [w.task for w in self.workers.values() if w.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _spawn_worker(self, w: ManagedWorker) -> None:
        if w.state == WorkerState.STOPPED_INTENTIONALLY:
            return

        w.tracker.reset()
        w.next_restart_at = None
        w.spawned_at = time.monotonic()
        w.state = WorkerState.RUNNING

        async def _wrapper() -> None:
            try:
                coro = w.spec.factory(w.tracker)
                await coro
            except asyncio.CancelledError:
                raise
            except Exception as e:
                w.tracker.tick_failed(e)
                log.error("supervisor.worker_crashed", worker=w.spec.name, error=str(e))
                # Do not raise to crash supervisor, let it see task.done()

        w.task = asyncio.create_task(_wrapper(), name=w.spec.name)

    def _restart_backoff(self, restart_count: int) -> float:
        return float(
            min(
                self._restart_backoff_base_seconds * (2**restart_count),
                self._restart_backoff_max_seconds,
            )
        )

    @staticmethod
    def _default_terminate_process(reason: str) -> None:
        log.error("supervisor.triggering_exit", reason=reason)
        os.kill(os.getpid(), signal.SIGTERM)

    def _terminal_failure(self, w: ManagedWorker, reason: str) -> bool:
        w.state = WorkerState.RESTART_EXHAUSTED
        self.gate.close(reason)
        if w.spec.is_critical or w.spec.policy == WorkerRestartPolicy.BOUNDED_FATAL:
            self._terminate_process(reason)
            return True
        return False

    async def _monitor_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                for name, w in self.workers.items():
                    if w.state == WorkerState.STOPPED_INTENTIONALLY:
                        continue

                    if w.state == WorkerState.FAILED and w.next_restart_at is not None:
                        if now >= w.next_restart_at:
                            w.restarts.append(now)
                            self._spawn_worker(w)
                        continue

                    if w.state == WorkerState.RESTART_EXHAUSTED:
                        continue

                    if (
                        w.spec.is_critical
                        and w.state == WorkerState.RUNNING
                        and w.tracker.last_success_at
                        and w.tracker.consecutive_failures == 0
                        and (now - w.tracker.last_success_at) <= w.spec.stale_timeout_seconds
                    ):
                        self.gate.mark_ready(name)

                    is_stale = False
                    if w.state == WorkerState.RUNNING:
                        # Check last_success_at
                        if w.tracker.last_success_at:
                            elapsed = now - w.tracker.last_success_at
                            if elapsed > w.spec.stale_timeout_seconds:
                                is_stale = True
                        elif w.tracker.last_started_at:
                            elapsed = now - w.tracker.last_started_at
                            if elapsed > w.spec.stale_timeout_seconds:
                                is_stale = True
                        elif w.spawned_at is not None:
                            # A worker that never emits its first heartbeat is
                            # stalled too; otherwise it could remain RUNNING
                            # forever without ever becoming restart-eligible.
                            is_stale = (now - w.spawned_at) > w.spec.stale_timeout_seconds

                    is_failed = False
                    if w.task and w.task.done():
                        is_failed = True

                    has_tick_errors = w.tracker.consecutive_failures > 0
                    persistent_tick_errors = (
                        w.tracker.consecutive_failures >= w.spec.max_consecutive_failures
                    )
                    if w.spec.is_critical and (is_stale or is_failed or has_tick_errors):
                        reason = (
                            f"worker {name} failed" if is_failed else f"worker {name} stale/errors"
                        )
                        self.gate.close(reason)

                    if (is_stale or persistent_tick_errors) and w.task and not w.task.done():
                        reason = "stale" if is_stale else "persistent_errors"
                        log.warning("supervisor.worker_unhealthy", worker=name, reason=reason)
                        w.task.cancel()
                        try:
                            await w.task
                        except asyncio.CancelledError:
                            pass
                        except Exception as e:
                            log.warning("supervisor.cancel_error", err=str(e))
                        is_failed = True

                    if is_failed:
                        w.state = WorkerState.FAILED
                        w.restarts = [
                            r for r in w.restarts if (now - r) <= w.spec.restart_window_seconds
                        ]

                        if w.spec.policy == WorkerRestartPolicy.NEVER:
                            log.error("supervisor.worker_fatal", worker=name, reason="policy=NEVER")
                            if self._terminal_failure(w, f"{name} failed with policy=NEVER"):
                                return
                        elif w.spec.policy == WorkerRestartPolicy.ALWAYS:
                            w.next_restart_at = now + self._restart_backoff(len(w.restarts))
                            log.info(
                                "supervisor.worker_restart",
                                worker=name,
                                backoff=w.next_restart_at - now,
                            )
                        elif len(w.restarts) >= w.spec.restart_budget:
                            log.error(
                                "supervisor.restart_exhausted",
                                worker=name,
                                budget=w.spec.restart_budget,
                            )
                            if self._terminal_failure(w, f"{name} exhausted restart budget"):
                                return
                        else:
                            w.next_restart_at = now + self._restart_backoff(len(w.restarts))
                            log.info(
                                "supervisor.worker_restart",
                                worker=name,
                                backoff=w.next_restart_at - now,
                            )

                await asyncio.sleep(self._monitor_interval_seconds)
        except Exception as e:
            log.error("supervisor.monitor_crashed", err=str(e))
            self.gate.close("supervisor monitor crashed")
            self._terminate_process("supervisor monitor crashed")
