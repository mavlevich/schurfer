import asyncio
from typing import Any

import structlog

from . import journal
from .account import fetch_positions
from .risk import DAILY_PNL_KEY, PNL_READY_KEY, encode_pnl_lease

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
    generation: int | None = None
    if db_url:
        snapshot = await journal.pnl_snapshot(db_url)
        if snapshot is None:
            # A DB error is NOT "$0 realized today" — writing that would
            # silently reset the daily loss circuit breaker.
            await rdb.delete(PNL_READY_KEY)
            log.warning("pnl_tracker.skipping_update", reason="realized_pnl_unavailable")
            return
        if await journal.any_pending_closes(rdb):
            # A close was confirmed on the exchange but hasn't been committed
            # to the journal yet: its loss isn't in the snapshot yet, so
            # daily_pnl would understate real exposure if declared ready now.
            await rdb.delete(PNL_READY_KEY)
            log.warning("pnl_tracker.skipping_update", reason="pending_close_outstanding")
            return
        if snapshot.blocker is not None:
            # A partial close, an unconfirmed fill price or a closed live trade
            # whose exchange costs are not settled: its PnL impact is not final,
            # so daily_pnl must not be declared fresh while it's outstanding.
            await rdb.delete(PNL_READY_KEY)
            log.warning("pnl_tracker.skipping_update", reason=snapshot.blocker)
            return
        realized = snapshot.realized
        generation = snapshot.generation
    else:
        realized = 0.0
    daily_pnl = round(realized + unrealized, 2)

    await rdb.set(DAILY_PNL_KEY, str(daily_pnl))
    await rdb.set(
        PNL_READY_KEY,
        encode_pnl_lease(generation=generation, daily_pnl=daily_pnl),
        ex=_PNL_READY_TTL,
    )

    # run_pnl_tracker and the closing paths are concurrent: a close can land
    # between the snapshot above and this SET, and its own revoke can run
    # before the SET. Entries are safe regardless: place_order admits only
    # while the accounting generation still equals the lease's. These rechecks
    # only keep a stale lease from lingering until the next tick.
    if db_url and await journal.any_pending_closes(rdb):
        await rdb.delete(PNL_READY_KEY)
        log.warning("pnl_tracker.lease_revoked_race", reason="pending_close_appeared_after_publish")
        return
    if db_url and await journal.pnl_generation(db_url) != generation:
        await rdb.delete(PNL_READY_KEY)
        log.warning("pnl_tracker.lease_revoked_race", reason="journal_changed_after_snapshot")
        return

    log.debug(
        "pnl_tracker.updated",
        daily_pnl=daily_pnl,
        realized=round(realized, 2),
        unrealized=round(unrealized, 2),
        open_positions=len(positions),
    )


async def run_pnl_tracker(
    exchanges: dict[str, Any], rdb: Any, db_url: str | None, tracker: Any = None
) -> None:
    while True:
        if tracker:
            tracker.tick_started()
        try:
            await _tick(exchanges, rdb, db_url)
            if tracker:
                tracker.tick_succeeded()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if tracker:
                tracker.tick_failed(e)
            log.error("pnl_tracker.error", err=str(e))
        await asyncio.sleep(_POLL_INTERVAL)
