from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

import redis.asyncio as aioredis
import structlog
import uvicorn
from fastapi import FastAPI

from .config import Config
from .exchanges import build_exchanges, close_exchanges
from .monitor import run_position_monitor
from .paper import run_paper_monitor
from .routers import account, control, orders
from .tracker import run_pnl_tracker
from .trader import run_signal_trader

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

log = structlog.get_logger()


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
    rdb = aioredis.from_url(f"redis://{host}:{port}")
    exchanges: dict[str, Any] = build_exchanges(cfg)

    app.state.cfg = cfg
    app.state.rdb = rdb
    app.state.exchanges = exchanges

    tracker = asyncio.create_task(run_pnl_tracker(exchanges, rdb))
    monitor = asyncio.create_task(run_position_monitor(exchanges, rdb, cfg))
    trader = (
        asyncio.create_task(run_signal_trader(exchanges, rdb, cfg))
        if cfg.auto_trade or cfg.dry_run
        else None
    )
    paper = asyncio.create_task(run_paper_monitor(exchanges, rdb, cfg)) if cfg.dry_run else None
    log.info(
        "execution.start",
        exchanges=list(exchanges.keys()),
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
    await close_exchanges(exchanges)
    await rdb.aclose()


app = FastAPI(title="schurfer-execution", lifespan=lifespan)

app.include_router(account.router)
app.include_router(orders.router)
app.include_router(control.router)


def main() -> None:
    uvicorn.run("schurfer_execution.main:app", host="0.0.0.0", port=8001, reload=False)  # noqa: S104
