import asyncio
from typing import Any

import structlog

from . import incidents, journal
from .account import fetch_positions
from .risk import DAILY_PNL_KEY, PNL_READY_KEY

log = structlog.get_logger()

_POLL_INTERVAL = 60
# Short TTL so a crashed/hung tracker process fails closed on its own within
# a couple of missed ticks, rather than leaving a stale "ready" lease behind.
_PNL_READY_TTL = 120


async def _tick(exchanges: dict[str, Any], rdb: Any, db_url: str | None) -> None:
    positions, failed = await fetch_positions(exchanges)
    if failed:
        # Current exposure can't be verified. Revoke any existing lease now
        # rather than letting a stale one (set on an earlier, still-valid
        # tick) keep permitting trades for up to its remaining TTL.
        await rdb.delete(PNL_READY_KEY)
        log.warning("pnl_tracker.skipping_update", failed_exchanges=failed)
        return

    unrealized = sum(float(p.get("unrealized_pnl", 0.0)) for p in positions)
    # Realized PnL is recomputed from the journal (source of truth) on every
    # tick rather than tracked in-process, so it survives execution restarts
    # and Redis eviction without any reset/rollover bookkeeping.
    if db_url:
        realized = await journal.realized_pnl_today(db_url)
        if realized is None:
            # A DB error is NOT "$0 realized today" — writing that would
            # silently reset the daily loss circuit breaker.
            await rdb.delete(PNL_READY_KEY)
            log.warning("pnl_tracker.skipping_update", reason="realized_pnl_unavailable")
            return
        if await journal.any_pending_closes(rdb):
            # A close was confirmed on the exchange but hasn't been committed
            # to the journal yet — its loss isn't in realized_pnl_today() yet,
            # so daily_pnl would understate real exposure if declared ready now.
            await rdb.delete(PNL_READY_KEY)
            log.warning("pnl_tracker.skipping_update", reason="pending_close_outstanding")
            return
        if await incidents.any_open_incidents(db_url):
            # A fill's price is not yet confirmed at all (see fill_price.py) —
            # its PnL impact is unknown, not zero, so daily_pnl must not be
            # declared fresh while it's outstanding.
            await rdb.delete(PNL_READY_KEY)
            log.warning("pnl_tracker.skipping_update", reason="fill_incident_outstanding")
            return
    else:
        realized = 0.0
    daily_pnl = round(realized + unrealized, 2)

    await rdb.set(DAILY_PNL_KEY, str(daily_pnl))
    await rdb.set(PNL_READY_KEY, "1", ex=_PNL_READY_TTL)

    # run_pnl_tracker and run_position_monitor are concurrent asyncio tasks —
    # a close's journal write can land between the any_pending_closes() check
    # above and this SET. Re-check immediately after publishing and revoke
    # again if one appeared; write_pending_close() also revokes on its own,
    # but this closes the gap for the specific interleaving where the lease
    # gets set after that revoke already ran.
    if db_url and await journal.any_pending_closes(rdb):
        await rdb.delete(PNL_READY_KEY)
        log.warning("pnl_tracker.lease_revoked_race", reason="pending_close_appeared_after_publish")
        return

    log.debug(
        "pnl_tracker.updated",
        daily_pnl=daily_pnl,
        realized=round(realized, 2),
        unrealized=round(unrealized, 2),
        open_positions=len(positions),
    )


async def run_pnl_tracker(exchanges: dict[str, Any], rdb: Any, db_url: str | None) -> None:
    while True:
        try:
            await _tick(exchanges, rdb, db_url)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("pnl_tracker.error", err=str(e))
        await asyncio.sleep(_POLL_INTERVAL)
