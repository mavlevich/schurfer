"""Order-book liquidity snapshot at decision time.

Spread and depth at the moment a signal is evaluated cannot be reconstructed
from historical OHLCV, so we capture them live for tradeable candidates. The
snapshot feeds realistic-fill and capacity analysis later.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Any

import structlog

log = structlog.get_logger()

# Notional sizes (USD) we measure price impact at.
_DEPTH_TARGETS_USD = (100.0, 500.0, 1000.0)

# Order-book depth to request. Enough levels to fill the largest target on
# normal books without pulling the whole book.
_BOOK_LIMIT = 50

_FETCH_TIMEOUT = 5  # seconds


@dataclass(frozen=True)
class MarketQualityCheck:
    """Execution-quality verdict for the measured two-sided order book."""

    allowed: bool
    reason: str
    depth_target_usd: float
    spread_bps: float | None
    bid_impact_bps: float | None
    ask_impact_bps: float | None


@dataclass(frozen=True)
class SnapshotCapture:
    """Bounded order-book capture with explicit failure provenance."""

    status: str
    observed_at_ms: int
    latency_ms: int
    snapshot: dict[str, Any] | None
    error: str | None


def depth_target_usd(position_usd: float, multiplier: float) -> float:
    """Return the rounded book depth required for a configured position cap."""
    return round(position_usd * multiplier, 2)


def depth_target_key(target_usd: float) -> str:
    """Return the canonical JSON key for a measured USD depth target."""
    return f"{target_usd:.2f}".rstrip("0").rstrip(".")


def _finite_non_negative(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def check_market_quality(
    snapshot: dict[str, Any] | None,
    *,
    target_usd: float,
    max_spread_bps: float,
    max_impact_bps: float,
) -> MarketQualityCheck:
    """Fail closed when a short entry or its buy-to-close path is not executable."""

    def result(
        allowed: bool,
        reason: str,
        *,
        spread: float | None = None,
        bid_impact: float | None = None,
        ask_impact: float | None = None,
    ) -> MarketQualityCheck:
        return MarketQualityCheck(
            allowed=allowed,
            reason=reason,
            depth_target_usd=target_usd,
            spread_bps=spread,
            bid_impact_bps=bid_impact,
            ask_impact_bps=ask_impact,
        )

    if snapshot is None:
        return result(False, "market_quality_snapshot_unavailable")

    spread = _finite_non_negative(snapshot.get("spread_bps"))
    if spread is None:
        return result(False, "market_quality_invalid_spread")
    if spread > max_spread_bps:
        return result(False, "market_quality_spread_too_wide", spread=spread)

    key = depth_target_key(target_usd)
    bid_impacts = snapshot.get("bid_impact_bps")
    ask_impacts = snapshot.get("ask_impact_bps")
    if not isinstance(bid_impacts, dict) or not isinstance(ask_impacts, dict):
        return result(False, "market_quality_invalid_depth", spread=spread)

    bid_impact = _finite_non_negative(bid_impacts.get(key))
    if bid_impact is None:
        return result(False, "market_quality_insufficient_bid_depth", spread=spread)
    ask_impact = _finite_non_negative(ask_impacts.get(key))
    if ask_impact is None:
        return result(
            False,
            "market_quality_insufficient_ask_depth",
            spread=spread,
            bid_impact=bid_impact,
        )
    if bid_impact > max_impact_bps:
        return result(
            False,
            "market_quality_entry_impact_too_high",
            spread=spread,
            bid_impact=bid_impact,
            ask_impact=ask_impact,
        )
    if ask_impact > max_impact_bps:
        return result(
            False,
            "market_quality_exit_impact_too_high",
            spread=spread,
            bid_impact=bid_impact,
            ask_impact=ask_impact,
        )
    return result(
        True,
        "ok",
        spread=spread,
        bid_impact=bid_impact,
        ask_impact=ask_impact,
    )


def _vwap_impact_bps(
    levels: list[Any],
    mid: float,
    target_usd: float,
    side: str,
    *,
    contract_size: float = 1.0,
) -> float | None:
    """Return the volume-weighted slippage in bps to fill target_usd across levels.

    None means the visible book cannot fill target_usd. side is "ask" (buying,
    price goes up) or "bid" (selling, price goes down); the result is the
    non-negative distance of the fill VWAP from mid.
    """
    _vwap, impact, _filled_usd = _execution_quote(
        levels,
        mid,
        target_usd,
        side,
        contract_size=contract_size,
    )
    return impact


def _execution_quote(
    levels: list[Any],
    mid: float,
    target_usd: float,
    side: str,
    *,
    contract_size: float = 1.0,
) -> tuple[float | None, float | None, float]:
    """Return executable VWAP, impact, and visible fill for a target notional."""
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
        take_usd = min(price * amount * contract_size, remaining)
        total_quote += take_usd
        total_base += take_usd / price

    filled_usd = round(total_quote, 4)
    if total_quote < target_usd or total_base <= 0:
        return None, None, filled_usd
    vwap = total_quote / total_base
    impact = (vwap - mid) / mid if side == "ask" else (mid - vwap) / mid
    result = round(impact * 10000, 2)
    if not (math.isfinite(vwap) and math.isfinite(result)):
        return None, None, filled_usd
    return vwap, result, filled_usd


def _summarize_book(
    ex: Any,
    symbol: str,
    book: Any,
    *,
    required_depth_usd: float | None,
) -> dict[str, Any]:
    if not isinstance(book, dict):
        raise ValueError("order book is not an object")
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not isinstance(bids, list) or not isinstance(asks, list):
        raise ValueError("order book sides are not lists")
    if not bids or not asks:
        raise ValueError("order book has an empty side")

    best_bid = float(bids[0][0])
    best_ask = float(asks[0][0])
    if not (math.isfinite(best_bid) and math.isfinite(best_ask)):
        raise ValueError("order book top is non-finite")
    if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
        raise ValueError("order book top is invalid or crossed")

    mid = (best_bid + best_ask) / 2
    contract_size = 1.0
    market_id = None
    markets = getattr(ex, "markets", None)
    market = markets.get(symbol) if isinstance(markets, dict) else None
    if isinstance(market, dict):
        raw_market_id = market.get("id")
        if raw_market_id is not None:
            market_id = str(raw_market_id)
        if market.get("contract"):
            parsed_contract_size = _finite_non_negative(market.get("contractSize"))
            if parsed_contract_size is None or parsed_contract_size == 0:
                raise ValueError("derivative contract size is unavailable")
            contract_size = parsed_contract_size

    targets = set(_DEPTH_TARGETS_USD)
    if required_depth_usd is not None:
        parsed_required = _finite_non_negative(required_depth_usd)
        if parsed_required is None or parsed_required == 0:
            raise ValueError("required depth must be finite and positive")
        targets.add(parsed_required)
    ordered_targets = sorted(targets)

    bid_quotes = {
        depth_target_key(target): _execution_quote(
            bids,
            mid,
            target,
            "bid",
            contract_size=contract_size,
        )
        for target in ordered_targets
    }
    ask_quotes = {
        depth_target_key(target): _execution_quote(
            asks,
            mid,
            target,
            "ask",
            contract_size=contract_size,
        )
        for target in ordered_targets
    }
    return {
        "ts": int(time.time()),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "market_id": market_id,
        "contract_size": contract_size,
        "spread_bps": round((best_ask - best_bid) / mid * 10000, 2),
        "depth_targets_usd": ordered_targets,
        # bid side = selling (short entry), ask side = buying (exit).
        "bid_vwap": {key: quote[0] for key, quote in bid_quotes.items()},
        "ask_vwap": {key: quote[0] for key, quote in ask_quotes.items()},
        "bid_impact_bps": {key: quote[1] for key, quote in bid_quotes.items()},
        "ask_impact_bps": {key: quote[1] for key, quote in ask_quotes.items()},
        "bid_filled_usd": {key: quote[2] for key, quote in bid_quotes.items()},
        "ask_filled_usd": {key: quote[2] for key, quote in ask_quotes.items()},
    }


def _capture_error(exc: BaseException) -> str:
    message = str(exc).strip()
    detail = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    return detail[:1000]


async def capture_snapshot(
    ex: Any,
    symbol: str,
    *,
    required_depth_usd: float | None = None,
) -> SnapshotCapture:
    """Fetch and parse a bounded book while preserving status and latency."""
    # symbol passed explicitly
    started = time.perf_counter()
    try:
        book = await asyncio.wait_for(
            ex.fetch_order_book(symbol, _BOOK_LIMIT), timeout=_FETCH_TIMEOUT
        )
        summarized = _summarize_book(
            ex,
            symbol,
            book,
            required_depth_usd=required_depth_usd,
        )
    except TimeoutError as exc:
        latency_ms = round((time.perf_counter() - started) * 1000)
        error = _capture_error(exc)
        log.warning("liquidity.snapshot_failed", symbol=symbol, status="timeout", err=error)
        return SnapshotCapture("timeout", int(time.time() * 1000), latency_ms, None, error)
    except (TypeError, ValueError, IndexError) as exc:
        latency_ms = round((time.perf_counter() - started) * 1000)
        error = _capture_error(exc)
        log.warning("liquidity.snapshot_failed", symbol=symbol, status="invalid_book", err=error)
        return SnapshotCapture(
            "invalid_book",
            int(time.time() * 1000),
            latency_ms,
            None,
            error,
        )
    except Exception as exc:
        latency_ms = round((time.perf_counter() - started) * 1000)
        error = _capture_error(exc)
        log.warning("liquidity.snapshot_failed", symbol=symbol, status="fetch_failed", err=error)
        return SnapshotCapture(
            "fetch_failed",
            int(time.time() * 1000),
            latency_ms,
            None,
            error,
        )
    latency_ms = round((time.perf_counter() - started) * 1000)
    return SnapshotCapture(
        "sampled",
        int(time.time() * 1000),
        latency_ms,
        summarized,
        None,
    )


async def snapshot(
    ex: Any,
    base: str,
    *,
    required_depth_usd: float | None = None,
) -> dict[str, Any] | None:
    """Fetch the order book for base on ex and summarize spread and depth impact.

    Returns None on any failure or an unusable book. Never raises: the fetch is
    bounded by a timeout and the entire parse is guarded, so a slow exchange or a
    malformed order book cannot block or abort the caller's tick.
    """
    capture = await capture_snapshot(ex, base, required_depth_usd=required_depth_usd)
    return capture.snapshot
