from typing import Any

import structlog
from fastapi import APIRouter, Request

from ..risk import TRADING_ENABLED_KEY

log = structlog.get_logger()

router = APIRouter()


@router.post("/stop")
async def emergency_stop(request: Request) -> dict[str, Any]:
    await request.app.state.rdb.set(TRADING_ENABLED_KEY, "0")
    log.warning("execution.emergency_stop")
    return {"trading_enabled": False}


@router.post("/resume")
async def resume_trading(request: Request) -> dict[str, Any]:
    await request.app.state.rdb.set(TRADING_ENABLED_KEY, "1")
    log.info("execution.resumed")
    return {"trading_enabled": True}
