from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from schurfer_analytics.market_path_cache import (
    MarketPathCacheCorruptError,
    MarketPathCacheWriteError,
    write_cached_candles,
)
from schurfer_analytics.ohlcv import (
    ONE_MINUTE_MS,
    TIMEFRAME,
    TIMEFRAME_MS,
    Candle,
    IncompleteFetchError,
    PageFetchObservation,
    closed_candles,
    fetch_candles,
    fetch_symbol_candles,
    next_timeframe_after,
    normalize_candles,
    window_bounds,
)

DAY_MS = 24 * 60 * 60 * 1000


def test_normalize_candles_validates_deduplicates_and_sorts() -> None:
    rows = [
        [2 * TIMEFRAME_MS, 10, 12, 9, 11, 100],
        [TIMEFRAME_MS, 10, 11, 9, 10.5, 50],
        [2 * TIMEFRAME_MS, 10, 13, 8, 12, 200],  # later duplicate wins
        [3 * TIMEFRAME_MS, 10, 9, 8, 11, 100],  # high below close
        [4 * TIMEFRAME_MS, "bad", 12, 9, 11, 100],
        [5 * TIMEFRAME_MS, 10, 12, 9, 11, -1],
        [6 * TIMEFRAME_MS, float("nan"), 12, 9, 11, 1],
        [7 * TIMEFRAME_MS + 1, 10, 12, 9, 11, 1],  # off-grid timestamp
    ]

    result = normalize_candles(rows)

    assert [c.ts_ms for c in result] == [TIMEFRAME_MS, 2 * TIMEFRAME_MS]
    assert result[1].high == 13
    assert result[1].volume == 200


def test_normalize_candles_keeps_price_data_when_volume_is_missing() -> None:
    result = normalize_candles([[TIMEFRAME_MS, 10, 12, 9, 11, None]])

    assert len(result) == 1
    assert result[0].volume is None


def test_window_bounds_excludes_partial_candle_at_both_edges() -> None:
    start, end, expected = window_bounds(datetime(2026, 7, 22, 12, 2, tzinfo=UTC), 15)

    assert datetime.fromtimestamp(start / 1000, UTC).minute == 5
    assert datetime.fromtimestamp(end / 1000, UTC).minute == 17
    assert expected == 2


def test_closed_candles_rejects_bars_outside_window() -> None:
    rows = normalize_candles(
        [
            [TIMEFRAME_MS, 10, 11, 9, 10, 1],
            [2 * TIMEFRAME_MS, 10, 11, 9, 10, 1],
            [3 * TIMEFRAME_MS, 10, 11, 9, 10, 1],
        ]
    )

    result = closed_candles(rows, 2 * TIMEFRAME_MS, 3 * TIMEFRAME_MS)

    assert [c.ts_ms for c in result] == [2 * TIMEFRAME_MS]


async def test_fetch_candles_excludes_bar_that_closes_after_horizon() -> None:
    start = int(datetime(2026, 7, 22, 12, 0, tzinfo=UTC).timestamp() * 1000)
    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(
        return_value=[
            [start, 100, 101, 99, 100, 1],
            [start + TIMEFRAME_MS, 100, 102, 98, 101, 1],
            [start + 2 * TIMEFRAME_MS, 101, 103, 100, 102, 1],
        ]
    )

    result = await fetch_candles(exchange, "ERA", start, start + 2 * TIMEFRAME_MS)

    assert [c.ts_ms for c in result] == [start, start + TIMEFRAME_MS]


async def test_fetch_candles_stops_when_exchange_does_not_advance_cursor() -> None:
    start = int(datetime(2026, 7, 22, 12, 0, tzinfo=UTC).timestamp() * 1000)
    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(return_value=[[start, 100, 101, 99, 100, 1]])

    result = await fetch_candles(exchange, "ERA", start, start + 3 * TIMEFRAME_MS)

    assert [c.ts_ms for c in result] == [start]
    assert exchange.fetch_ohlcv.await_count == 2


def _bar(start: int, offset: int) -> list[float]:
    ts = start + offset * TIMEFRAME_MS
    return [ts, 100, 101, 99, 100, 1]


