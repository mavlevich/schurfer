"""Reusable bounded history fetching for unified CCXT derivatives methods."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, Literal

DEFAULT_FETCH_LIMIT = 200
DEFAULT_MAX_PAGES = 10
TIMEFRAME = "5m"

RowKind = Literal["object", "ohlcv"]
SeriesKind = Literal["event", "regular"]
HistoryFetchError = Literal["fetch_failed", "invalid_response"]


@dataclass(frozen=True)
class DerivativesHistoryMethod:
    name: str
    capability: str
    callable_name: str
    row_kind: RowKind
    series_kind: SeriesKind
    timeframe: str | None = None


METHODS: tuple[DerivativesHistoryMethod, ...] = (
    DerivativesHistoryMethod(
        "funding_rate_history",
        "fetchFundingRateHistory",
        "fetch_funding_rate_history",
        "object",
        "event",
    ),
    DerivativesHistoryMethod(
        "open_interest_history",
        "fetchOpenInterestHistory",
        "fetch_open_interest_history",
        "object",
        "regular",
        TIMEFRAME,
    ),
    DerivativesHistoryMethod(
        "mark_ohlcv",
        "fetchMarkOHLCV",
        "fetch_mark_ohlcv",
        "ohlcv",
        "regular",
        TIMEFRAME,
    ),
    DerivativesHistoryMethod(
        "index_ohlcv",
        "fetchIndexOHLCV",
        "fetch_index_ohlcv",
        "ohlcv",
        "regular",
        TIMEFRAME,
    ),
    DerivativesHistoryMethod(
        "premium_index_ohlcv",
        "fetchPremiumIndexOHLCV",
        "fetch_premium_index_ohlcv",
        "ohlcv",
        "regular",
        TIMEFRAME,
    ),
    DerivativesHistoryMethod(
        "long_short_ratio_history",
        "fetchLongShortRatioHistory",
        "fetch_long_short_ratio_history",
        "object",
        "regular",
        TIMEFRAME,
    ),
    DerivativesHistoryMethod(
        "liquidations",
        "fetchLiquidations",
        "fetch_liquidations",
        "object",
        "event",
    ),
)
METHOD_BY_NAME = {method.name: method for method in METHODS}

TIMEFRAME_OVERRIDES: dict[tuple[str, str], str] = {
    ("htx", "open_interest_history"): "1h",
}

LIMIT_OVERRIDES: dict[tuple[str, str], int] = {
    ("htx", "funding_rate_history"): 100,
    ("htx", "liquidations"): 100,
}


@dataclass(frozen=True)
class DerivativesHistoryFetch:
    rows: tuple[Any, ...]
    request_count: int
    pagination_exhausted: bool = False
    error_status: HistoryFetchError | None = None
    error: str | None = None


@dataclass(frozen=True)
class RegularWindowCoverage:
    timestamps: tuple[int, ...]
    expected_rows: int
    coverage_ratio: float
    covers_start: bool
    covers_end: bool
    missing_rows: int
    duplicate_rows: int
    max_gap_minutes: float | None


def source_timestamp_ms(row: Any, row_kind: RowKind) -> int | None:
    raw: Any
    if row_kind == "ohlcv":
        if not isinstance(row, list | tuple) or not row:
            return None
        raw = row[0]
    else:
        if not isinstance(row, dict):
            return None
        raw = row.get("timestamp")
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    if not math.isfinite(float(raw)):
        return None
    timestamp_ms = int(raw)
    # Unified CCXT timestamps are milliseconds. Do not silently repair seconds:
    # parser conformance must remain observable to callers.
    if timestamp_ms < 946684800000:
        return None
    return timestamp_ms


def effective_timeframe(
    exchange: str,
    method: DerivativesHistoryMethod,
) -> str | None:
    return TIMEFRAME_OVERRIDES.get((exchange, method.name), method.timeframe)


def effective_limit(
    exchange: str,
    method: DerivativesHistoryMethod,
    requested_limit: int,
) -> int:
    """Apply a documented venue cap without increasing the caller's bound."""
    override = LIMIT_OVERRIDES.get((exchange, method.name))
    return min(requested_limit, override) if override is not None else requested_limit


def timeframe_ms(timeframe: str | None) -> int | None:
    if timeframe is None:
        return None
    unit = timeframe[-1:]
    try:
        value = int(timeframe[:-1])
    except ValueError as exc:
        raise ValueError(f"unsupported derivatives timeframe: {timeframe}") from exc
    multipliers = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    if value <= 0 or unit not in multipliers:
        raise ValueError(f"unsupported derivatives timeframe: {timeframe}")
    return value * multipliers[unit]


