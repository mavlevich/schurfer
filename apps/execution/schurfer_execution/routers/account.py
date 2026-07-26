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
    balances = await fetch_balance(request.app.state.trading_exchanges)
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
    positions, _ = await fetch_positions(request.app.state.trading_exchanges)
    return {"positions": positions, "count": len(positions)}


@router.post("/positions/close")
async def manual_close_position(body: CloseBody, request: Request) -> dict[str, Any]:
    cfg: Config = request.app.state.cfg
    rdb = request.app.state.rdb

    result = await close_position(
        exchanges=request.app.state.trading_exchanges,
        exchange=body.exchange,
        base=body.base,
        reason="manual",
        rdb=rdb,
    )

    if result.get("closed"):
        if cfg.db_url:
            trade_id_key = f"trade:id:{body.exchange}:{body.base.upper()}"
            trade_id_raw = await rdb.get(trade_id_key)
            if trade_id_raw:
                trade_id = int(trade_id_raw)
                exit_price: float | None = result.get("exit_price")
                if not exit_price:
                    # No usable price at all — can't safely commit or queue a
                    # retry (0 would read as a false profit). Leave the
                    # trade-id pointer in place for a human to investigate.
                    # PnL impact is unknown, so any active readiness lease is
                    # stale — revoke it now rather than waiting for the
                    # tracker's next tick.
                    await journal.revoke_pnl_readiness(rdb)
                    log.warning(
                        "manual_close.no_exit_price",
                        exchange=body.exchange,
                        base=body.base,
                        trade_id=trade_id,
                    )
                else:
                    # entry_price/side are loaded from the trade's own DB row
                    # by journal.close_trade — no dependency on the Redis
                    # entry/side cache, which may have been evicted.
                    committed = await journal.try_commit_close(
                        cfg.db_url,
                        rdb,
                        exchange=body.exchange,
                        base=body.base.upper(),
                        trade_id=trade_id,
                        exit_order_id=result.get("order_id"),
                        exit_price=exit_price,
                        reason="manual",
                    )
                    # Only drop the pointer once durably recorded, and only if
                    # it still points at this trade — otherwise a DB outage
                    # here permanently loses this trade's PnL, or a slow retry
                    # could delete a newer trade's pointer for this symbol.
                    if committed:
                        await journal.delete_trade_id_if_matches(rdb, trade_id_key, trade_id)
                    else:
                        log.error(
                            "manual_close.journal_close_failed_pending_retry",
                            exchange=body.exchange,
                            base=body.base,
                            trade_id=trade_id,
                        )
        await rdb.delete(exit_module.best_price_key(body.exchange, body.base))
        await rdb.delete(exit_module.params_key(body.exchange, body.base))
        await rdb.delete(exit_module.entry_key(body.exchange, body.base))
        await rdb.delete(exit_module.side_key(body.exchange, body.base))

    return result


@router.get("/risk")
async def get_risk(request: Request) -> dict[str, Any]:
    cfg: Config = request.app.state.cfg
    rdb = request.app.state.rdb

    positions, _ = await fetch_positions(request.app.state.trading_exchanges)
    daily_pnl = float(await rdb.get(DAILY_PNL_KEY) or 0)
    # Mirrors the fail-closed default in orders.place_order.
    trading_enabled = (await rdb.get(TRADING_ENABLED_KEY) or b"0").decode()

    return {
        "trading_enabled": trading_enabled not in ("0", "false"),
        "open_positions": len(positions),
        "max_positions": cfg.max_positions,
        "slots_free": max(0, cfg.max_positions - len(positions)),
        "daily_pnl_usd": round(daily_pnl, 2),
        "daily_loss_limit_usd": cfg.daily_loss_limit_usd,
    }
