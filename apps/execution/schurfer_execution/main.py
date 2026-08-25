from __future__ import annotations

import asyncio
import contextlib
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
from .routers import account, control, health, orders
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

    # One Broker per strategy, resolved once at startup from the same
    # cfg.dry_run/cfg.auto_trade + per-strategy mode override
    # execution_intent.Config.__post_init__ already validated -- this can
    # never raise here (a bad config already failed Config() construction
    # above). Task-creation conditions below (cfg.dry_run / cfg.auto_trade)
    # are unchanged by this -- a broker existing does not mean its task
    # runs; resolve_mode only decides what an already-started task's own
    # entry call does.
    pump_short_broker: Broker = build_broker(
        resolve_mode(cfg, STRATEGY_PUMP_SHORT), exchanges=strategy_exchanges
    )
    early_momentum_broker: Broker = build_broker(
        resolve_mode(cfg, STRATEGY_EARLY_MOMENTUM), exchanges=market_exchanges
    )
    liquidation_cascade_broker: Broker = build_broker(
        resolve_mode(cfg, STRATEGY_LIQUIDATION_CASCADE), exchanges=market_exchanges
    )

    early_momentum_cohort_started_at = None
    early_momentum_enabled = early_momentum_broker.mode is not TradingMode.DISABLED
    if cfg.dry_run and cfg.db_url and early_momentum_enabled:
        validate_prospective_runtime_policy(cfg, trading_mode=early_momentum_broker.mode.value)
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

    tracker = asyncio.create_task(run_pnl_tracker(trading_exchanges, rdb, cfg.db_url))
    monitor = asyncio.create_task(run_position_monitor(trading_exchanges, rdb, cfg))
    trader = (
        asyncio.create_task(run_signal_trader(strategy_exchanges, rdb, cfg, pump_short_broker))
        if cfg.auto_trade or cfg.dry_run
        else None
    )
    paper = (
        asyncio.create_task(run_paper_monitor(market_exchanges, rdb, cfg)) if cfg.dry_run else None
    )
    early_momentum_scanner = (
        asyncio.create_task(run_early_momentum_scanner(rdb, cfg))
        if cfg.dry_run and early_momentum_enabled
        else None
    )
    early_momentum_trigger = (
        asyncio.create_task(
            run_early_momentum_trigger(market_exchanges, rdb, cfg, early_momentum_broker)
        )
        if cfg.dry_run and early_momentum_enabled
        else None
    )
    # Deliberately its own task, independent of the two above: if trigger
    # deadlocks, this must keep ticking and still be able to report it.
    early_momentum_health_monitor = (
        asyncio.create_task(
            run_early_momentum_health_monitor(rdb, cfg, startup_at=early_momentum_startup_at)
        )
        if cfg.dry_run and early_momentum_enabled
        else None
    )
    liquidation_cascade_scanner = (
        asyncio.create_task(
            run_liquidation_cascade_scanner(market_exchanges, rdb, cfg, liquidation_cascade_broker)
        )
        if cfg.dry_run
        else None
    )
    # The decision writer does long blocking XREADGROUP reads, so it gets its own client
    # with a socket timeout above the BLOCK window. Keeping it separate leaves the trading
    # hot path fail-fast instead of inheriting the writer's longer timeout.
    writer_rdb = None
    dec_writer = None
    if cfg.db_url:
        writer_rdb = aioredis.from_url(
            f"redis://{host}:{port}",
            socket_timeout=_REDIS_SOCKET_TIMEOUT_SECONDS,
            socket_connect_timeout=5.0,
        )
        dec_writer = asyncio.create_task(run_decision_writer(writer_rdb, cfg.db_url))
    incident_worker = (
        asyncio.create_task(run_incident_worker(trading_exchanges, rdb, cfg))
        if cfg.db_url
        else None
    )
    # pump_short's own broker/mode is only ever reached inside trader.py's
    # cfg.dry_run branch -- under AUTO_TRADE=true that branch never runs
    # (real orders go through the untouched place_order path instead), so
    # logging pump_short_broker.mode there would claim a mode that governs
    # nothing. "legacy_live" names what actually executes instead
    # (colleague review, P0 -- Config already refuses PUMP_SHORT_MODE
    # whenever AUTO_TRADE=true, so this is never a config override hiding
    # behind a misleading log line, just an accurate description of the
    # untouched pre-existing path).
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
    background_tasks = [
        t
        for t in (
            tracker,
            monitor,
            trader,
            paper,
            early_momentum_scanner,
            early_momentum_trigger,
            early_momentum_health_monitor,
            liquidation_cascade_scanner,
            dec_writer,
            incident_worker,
        )
        if t is not None
    ]
    for task in background_tasks:
        task.cancel()
    if background_tasks:
        await asyncio.gather(*background_tasks, return_exceptions=True)

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
