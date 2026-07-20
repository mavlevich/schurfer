"""Order-book liquidity snapshot at decision time.

Spread and depth at the moment a signal is evaluated cannot be reconstructed
from historical OHLCV, so we capture them live for tradeable candidates. The
snapshot feeds realistic-fill and capacity analysis later.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any

import structlog

log = structlog.get_logger()

# Notional sizes (USD) we measure price impact at.
_DEPTH_TARGETS_USD = (100.0, 500.0, 1000.0)

# Order-book depth to request. Enough levels to fill the largest target on
# normal books without pulling the whole book.
_BOOK_LIMIT = 50

_FETCH_TIMEOUT = 5  # seconds


def _vwap_impact_bps(levels: list[Any], mid: float, target_usd: float, side: str) -> float | None:
    """Return the volume-weighted slippage in bps to fill target_usd across levels.

    None means the visible book cannot fill target_usd. side is "ask" (buying,
    price goes up) or "bid" (selling, price goes down); the result is the
    non-negative distance of the fill VWAP from mid.
    """
    total_quote = 0.0
    total_base = 0.0
    for level in levels:
        price = float(level[0])
        amount = float(level[1])
        # NaN/inf never raise from float(), so guard explicitly. A non-finite or
        # non-positive level is skipped rather than poisoning the whole result.
        if not (math.isfinite(price) and math.isfinite(amount)) or price <= 0 or amount <= 0:
            continue
        remaining = target_usd - total_quote
        if remaining <= 0:
            break
        take_usd = min(price * amount, remaining)
        total_quote += take_usd
        total_base += take_usd / price

    if total_quote < target_usd or total_base <= 0:
        return None
    vwap = total_quote / total_base
    impact = (vwap - mid) / mid if side == "ask" else (mid - vwap) / mid
    result = round(impact * 10000, 2)
    return result if math.isfinite(result) else None


async def snapshot(ex: Any, base: str) -> dict[str, Any] | None:
    """Fetch the order book for base on ex and summarize spread and depth impact.

    Returns None on any failure or an unusable book. Never raises: the fetch is
    bounded by a timeout and the entire parse is guarded, so a slow exchange or a
    malformed order book cannot block or abort the caller's tick.
    """
    symbol = f"{base.upper()}/USDT:USDT"
    try:
        book = await asyncio.wait_for(
            ex.fetch_order_book(symbol, _BOOK_LIMIT), timeout=_FETCH_TIMEOUT
        )
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return None

        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        # Reject NaN/inf tops of book: they do not raise, and would serialize to
        # an invalid JSONB token that the writer could never insert (poison row).
        if not (math.isfinite(best_bid) and math.isfinite(best_ask)):
            return None
        if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
            return None

        mid = (best_bid + best_ask) / 2
        return {
            "ts": int(time.time()),
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": mid,
            "spread_bps": round((best_ask - best_bid) / mid * 10000, 2),
            # bid side = selling (short entry), ask side = buying (exit).
            "bid_impact_bps": {
                str(int(t)): _vwap_impact_bps(bids, mid, t, "bid") for t in _DEPTH_TARGETS_USD
            },
            "ask_impact_bps": {
                str(int(t)): _vwap_impact_bps(asks, mid, t, "ask") for t in _DEPTH_TARGETS_USD
            },
        }
    except Exception as exc:
        # Includes TimeoutError from wait_for and any parse error on a malformed book.
        log.warning("liquidity.snapshot_failed", symbol=symbol, err=str(exc))
        return None
