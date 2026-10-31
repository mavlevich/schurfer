"""The entry whose submission result is unknown.

docs/engineering/live-entry-failure-paths-design-v1.md, section 3. When the entry
request times out or fails without a determinable answer, the attempt is already
durable as ``submission_unknown`` and entries are blocked before anything here
runs, so a crash during the lookup leaves a recoverable state. Everything here runs
under the entry's order lock.

  * The order is looked up by its ``clientOrderId`` at about 1, 3, 6 and 10 s. A
    lookup that finds nothing proves nothing: the attempt stays unknown and
    reconciliation continues the lookup.
  * An order still active is cancelled, and the cancel is confirmed by finding the
    order terminal. Every fill seen meanwhile is reported to the caller as it grows,
    and the terminal order carries the final amount.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import structlog

from . import exchange_orders, order_ids

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = structlog.get_logger()

# Seconds since the failed request, measured on testnet before mainnet.
ENTRY_LOOKUP_AT_SECONDS: tuple[float, ...] = (1.0, 3.0, 6.0, 10.0)
# Seconds since the cancel of what is left of an active entry.
CANCEL_CONFIRM_AT_SECONDS: tuple[float, ...] = (0.5, 1.5, 3.0, 5.0)
LOOKUP_TIMEOUT_SECONDS = 3.0


async def _poll(
    at_seconds: tuple[float, ...],
    probe: Callable[[], Awaitable[dict[str, Any] | None]],
    sleep: Callable[[float], Awaitable[None]],
) -> dict[str, Any] | None:
    started = time.monotonic()
    for at in at_seconds:
        wait = at - (time.monotonic() - started)
        if wait > 0:
            await sleep(wait)
        found = await probe()
        if found is not None:
            return found
    return None


async def find_entry(
    ex: Any,
    symbol: str,
    client_order_id: str,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, Any] | None:
    """The entry order in whatever state it is found, or None."""

    async def probe() -> dict[str, Any] | None:
        return await exchange_orders.look_up(ex, symbol, client_order_id, LOOKUP_TIMEOUT_SECONDS)

    return await _poll(ENTRY_LOOKUP_AT_SECONDS, probe, sleep)


async def settle_active_entry(
    ex: Any,
    symbol: str,
    order: dict[str, Any],
    *,
    on_active: Callable[[dict[str, Any]], Awaitable[None]],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """``(terminal, latest)``: the entry order once terminal (None when it is not
    confirmed terminal), and the latest view of it seen. What is left is
    cancelled; ``on_active`` sees the order, before the cancel and at every look
    that finds it still active, so a fill that grows meanwhile is acted on."""
    if exchange_orders.is_terminal(order):
        return order, order
    client_order_id = exchange_orders.client_order_id(order)
    await on_active(order)
    if client_order_id is None:
        return None, order
    try:
        await ex.cancel_order(str(order["id"]), symbol)
    except Exception as exc:
        # An order that filled meanwhile can no longer be cancelled; only the
        # lookups below say how it ended.
        log.warning(
            "execution.entry_recovery.cancel_failed", client_order_id=client_order_id, err=str(exc)
        )
    latest = order
    started = time.monotonic()
    for at in CANCEL_CONFIRM_AT_SECONDS:
        wait = at - (time.monotonic() - started)
        if wait > 0:
            await sleep(wait)
        found = await exchange_orders.look_up(ex, symbol, client_order_id, LOOKUP_TIMEOUT_SECONDS)
        if found is None:
            continue
        latest = found
        if exchange_orders.is_terminal(found):
            return found, found
        await on_active(found)
    return None, latest


async def entry_stops(
    ex: Any, symbol: str, entry_client_order_id: str
) -> list[dict[str, Any]] | None:
    """Every stop of the entry, ``<entry>s*``, in whatever state, or None when the
    listing fails: a partial view could hide a stop's fill."""
    try:
        return await asyncio.wait_for(
            exchange_orders.orders_with_prefix(
                ex, symbol, order_ids.stop_prefix(entry_client_order_id)
            ),
            timeout=LOOKUP_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        log.warning(
            "execution.entry_recovery.stop_listing_failed",
            client_order_id=entry_client_order_id,
            err=str(exc),
        )
        return None
