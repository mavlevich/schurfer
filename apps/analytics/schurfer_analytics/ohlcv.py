"""Reusable, look-ahead-safe OHLCV loading and validation."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

TIMEFRAME = "5m"
TIMEFRAME_MINUTES = 5
TIMEFRAME_MS = TIMEFRAME_MINUTES * 60 * 1000
ONE_MINUTE_TIMEFRAME = "1m"
ONE_MINUTE_MS = 60 * 1000
_FETCH_LIMIT = 1000
_FETCH_TIMEOUT_SECONDS = 20
# Every window fetched here is fully historical (always well before "now"), so an
# empty page is always a fetch anomaly, never "no data yet" — retry it a bounded
# number of times before giving up. Genuinely useful for a transient exchange
# hiccup, but it turned out NOT to be the cause of the 2026-08-05 finding below —
# kept as a separate, complementary defense.
_EMPTY_PAGE_MAX_RETRIES = 2
_EMPTY_PAGE_RETRY_DELAY_SECONDS = 1.0
# Root cause of the 2026-08-05 finding (an exit-policy replay missing exactly one
# bar out of 72 on Bitget): `since` is exclusive on at least Bitget's swap OHLCV
# endpoint — `fetch_ohlcv(..., since=X)` returns bars strictly after X, dropping the
# bar starting exactly at X. Confirmed directly against the exchange (two different
# `since` values, same off-by-one both times). Requesting one bar earlier than the
# logical cursor and letting the existing start_ms filter in closed_candles() trim
# the (possibly duplicate, always harmless) extra leading bar works for both
# exclusive-since exchanges (this recovers the dropped bar) and inclusive-since ones
# (the duplicate is deduplicated by normalize_candles's timestamp keying).
_SINCE_LOOKBACK_BARS = 1


@dataclass(frozen=True)
class Candle:
    ts_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float | None


def finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def ceil_to_timeframe(ts_ms: int, timeframe_ms: int = TIMEFRAME_MS) -> int:
    if timeframe_ms <= 0:
        raise ValueError("timeframe_ms must be positive")
    return ((ts_ms + timeframe_ms - 1) // timeframe_ms) * timeframe_ms


def next_timeframe_after(ts_ms: int, timeframe_ms: int = TIMEFRAME_MS) -> int:
    """Return the first bar boundary strictly after a point-in-time event."""
    if timeframe_ms <= 0:
        raise ValueError("timeframe_ms must be positive")
    return (ts_ms // timeframe_ms + 1) * timeframe_ms


def window_bounds(ts: datetime, horizon_minutes: int) -> tuple[int, int, int]:
    """Return first safe bar, horizon end, and expected full-bar count."""
    decision_ms = int(ts.timestamp() * 1000)
    start_ms = ceil_to_timeframe(decision_ms)
    end_ms = decision_ms + horizon_minutes * 60 * 1000
    expected_bars = max(0, (end_ms - start_ms) // TIMEFRAME_MS)
    return start_ms, end_ms, expected_bars


def normalize_candles(
    rows: Iterable[Any],
    *,
    timeframe_ms: int = TIMEFRAME_MS,
) -> list[Candle]:
    """Validate, deduplicate, and sort ccxt OHLCV rows."""
    by_ts: dict[int, Candle] = {}
    for row in rows:
        if not isinstance(row, list | tuple) or len(row) < 6:
            continue
        ts = finite_float(row[0])
        prices = [finite_float(value) for value in row[1:5]]
        volume = finite_float(row[5])
        if ts is None or any(value is None for value in prices):
            continue
        open_, high, low, close = prices
        if open_ is None or high is None or low is None or close is None:
            continue
        ts_ms = int(ts)
        if ts != ts_ms or ts_ms % timeframe_ms != 0:
            continue
        if ts <= 0 or min(open_, high, low, close) <= 0 or (volume is not None and volume < 0):
            continue
        if high < max(open_, close, low) or low > min(open_, close, high):
            continue
        candle = Candle(ts_ms, open_, high, low, close, volume)
        by_ts[candle.ts_ms] = candle
    return [by_ts[ts] for ts in sorted(by_ts)]


def closed_candles(
    candles: Iterable[Candle],
    start_ms: int,
    end_ms: int,
    *,
    timeframe_ms: int = TIMEFRAME_MS,
) -> list[Candle]:
    """Keep full bars inside [start_ms, end_ms], excluding both partial edges."""
    return [
        candle
        for candle in candles
        if candle.ts_ms >= start_ms and candle.ts_ms + timeframe_ms <= end_ms
    ]


async def fetch_candles(
    exchange: Any,
    base: str,
    start_ms: int,
    end_ms: int,
    *,
    timeframe: str = TIMEFRAME,
    timeframe_ms: int = TIMEFRAME_MS,
) -> list[Candle]:
    """Page through ccxt OHLCV and return only fully closed bars in the window."""
    symbol = f"{base.upper()}/USDT:USDT"
    return await fetch_symbol_candles(
        exchange,
        symbol,
        start_ms,
        end_ms,
        timeframe=timeframe,
        timeframe_ms=timeframe_ms,
    )


async def fetch_symbol_candles(
    exchange: Any,
    symbol: str,
    start_ms: int,
    end_ms: int,
    *,
    timeframe: str = TIMEFRAME,
    timeframe_ms: int = TIMEFRAME_MS,
) -> list[Candle]:
    """Fetch candles for an already identity-validated exact unified symbol."""
    if not symbol.strip():
        raise ValueError("symbol must not be empty")
    cursor = start_ms
    collected: list[Candle] = []
    if timeframe_ms <= 0:
        raise ValueError("timeframe_ms must be positive")
    max_pages = math.ceil(max(0, end_ms - start_ms) / timeframe_ms / _FETCH_LIMIT) + 2

    for _ in range(max_pages):
        if cursor >= end_ms:
            break
        remaining = math.ceil((end_ms - cursor) / timeframe_ms)
        limit = max(1, min(_FETCH_LIMIT, remaining + 1 + _SINCE_LOOKBACK_BARS))
        fetch_since = max(0, cursor - _SINCE_LOOKBACK_BARS * timeframe_ms)
        page: list[Candle] = []
        for attempt in range(_EMPTY_PAGE_MAX_RETRIES + 1):
            raw = await asyncio.wait_for(
                exchange.fetch_ohlcv(symbol, timeframe, since=fetch_since, limit=limit),
                timeout=_FETCH_TIMEOUT_SECONDS,
            )
            page = normalize_candles(raw, timeframe_ms=timeframe_ms)
            if page or attempt == _EMPTY_PAGE_MAX_RETRIES:
                break
            await asyncio.sleep(_EMPTY_PAGE_RETRY_DELAY_SECONDS)
        if not page:
            break
        collected.extend(page)
        next_cursor = page[-1].ts_ms + timeframe_ms
        if next_cursor <= cursor:
            break
        cursor = next_cursor

    return closed_candles(
        normalize_candles(
            [[c.ts_ms, c.open, c.high, c.low, c.close, c.volume] for c in collected],
            timeframe_ms=timeframe_ms,
        ),
        start_ms,
        end_ms,
        timeframe_ms=timeframe_ms,
    )
