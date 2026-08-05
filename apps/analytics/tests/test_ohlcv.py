from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from schurfer_analytics.ohlcv import (
    ONE_MINUTE_MS,
    TIMEFRAME_MS,
    closed_candles,
    fetch_candles,
    fetch_symbol_candles,
    next_timeframe_after,
    normalize_candles,
    window_bounds,
)


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


def test_one_minute_normalization_and_strict_next_bar_are_explicit() -> None:
    aligned = 10 * ONE_MINUTE_MS
    rows = normalize_candles(
        [[aligned, 10, 11, 9, 10, 1]],
        timeframe_ms=ONE_MINUTE_MS,
    )

    assert rows[0].ts_ms == aligned
    assert next_timeframe_after(aligned, ONE_MINUTE_MS) == aligned + ONE_MINUTE_MS
    assert next_timeframe_after(aligned + 1, ONE_MINUTE_MS) == aligned + ONE_MINUTE_MS
