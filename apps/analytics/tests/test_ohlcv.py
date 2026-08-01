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


async def test_fetch_symbol_candles_preserves_identity_validated_symbol() -> None:
    start = int(datetime(2026, 7, 22, 12, 0, tzinfo=UTC).timestamp() * 1000)
    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(return_value=[])

    await fetch_symbol_candles(
        exchange,
        "1000EDGE/USDT:USDT",
        start,
        start + TIMEFRAME_MS,
    )

    exchange.fetch_ohlcv.assert_awaited_once_with(
        "1000EDGE/USDT:USDT",
        "5m",
        since=start,
        limit=2,
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