async def test_fetch_symbol_candles_retries_transient_empty_page_mid_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (2026-08-05): a single transient empty response partway through a
    long historical window (e.g. Bitget mid-fetch) must not be treated as "no more
    data" — every window fetched here is fully in the past, so more data is expected
    to exist until the requested end."""
    monkeypatch.setattr("schurfer_analytics.ohlcv._EMPTY_PAGE_RETRY_DELAY_SECONDS", 0)
    start = int(datetime(2026, 7, 22, 12, 0, tzinfo=UTC).timestamp() * 1000)
    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(
        side_effect=[
            [_bar(start, offset) for offset in range(4)],  # page 1: bars 0-3
            [],  # page 2: transient empty response from the exchange
            [_bar(start, offset) for offset in range(4, 10)],  # retry completes the window
        ]
    )

    result = await fetch_symbol_candles(
        exchange,
        "ERA/USDT:USDT",
        start,
        start + 10 * TIMEFRAME_MS,
    )

    assert [c.ts_ms for c in result] == [start + offset * TIMEFRAME_MS for offset in range(10)]
    assert exchange.fetch_ohlcv.await_count == 3


async def test_fetch_symbol_candles_gives_up_after_bounded_empty_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("schurfer_analytics.ohlcv._EMPTY_PAGE_RETRY_DELAY_SECONDS", 0)
    start = int(datetime(2026, 7, 22, 12, 0, tzinfo=UTC).timestamp() * 1000)
    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(return_value=[])

    result = await fetch_symbol_candles(
        exchange,
        "ERA/USDT:USDT",
        start,
        start + 6 * TIMEFRAME_MS,
    )

    assert result == []
    # 1 initial attempt + _EMPTY_PAGE_MAX_RETRIES retries, then give up — not an
    # unbounded loop.
    assert exchange.fetch_ohlcv.await_count == 3


async def test_fetch_symbol_candles_retries_empty_page_at_the_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (2026-08-05): the exact real-world case that slipped through the
    first version of this fix — a window one bar short, where the missing bar was
    the very last one. A fully historical window has no legitimate "hasn't happened
    yet" bar anywhere, including at the tail, so the tail must retry too."""
    monkeypatch.setattr("schurfer_analytics.ohlcv._EMPTY_PAGE_RETRY_DELAY_SECONDS", 0)
    start = int(datetime(2026, 7, 22, 12, 0, tzinfo=UTC).timestamp() * 1000)
    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(
        side_effect=[
            [],  # transient empty response for the one and only requested bar
            [_bar(start, 0)],  # retry succeeds
        ]
    )

    result = await fetch_symbol_candles(
        exchange,
        "ERA/USDT:USDT",
        start,
        start + TIMEFRAME_MS,
    )

    assert [c.ts_ms for c in result] == [start]
    assert exchange.fetch_ohlcv.await_count == 2


async def test_fetch_symbol_candles_recovers_bar_dropped_by_exclusive_since() -> None:
    """Regression (2026-08-05): the actual root cause behind the SKUU/Bitget
    exit-policy gap — `since` is exclusive on at least Bitget's swap OHLCV endpoint,
    so requesting since=X silently drops the bar starting exactly at X. Confirmed
    directly against the exchange for two different `since` values. The earlier
    empty-page retries (above) do not help here: the page is never empty, it is
    just missing its first bar."""
    start = int(datetime(2026, 7, 22, 12, 0, tzinfo=UTC).timestamp() * 1000)

    async def exclusive_since_fetch_ohlcv(
        _symbol: str, _timeframe: str, since: int, limit: int
    ) -> list[list[float]]:
        bars = [_bar(start, offset) for offset in range(6) if start + offset * TIMEFRAME_MS > since]
        return bars[:limit]

    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(side_effect=exclusive_since_fetch_ohlcv)

    result = await fetch_symbol_candles(
        exchange,
        "SKUU/USDT:USDT",
        start,
        start + 6 * TIMEFRAME_MS,
    )

    assert [c.ts_ms for c in result] == [start + offset * TIMEFRAME_MS for offset in range(6)]


