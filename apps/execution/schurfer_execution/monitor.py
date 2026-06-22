from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import structlog

from .account import fetch_positions
from .orders import close_position

if TYPE_CHECKING:
    from .config import Config

log = structlog.get_logger()

_INTERVAL_SECONDS = 30


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

    reason: str | None = None

    if entry > 0 and mark > 0:
        pnl_pct = (entry - mark) / entry * 100 if side == "short" else (mark - entry) / entry * 100

        if pnl_pct >= cfg.take_profit_pct:
            reason = f"take_profit pnl={pnl_pct:.1f}%"
        elif pnl_pct <= -cfg.stop_loss_pct:
            reason = f"stop_loss pnl={pnl_pct:.1f}%"

    if reason is None:
        opened_at_raw = await rdb.get(f"position:opened_at:{exchange}:{base}")
        if opened_at_raw:
            age_min = (time.time() - float(opened_at_raw)) / 60
            if age_min >= cfg.max_hold_minutes:
                reason = f"max_hold age={age_min:.0f}min"

    if reason:
        await close_position(
            exchanges=exchanges,
            exchange=exchange,
            base=base,
            reason=reason,
            rdb=rdb,
        )
