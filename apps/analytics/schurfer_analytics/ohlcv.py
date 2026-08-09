"""Reusable, look-ahead-safe OHLCV loading and validation."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from datetime import datetime

TIMEFRAME = "5m"
TIMEFRAME_MINUTES = 5
TIMEFRAME_MS = TIMEFRAME_MINUTES * 60 * 1000
ONE_MINUTE_TIMEFRAME = "1m"
ONE_MINUTE_MS = 60 * 1000
_FETCH_LIMIT = 1000
_FETCH_TIMEOUT_SECONDS = 20
# Every window fetched here is fully historical (always well before "now"), so an
# empty page is always a fetch anomaly, never "no data yet". Retry it a bounded
# number of times before giving up. Genuinely useful for a transient exchange
# hiccup, but it turned out NOT to be the cause of the 2026-08-05 finding below;
# kept as a separate, complementary defense.
_EMPTY_PAGE_MAX_RETRIES = 2
_EMPTY_PAGE_RETRY_DELAY_SECONDS = 1.0
# Root cause of the 2026-08-05 finding (an exit-policy replay missing exactly one
# bar out of 72 on Bitget): `since` is exclusive on at least Bitget's swap OHLCV
# endpoint. `fetch_ohlcv(..., since=X)` returns bars strictly after X, dropping the
# bar starting exactly at X. Confirmed directly against the exchange (two different
# `since` values, same off-by-one both times). Requesting one bar earlier than the
# logical cursor and letting the existing start_ms filter in closed_candles() trim
# the (possibly duplicate, always harmless) extra leading bar works for both
# exclusive-since exchanges (this recovers the dropped bar) and inclusive-since ones
# (the duplicate is deduplicated by normalize_candles's timestamp keying).
_SINCE_LOOKBACK_BARS = 1
# 2026-08-10 finding: `max_pages` used to be sized assuming every page returns a
# full `_FETCH_LIMIT` bars. An exchange that silently caps pages lower than the
# requested `limit` (plausible for a 90-365 day request; no existing caller before
# this had ever asked for a window longer than a few hours) would then exhaust
# `max_pages` before reaching `end_ms`, and the function would just return the
# partial result with no signal that anything was cut short. `expected_bars` is a
# mathematically exact worst-case bound instead of a guess about exchange
# behavior: as long as a page contributes at least one new bar (the only case this
# guards; see `_exhausted_page_budget` below), the fetch cannot need more than
# `expected_bars` pages, plus a small buffer for the lookback/dedup overlap.
# `_MAX_PAGES_HARD_CAP` is a secondary sanity limit on page count, not a
# wall-clock budget: at `_FETCH_TIMEOUT_SECONDS` per page, this still allows a
# multi-hour worst case if an exchange keeps genuinely paging that long. It is
# sized with headroom over the known 90-365 day daily-timeframe windows
# (expected_bars there tops out around 367), not as a guarantee against a
# pathological exchange; a real wall-clock budget would need its own,
# separate check if a future caller asks for a window large enough to need one.
_MAX_PAGES_HARD_CAP = 512


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


class IncompleteFetchError(RuntimeError):
    """Raised when `fetch_symbol_candles` could not reach `end_ms` within its
    page budget, even though every page it received was non-empty and kept
    advancing the cursor. This is the silent-truncation risk the 2026-08-10
    finding identified: an exchange that keeps returning genuine data, just
    in smaller pages than expected, used to make the fetch return an
    unflagged partial result instead. It is deliberately NOT raised for an
    empty page or a stalled cursor (see `fetch_symbol_candles`'s docstring):
    those are pre-existing, silent partial-result terminations that this fix
    does not change, since there is no way from here to tell a real retention
    limit apart from a genuine fetch failure, and treating that ambiguity as
    an error would be a much larger behavior change than this fix is scoped
    to."""

    def __init__(
        self,
        *,
        symbol: str,
        start_ms: int,
        end_ms: int,
        next_cursor_ms: int,
        successful_pages: int,
        api_calls: int,
    ) -> None:
        self.symbol = symbol
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.next_cursor_ms = next_cursor_ms
        self.successful_pages = successful_pages
        self.api_calls = api_calls
        super().__init__(
            f"{symbol}: exhausted the page budget ({successful_pages} successful "
            f"pages, {api_calls} API calls) before reaching end_ms; cursor stopped "
            f"at {next_cursor_ms}, needed {end_ms}"
        )


@dataclass(frozen=True)
class PageFetchObservation:
    """One real API call made by `fetch_symbol_candles`, passed to an optional
    `on_page` callback. Exists so a caller (the token-behavior-history live
    sample, specifically) can measure actual exchange paging behavior without
    duplicating the pagination loop itself. `raw_bar_count` and
    `normalized_bar_count` are reported separately because they can honestly
    differ: an exchange can return 200 rows of which `normalize_candles`
    accepts only 198, and collapsing that distinction into one count would
    silently mix "how big is the page" with "how much of it was valid data."
    """

    api_call_index: int
    attempt_index: int
    requested_since_ms: int
    requested_limit: int
    cursor_before_ms: int
    raw_bar_count: int | None
    normalized_bar_count: int | None
    latency_seconds: float
    outcome: str  # "success" | "empty" | "timeout" | "error"
    error_type: str | None


def _observe_page(
    on_page: Callable[[PageFetchObservation], None] | None,
    *,
    api_call_index: int,
    attempt_index: int,
    requested_since_ms: int,
    requested_limit: int,
    cursor_before_ms: int,
    raw_bar_count: int | None,
    normalized_bar_count: int | None,
    latency_seconds: float,
    outcome: str,
    error_type: str | None,
) -> None:
    if on_page is None:
        return
    on_page(
        PageFetchObservation(
            api_call_index=api_call_index,
            attempt_index=attempt_index,
            requested_since_ms=requested_since_ms,
            requested_limit=requested_limit,
            cursor_before_ms=cursor_before_ms,
            raw_bar_count=raw_bar_count,
            normalized_bar_count=normalized_bar_count,
            latency_seconds=latency_seconds,
            outcome=outcome,
            error_type=error_type,
        )
    )


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
    on_page: Callable[[PageFetchObservation], None] | None = None,
) -> list[Candle]:
    """Fetch candles for an already identity-validated exact unified symbol.

    Can return a PARTIAL result (fewer bars than the window implies) without
    raising, in two cases that predate this docstring: the exchange returned
    an empty page even after retries (it may have no more historical data,
    e.g. a retention limit or a young/delisted instrument), or the cursor
    failed to advance between pages. Neither is distinguishable from here as
    "expected" versus "wrong": the caller must compare the returned bar count
    against what the window implies and classify the gap itself. This
    function only raises `IncompleteFetchError` for a third, different
    condition: the page budget ran out while every page was still genuinely
    non-empty and advancing, which is the silent-truncation bug this queries
    were never at risk of failing on before requests started spanning months
    instead of hours (see the 2026-08-10 finding above).

    `on_page`, if given, is called once for every real API call this makes
    (including retry attempts and calls that time out or raise), with a
    `PageFetchObservation` describing it. This is a pure diagnostic hook: it
    must not raise, and it cannot change what this function returns. If it
    does raise, that exception propagates immediately and aborts the fetch,
    same as any other unexpected error here; the callback is not wrapped in
    its own try/except, so a broken observer is a hard failure, not a
    silently swallowed one.
    """
    if not symbol.strip():
        raise ValueError("symbol must not be empty")
    cursor = start_ms
    collected: list[Candle] = []
    if timeframe_ms <= 0:
        raise ValueError("timeframe_ms must be positive")
    expected_bars = math.ceil(max(0, end_ms - start_ms) / timeframe_ms)
    max_pages = min(expected_bars + 2, _MAX_PAGES_HARD_CAP)

    successful_pages = 0
    api_calls = 0
    exhausted_page_budget = True
    for _ in range(max_pages):
        if cursor >= end_ms:
            exhausted_page_budget = False
            break
        remaining = math.ceil((end_ms - cursor) / timeframe_ms)
        limit = max(1, min(_FETCH_LIMIT, remaining + 1 + _SINCE_LOOKBACK_BARS))
        fetch_since = max(0, cursor - _SINCE_LOOKBACK_BARS * timeframe_ms)
        page: list[Candle] = []
        for attempt in range(_EMPTY_PAGE_MAX_RETRIES + 1):
            api_calls += 1
            started = time.perf_counter()
            try:
                raw = await asyncio.wait_for(
                    exchange.fetch_ohlcv(symbol, timeframe, since=fetch_since, limit=limit),
                    timeout=_FETCH_TIMEOUT_SECONDS,
                )
            except TimeoutError as exc:
                _observe_page(
                    on_page,
                    api_call_index=api_calls,
                    attempt_index=attempt,
                    requested_since_ms=fetch_since,
                    requested_limit=limit,
                    cursor_before_ms=cursor,
                    raw_bar_count=None,
                    normalized_bar_count=None,
                    latency_seconds=time.perf_counter() - started,
                    outcome="timeout",
                    error_type=type(exc).__name__,
                )
                raise
            except Exception as exc:
                _observe_page(
                    on_page,
                    api_call_index=api_calls,
                    attempt_index=attempt,
                    requested_since_ms=fetch_since,
                    requested_limit=limit,
                    cursor_before_ms=cursor,
                    raw_bar_count=None,
                    normalized_bar_count=None,
                    latency_seconds=time.perf_counter() - started,
                    outcome="error",
                    error_type=type(exc).__name__,
                )
                raise
            latency_seconds = time.perf_counter() - started
            page = normalize_candles(raw, timeframe_ms=timeframe_ms)
            try:
                raw_bar_count = len(raw)
            except TypeError:
                raw_bar_count = None
            _observe_page(
                on_page,
                api_call_index=api_calls,
                attempt_index=attempt,
                requested_since_ms=fetch_since,
                requested_limit=limit,
                cursor_before_ms=cursor,
                raw_bar_count=raw_bar_count,
                normalized_bar_count=len(page),
                latency_seconds=latency_seconds,
                outcome="success" if page else "empty",
                error_type=None,
            )
            if page or attempt == _EMPTY_PAGE_MAX_RETRIES:
                break
            await asyncio.sleep(_EMPTY_PAGE_RETRY_DELAY_SECONDS)
        if not page:
            # The exchange has no more data, or gave up after retries. Not a
            # page-budget problem, and not distinguishable from here as a real
            # retention limit versus a fetch failure, so it stays a silent
            # pre-existing partial-result termination, matching the behavior
            # this fix does not change.
            exhausted_page_budget = False
            break
        successful_pages += 1
        collected.extend(page)
        next_cursor = page[-1].ts_ms + timeframe_ms
        if next_cursor <= cursor:
            # The cursor stalled: also a pre-existing, separate failure
            # mode, not a page-budget problem. Kept silent, same as above.
            exhausted_page_budget = False
            break
        cursor = next_cursor

    if exhausted_page_budget and cursor < end_ms:
        raise IncompleteFetchError(
            symbol=symbol,
            start_ms=start_ms,
            end_ms=end_ms,
            next_cursor_ms=cursor,
            successful_pages=successful_pages,
            api_calls=api_calls,
        )

    return closed_candles(
        normalize_candles(
            [[c.ts_ms, c.open, c.high, c.low, c.close, c.volume] for c in collected],
            timeframe_ms=timeframe_ms,
        ),
        start_ms,
        end_ms,
        timeframe_ms=timeframe_ms,
    )
