from unittest.mock import AsyncMock, MagicMock

import pytest
from schurfer_analytics.derivatives_history import (
    METHODS,
    fetch_derivatives_history,
)

SINCE_MS = 1_786_003_200_000
UNTIL_MS = SINCE_MS + 15 * 60_000


async def test_fetcher_paginates_with_the_ccxt_cursor_contract() -> None:
    method = METHODS[2]
    exchange = MagicMock()
    exchange.fetch_mark_ohlcv = AsyncMock(
        side_effect=[
            [
                [SINCE_MS, 1, 2, 0.5, 1.5, 10],
                [SINCE_MS + 5 * 60_000, 1, 2, 0.5, 1.5, 10],
            ],
            [
                [SINCE_MS + 10 * 60_000, 1, 2, 0.5, 1.5, 10],
            ],
        ]
    )

    result = await fetch_derivatives_history(
        exchange,
        method,
        "ERA/USDT:USDT",
        timeframe="5m",
        since_ms=SINCE_MS,
        until_ms=UNTIL_MS,
        limit=2,
        max_pages=3,
        timeout_seconds=1,
    )

    assert result.request_count == 2
    assert len(result.rows) == 3
    assert result.error_status is None
    assert result.pagination_exhausted is False
    exchange.fetch_mark_ohlcv.assert_any_await(
        "ERA/USDT:USDT",
        "5m",
        SINCE_MS + 10 * 60_000,
        2,
    )


async def test_event_pagination_stall_is_reported_as_incomplete() -> None:
    method = METHODS[0]
    event = {"timestamp": SINCE_MS + 60_000}
    exchange = MagicMock()
    exchange.fetch_funding_rate_history = AsyncMock(side_effect=[[event], [event]])

    result = await fetch_derivatives_history(
        exchange,
        method,
        "ERA/USDT:USDT",
        timeframe=None,
        since_ms=SINCE_MS,
        until_ms=UNTIL_MS,
        limit=2,
        max_pages=3,
        timeout_seconds=1,
    )

    assert result.request_count == 2
    assert result.pagination_exhausted is True
    assert result.error == "pagination made no forward progress"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"since_ms": UNTIL_MS}, "since must be earlier"),
        ({"limit": 0}, "page limit"),
        ({"max_pages": 0}, "max pages"),
        ({"timeout_seconds": 0}, "timeout"),
        ({"timeframe": None}, "requires a timeframe"),
    ],
)
async def test_fetcher_rejects_unbounded_or_ambiguous_inputs(
    change: dict[str, object],
    message: str,
) -> None:
    values: dict[str, object] = {
        "timeframe": "5m",
        "since_ms": SINCE_MS,
        "until_ms": UNTIL_MS,
        "limit": 2,
        "max_pages": 3,
        "timeout_seconds": 1,
    }
    values.update(change)

    with pytest.raises(ValueError, match=message):
        await fetch_derivatives_history(
            MagicMock(),
            METHODS[2],
            "ERA/USDT:USDT",
            **values,  # type: ignore[arg-type]
        )
