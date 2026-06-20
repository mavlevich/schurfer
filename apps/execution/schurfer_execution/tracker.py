import asyncio
from datetime import UTC, datetime
from typing import Any

import structlog

from .account import fetch_positions
from .risk import DAILY_PNL_KEY

log = structlog.get_logger()

_POLL_INTERVAL = 60


async def _tick(exchanges: dict[str, Any], rdb: Any, last_date: str | None) -> str:
    today = datetime.now(UTC).strftime("%Y-%m-%d")

    if last_date != today:
        await rdb.set(DAILY_PNL_KEY, "0")
        current = 0.0
        log.info("pnl_tracker.daily_reset", date=today)
    else:
        current = float(await rdb.get(DAILY_PNL_KEY) or 0)

    positions, failed = await fetch_positions(exchanges)
    if failed:
        # Fail-closed: don't overwrite with incomplete data.
        log.warning("pnl_tracker.skipping_update", failed_exchanges=failed)
        return today

    unrealized = round(sum(float(p.get("unrealized_pnl", 0.0)) for p in positions), 2)
    # Only record a new low: closed losses remain visible for the rest of the day.
    if unrealized < current:
        await rdb.set(DAILY_PNL_KEY, str(unrealized))
        log.debug("pnl_tracker.updated", daily_pnl=unrealized, open_positions=len(positions))

    return today


async def run_pnl_tracker(exchanges: dict[str, Any], rdb: Any) -> None:
    last_date: str | None = None
    while True:
        try:
            last_date = await _tick(exchanges, rdb, last_date)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("pnl_tracker.error", err=str(e))
        await asyncio.sleep(_POLL_INTERVAL)