async def test_fetch_symbol_candles_preserves_identity_validated_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("schurfer_analytics.ohlcv._EMPTY_PAGE_RETRY_DELAY_SECONDS", 0)
    start = int(datetime(2026, 7, 22, 12, 0, tzinfo=UTC).timestamp() * 1000)
    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(return_value=[])

    await fetch_symbol_candles(
        exchange,
        "1000EDGE/USDT:USDT",
        start,
        start + TIMEFRAME_MS,
    )

    exchange.fetch_ohlcv.assert_awaited_with(
        "1000EDGE/USDT:USDT",
        "5m",
        since=start - TIMEFRAME_MS,
        limit=3,
    )


async def test_fetch_symbol_candles_rejects_empty_symbol() -> None:
    with pytest.raises(ValueError, match="symbol"):
        await fetch_symbol_candles(AsyncMock(), " ", 0, TIMEFRAME_MS)


# --- 2026-08-10 finding: max_pages must not assume full-sized pages --------


def _daily_bar(start: int, offset: int) -> list[float]:
    ts = start + offset * DAY_MS
    return [ts, 100, 101, 99, 100, 1]


async def test_fetch_symbol_candles_completes_large_window_despite_capped_pages() -> None:
    """Regression (2026-08-10): a 365-day daily window used to size its page
    budget assuming full 1000-bar pages. An exchange that silently caps
    pages at a fraction of that (plausible for a historical daily endpoint)
    used to make the fetch return a truncated result with no signal. The
    response is derived from the real `since`/`limit` arguments, not a fixed
    sequence, so this also exercises the real cursor/lookback/dedup math,
    not just the mock's ability to hand back eight pages."""
    start = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
    total_days = 365
    page_cap = 50  # far below what the exchange was actually asked for

    async def capped_fetch_ohlcv(
        _symbol: str, _timeframe: str, since: int, limit: int
    ) -> list[list[float]]:
        bars = [
            _daily_bar(start, offset)
            for offset in range(total_days)
            if start + offset * DAY_MS >= since
        ]
        return bars[: min(limit, page_cap)]

    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(side_effect=capped_fetch_ohlcv)

    result = await fetch_symbol_candles(
        exchange,
        "ERA/USDT:USDT",
        start,
        start + total_days * DAY_MS,
        timeframe="1d",
        timeframe_ms=DAY_MS,
    )

    assert [c.ts_ms for c in result] == [start + offset * DAY_MS for offset in range(total_days)]
    # ceil(365 / 50) = 8 pages needed; well within the corrected budget.
    assert exchange.fetch_ohlcv.await_count == 8


async def test_fetch_symbol_candles_raises_when_page_budget_truly_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pathological exchange that only ever advances by one bar per page,
    for a window whose hard page-count cap has been lowered (here, to keep
    the test fast) below what that would take, must fail closed instead of
    returning a silently truncated result."""
    monkeypatch.setattr("schurfer_analytics.ohlcv._MAX_PAGES_HARD_CAP", 3)
    start = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
    total_days = 10  # needs 10 pages at 1 bar/page, but the cap above is 3

    async def one_bar_at_a_time_fetch_ohlcv(
        _symbol: str, _timeframe: str, since: int, limit: int
    ) -> list[list[float]]:
        # Exclusive since, matching exclusive_since_fetch_ohlcv above: a page cap
        # of 1 bar combined with an inclusive-since mock would keep returning the
        # same lookback-overlap bar forever, hitting the stall path instead of
        # actually exercising genuine per-page progress.
        bars = [
            _daily_bar(start, offset)
            for offset in range(total_days)
            if start + offset * DAY_MS > since
        ]
        return bars[:1]

    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(side_effect=one_bar_at_a_time_fetch_ohlcv)

    with pytest.raises(IncompleteFetchError) as exc_info:
        await fetch_symbol_candles(
            exchange,
            "ERA/USDT:USDT",
            start,
            start + total_days * DAY_MS,
            timeframe="1d",
            timeframe_ms=DAY_MS,
        )

    assert exc_info.value.symbol == "ERA/USDT:USDT"
    assert exc_info.value.successful_pages == 3
    assert exc_info.value.next_cursor_ms < start + total_days * DAY_MS


async def test_fetch_symbol_candles_boundary_completion_on_last_budgeted_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window completes on exactly the last page the budget allows.
    This must succeed, not raise: `exhausted_page_budget` is only a problem
    when the cursor has NOT reached end_ms by the time the budget runs out."""
    monkeypatch.setattr("schurfer_analytics.ohlcv._MAX_PAGES_HARD_CAP", 3)
    start = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
    total_days = 3  # exactly matches the lowered hard cap of 3 one-bar pages

    async def one_bar_at_a_time_fetch_ohlcv(
        _symbol: str, _timeframe: str, since: int, limit: int
    ) -> list[list[float]]:
        # Exclusive since, matching exclusive_since_fetch_ohlcv above: see the
        # comment in the exhaustion test just above for why.
        bars = [
            _daily_bar(start, offset)
            for offset in range(total_days)
            if start + offset * DAY_MS > since
        ]
        return bars[:1]

    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(side_effect=one_bar_at_a_time_fetch_ohlcv)

    result = await fetch_symbol_candles(
        exchange,
        "ERA/USDT:USDT",
        start,
        start + total_days * DAY_MS,
        timeframe="1d",
        timeframe_ms=DAY_MS,
    )

    assert [c.ts_ms for c in result] == [start + offset * DAY_MS for offset in range(total_days)]


