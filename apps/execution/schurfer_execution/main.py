from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

import redis.asyncio as aioredis
import structlog
import uvicorn
from fastapi import FastAPI

from .config import Config
from .decisions import _REDIS_SOCKET_TIMEOUT_SECONDS, run_decision_writer
from .early_momentum import run_early_momentum_scanner, run_early_momentum_trigger
from .exchanges import build_exchange_clients, close_exchange_clients
from .incident_worker import run_incident_worker
from .liquidation_cascade import run_liquidation_cascade_scanner
from .monitor import run_position_monitor
from .paper import run_paper_monitor
from .routers import account, control, orders
from .tracker import run_pnl_tracker
from .trader import run_signal_trader

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

log = structlog.get_logger()

# Fail-fast socket timeout for the shared trading hot-path client (kill-switch, order
# locks, position state). The decision writer uses its own client with a longer timeout.
_HOT_PATH_SOCKET_TIMEOUT_SECONDS = 5.0


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

    app.state.cfg = cfg
    app.state.rdb = rdb
    app.state.trading_exchanges = trading_exchanges

    tracker = asyncio.create_task(run_pnl_tracker(trading_exchanges, rdb, cfg.db_url))
    monitor = asyncio.create_task(run_position_monitor(trading_exchanges, rdb, cfg))
    trader = (
        asyncio.create_task(run_signal_trader(strategy_exchanges, rdb, cfg))
        if cfg.auto_trade or cfg.dry_run
        else None
    )
    paper = (
        asyncio.create_task(run_paper_monitor(market_exchanges, rdb, cfg)) if cfg.dry_run else None
    )
    early_momentum_scanner = (
        asyncio.create_task(run_early_momentum_scanner(rdb, cfg)) if cfg.dry_run else None
    )
    early_momentum_trigger = (
        asyncio.create_task(run_early_momentum_trigger(market_exchanges, rdb, cfg))
        if cfg.dry_run
        else None
    )
    liquidation_cascade_scanner = (
        asyncio.create_task(run_liquidation_cascade_scanner(rdb, cfg)) if cfg.dry_run else None
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
    log.info(
        "execution.start",
        market_exchanges=list(market_exchanges),
        trading_exchanges=list(trading_exchanges),
        auto_trade=cfg.auto_trade,
        dry_run=cfg.dry_run,
    )
    yield

    tracker.cancel()
    monitor.cancel()
    if trader:
        trader.cancel()
    if paper:
        paper.cancel()
    if early_momentum_scanner:
        early_momentum_scanner.cancel()
    if early_momentum_trigger:
        early_momentum_trigger.cancel()
    if liquidation_cascade_scanner:
        liquidation_cascade_scanner.cancel()
    if dec_writer:
        # Unacked entries stay in the Redis Stream and are reprocessed on restart,
        # so there is no in-process queue to drain here.
        dec_writer.cancel()
    if incident_worker:
        incident_worker.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await tracker
    with contextlib.suppress(asyncio.CancelledError):
        await monitor
    if trader:
        with contextlib.suppress(asyncio.CancelledError):
            await trader
    if paper:
        with contextlib.suppress(asyncio.CancelledError):
            await paper
    if dec_writer:
        with contextlib.suppress(asyncio.CancelledError):
            await dec_writer
    if incident_worker:
        with contextlib.suppress(asyncio.CancelledError):
            await incident_worker
    await close_exchange_clients(clients)
    await rdb.aclose()
    if writer_rdb is not None:
        await writer_rdb.aclose()


app = FastAPI(title="schurfer-execution", lifespan=lifespan)

app.include_router(account.router)
app.include_router(orders.router)
app.include_router(control.router)


def main() -> None:
    uvicorn.run("schurfer_execution.main:app", host="0.0.0.0", port=8001, reload=False)  # noqa: S104
