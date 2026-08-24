import re
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from .. import symbols
from ..orders import place_order

log = structlog.get_logger()

router = APIRouter()

_BASE_RE = re.compile(r"^[A-Z0-9]{2,20}$")


class OrderRequest(BaseModel):
    base: str
    exchange: str
    side: Literal["short", "long"]
    size_usd: Annotated[float, Field(gt=0, le=10_000)]
    leverage: Annotated[int, Field(ge=1, le=125)] = 2

    @field_validator("base")
    @classmethod
    def normalize_base(cls, v: str) -> str:
        v = v.strip().upper()
        if not _BASE_RE.match(v):
            raise ValueError("base must be 2-20 uppercase alphanumeric characters (A-Z, 0-9)")
        return v


@router.post("/order")
async def post_order(req: OrderRequest, request: Request) -> dict[str, Any]:
    cfg = request.app.state.cfg
    exchanges = request.app.state.trading_exchanges

    if req.exchange not in exchanges:
        raise HTTPException(status_code=400, detail=f"exchange {req.exchange!r} not configured")

    try:
        instrument = symbols.resolve_execution_instrument(exchanges[req.exchange], req.base)
        result: dict[str, Any] = await place_order(
            base=req.base,
            symbol=instrument.symbol,
            exchange=req.exchange,
            side=req.side,
            size_usd=req.size_usd,
            leverage=req.leverage,
            exchanges=exchanges,
            rdb=request.app.state.rdb,
            max_positions=cfg.max_positions,
            max_position_usd=cfg.max_position_usd,
            daily_loss_limit_usd=cfg.daily_loss_limit_usd,
            liquidation_buffer_pct=cfg.liquidation_buffer_pct,
            # cfg (not passed before this) is what lets place_order's own
            # journal.complete_open give this order a real app.trades row --
            # previously a manually-triggered order was placed for real but
            # never journaled at all, since journal.open_trade used to live
            # only in trader.py's automated path. An explicit strategy
            # identity, not the pump_short default journal.strategy_identity
            # falls back to, so a manual order is never misattributed to the
            # automated strategy in the ledger.
            cfg=cfg,
            setup_context={"strategy_name": "manual", "strategy_version": "1"},
        )
    except (RuntimeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        log.error("execution.order.failed", base=req.base, exchange=req.exchange, err=str(e))
        raise HTTPException(status_code=502, detail=str(e)) from e

    if not result["allowed"]:
        raise HTTPException(status_code=409, detail=result["reason"])

    return result
