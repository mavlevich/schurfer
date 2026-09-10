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
from datetime import UTC, datetime
from typing import Any

FILL_CONFIRMED = "confirmed"
FILL_PARTIAL = "partial"
# The exchange positively reports that nothing was filled. Distinct from
# `unresolved` (we do not know) and from `confirmed` (we know it filled): a
# zero-fill order opened no position, so the caller must unwind rather than
# journal an entry or claim a close (ENG-022 / audit C-3).
FILL_NONE = "none"
FILL_UNRESOLVED = "unresolved"

_DEFAULT_TIMEOUT_SECONDS = 10.0
_PARTIAL_FILL_TOLERANCE = 0.001  # 0.1% rounding slack before calling a fill partial
_TERMINAL_ORDER_STATUSES = frozenset({"closed", "canceled", "cancelled", "expired", "rejected"})


@dataclass(frozen=True)
class FillResolution:
    status: str  # confirmed | partial | none | unresolved
    price: float | None
    source: str
    filled_amount: float | None
    executed_at: datetime | None
    execution_time_source: str | None


def order_is_terminal(order: dict[str, Any]) -> bool:
    """Whether the unified exchange payload proves this order cannot fill more."""
    status = order.get("status")
    return isinstance(status, str) and status.lower() in _TERMINAL_ORDER_STATUSES


