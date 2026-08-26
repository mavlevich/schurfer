from __future__ import annotations

import asyncio
import contextlib
import typing
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import redis.asyncio as aioredis
import structlog
import uvicorn
from fastapi import FastAPI

from .config import Config
from .decisions import _REDIS_SOCKET_TIMEOUT_SECONDS, run_decision_writer
from .early_momentum import (
    CONTRACT_SHA256 as EARLY_MOMENTUM_CONTRACT_SHA256,
)
from .early_momentum import (
    PROSPECTIVE_RUNTIME_POLICY_SHA256,
    run_early_momentum_health_monitor,
    run_early_momentum_scanner,
    run_early_momentum_trigger,
    validate_prospective_runtime_policy,
)
from .early_momentum_prospective_cohort import register_prospective_cohort
from .exchanges import build_exchange_clients, close_exchange_clients
from .execution_intent import (
    STRATEGY_EARLY_MOMENTUM,
    STRATEGY_LIQUIDATION_CASCADE,
    STRATEGY_PUMP_SHORT,
    Broker,
    TradingMode,
    build_broker,
    resolve_mode,
)
from .incident_worker import run_incident_worker
from .liquidation_cascade import run_liquidation_cascade_scanner
from .monitor import run_position_monitor
from .paper import run_paper_monitor
from .reconciliation import STARTUP_BLOCKER
from .reconciliation_worker import ReconciliationWorker
from .routers import account, control, health, orders
from .supervisor import (
    WorkerRestartPolicy,
    WorkerSpec,
    WorkerSupervisor,
)
from .tracker import run_pnl_tracker
from .trader import run_signal_trader

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

log = structlog.get_logger()

# Fail-fast socket timeout for the shared trading hot-path client (kill-switch, order
# locks, position state). The decision writer uses its own client with a longer timeout.
_HOT_PATH_SOCKET_TIMEOUT_SECONDS = 5.0