async def test_fetch_symbol_candles_empty_page_still_returns_partial_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unchanged pre-existing semantics, documented explicitly: an empty
    page after retries is still a silent partial result, not
    IncompleteFetchError. This fix only changes the page-budget-exhaustion
    case."""
    monkeypatch.setattr("schurfer_analytics.ohlcv._EMPTY_PAGE_RETRY_DELAY_SECONDS", 0)
    start = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(
        side_effect=[
            [_daily_bar(start, 0), _daily_bar(start, 1)],
            [],
            [],
            [],
        ]
    )

    result = await fetch_symbol_candles(
        exchange,
        "ERA/USDT:USDT",
        start,
        start + 5 * DAY_MS,
        timeframe="1d",
        timeframe_ms=DAY_MS,
    )

    assert [c.ts_ms for c in result] == [start, start + DAY_MS]


async def test_fetch_symbol_candles_on_page_reports_full_field_set_on_success() -> None:
    start = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(return_value=[_daily_bar(start, 0)])
    observations: list[PageFetchObservation] = []

    await fetch_symbol_candles(
        exchange,
        "ERA/USDT:USDT",
        start,
        start + DAY_MS,
        timeframe="1d",
        timeframe_ms=DAY_MS,
        on_page=observations.append,
    )

    assert len(observations) == 1
    observation = observations[0]
    assert observation.api_call_index == 1
    assert observation.attempt_index == 0
    assert observation.cursor_before_ms == start
    assert observation.requested_since_ms == start - DAY_MS
    assert observation.raw_bar_count == 1
    assert observation.normalized_bar_count == 1
    assert observation.outcome == "success"
    assert observation.error_type is None
    assert observation.latency_seconds >= 0


async def test_fetch_symbol_candles_on_page_distinguishes_raw_from_normalized_counts() -> None:
    """A page can contain rows the exchange sent but normalize_candles rejects
    (here: high below close, an invalid OHLC ordering). raw_bar_count must
    still reflect what the exchange actually returned, separate from
    normalized_bar_count, so page-size measurement is not silently mixed
    with data-quality filtering."""
    start = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
    valid = _daily_bar(start, 0)
    invalid = [start + DAY_MS, 100, 90, 99, 100, 1]  # high < close: rejected
    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(return_value=[valid, invalid])
    observations: list[PageFetchObservation] = []

    result = await fetch_symbol_candles(
        exchange,
        "ERA/USDT:USDT",
        start,
        start + DAY_MS,
        timeframe="1d",
        timeframe_ms=DAY_MS,
        on_page=observations.append,
    )

    assert [c.ts_ms for c in result] == [start]
    assert len(observations) == 1
    assert observations[0].raw_bar_count == 2
    assert observations[0].normalized_bar_count == 1
    assert observations[0].outcome == "success"


async def test_fetch_symbol_candles_on_page_reports_timeout_and_propagates() -> None:
    start = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(side_effect=TimeoutError("exchange hung"))
    observations: list[PageFetchObservation] = []

    with pytest.raises(TimeoutError):
        await fetch_symbol_candles(
            exchange,
            "ERA/USDT:USDT",
            start,
            start + DAY_MS,
            timeframe="1d",
            timeframe_ms=DAY_MS,
            on_page=observations.append,
        )

    assert len(observations) == 1
    assert observations[0].outcome == "timeout"
    assert observations[0].error_type == "TimeoutError"
    assert observations[0].raw_bar_count is None
    assert observations[0].normalized_bar_count is None


async def test_fetch_symbol_candles_on_page_reports_error_and_propagates() -> None:
    start = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(side_effect=ValueError("exchange rejected request"))
    observations: list[PageFetchObservation] = []

    with pytest.raises(ValueError, match="exchange rejected request"):
        await fetch_symbol_candles(
            exchange,
            "ERA/USDT:USDT",
            start,
            start + DAY_MS,
            timeframe="1d",
            timeframe_ms=DAY_MS,
            on_page=observations.append,
        )

    assert len(observations) == 1
    assert observations[0].outcome == "error"
    assert observations[0].error_type == "ValueError"


async def test_fetch_symbol_candles_on_page_callback_exception_propagates() -> None:
    """The callback contract is deliberately unguarded: a broken observer is a
    hard failure, not something fetch_symbol_candles silently swallows."""
    start = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(return_value=[_daily_bar(start, 0)])

    def broken_observer(_observation: PageFetchObservation) -> None:
        raise RuntimeError("observer bug")

    with pytest.raises(RuntimeError, match="observer bug"):
        await fetch_symbol_candles(
            exchange,
            "ERA/USDT:USDT",
            start,
            start + DAY_MS,
            timeframe="1d",
            timeframe_ms=DAY_MS,
            on_page=broken_observer,
        )


async def test_fetch_symbol_candles_serves_a_repeat_call_from_cache_with_no_api_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The reproducibility fix (2026-08-24): a second call with the exact
    same (exchange, symbol, timeframe, start_ms, end_ms) and use_cache=True
    must be served from the immutable cache, with zero further calls to
    fetch_ohlcv -- so a token being delisted between the two calls can never
    turn a previously-resolved episode into fetch_failed."""
    monkeypatch.setenv("SCHURFER_MARKET_PATH_CACHE_DIR", str(tmp_path))
    start = int(datetime(2026, 7, 22, 12, 0, tzinfo=UTC).timestamp() * 1000)
    exchange = AsyncMock()
    exchange.id = "binance"
    exchange.fetch_ohlcv = AsyncMock(return_value=[_bar(start, offset) for offset in range(4)])

    first = await fetch_symbol_candles(
        exchange, "DAM/USDT:USDT", start, start + 4 * TIMEFRAME_MS, use_cache=True
    )
    assert exchange.fetch_ohlcv.await_count == 1

    # Simulate the symbol being delisted: any further real call would now
    # fail. The second call must never reach it.
    exchange.fetch_ohlcv.side_effect = Exception(
        "binance does not have market symbol DAM/USDT:USDT"
    )

    second = await fetch_symbol_candles(
        exchange, "DAM/USDT:USDT", start, start + 4 * TIMEFRAME_MS, use_cache=True
    )

    assert second == first
    assert exchange.fetch_ohlcv.await_count == 1


