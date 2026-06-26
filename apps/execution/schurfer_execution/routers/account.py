from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from fastapi import APIRouter, Request
from pydantic import BaseModel

from .. import exit as exit_module
from .. import journal
from ..account import fetch_balance, fetch_positions
from ..orders import close_position
from ..risk import DAILY_PNL_KEY, TRADING_ENABLED_KEY

log = structlog.get_logger()

if TYPE_CHECKING:
    from ..config import Config


class CloseBody(BaseModel):
    exchange: str
    base: str


router = APIRouter()


@router.get("/balance")
async def get_balance(request: Request) -> dict[str, Any]:
    balances = await fetch_balance(request.app.state.exchanges)
    total_usdt = sum(
        b["total"]
        for b in balances
        if b.get("tradeable", True) and b.get("asset", "USDT") == "USDT"
    )
    total_all = sum(b.get("usd_value", 0.0) for b in balances)
    failed = list({b["exchange"] for b in balances if b.get("error")})
    return {
        "balances": balances,
        "total_usd": round(total_usdt, 2),
        "total_usd_all": round(total_all, 2),
        "failed_exchanges": failed,
    }


@router.get("/positions")
async def get_positions(request: Request) -> dict[str, Any]:
    positions, _ = await fetch_positions(request.app.state.exchanges)
    return {"positions": positions, "count": len(positions)}


@router.post("/positions/close")
async def manual_close_position(body: CloseBody, request: Request) -> dict[str, Any]:
    cfg: Config = request.app.state.cfg
    rdb = request.app.state.rdb

    result = await close_position(
        exchanges=request.app.state.exchanges,
        exchange=body.exchange,
        base=body.base,
        reason="manual",
        rdb=rdb,
    )

    if result.get("closed"):
        if cfg.db_url:
            trade_id_raw = await rdb.get(f"trade:id:{body.exchange}:{body.base.upper()}")
            if trade_id_raw:
                entry_raw = await rdb.get(exit_module.entry_key(body.exchange, body.base))
                side_raw = await rdb.get(exit_module.side_key(body.exchange, body.base))
                entry_price = float(entry_raw) if entry_raw else 0.0
                side = side_raw.decode() if side_raw else result.get("side", "short")
                exit_price: float | None = result.get("exit_price")
                if not exit_price:
                    log.warning(
                        "manual_close.no_exit_price",
                        exchange=body.exchange,
                        base=body.base,
                        trade_id=int(trade_id_raw),
                    )
                else:
                    await journal.close_trade(
                        cfg.db_url,
                        trade_id=int(trade_id_raw),
                        exit_order_id=result.get("order_id"),
                        exit_price=exit_price,
                        entry_price=entry_price,
                        side=side,
                        reason="manual",
                    )
                await rdb.delete(f"trade:id:{body.exchange}:{body.base.upper()}")
        await rdb.delete(exit_module.best_price_key(body.exchange, body.base))
        await rdb.delete(exit_module.params_key(body.exchange, body.base))
        await rdb.delete(exit_module.entry_key(body.exchange, body.base))
        await rdb.delete(exit_module.side_key(body.exchange, body.base))

    return result


@router.get("/risk")
async def get_risk(request: Request) -> dict[str, Any]:
    cfg: Config = request.app.state.cfg
    rdb = request.app.state.rdb

    positions, _ = await fetch_positions(request.app.state.exchanges)
    daily_pnl = float(await rdb.get(DAILY_PNL_KEY) or 0)
    trading_enabled = (await rdb.get(TRADING_ENABLED_KEY) or b"1").decode()

    return {
        "trading_enabled": trading_enabled not in ("0", "false"),
        "open_positions": len(positions),
        "max_positions": cfg.max_positions,
        "slots_free": max(0, cfg.max_positions - len(positions)),
        "daily_pnl_usd": round(daily_pnl, 2),
        "daily_loss_limit_usd": cfg.daily_loss_limit_usd,
    }
