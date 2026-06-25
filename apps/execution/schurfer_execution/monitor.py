from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import structlog

from . import exit as exit_module
from . import journal, notify
from .account import fetch_positions
from .orders import close_position

if TYPE_CHECKING:
    from .config import Config

log = structlog.get_logger()

_INTERVAL_SECONDS = 30
_TRADE_ID_KEY = "trade:id:{exchange}:{base}"


async def run_position_monitor(
    exchanges: dict[str, Any],
    rdb: Any,
    cfg: Config,
) -> None:
    while True:
        await asyncio.sleep(_INTERVAL_SECONDS)
        try:
            await _tick(exchanges, rdb, cfg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("position_monitor.error", err=str(e))


async def _tick(exchanges: dict[str, Any], rdb: Any, cfg: Config) -> None:
    positions, failed = await fetch_positions(exchanges)
    for pos in positions:
        if pos["exchange"] in failed:
            continue
        await _check_exit(pos, rdb, cfg, exchanges)


async def _check_exit(
    position: dict[str, Any],
    rdb: Any,
    cfg: Config,
    exchanges: dict[str, Any],
) -> None:
    exchange = position["exchange"]
    base = position["base"]
    side = position["side"]
    entry = position["entry_price"]
    mark = position.get("mark_price", 0.0)

    if entry <= 0 or mark <= 0:
        return

    opened_at_raw = await rdb.get(f"position:opened_at:{exchange}:{base}")
    opened_at = float(opened_at_raw) if opened_at_raw else time.time()

    params_raw = await rdb.get(exit_module.params_key(exchange, base))
    params = exit_module.load_exit_params(params_raw)

    bp_key = exit_module.best_price_key(exchange, base)
    reason = await exit_module.check_exit(
        side=side,
        entry_price=entry,
        current_price=mark,
        opened_at=opened_at,
        params=params,
        rdb=rdb,
        bp_key=bp_key,
    )

    if not reason:
        return

    result = await close_position(
        exchanges=exchanges,
        exchange=exchange,
        base=base,
        reason=reason,
        rdb=rdb,
    )

    if result.get("closed"):
        exit_price = float(result.get("exit_price") or mark)
        await rdb.delete(bp_key)
        await rdb.delete(exit_module.params_key(exchange, base))

        trade_id_raw = await rdb.get(_TRADE_ID_KEY.format(exchange=exchange, base=base.upper()))
        if trade_id_raw and cfg.db_url:
            await journal.close_trade(
                cfg.db_url,
                trade_id=int(trade_id_raw),
                exit_order_id=result.get("order_id"),
                exit_price=exit_price,
                entry_price=entry,
                side=side,
                reason=reason,
            )
            await rdb.delete(_TRADE_ID_KEY.format(exchange=exchange, base=base.upper()))

        creds = notify.credentials(cfg)
        if creds:
            pnl_pct_final = (
                (entry - exit_price) / entry * 100
                if side == "short"
                else (exit_price - entry) / entry * 100
            )
            await notify.notify_close(
                *creds,
                base=base,
                exchange=exchange,
                entry_price=entry,
                exit_price=exit_price,
                pnl_pct=pnl_pct_final,
                reason=reason,
                paper=False,
            )
