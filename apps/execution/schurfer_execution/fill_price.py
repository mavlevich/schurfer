"""Shared, honest fill-price resolution for real exchange orders.

Ticker or mark price is never used as a substitute for a confirmed fill. If the
exchange has not confirmed a price through any of the recognized fields, the
caller gets `unresolved` and must not fabricate one — see incidents.py for what
happens next.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Any

FILL_CONFIRMED = "confirmed"
FILL_PARTIAL = "partial"
FILL_UNRESOLVED = "unresolved"

_DEFAULT_TIMEOUT_SECONDS = 10.0
_PARTIAL_FILL_TOLERANCE = 0.001  # 0.1% rounding slack before calling a fill partial


@dataclass(frozen=True)
class FillResolution:
    status: str  # confirmed | partial | unresolved
    price: float | None
    source: str
    filled_amount: float | None


def _finite_positive(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _from_order_fields(order: dict[str, Any]) -> tuple[float | None, float | None, str | None]:
    """Try average, then price, then cost/filled. Returns (price, filled_amount, source)."""
    filled = _finite_positive(order.get("filled"))
    average = _finite_positive(order.get("average"))
    if average is not None:
        return average, filled, "order.average"
    price = _finite_positive(order.get("price"))
    if price is not None:
        return price, filled, "order.price"
    cost = _finite_positive(order.get("cost"))
    if cost is not None and filled is not None:
        return cost / filled, filled, "order.cost_filled"
    return None, filled, None


async def _vwap_from_trades(
    exchange: Any,
    symbol: str,
    order_id: str,
    timeout_seconds: float,
) -> tuple[float | None, float | None]:
    """Best-effort VWAP from confirmed trades tied to this order id."""
    has = exchange.has if isinstance(exchange.has, dict) else {}
    trades: Any = None
    try:
        if has.get("fetchOrderTrades"):
            trades = await asyncio.wait_for(
                exchange.fetch_order_trades(order_id, symbol), timeout=timeout_seconds
            )
        elif has.get("fetchMyTrades"):
            trades = await asyncio.wait_for(
                exchange.fetch_my_trades(symbol, params={"orderId": order_id}),
                timeout=timeout_seconds,
            )
    except Exception:
        return None, None
    if not isinstance(trades, list) or not trades:
        return None, None
    total_cost = 0.0
    total_amount = 0.0
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        price = _finite_positive(trade.get("price"))
        amount = _finite_positive(trade.get("amount"))
        if price is None or amount is None:
            continue
        total_cost += price * amount
        total_amount += amount
    if total_amount <= 0:
        return None, None
    return total_cost / total_amount, total_amount


def _status_for(filled_amount: float | None, requested_amount: float | None) -> str:
    if (
        requested_amount is not None
        and filled_amount is not None
        and filled_amount < requested_amount * (1 - _PARTIAL_FILL_TOLERANCE)
    ):
        return FILL_PARTIAL
    return FILL_CONFIRMED


async def resolve_fill_price(
    exchange: Any,
    *,
    symbol: str,
    order: dict[str, Any],
    requested_amount: float | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> FillResolution:
    """Resolve an order's actual fill price without ever fabricating one.

    Priority: order.average -> order.price -> order.cost/filled -> re-fetch the
    order and retry the same chain -> VWAP of confirmed trades tied to the order
    id -> unresolved. Ticker/mark price is never used as a substitute.
    """
    order_id = order.get("id")
    price, filled_amount, source = _from_order_fields(order)
    if price is not None and source is not None:
        return FillResolution(
            status=_status_for(filled_amount, requested_amount),
            price=price,
            source=source,
            filled_amount=filled_amount,
        )

    if order_id is None:
        return FillResolution(
            status=FILL_UNRESOLVED, price=None, source="unresolved", filled_amount=None
        )

    try:
        refreshed = await asyncio.wait_for(
            exchange.fetch_order(order_id, symbol), timeout=timeout_seconds
        )
    except Exception:
        refreshed = None
    if isinstance(refreshed, dict):
        price, filled_amount, source = _from_order_fields(refreshed)
        if price is not None and source is not None:
            return FillResolution(
                status=_status_for(filled_amount, requested_amount),
                price=price,
                source=f"refetch.{source}",
                filled_amount=filled_amount,
            )

    vwap, trade_amount = await _vwap_from_trades(exchange, symbol, order_id, timeout_seconds)
    if vwap is not None:
        return FillResolution(
            status=_status_for(trade_amount, requested_amount),
            price=vwap,
            source="trades.vwap",
            filled_amount=trade_amount,
        )

    return FillResolution(
        status=FILL_UNRESOLVED, price=None, source="unresolved", filled_amount=None
    )
