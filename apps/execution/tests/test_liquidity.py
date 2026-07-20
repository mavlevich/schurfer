import asyncio
import math
from unittest.mock import AsyncMock, patch

import pytest
from schurfer_execution.liquidity import _vwap_impact_bps, snapshot


def test_vwap_impact_single_level() -> None:
    # $100 fills inside the first ask level at 100.1, mid 100 -> 10 bps.
    asks = [[100.1, 100.0], [100.2, 100.0]]
    assert _vwap_impact_bps(asks, mid=100.0, target_usd=100.0, side="ask") == 10.0


def test_vwap_impact_walks_levels() -> None:
    # $150 takes all of level 1 ($100 at 100.0) then $50 at 102.0.
    # base = 100/100 + 50/102 = 1.0 + 0.4902 = 1.4902; vwap = 150 / 1.4902 = 100.657
    asks = [[100.0, 1.0], [102.0, 100.0]]
    impact = _vwap_impact_bps(asks, mid=100.0, target_usd=150.0, side="ask")
    assert impact == pytest.approx(65.7, abs=0.5)


def test_vwap_impact_bid_side_is_positive_distance() -> None:
    bids = [[99.9, 100.0]]
    assert _vwap_impact_bps(bids, mid=100.0, target_usd=100.0, side="bid") == 10.0


def test_vwap_impact_none_when_book_too_thin() -> None:
    asks = [[100.1, 0.1]]  # only ~$10 of depth, cannot fill $100
    assert _vwap_impact_bps(asks, mid=100.0, target_usd=100.0, side="ask") is None


async def test_snapshot_summarizes_book() -> None:
    ex = AsyncMock()
    ex.fetch_order_book = AsyncMock(
        return_value={
            "bids": [[99.9, 100.0], [99.8, 100.0]],
            "asks": [[100.1, 100.0], [100.2, 100.0]],
        }
    )
    snap = await snapshot(ex, "BEAT")
    assert snap is not None
    assert snap["best_bid"] == 99.9
    assert snap["best_ask"] == 100.1
    assert snap["mid"] == 100.0
    assert snap["spread_bps"] == 20.0
    assert snap["ask_impact_bps"]["100"] == 10.0
    assert snap["bid_impact_bps"]["100"] == 10.0
    ex.fetch_order_book.assert_awaited_once_with("BEAT/USDT:USDT", 50)


async def test_snapshot_returns_none_on_fetch_error() -> None:
    ex = AsyncMock()
    ex.fetch_order_book = AsyncMock(side_effect=RuntimeError("boom"))
    assert await snapshot(ex, "BEAT") is None


async def test_snapshot_returns_none_on_empty_book() -> None:
    ex = AsyncMock()
    ex.fetch_order_book = AsyncMock(return_value={"bids": [], "asks": []})
    assert await snapshot(ex, "BEAT") is None


async def test_snapshot_returns_none_on_crossed_book() -> None:
    ex = AsyncMock()
    ex.fetch_order_book = AsyncMock(return_value={"bids": [[101.0, 1.0]], "asks": [[100.0, 1.0]]})
    assert await snapshot(ex, "BEAT") is None


async def test_snapshot_returns_none_on_malformed_levels_without_raising() -> None:
    # A valid response shape but non-numeric level values must not propagate out
    # of snapshot and abort the caller's tick.
    ex = AsyncMock()
    ex.fetch_order_book = AsyncMock(return_value={"bids": [["oops", None]], "asks": [[100.1, 1.0]]})
    assert await snapshot(ex, "BEAT") is None


async def test_snapshot_returns_none_on_non_list_book() -> None:
    ex = AsyncMock()
    ex.fetch_order_book = AsyncMock(return_value={"bids": "nope", "asks": "nope"})
    assert await snapshot(ex, "BEAT") is None


async def test_snapshot_returns_none_on_nan_prices() -> None:
    # float("nan") does not raise, so it would slip through and serialize to an
    # invalid jsonb token. snapshot must reject it up front.
    ex = AsyncMock()
    ex.fetch_order_book = AsyncMock(
        return_value={"bids": [["nan", 100.0]], "asks": [["nan", 100.0]]}
    )
    assert await snapshot(ex, "BEAT") is None


async def test_snapshot_impacts_are_finite_or_none() -> None:
    ex = AsyncMock()
    ex.fetch_order_book = AsyncMock(
        return_value={
            "bids": [[99.9, 100.0]],
            "asks": [[100.1, 100.0]],
        }
    )
    snap = await snapshot(ex, "BEAT")
    assert snap is not None
    for impacts in (snap["bid_impact_bps"], snap["ask_impact_bps"]):
        for v in impacts.values():
            assert v is None or math.isfinite(v)


async def test_snapshot_returns_none_on_timeout() -> None:
    async def _hang(*_args: object, **_kwargs: object) -> dict[str, object]:
        await asyncio.sleep(10)
        return {}

    ex = AsyncMock()
    ex.fetch_order_book = _hang
    with patch("schurfer_execution.liquidity._FETCH_TIMEOUT", 0.01):
        assert await snapshot(ex, "BEAT") is None
