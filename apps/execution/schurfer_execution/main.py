from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

import redis.asyncio as aioredis
import structlog
import uvicorn
from fastapi import FastAPI

from .config import Config
from .exchanges import build_exchanges, close_exchanges
from .routers import account, control, orders

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

    log.info("execution.start", exchanges=list(exchanges.keys()))
    yield

    await close_exchanges(exchanges)
    await rdb.aclose()


app = FastAPI(title="schurfer-execution", lifespan=lifespan)

app.include_router(account.router)
app.include_router(orders.router)
app.include_router(control.router)


def main() -> None:
    uvicorn.run("schurfer_execution.main:app", host="0.0.0.0", port=8001, reload=False)  # noqa: S104