def measure_regular_window(
    timestamps: list[int],
    *,
    since_ms: int,
    until_ms: int,
    step_ms: int,
) -> RegularWindowCoverage:
    """Measure a regular series against its observed grid phase."""
    if since_ms >= until_ms:
        raise ValueError("coverage since must be earlier than until")
    if step_ms <= 0:
        raise ValueError("coverage step must be positive")

    unique = tuple(sorted(set(timestamps)))
    duplicate_rows = len(timestamps) - len(unique)
    if unique:
        first_expected = since_ms + ((unique[0] - since_ms) % step_ms)
        expected_rows = math.ceil((until_ms - first_expected) / step_ms)
        last_expected = first_expected + (expected_rows - 1) * step_ms
        covers_start = unique[0] == first_expected
        covers_end = unique[-1] >= last_expected
    else:
        expected_rows = math.ceil((until_ms - since_ms) / step_ms)
        covers_start = False
        covers_end = False
    missing_rows = max(expected_rows - len(unique), 0)
    coverage_ratio = min(len(unique) / expected_rows, 1.0) if expected_rows else 1.0
    if len(unique) >= 2:
        max_gap_minutes = max(later - earlier for earlier, later in pairwise(unique)) / 60_000
    elif unique:
        max_gap_minutes = 0.0
    else:
        max_gap_minutes = None
    return RegularWindowCoverage(
        timestamps=unique,
        expected_rows=expected_rows,
        coverage_ratio=coverage_ratio,
        covers_start=covers_start,
        covers_end=covers_end,
        missing_rows=missing_rows,
        duplicate_rows=duplicate_rows,
        max_gap_minutes=max_gap_minutes,
    )


async def _call_method(
    exchange: Any,
    method: DerivativesHistoryMethod,
    symbol: str,
    timeframe: str | None,
    since_ms: int,
    limit: int,
) -> Any:
    fetcher = getattr(exchange, method.callable_name)
    if timeframe is None:
        return await fetcher(symbol, since_ms, limit)
    return await fetcher(symbol, timeframe, since_ms, limit)


async def fetch_derivatives_history(
    exchange: Any,
    method: DerivativesHistoryMethod,
    symbol: str,
    *,
    timeframe: str | None,
    since_ms: int,
    until_ms: int,
    limit: int,
    max_pages: int,
    timeout_seconds: float,
) -> DerivativesHistoryFetch:
    """Fetch a bounded forward-moving window without trusting venue page caps."""
    if since_ms >= until_ms:
        raise ValueError("history since must be earlier than until")
    if not 1 <= limit <= 1000:
        raise ValueError("history page limit must be between 1 and 1000")
    if not 1 <= max_pages <= 50:
        raise ValueError("history max pages must be between 1 and 50")
    if not 0 < timeout_seconds <= 120:
        raise ValueError("history timeout must be in (0, 120]")
    if method.series_kind == "regular" and timeframe is None:
        raise ValueError(f"regular derivatives method {method.name} requires a timeframe")

    rows: list[Any] = []
    cursor = since_ms
    step_ms = timeframe_ms(timeframe) or 1
    for page_number in range(max_pages):
        try:
            response = await asyncio.wait_for(
                _call_method(
                    exchange,
                    method,
                    symbol,
                    timeframe,
                    cursor,
                    limit,
                ),
                timeout=timeout_seconds,
            )
        except Exception as exc:
            return DerivativesHistoryFetch(
                rows=tuple(rows),
                request_count=page_number + 1,
                error_status="fetch_failed",
                error=str(exc)[:1000],
            )
        if not isinstance(response, list):
            return DerivativesHistoryFetch(
                rows=tuple(rows),
                request_count=page_number + 1,
                error_status="invalid_response",
                error=f"expected list response, got {type(response).__name__}",
            )
        rows.extend(response)
        if not response:
            return DerivativesHistoryFetch(tuple(rows), page_number + 1)
        page_timestamps = [
            timestamp
            for row in response
            if (timestamp := source_timestamp_ms(row, method.row_kind)) is not None
        ]
        if not page_timestamps:
            return DerivativesHistoryFetch(
                rows=tuple(rows),
                request_count=page_number + 1,
                error_status="invalid_response",
                error="response contained no valid unified millisecond timestamps",
            )
        last_timestamp = max(page_timestamps)
        window_end_reached = (
            last_timestamp >= until_ms
            if method.series_kind == "event"
            else last_timestamp >= until_ms - step_ms
        )
        if window_end_reached:
            return DerivativesHistoryFetch(tuple(rows), page_number + 1)
        next_cursor = last_timestamp + step_ms
        if next_cursor <= cursor:
            return DerivativesHistoryFetch(
                rows=tuple(rows),
                request_count=page_number + 1,
                pagination_exhausted=True,
                error="pagination made no forward progress",
            )
        cursor = next_cursor
    return DerivativesHistoryFetch(
        rows=tuple(rows),
        request_count=max_pages,
        pagination_exhausted=True,
        error=f"pagination reached the configured {max_pages}-page limit",
    )