async def test_fetch_symbol_candles_defaults_to_no_caching(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """use_cache defaults to False: a coverage/latency-diagnostic report
    that wants real API telemetry on every run (via on_page) must not be
    silently affected just because a valid exchange.id happens to be
    present. Only a caller that explicitly opts in gets caching."""
    monkeypatch.setenv("SCHURFER_MARKET_PATH_CACHE_DIR", str(tmp_path))
    start = int(datetime(2026, 7, 22, 12, 0, tzinfo=UTC).timestamp() * 1000)
    exchange = AsyncMock()
    exchange.id = "binance"
    exchange.fetch_ohlcv = AsyncMock(return_value=[_bar(start, 0)])

    await fetch_symbol_candles(exchange, "DAM/USDT:USDT", start, start + TIMEFRAME_MS)
    await fetch_symbol_candles(exchange, "DAM/USDT:USDT", start, start + TIMEFRAME_MS)

    assert exchange.fetch_ohlcv.await_count == 2
    assert list(tmp_path.rglob("*.json")) == []


async def test_fetch_symbol_candles_cache_is_keyed_per_exchange_symbol_and_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SCHURFER_MARKET_PATH_CACHE_DIR", str(tmp_path))
    start = int(datetime(2026, 7, 22, 12, 0, tzinfo=UTC).timestamp() * 1000)
    exchange = AsyncMock()
    exchange.id = "binance"
    exchange.fetch_ohlcv = AsyncMock(return_value=[_bar(start, 0)])

    await fetch_symbol_candles(
        exchange, "DAM/USDT:USDT", start, start + TIMEFRAME_MS, use_cache=True
    )
    # A different symbol on the same exchange/window is a cache miss and
    # must still call the exchange.
    await fetch_symbol_candles(
        exchange, "OTHER/USDT:USDT", start, start + TIMEFRAME_MS, use_cache=True
    )

    assert exchange.fetch_ohlcv.await_count == 2


async def test_fetch_symbol_candles_without_a_real_exchange_id_never_caches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unconfigured test double's `.id` is itself a mock object, not a
    string -- this must be treated as "no cache identity", not accidentally
    cached under the mock's repr, even with use_cache=True. Every
    pre-existing test in this file relies on exactly this to keep behaving
    unchanged."""
    monkeypatch.setenv("SCHURFER_MARKET_PATH_CACHE_DIR", str(tmp_path))
    start = int(datetime(2026, 7, 22, 12, 0, tzinfo=UTC).timestamp() * 1000)
    exchange = AsyncMock()  # exchange.id is an auto-generated Mock, not a str
    exchange.fetch_ohlcv = AsyncMock(return_value=[_bar(start, 0)])

    await fetch_symbol_candles(
        exchange, "DAM/USDT:USDT", start, start + TIMEFRAME_MS, use_cache=True
    )
    await fetch_symbol_candles(
        exchange, "DAM/USDT:USDT", start, start + TIMEFRAME_MS, use_cache=True
    )

    assert exchange.fetch_ohlcv.await_count == 2
    assert list(tmp_path.rglob("*.json")) == []


async def test_fetch_symbol_candles_never_caches_a_raised_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A transient failure (timeout, connectivity error, IncompleteFetchError)
    must never be memorized as a permanent answer -- the next call has to be
    allowed to actually retry against the exchange."""
    monkeypatch.setattr("schurfer_analytics.ohlcv._EMPTY_PAGE_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setenv("SCHURFER_MARKET_PATH_CACHE_DIR", str(tmp_path))
    start = int(datetime(2026, 7, 22, 12, 0, tzinfo=UTC).timestamp() * 1000)
    exchange = AsyncMock()
    exchange.id = "binance"
    exchange.fetch_ohlcv = AsyncMock(side_effect=TimeoutError("slow venue"))

    with pytest.raises(TimeoutError):
        await fetch_symbol_candles(
            exchange, "DAM/USDT:USDT", start, start + TIMEFRAME_MS, use_cache=True
        )

    assert list(tmp_path.rglob("*.json")) == []

    # A later, successful call must not be blocked by the earlier failure.
    exchange.fetch_ohlcv.side_effect = None
    exchange.fetch_ohlcv.return_value = [_bar(start, 0)]
    result = await fetch_symbol_candles(
        exchange, "DAM/USDT:USDT", start, start + TIMEFRAME_MS, use_cache=True
    )
    assert [c.ts_ms for c in result] == [start]


async def test_fetch_symbol_candles_never_caches_an_ambiguous_empty_page_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty-page-after-retries result is, by this function's own
    docstring, not distinguishable here from a transient hiccup versus a
    real retention limit/delisted instrument -- it must never be cached,
    even with use_cache=True, or a single bad response could freeze a wrong
    `no_data` result in place forever."""
    monkeypatch.setattr("schurfer_analytics.ohlcv._EMPTY_PAGE_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setenv("SCHURFER_MARKET_PATH_CACHE_DIR", str(tmp_path))
    start = int(datetime(2026, 7, 22, 12, 0, tzinfo=UTC).timestamp() * 1000)
    exchange = AsyncMock()
    exchange.id = "binance"
    exchange.fetch_ohlcv = AsyncMock(return_value=[])  # exhausts retries, returns partial (empty)

    first = await fetch_symbol_candles(
        exchange, "DAM/USDT:USDT", start, start + 3 * TIMEFRAME_MS, use_cache=True
    )
    assert first == []
    calls_before = exchange.fetch_ohlcv.await_count
    assert calls_before > 0
    assert list(tmp_path.rglob("*.json")) == []

    second = await fetch_symbol_candles(
        exchange, "DAM/USDT:USDT", start, start + 3 * TIMEFRAME_MS, use_cache=True
    )
    assert second == []
    # A real, un-cached retry -- more calls, not served from a frozen cache.
    assert exchange.fetch_ohlcv.await_count > calls_before


async def test_fetch_symbol_candles_never_caches_a_cursor_stall_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Same reasoning as the empty-page case, for the other ambiguous
    partial-result path."""
    monkeypatch.setenv("SCHURFER_MARKET_PATH_CACHE_DIR", str(tmp_path))
    start = int(datetime(2026, 7, 22, 12, 0, tzinfo=UTC).timestamp() * 1000)
    exchange = AsyncMock()
    exchange.id = "binance"
    exchange.fetch_ohlcv = AsyncMock(return_value=[_bar(start, 0)])  # never advances the cursor

    result = await fetch_symbol_candles(
        exchange, "DAM/USDT:USDT", start, start + 3 * TIMEFRAME_MS, use_cache=True
    )

    assert [c.ts_ms for c in result] == [start]
    assert list(tmp_path.rglob("*.json")) == []


async def test_fetch_symbol_candles_propagates_cache_corruption_instead_of_masking_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A corrupt cache entry must be a hard failure for a formal report, not
    a silent fall-through to a live re-fetch -- silently masking it would
    reintroduce exactly the "same manifest, different result" hazard this
    cache exists to close."""
    monkeypatch.setenv("SCHURFER_MARKET_PATH_CACHE_DIR", str(tmp_path))
    start = int(datetime(2026, 7, 22, 12, 0, tzinfo=UTC).timestamp() * 1000)
    exchange = AsyncMock()
    exchange.id = "binance"
    exchange.fetch_ohlcv = AsyncMock(return_value=[_bar(start, offset) for offset in range(4)])

    await fetch_symbol_candles(
        exchange, "DAM/USDT:USDT", start, start + 4 * TIMEFRAME_MS, use_cache=True
    )
    cache_files = list(tmp_path.rglob("*.json"))
    assert len(cache_files) == 1
    cache_files[0].write_text("{not valid json")

    with pytest.raises(MarketPathCacheCorruptError):
        await fetch_symbol_candles(
            exchange, "DAM/USDT:USDT", start, start + 4 * TIMEFRAME_MS, use_cache=True
        )


async def test_fetch_symbol_candles_never_caches_a_result_with_a_leading_gap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """cursor >= end_ms alone does not prove the window's FIRST bar arrived
    -- an exchange can start its first page later than start_ms while still
    reaching end_ms by the last page. This must never be cached."""
    monkeypatch.setenv("SCHURFER_MARKET_PATH_CACHE_DIR", str(tmp_path))
    start = int(datetime(2026, 7, 22, 12, 0, tzinfo=UTC).timestamp() * 1000)
    exchange = AsyncMock()
    exchange.id = "binance"
    # Window is [start, start+3*TF); bar at offset 0 (start) never arrives.
    exchange.fetch_ohlcv = AsyncMock(return_value=[_bar(start, offset) for offset in (1, 2)])

    result = await fetch_symbol_candles(
        exchange, "DAM/USDT:USDT", start, start + 3 * TIMEFRAME_MS, use_cache=True
    )

    assert [c.ts_ms for c in result] == [start + TIMEFRAME_MS, start + 2 * TIMEFRAME_MS]
    assert list(tmp_path.rglob("*.json")) == []


async def test_fetch_symbol_candles_never_caches_a_result_with_an_internal_gap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Same reasoning for a gap in the middle of the window rather than at
    the edge -- cursor >= end_ms cannot see it either."""
    monkeypatch.setenv("SCHURFER_MARKET_PATH_CACHE_DIR", str(tmp_path))
    start = int(datetime(2026, 7, 22, 12, 0, tzinfo=UTC).timestamp() * 1000)
    exchange = AsyncMock()
    exchange.id = "binance"
    # Window is [start, start+4*TF); bar at offset 2 never arrives.
    exchange.fetch_ohlcv = AsyncMock(return_value=[_bar(start, offset) for offset in (0, 1, 3)])

    result = await fetch_symbol_candles(
        exchange, "DAM/USDT:USDT", start, start + 4 * TIMEFRAME_MS, use_cache=True
    )

    assert [c.ts_ms for c in result] == [start, start + TIMEFRAME_MS, start + 3 * TIMEFRAME_MS]
    assert list(tmp_path.rglob("*.json")) == []


async def test_fetch_symbol_candles_returns_the_winners_candles_after_losing_a_write_race(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If persisting loses the first-writer-wins race, the already-durable
    winner's candles must be returned instead of this call's own -- two
    callers racing on the same window must never end up disagreeing about
    what the "reproducible" answer is."""
    monkeypatch.setenv("SCHURFER_MARKET_PATH_CACHE_DIR", str(tmp_path))
    start = int(datetime(2026, 7, 22, 12, 0, tzinfo=UTC).timestamp() * 1000)
    winner_candles = [Candle(ts_ms=start, open=1.0, high=1.1, low=0.9, close=1.05, volume=10.0)]
    # A concurrent writer already durably won this exact key before this
    # call ever runs.
    write_cached_candles(
        exchange_id="binance",
        symbol="DAM/USDT:USDT",
        timeframe=TIMEFRAME,
        start_ms=start,
        end_ms=start + TIMEFRAME_MS,
        candles=winner_candles,
    )

    exchange = AsyncMock()
    exchange.id = "binance"
    # This call's own fetch would produce DIFFERENT candles if it were used.
    exchange.fetch_ohlcv = AsyncMock(return_value=[_bar(start, 0)])

    result = await fetch_symbol_candles(
        exchange, "DAM/USDT:USDT", start, start + TIMEFRAME_MS, use_cache=True
    )

    assert result == winner_candles


async def test_fetch_symbol_candles_raises_when_the_write_itself_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """use_cache=True is a durability contract, not a best-effort hint: if a
    formal report finishes without actually persisting anything, it would
    silently believe it is protected against a future delisting when it is
    not. This must fail loudly instead."""
    # r-x, not read-only: the cache-check read at the top of
    # fetch_symbol_candles must see a clean miss (it can traverse and stat),
    # so the failure this test exercises is specifically the write, not an
    # earlier read blowing up for an unrelated reason.
    unwritable = tmp_path / "read-only-cache-root"
    unwritable.mkdir(mode=0o500)
    try:
        monkeypatch.setenv("SCHURFER_MARKET_PATH_CACHE_DIR", str(unwritable / "cache"))
        start = int(datetime(2026, 7, 22, 12, 0, tzinfo=UTC).timestamp() * 1000)
        exchange = AsyncMock()
        exchange.id = "binance"
        exchange.fetch_ohlcv = AsyncMock(return_value=[_bar(start, 0)])

        with pytest.raises(MarketPathCacheWriteError):
            await fetch_symbol_candles(
                exchange, "DAM/USDT:USDT", start, start + TIMEFRAME_MS, use_cache=True
            )
    finally:
        unwritable.chmod(0o700)


def test_one_minute_normalization_and_strict_next_bar_are_explicit() -> None:
    aligned = 10 * ONE_MINUTE_MS
    rows = normalize_candles(
        [[aligned, 10, 11, 9, 10, 1]],
        timeframe_ms=ONE_MINUTE_MS,
    )

    assert rows[0].ts_ms == aligned
    assert next_timeframe_after(aligned, ONE_MINUTE_MS) == aligned + ONE_MINUTE_MS
    assert next_timeframe_after(aligned + 1, ONE_MINUTE_MS) == aligned + ONE_MINUTE_MS
