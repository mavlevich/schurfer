import asyncio
import math
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from schurfer_execution.liquidity import (
    _vwap_impact_bps,
    capture_snapshot,
    check_market_quality,
    depth_target_key,
    depth_target_usd,
    snapshot,
)


def _quality_snapshot(
    *,
    spread: object = 20.0,
    bid_impact: object = 30.0,
    ask_impact: object = 40.0,
) -> dict[str, Any]:
    return {
        "spread_bps": spread,
        "bid_impact_bps": {"100": bid_impact},
        "ask_impact_bps": {"100": ask_impact},
    }


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


def test_vwap_impact_converts_contract_amount_to_base_amount() -> None:
    # Two contracts represent 0.02 base units, so this level contains only $2.
    asks = [[100.0, 2.0]]

    assert (
        _vwap_impact_bps(
            asks,
            mid=100.0,
            target_usd=100.0,
            side="ask",
            contract_size=0.01,
        )
        is None
    )


def test_depth_target_uses_position_cap_and_multiplier() -> None:
    assert depth_target_usd(33.333, 2.0) == 66.67


def test_depth_target_key_is_canonical() -> None:
    assert depth_target_key(50.0) == "50"
    assert depth_target_key(66.67) == "66.67"


def test_market_quality_accepts_threshold_boundaries() -> None:
    check = check_market_quality(
        _quality_snapshot(spread=50, bid_impact=50, ask_impact=50),
        target_usd=100,
        max_spread_bps=50,
        max_impact_bps=50,
    )

    assert check.allowed
    assert check.reason == "ok"
    assert check.depth_target_usd == 100


@pytest.mark.parametrize(
    ("snapshot_data", "expected_reason"),
    [
        (None, "market_quality_snapshot_unavailable"),
        (_quality_snapshot(spread="nan"), "market_quality_invalid_spread"),
        (_quality_snapshot(spread=50.01), "market_quality_spread_too_wide"),
        (
            _quality_snapshot(bid_impact=None),
            "market_quality_insufficient_bid_depth",
        ),
        (
            _quality_snapshot(ask_impact=None),
            "market_quality_insufficient_ask_depth",
        ),
        (
            _quality_snapshot(bid_impact=50.01),
            "market_quality_entry_impact_too_high",
        ),
        (
            _quality_snapshot(ask_impact=50.01),
            "market_quality_exit_impact_too_high",
        ),
    ],
)
def test_market_quality_fails_closed(
    snapshot_data: dict[str, Any] | None,
    expected_reason: str,
) -> None:
    check = check_market_quality(
        snapshot_data,
        target_usd=100,
        max_spread_bps=50,
        max_impact_bps=50,
    )

    assert not check.allowed
    assert check.reason == expected_reason


async def test_snapshot_summarizes_book() -> None:
    ex = AsyncMock()
    ex.fetch_order_book = AsyncMock(
        return_value={
            "bids": [[99.9, 100.0], [99.8, 100.0]],
            "asks": [[100.1, 100.0], [100.2, 100.0]],
        }
    )
    snap = await snapshot(ex, "BEAT/USDT:USDT")
    assert snap is not None
    assert snap["best_bid"] == 99.9
    assert snap["best_ask"] == 100.1
    assert snap["mid"] == 100.0
    assert snap["contract_size"] == 1.0
    assert snap["spread_bps"] == 20.0
    assert snap["depth_targets_usd"] == [100.0, 500.0, 1000.0]
    assert snap["ask_impact_bps"]["100"] == 10.0
    assert snap["bid_impact_bps"]["100"] == 10.0
    assert snap["ask_vwap"]["100"] == 100.1
    assert snap["ask_filled_usd"]["100"] == 100.0
    ex.fetch_order_book.assert_awaited_once_with("BEAT/USDT:USDT", 50)


async def test_snapshot_adds_exact_required_depth_without_duplicates() -> None:
    ex = AsyncMock()
    ex.fetch_order_book = AsyncMock(
        return_value={
            "bids": [[99.9, 1000.0]],
            "asks": [[100.1, 1000.0]],
        }
    )

    custom = await snapshot(ex, "BEAT/USDT:USDT", required_depth_usd=66.67)
    existing = await snapshot(ex, "BEAT/USDT:USDT", required_depth_usd=100.0)

    assert custom is not None
    assert custom["depth_targets_usd"] == [66.67, 100.0, 500.0, 1000.0]
    assert "66.67" in custom["bid_impact_bps"]
    assert existing is not None
    assert existing["depth_targets_usd"] == [100.0, 500.0, 1000.0]


