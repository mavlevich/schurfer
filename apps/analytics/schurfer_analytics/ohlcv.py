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
_FETCH_LIMIT = 1000
_FETCH_TIMEOUT_SECONDS = 20


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


def ceil_to_timeframe(ts_ms: int) -> int:
    return ((ts_ms + TIMEFRAME_MS - 1) // TIMEFRAME_MS) * TIMEFRAME_MS


def window_bounds(ts: datetime, horizon_minutes: int) -> tuple[int, int, int]:
    """Return first safe bar, horizon end, and expected full-bar count."""
    decision_ms = int(ts.timestamp() * 1000)
    start_ms = ceil_to_timeframe(decision_ms)
    end_ms = decision_ms + horizon_minutes * 60 * 1000
    expected_bars = max(0, (end_ms - start_ms) // TIMEFRAME_MS)
    return start_ms, end_ms, expected_bars


def normalize_candles(rows: Iterable[Any]) -> list[Candle]:
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
        if ts != ts_ms or ts_ms % TIMEFRAME_MS != 0:
            continue
        if ts <= 0 or min(open_, high, low, close) <= 0 or (volume is not None and volume < 0):
            continue
        if high < max(open_, close, low) or low > min(open_, close, high):
            continue
        candle = Candle(ts_ms, open_, high, low, close, volume)
        by_ts[candle.ts_ms] = candle
    return [by_ts[ts] for ts in sorted(by_ts)]


def closed_candles(candles: Iterable[Candle], start_ms: int, end_ms: int) -> list[Candle]:
    """Keep full bars inside [start_ms, end_ms], excluding both partial edges."""
    return [
        candle
        for candle in candles
        if candle.ts_ms >= start_ms and candle.ts_ms + TIMEFRAME_MS <= end_ms
    ]


async def fetch_candles(
    exchange: Any,
    base: str,
    start_ms: int,
    end_ms: int,
) -> list[Candle]:
    """Page through ccxt OHLCV and return only fully closed bars in the window."""
    symbol = f"{base.upper()}/USDT:USDT"
    cursor = start_ms
    collected: list[Candle] = []
    max_pages = math.ceil(max(0, end_ms - start_ms) / TIMEFRAME_MS / _FETCH_LIMIT) + 2

    for _ in range(max_pages):
        if cursor >= end_ms:
            break
        remaining = math.ceil((end_ms - cursor) / TIMEFRAME_MS)
        limit = max(1, min(_FETCH_LIMIT, remaining + 1))
        raw = await asyncio.wait_for(
            exchange.fetch_ohlcv(symbol, TIMEFRAME, since=cursor, limit=limit),
            timeout=_FETCH_TIMEOUT_SECONDS,
        )
        page = normalize_candles(raw)
        if not page:
            break
        collected.extend(page)
        next_cursor = page[-1].ts_ms + TIMEFRAME_MS
        if next_cursor <= cursor:
            break
        cursor = next_cursor

    return closed_candles(
        normalize_candles([[c.ts_ms, c.open, c.high, c.low, c.close, c.volume] for c in collected]),
        start_ms,
        end_ms,
    )
