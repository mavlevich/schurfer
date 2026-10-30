"""Exchange order lookups shared by reconciliation and the emergency close executor.

A lookup that finds nothing proves nothing (design principle 3): callers treat a
missing order as unknown, never as absent.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

NO_FILL_STATUSES = frozenset({"canceled", "cancelled", "rejected", "expired"})
TERMINAL_STATUSES = NO_FILL_STATUSES | {"closed"}

# Plain listings on Bybit return active orders only; conditional (stop) orders
# need the trigger filter. Both are read so a resting stop is never missed.
_LISTING_PARAMS: tuple[dict[str, Any], ...] = ({}, {"trigger": True})


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def client_order_id(order: dict[str, Any]) -> str | None:
    info = order.get("info")
    info_dict = info if isinstance(info, dict) else {}
    value = (
        order.get("clientOrderId")
        or info_dict.get("clientOrderId")
        or info_dict.get("clientOid")
        or info_dict.get("orderLinkId")
    )
    return str(value) if value is not None else None


def is_terminal(order: dict[str, Any]) -> bool:
    return str(order.get("status") or "").lower() in TERMINAL_STATUSES


def attempt_state_from_order(
    order: dict[str, Any],
) -> tuple[str, Decimal | None, str | None]:
    filled = decimal_or_none(order.get("filled"))
    exchange_status = str(order.get("status") or "").lower()
    if filled is not None and filled > 0:
        if exchange_status in TERMINAL_STATUSES:
            return "completed", filled, None
        # Filled in part while the order may still fill further: the final
        # quantity is not known yet, so the attempt stays unresolved
        # ("accepted" with the filled amount so far) and keeps blocking entries.
        return "accepted", filled, None
    if exchange_status in TERMINAL_STATUSES and filled == 0:
        return "no_fill", filled, None
    if exchange_status in TERMINAL_STATUSES:
        return "manual_required", filled, "terminal exchange order omitted filled amount"
    return "accepted", filled, None


async def list_orders(exchange: Any, symbol: str) -> list[dict[str, Any]]:
    """Every open and closed order of the symbol, plain and conditional, by id.

    A failed listing raises: a partial view could hide a resting order.
    """
    orders: dict[str, dict[str, Any]] = {}
    for capability, method_name in (
        ("fetchOpenOrders", "fetch_open_orders"),
        ("fetchClosedOrders", "fetch_closed_orders"),
    ):
        if not exchange.has.get(capability):
            raise RuntimeError(f"exchange cannot {capability}")
        for params in _LISTING_PARAMS:
            for order in await getattr(exchange, method_name)(symbol, params=dict(params)):
                if isinstance(order, dict):
                    key = str(order.get("id") or client_order_id(order) or id(order))
                    orders[key] = _more_complete(orders.get(key), order)
    return list(orders.values())


def _more_complete(seen: dict[str, Any] | None, order: dict[str, Any]) -> dict[str, Any]:
    """The same order can appear in both listings (a triggered stop becomes a
    market order under its id); keep the view with the larger fill, then the
    terminal one."""
    if seen is None:
        return order

    def rank(candidate: dict[str, Any]) -> tuple[Decimal, bool]:
        filled = decimal_or_none(candidate.get("filled")) or Decimal(0)
        return filled, is_terminal(candidate)

    return order if rank(order) > rank(seen) else seen


async def find_by_client_order_id(exchange: Any, symbol: str, wanted: str) -> dict[str, Any] | None:
    for order in await list_orders(exchange, symbol):
        if client_order_id(order) == wanted:
            return order
    return None


async def orders_with_prefix(exchange: Any, symbol: str, prefix: str) -> list[dict[str, Any]]:
    return [
        order
        for order in await list_orders(exchange, symbol)
        if (client_order_id(order) or "").startswith(prefix)
    ]