def _finite_positive(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _finite_non_negative(value: Any) -> float | None:
    """Filled volume specifically: an explicit 0 is evidence, not absence.

    `_finite_positive` collapses "filled 0" and "no filled field at all" into
    the same None, which is what let a zero-fill order with a stale average
    price be reported as a confirmed fill.
    """
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _execution_time(
    payload: dict[str, Any], *, trade_payload: bool = False
) -> tuple[datetime | None, str | None]:
    """Read CCXT's unified execution timestamp without inventing one.

    ``lastTradeTimestamp`` is the strongest order-level evidence.  Unified
    trade payloads may instead expose ``timestamp``/``datetime``; their
    weaker provenance is kept alongside the value so the journal can
    distinguish exchange evidence from a local observation-time fallback.
    """
    keys = ("lastTradeTimestamp", "timestamp") if trade_payload else ("lastTradeTimestamp",)
    for key in keys:
        raw = payload.get(key)
        if raw is None:
            continue
        try:
            numeric = float(raw)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(numeric) or numeric <= 0:
            continue
        # CCXT unified timestamps are milliseconds.  Accept seconds too for
        # defensive compatibility with hand-written/test clients.
        seconds = numeric / 1000 if numeric >= 100_000_000_000 else numeric
        try:
            return datetime.fromtimestamp(seconds, tz=UTC), f"exchange.{key}"
        except (OverflowError, OSError, ValueError):
            continue

    # Unified trade ``timestamp``/``datetime`` is execution evidence.  For
    # an order, those fields normally mean creation/submission time (a
    # standing stop may fill hours later), so only lastTradeTimestamp is
    # safe to treat as execution time above.
    raw_datetime = payload.get("datetime") if trade_payload else None
    if isinstance(raw_datetime, str) and raw_datetime:
        try:
            parsed = datetime.fromisoformat(raw_datetime.replace("Z", "+00:00"))
        except ValueError:
            return None, None
        if parsed.tzinfo is None:
            return None, None
        return parsed.astimezone(UTC), "exchange.datetime"
    return None, None


def _from_order_fields(order: dict[str, Any]) -> tuple[float | None, float | None, str | None]:
    """Try average, then price, then cost/filled. Returns (price, filled_amount, source)."""
    filled = _finite_non_negative(order.get("filled"))
    average = _finite_positive(order.get("average"))
    if average is not None:
        return average, filled, "order.average"
    price = _finite_positive(order.get("price"))
    if price is not None:
        return price, filled, "order.price"
    cost = _finite_positive(order.get("cost"))
    if cost is not None and filled is not None and filled > 0:
        return cost / filled, filled, "order.cost_filled"
    return None, filled, None


async def _vwap_from_trades(
    exchange: Any,
    symbol: str,
    order_id: str,
    timeout_seconds: float,
) -> tuple[float | None, float | None, datetime | None, str | None]:
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
        return None, None, None, None
    if not isinstance(trades, list) or not trades:
        return None, None, None, None
    total_cost = 0.0
    total_amount = 0.0
    latest_at: datetime | None = None
    latest_source: str | None = None
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        price = _finite_positive(trade.get("price"))
        amount = _finite_positive(trade.get("amount"))
        if price is None or amount is None:
            continue
        total_cost += price * amount
        total_amount += amount
        executed_at, time_source = _execution_time(trade, trade_payload=True)
        if executed_at is not None and (latest_at is None or executed_at > latest_at):
            latest_at = executed_at
            latest_source = time_source
    if total_amount <= 0:
        return None, None, None, None
    return total_cost / total_amount, total_amount, latest_at, latest_source


def _status_for(filled_amount: float | None, requested_amount: float | None) -> str:
    if (
        requested_amount is not None
        and filled_amount is not None
        and filled_amount < requested_amount * (1 - _PARTIAL_FILL_TOLERANCE)
    ):
        return FILL_PARTIAL
    return FILL_CONFIRMED


def _resolved(
    *,
    price: float,
    filled_amount: float | None,
    requested_amount: float | None,
    source: str,
    executed_at: datetime | None,
    execution_time_source: str | None,
) -> FillResolution:
    return FillResolution(
        status=_status_for(filled_amount, requested_amount),
        price=price,
        source=source,
        filled_amount=filled_amount,
        executed_at=executed_at,
        execution_time_source=execution_time_source,
    )


def _unresolved() -> FillResolution:
    return FillResolution(
        status=FILL_UNRESOLVED,
        price=None,
        source="unresolved",
        filled_amount=None,
        executed_at=None,
        execution_time_source=None,
    )


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
    id -> no_fill or unresolved. Ticker/mark price is never used as a substitute.

    An explicit filled=0 is never reported as a fill, whatever price the payload
    carries alongside it: that is what let an order which executed nothing be
    journalled as a real position at a stale average (ENG-022 / audit C-3).
    Such an order is re-checked and then reported as `none`. A missing filled
    field is a different thing, unknown volume rather than zero, and still
    resolves on price as before; callers that need a real notional must handle
    filled_amount being None rather than assume a full fill.
    """
    order_id = order.get("id")
    price, filled_amount, source = _from_order_fields(order)
    if price is not None and source is not None and filled_amount != 0:
        executed_at, execution_time_source = _execution_time(order)
        return _resolved(
            price=price,
            filled_amount=filled_amount,
            requested_amount=requested_amount,
            source=source,
            executed_at=executed_at,
            execution_time_source=execution_time_source,
        )

    if order_id is None:
        return _unresolved()

    # Either there is no price yet, or the exchange reported an explicit
    # filled=0 next to one. A zero-filled order executed nothing, so its
    # average is not a fill price and must not be returned as one; that is what
    # let an order which never executed be journalled as a real position
    # (ENG-022 / audit C-3). Re-fetch before concluding: a market order can
    # report zero filled in the immediate create response and settle a moment
    # later. A missing filled field is a different case and still resolves
    # normally above -- unknown volume, not zero.
    try:
        refreshed = await asyncio.wait_for(
            exchange.fetch_order(order_id, symbol), timeout=timeout_seconds
        )
    except Exception:
        refreshed = None
    refreshed_filled: float | None = None
    if isinstance(refreshed, dict):
        refreshed_price, refreshed_filled, refreshed_source = _from_order_fields(refreshed)
        if refreshed_price is not None and refreshed_source is not None and refreshed_filled != 0:
            executed_at, execution_time_source = _execution_time(refreshed)
            return _resolved(
                price=refreshed_price,
                filled_amount=refreshed_filled,
                requested_amount=requested_amount,
                source=f"refetch.{refreshed_source}",
                executed_at=executed_at,
                execution_time_source=execution_time_source,
            )

    vwap, trade_amount, executed_at, execution_time_source = await _vwap_from_trades(
        exchange, symbol, order_id, timeout_seconds
    )
    if vwap is not None and trade_amount is not None and trade_amount > 0:
        return _resolved(
            price=vwap,
            filled_amount=trade_amount,
            requested_amount=requested_amount,
            source="trades.vwap",
            executed_at=executed_at,
            execution_time_source=execution_time_source,
        )

    # Nothing filled, positively: both the freshest order view and the trade
    # history agree there is no executed volume. Reported as its own status so
    # the caller unwinds instead of opening incident-driven retries for a fill
    # that is never going to arrive.
    latest_filled = refreshed_filled if refreshed_filled is not None else filled_amount
    if latest_filled == 0:
        return FillResolution(
            status=FILL_NONE,
            price=None,
            source="no_fill",
            filled_amount=0.0,
            executed_at=None,
            execution_time_source=None,
        )

    return _unresolved()