async def test_snapshot_uses_derivative_contract_size_for_depth() -> None:
    ex = AsyncMock()
    ex.markets = {
        "BEAT/USDT:USDT": {
            "contract": True,
            "contractSize": 0.01,
        }
    }
    ex.fetch_order_book = AsyncMock(
        return_value={
            "bids": [[99.9, 10.0]],
            "asks": [[100.1, 10.0]],
        }
    )

    snap = await snapshot(ex, "BEAT/USDT:USDT")

    assert snap is not None
    assert snap["contract_size"] == 0.01
    assert snap["bid_impact_bps"]["100"] is None
    assert snap["ask_impact_bps"]["100"] is None


async def test_snapshot_returns_none_on_fetch_error() -> None:
    ex = AsyncMock()
    ex.fetch_order_book = AsyncMock(side_effect=RuntimeError("boom"))
    assert await snapshot(ex, "BEAT/USDT:USDT") is None


async def test_snapshot_returns_none_on_empty_book() -> None:
    ex = AsyncMock()
    ex.fetch_order_book = AsyncMock(return_value={"bids": [], "asks": []})
    assert await snapshot(ex, "BEAT/USDT:USDT") is None


async def test_snapshot_returns_none_on_crossed_book() -> None:
    ex = AsyncMock()
    ex.fetch_order_book = AsyncMock(return_value={"bids": [[101.0, 1.0]], "asks": [[100.0, 1.0]]})
    assert await snapshot(ex, "BEAT/USDT:USDT") is None


async def test_snapshot_returns_none_on_malformed_levels_without_raising() -> None:
    # A valid response shape but non-numeric level values must not propagate out
    # of snapshot and abort the caller's tick.
    ex = AsyncMock()
    ex.fetch_order_book = AsyncMock(return_value={"bids": [["oops", None]], "asks": [[100.1, 1.0]]})
    assert await snapshot(ex, "BEAT/USDT:USDT") is None


async def test_snapshot_returns_none_on_non_list_book() -> None:
    ex = AsyncMock()
    ex.fetch_order_book = AsyncMock(return_value={"bids": "nope", "asks": "nope"})
    assert await snapshot(ex, "BEAT/USDT:USDT") is None


async def test_snapshot_returns_none_on_nan_prices() -> None:
    # float("nan") does not raise, so it would slip through and serialize to an
    # invalid jsonb token. snapshot must reject it up front.
    ex = AsyncMock()
    ex.fetch_order_book = AsyncMock(
        return_value={"bids": [["nan", 100.0]], "asks": [["nan", 100.0]]}
    )
    assert await snapshot(ex, "BEAT/USDT:USDT") is None


async def test_snapshot_impacts_are_finite_or_none() -> None:
    ex = AsyncMock()
    ex.fetch_order_book = AsyncMock(
        return_value={
            "bids": [[99.9, 100.0]],
            "asks": [[100.1, 100.0]],
        }
    )
    snap = await snapshot(ex, "BEAT/USDT:USDT")
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
        assert await snapshot(ex, "BEAT/USDT:USDT") is None


async def test_capture_snapshot_preserves_fetch_failure_status() -> None:
    ex = AsyncMock()
    ex.fetch_order_book = AsyncMock(side_effect=RuntimeError("venue unavailable"))

    capture = await capture_snapshot(ex, "BEAT/USDT:USDT", required_depth_usd=50)

    assert capture.status == "fetch_failed"
    assert capture.snapshot is None
    assert capture.error == "RuntimeError: venue unavailable"
    assert capture.latency_ms >= 0


async def test_capture_snapshot_distinguishes_invalid_book() -> None:
    ex = AsyncMock()
    ex.fetch_order_book = AsyncMock(return_value={"bids": [], "asks": []})

    capture = await capture_snapshot(ex, "BEAT/USDT:USDT", required_depth_usd=50)

    assert capture.status == "invalid_book"
    assert capture.snapshot is None
    assert capture.error == "ValueError: order book has an empty side"


async def test_capture_snapshot_preserves_timeout_status() -> None:
    async def _hang(*_args: object, **_kwargs: object) -> dict[str, object]:
        await asyncio.sleep(10)
        return {}

    ex = AsyncMock()
    ex.fetch_order_book = _hang
    with patch("schurfer_execution.liquidity._FETCH_TIMEOUT", 0.01):
        capture = await capture_snapshot(ex, "BEAT/USDT:USDT", required_depth_usd=50)

    assert capture.status == "timeout"
    assert capture.snapshot is None
    assert capture.error is not None
