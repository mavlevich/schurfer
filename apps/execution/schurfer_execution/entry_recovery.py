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
    and the terminal order carries the final amount; ``FillGuard`` keeps what is
    held of it protected.

Reconciliation runs the same lookup, settle and guard under the same lock for an
entry it finds later (section 4, late_fill.py).
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from . import exchange_orders, order_ids, protective_stop

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


def stop_trigger(
    ex: Any, symbol: str, side: str, reference_price: float, initial_sl_pct: float
) -> float:
    trigger_price = (
        reference_price * (1 + initial_sl_pct / 100)
        if side == "short"
        else reference_price * (1 - initial_sl_pct / 100)
    )
    return float(ex.price_to_precision(symbol, trigger_price))


class FillGuard:
    """Protects what is left of every fill seen on an entry that is still active.

    At each look the stops of the entry, ``<entry>s1``, ``<entry>s2``, ..., are
    read back: what is left is the entry's fill less the stops' fills, and only a
    stop not yet terminal protects, for its amount less its fill. A gap gets a new
    stop of its own. No stop is resized or reused, so none has to be cancelled
    here: the emergency close task that flattens the entry cancels them all and
    confirms each one terminal (review: a stop that fired and ended no longer
    covers anything)."""

    def __init__(
        self,
        ex: Any,
        *,
        symbol: str,
        side: str,
        stop_side: str,
        reference_price: float,
        initial_sl_pct: float,
        entry_client_order_id: str,
    ) -> None:
        self.ex = ex
        self.symbol = symbol
        self.side = side
        self.stop_side = stop_side
        self.reference_price = reference_price
        self.initial_sl_pct = initial_sl_pct
        self.entry_client_order_id = entry_client_order_id
        self.next_sequence = 1
        self.placed: set[str] = set()
        self.seen_filled = Decimal(0)
        self.failure: str | None = None
        self.report = "nothing filled"

    async def adopt_existing(self) -> bool:
        """Take over the stops an earlier look placed (reconciliation finds an entry
        the entry path left active): they count from now on, and a new stop takes
        the next free sequence. False when the stops cannot be read."""
        stops = await entry_stops(self.ex, self.symbol, self.entry_client_order_id)
        if stops is None:
            return False
        prefix = order_ids.stop_prefix(self.entry_client_order_id)
        for stop in stops:
            client_order_id = exchange_orders.client_order_id(stop) or ""
            sequence = client_order_id.removeprefix(prefix)
            if sequence.isdigit():
                self.placed.add(client_order_id)
                self.next_sequence = max(self.next_sequence, int(sequence) + 1)
        return True

    async def _stops_view(self) -> tuple[Decimal, Decimal] | None:
        """``(filled, protecting)`` over the entry's stops, or None when any of it
        cannot be read: a stop placed here but missing from the listing proves
        nothing, and neither does a fill or an amount the venue omits."""
        stops = await entry_stops(self.ex, self.symbol, self.entry_client_order_id)
        if stops is None:
            return None
        if self.placed - {exchange_orders.client_order_id(stop) for stop in stops}:
            return None
        filled = protecting = Decimal(0)
        for stop in stops:
            stop_filled = exchange_orders.decimal_or_none(stop.get("filled"))
            if stop_filled is None:
                return None
            filled += stop_filled
            if not exchange_orders.is_terminal(stop):
                amount = exchange_orders.decimal_or_none(stop.get("amount"))
                if amount is None:
                    return None
                protecting += max(amount - stop_filled, Decimal(0))
        return filled, protecting

    async def cover(self, order: dict[str, Any]) -> None:
        filled = exchange_orders.decimal_or_none(order.get("filled")) or Decimal(0)
        view = await self._stops_view() if self.next_sequence > 1 else (Decimal(0), Decimal(0))
        if view is None:
            # The stops cannot be read: only the fill beyond the last look is
            # known to be bare.
            left, protecting = None, Decimal(0)
            gap = filled - self.seen_filled
        else:
            stops_filled, protecting = view
            left = max(filled - stops_filled, Decimal(0))
            gap = left - protecting
        self.seen_filled = max(self.seen_filled, filled)
        if gap > 0 and self.failure is None:
            average = exchange_orders.decimal_or_none(order.get("average"))
            placement = await protective_stop.place_protective_stop(
                self.ex,
                symbol=self.symbol,
                stop_side=self.stop_side,
                amount=float(gap),
                trigger_price=stop_trigger(
                    self.ex,
                    self.symbol,
                    self.side,
                    float(average) if average else self.reference_price,
                    self.initial_sl_pct,
                ),
                entry_client_order_id=self.entry_client_order_id,
                first_sequence=self.next_sequence,
            )
            self.next_sequence = placement.sequence + 1
            if placement.order is None:
                # Not resting: rejected, or already triggered. Either way the
                # flatten that follows settles it; no further stop is tried.
                self.failure = placement.error
            else:
                self.placed.add(placement.client_order_id)
                protecting += gap
        self.report = self._describe(filled, left, protecting)

    def _describe(self, filled: Decimal, left: Decimal | None, protecting: Decimal) -> str:
        if left is None:
            return (
                f"{filled} filled, and the fills and states of its stops could not be "
                "read: what is left UNPROTECTED is unknown"
            )
        held = f"{left} of the {filled} filled is still held"
        if protecting >= left:
            return f"{held}, all of it covered by resting stops"
        missing = f"{left - protecting} UNPROTECTED: {held}, {protecting} covered by resting stops"
        return f"{missing} ({self.failure})" if self.failure else missing