async def _preload_markets(exchanges: dict[str, Any]) -> set[str]:
    """Load venue metadata without making optional venues a global dependency."""
    names = list(exchanges)
    results = await asyncio.gather(
        *(exchanges[name].load_markets() for name in names),
        return_exceptions=True,
    )
    failed: set[str] = set()
    for name, result in zip(names, results, strict=True):
        if isinstance(result, BaseException):
            failed.add(name)
            log.error("startup.preload_markets_failed", exchange=name, err=str(result))
    return failed


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )
    cfg = Config()
    host, port = [*cfg.redis_addr.split(":"), "6379"][:2]
    # Shared client for the trading hot path, on an explicit fail-fast socket timeout.
    rdb = aioredis.from_url(
        f"redis://{host}:{port}",
        socket_timeout=_HOT_PATH_SOCKET_TIMEOUT_SECONDS,
        socket_connect_timeout=5.0,
    )
    clients = build_exchange_clients(cfg)
    market_exchanges: dict[str, Any] = clients.market
    trading_exchanges: dict[str, Any] = clients.trading
    strategy_exchanges = clients.strategy_clients(dry_run=cfg.dry_run)

    # Resolve modes once. The same values decide whether a worker is enabled
    # and which Broker its eventual factory receives; mode and control flow
    # cannot drift through independent computations.
    pump_short_mode = resolve_mode(cfg, STRATEGY_PUMP_SHORT)
    early_momentum_mode = resolve_mode(cfg, STRATEGY_EARLY_MOMENTUM)
    liquidation_cascade_mode = resolve_mode(cfg, STRATEGY_LIQUIDATION_CASCADE)

    early_momentum_cohort_started_at = None
    early_momentum_enabled = early_momentum_mode is not TradingMode.DISABLED
    if cfg.dry_run and cfg.db_url and early_momentum_enabled:
        validate_prospective_runtime_policy(cfg, trading_mode=early_momentum_mode.value)
        early_momentum_cohort_started_at = await register_prospective_cohort(
            cfg.db_url,
            contract_sha256=EARLY_MOMENTUM_CONTRACT_SHA256,
            runtime_policy_sha256=PROSPECTIVE_RUNTIME_POLICY_SHA256,
        )

    app.state.cfg = cfg
    app.state.rdb = rdb
    app.state.trading_exchanges = trading_exchanges
    # Shared with the early_momentum health monitor task below, so the
    # startup-grace-period clock and the HTTP health endpoint's read of it
    # (routers/health.py) can never disagree about when the process
    # actually started.
    early_momentum_startup_at = datetime.now(tz=UTC)
    app.state.early_momentum_startup_at = early_momentum_startup_at

    if market_exchanges:
        log.info("startup.preload_markets", count=len(market_exchanges))
        await _preload_markets(market_exchanges)

    writer_rdb = (
        aioredis.from_url(
            f"redis://{host}:{port}",
            socket_timeout=_REDIS_SOCKET_TIMEOUT_SECONDS,
            socket_connect_timeout=5.0,
        )
        if cfg.db_url
        else None
    )

    # These names are deliberately bound after WorkerSupervisor derives its
    # gate from the specs. Python closures resolve them only when start()
    # creates the tasks, after all brokers have received that exact gate.
    pump_short_broker: Broker
    early_momentum_broker: Broker
    liquidation_cascade_broker: Broker
    reconciliation_worker: ReconciliationWorker

    reconciliation_enabled = bool(cfg.db_url and trading_exchanges)

    specs = [
        WorkerSpec(
            name="position_monitor",
            factory=lambda tr: run_position_monitor(trading_exchanges, rdb, cfg, tr),
            policy=WorkerRestartPolicy.BOUNDED_FATAL,
            is_critical=True,
            restart_budget=3,
            restart_window_seconds=60.0,
            stale_timeout_seconds=120.0,
        ),
        WorkerSpec(
            name="pnl_tracker",
            factory=lambda tr: run_pnl_tracker(trading_exchanges, rdb, cfg.db_url, tr),
            policy=WorkerRestartPolicy.BOUNDED_FATAL
            if cfg.auto_trade
            else WorkerRestartPolicy.BOUNDED_DEGRADED,
            is_critical=True,
            restart_budget=3,
            restart_window_seconds=60.0,
            stale_timeout_seconds=120.0,
        ),
        WorkerSpec(
            name="incident_worker",
            factory=lambda tr: run_incident_worker(trading_exchanges, rdb, cfg, tr),
            policy=(
                WorkerRestartPolicy.BOUNDED_FATAL
                if cfg.auto_trade
                else WorkerRestartPolicy.BOUNDED_DEGRADED
            ),
            is_critical=True,
            restart_budget=5,
            restart_window_seconds=120.0,
            stale_timeout_seconds=120.0,
            enabled=bool(cfg.db_url),
        ),
        WorkerSpec(
            name="position_reconciler",
            factory=lambda tr: reconciliation_worker(tr),
            policy=WorkerRestartPolicy.BOUNDED_FATAL,
            is_critical=True,
            restart_budget=5,
            restart_window_seconds=120.0,
            stale_timeout_seconds=120.0,
            enabled=reconciliation_enabled,
        ),
        WorkerSpec(
            name="decision_writer",
            factory=lambda tr: run_decision_writer(
                typing.cast("Any", writer_rdb), typing.cast("str", cfg.db_url), tr
            ),
            policy=WorkerRestartPolicy.ALWAYS,
            is_critical=False,
            stale_timeout_seconds=180.0,
            enabled=bool(cfg.db_url),
        ),
        WorkerSpec(
            name="signal_trader",
            factory=lambda tr: run_signal_trader(
                strategy_exchanges,
                rdb,
                cfg,
                pump_short_broker,
                tr,
                worker_gate=supervisor.gate,
            ),
            policy=WorkerRestartPolicy.NEVER,
            is_critical=True,
            stale_timeout_seconds=120.0,
            enabled=cfg.auto_trade or cfg.dry_run,
        ),
        WorkerSpec(
            name="paper_monitor",
            factory=lambda tr: run_paper_monitor(market_exchanges, rdb, cfg, tr),
            policy=WorkerRestartPolicy.BOUNDED_DEGRADED,
            is_critical=True,
            restart_budget=3,
            stale_timeout_seconds=120.0,
            enabled=cfg.dry_run,
        ),
        WorkerSpec(
            name="liquidation_cascade_scanner",
            factory=lambda tr: run_liquidation_cascade_scanner(
                market_exchanges, rdb, cfg, liquidation_cascade_broker, tr
            ),
            policy=WorkerRestartPolicy.BOUNDED_DEGRADED,
            is_critical=False,
            restart_budget=3,
            stale_timeout_seconds=120.0,
            enabled=(
                cfg.dry_run
                and bool(cfg.db_url)
                and liquidation_cascade_mode is not TradingMode.DISABLED
            ),
        ),
        WorkerSpec(
            name="early_momentum_scanner",
            factory=lambda tr: run_early_momentum_scanner(rdb, cfg, tr),
            policy=WorkerRestartPolicy.BOUNDED_DEGRADED,
            is_critical=False,
            restart_budget=3,
            stale_timeout_seconds=120.0,
            enabled=cfg.dry_run and bool(cfg.db_url) and early_momentum_enabled,
        ),
        WorkerSpec(
            name="early_momentum_trigger",
            factory=lambda tr: run_early_momentum_trigger(
                market_exchanges, rdb, cfg, early_momentum_broker, tr
            ),
            policy=WorkerRestartPolicy.BOUNDED_DEGRADED,
            is_critical=False,
            restart_budget=3,
            stale_timeout_seconds=120.0,
            enabled=cfg.dry_run and bool(cfg.db_url) and early_momentum_enabled,
        ),
        WorkerSpec(
            name="early_momentum_health_monitor",
            factory=lambda tr: run_early_momentum_health_monitor(
                rdb, cfg, startup_at=early_momentum_startup_at, tracker=tr
            ),
            policy=WorkerRestartPolicy.ALWAYS,
            is_critical=False,
            stale_timeout_seconds=120.0,
            enabled=cfg.dry_run and bool(cfg.db_url) and early_momentum_enabled,
        ),
    ]

    supervisor = WorkerSupervisor(specs)
    worker_gate = supervisor.gate
    if reconciliation_enabled:
        worker_gate.set_safety_blocker(STARTUP_BLOCKER)
        reconciliation_worker = ReconciliationWorker(
            trading_exchanges,
            typing.cast("str", cfg.db_url),
            rdb,
            worker_gate,
        )
    pump_short_broker = build_broker(
        pump_short_mode, exchanges=strategy_exchanges, gate=worker_gate
    )
    early_momentum_broker = build_broker(
        early_momentum_mode, exchanges=market_exchanges, gate=worker_gate
    )
    liquidation_cascade_broker = build_broker(
        liquidation_cascade_mode, exchanges=market_exchanges, gate=worker_gate
    )

    app.state.supervisor = supervisor
    app.state.worker_gate = supervisor.gate

    await supervisor.start()

    pump_short_execution_path = "legacy_live" if cfg.auto_trade else pump_short_broker.mode.value
    log.info(
        "execution.start",
        market_exchanges=list(market_exchanges),
        trading_exchanges=list(trading_exchanges),
        auto_trade=cfg.auto_trade,
        dry_run=cfg.dry_run,
        pump_short_execution_path=pump_short_execution_path,
        early_momentum_mode=early_momentum_broker.mode.value,
        early_momentum_prospective_cohort_started_at=(
            early_momentum_cohort_started_at.isoformat()
            if early_momentum_cohort_started_at is not None
            else None
        ),
        liquidation_cascade_mode=liquidation_cascade_broker.mode.value,
    )
    yield

    # Cancel every background task first, then wait for all of them to
    # actually finish unwinding before tearing down anything they might
    # still touch mid-cancellation (exchange clients, rdb) -- cancel() only
    # schedules a CancelledError at the task's next await point, it doesn't
    # guarantee the task has stopped running yet. A task whose cleanup
    # (e.g. a heartbeat's `finally` write) runs after clients/rdb are
    # already closed would fail loudly instead of shutting down cleanly
    # (colleague review). Unacked decision-writer stream entries are
    # reprocessed on restart, so there's no in-process queue to drain for
    # it specifically -- it's still collected here like every other task.
    supervisor.stop()
    await supervisor.wait_stopped()

    await close_exchange_clients(clients)
    await rdb.aclose()
    if writer_rdb is not None:
        await writer_rdb.aclose()


app = FastAPI(title="schurfer-execution", lifespan=lifespan)

app.include_router(account.router)
app.include_router(orders.router)
app.include_router(control.router)
app.include_router(health.router)


def main() -> None:
    uvicorn.run("schurfer_execution.main:app", host="0.0.0.0", port=8001, reload=False)  # noqa: S104
