from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request

from ..account import fetch_balance, fetch_positions
from ..risk import DAILY_PNL_KEY, TRADING_ENABLED_KEY

if TYPE_CHECKING:
    from ..config import Config

router = APIRouter()


@router.get("/balance")
async def get_balance(request: Request) -> dict[str, Any]:
    balances = await fetch_balance(request.app.state.exchanges)
    total = sum(b["total"] for b in balances)
    return {"balances": balances, "total_usd": round(total, 2)}


@router.get("/positions")
async def get_positions(request: Request) -> dict[str, Any]:
    positions, _ = await fetch_positions(request.app.state.exchanges)
    return {"positions": positions, "count": len(positions)}


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
