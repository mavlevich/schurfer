"""Keep each exchange client's market metadata current while the process runs.

CCXT caches an exchange's instrument catalog in `client.markets`, and this
service loaded it exactly once, at startup (`main._preload_markets`). Nothing
refreshed it afterwards, so every instrument listed after the process started
stayed invisible to `symbols.resolve_execution_instrument` for the entire
lifetime of that process.

That is not a theoretical gap. Production evidence, 2026-09-07 (ENG-031):
AMEMECOIN was onboarded on bingx on 2026-09-04 while the execution container
had been running since roughly 2026-08-28. Its pump episode ran to +480% over
sixteen hours, and the trader wrote 29 consecutive `skipped` decisions with
reason `execution_instrument_unresolved` across thirteen hours of it. The very
next evaluation after a deploy restarted the service opened a paper trade on
the same instrument. Over thirty days the same reason accounts for 816 skipped
evaluations across seven venues, including pumps of 844% and 907%.

The strategy family here is new-listing pumps, so the blind spot lined up
exactly with the instruments the system exists to evaluate. And because
production runs paper, the cost was not missed trades but missed evidence: the
observations that candidate promotion depends on were never generated.

Two mechanisms, deliberately both:

- a periodic refresh, so the catalog cannot drift for longer than one interval
  no matter what else happens;
- a refresh on a resolution miss, because a pump on a fresh listing can be over
  in minutes and waiting out a full interval would still miss it.

Both go through the same per-exchange cooldown, so a base that genuinely does
not exist on a venue (CZ is a real one: listed on lbank, absent from mexc and
bingx) cannot turn every tick into an API call.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

log = structlog.get_logger()

DEFAULT_REFRESH_INTERVAL_SECONDS = 900.0
#: Floor between two reloads of the SAME exchange, whichever mechanism asks.
DEFAULT_MIN_REFRESH_INTERVAL_SECONDS = 60.0
_RELOAD_TIMEOUT_SECONDS = 30.0


class MarketRefresher:
    """Reloads `client.markets` for a set of exchange clients, with a cooldown.

    Holds the same client objects every other component already uses, so a
    reload here is immediately visible to them: ccxt mutates the client's own
    `markets` dict in place.
    """

    def __init__(
        self,
        exchanges: dict[str, Any],
        *,
        min_interval_seconds: float = DEFAULT_MIN_REFRESH_INTERVAL_SECONDS,
        now: Any = time.monotonic,
    ) -> None:
        self._exchanges = exchanges
        self._min_interval = min_interval_seconds
        self._now = now
        self._last_refresh: dict[str, float] = {}
        # One lock per exchange: two concurrent misses on the same venue must
        # not both issue a reload, while a miss on one venue must not wait on
        # another venue's slow load_markets.
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, name: str) -> asyncio.Lock:
        lock = self._locks.get(name)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[name] = lock
        return lock

    async def refresh(self, name: str, *, reason: str, force: bool = False) -> bool:
        """Reload one exchange. Returns True when a reload actually happened.

        `force` skips the cooldown and is for the periodic sweep, whose own
        interval is the rate limit. A miss-triggered call never forces.
        """
        client = self._exchanges.get(name)
        if client is None:
            return False
        async with self._lock(name):
            last = self._last_refresh.get(name)
            if not force and last is not None and (self._now() - last) < self._min_interval:
                log.debug("market_refresh.skipped_cooldown", exchange=name, reason=reason)
                return False
            # Stamped before the await, not after: a reload that fails or times
            # out must still hold off the next attempt, or a permanently broken
            # venue turns every miss into another call.
            self._last_refresh[name] = self._now()
            try:
                await asyncio.wait_for(client.load_markets(True), timeout=_RELOAD_TIMEOUT_SECONDS)
            except Exception as exc:
                log.warning("market_refresh.failed", exchange=name, reason=reason, err=str(exc))
                return False
            log.info(
                "market_refresh.reloaded",
                exchange=name,
                reason=reason,
                markets=len(getattr(client, "markets", {}) or {}),
            )
            return True

    async def refresh_all(self) -> int:
        """Reload every client, one venue's failure never stopping the others."""
        results = await asyncio.gather(
            *(self.refresh(name, reason="periodic", force=True) for name in self._exchanges),
            return_exceptions=True,
        )
        return sum(1 for result in results if result is True)


async def run_market_refresher(
    refresher: MarketRefresher,
    *,
    interval_seconds: float = DEFAULT_REFRESH_INTERVAL_SECONDS,
    tracker: Any = None,
) -> None:
    """Long-running task: reload every exchange's catalog on a fixed cadence.

    Deliberately does NOT refresh immediately on start: main already preloads
    the catalog before any worker runs, and reloading it again seconds later
    would only spend rate limit.
    """
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            if tracker:
                tracker.tick_started()
            reloaded = await refresher.refresh_all()
            if tracker:
                if reloaded:
                    tracker.tick_succeeded()
                else:
                    tracker.tick_idle()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("market_refresh.tick_failed", err=str(exc))
            if tracker:
                tracker.tick_failed(exc)
