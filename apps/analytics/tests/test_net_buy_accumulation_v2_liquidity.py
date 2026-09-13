"""Tests for the outcome-blind executability / capacity analysis (dynamic sizing on
point-in-time trailing flow)."""

from __future__ import annotations

from schurfer_analytics.net_buy_accumulation_v2_liquidity import (
    FireFlow,
    capacity_at_target,
    capacity_curve,
)


def _fire(trailing_flow: float, fire_minute: float = 0.0) -> FireFlow:
    return FireFlow("bybit", "FOOUSDT", trailing_flow, fire_minute)


def test_deliverable_is_capped_by_trailing_flow() -> None:
    # cap 0.10 of 5_000 trailing flow = 500 deliverable, below a 1500 target.
    f = _fire(5_000.0)
    assert f.deliverable_notional(1500.0, 0.10) == 500.0


def test_deliverable_reaches_full_target_when_flow_is_deep() -> None:
    # cap 0.10 of 100_000 = 10_000 >= 1500, so we deliver the full target.
    f = _fire(100_000.0)
    assert f.deliverable_notional(1500.0, 0.10) == 1500.0


def test_zero_trailing_flow_delivers_nothing() -> None:
    assert _fire(0.0).deliverable_notional(1500.0, 0.10) == 0.0


def test_fire_minute_notional_is_diagnostic_not_used_in_sizing() -> None:
    # A huge fire-minute notional must not change the deliverable: sizing is on
    # trailing flow only (no look-ahead).
    lean = _fire(5_000.0, fire_minute=0.0)
    rich = _fire(5_000.0, fire_minute=10_000_000.0)
    assert lean.deliverable_notional(1500.0, 0.10) == rich.deliverable_notional(1500.0, 0.10)


def test_capacity_counts_only_fires_above_min_economic_notional() -> None:
    # target 1500, cap 0.10, min economic 50.
    fires = [
        _fire(100_000.0),  # deliver 1500 (full)
        _fire(5_000.0),  # deliver 500 (flow-capped, still >= 50)
        _fire(400.0),  # deliver 40 (< 50 -> not tradeable)
        _fire(0.0),  # zero flow -> not tradeable
    ]
    cp = capacity_at_target(
        "P-MAG", 0.25, fires, target_notional_usd=1500.0, cap=0.10, min_economic_notional_usd=50.0
    )
    assert cp.n_fires == 4
    assert cp.n_tradeable == 2
    assert cp.flow_capped_fires == 1
    assert cp.zero_flow_fires == 1
    assert cp.tradeable_rate == 0.5
    assert cp.delivered_median_usd == 1000.0  # median of [500, 1500]
    assert cp.delivered_p25_usd == 750.0  # interpolated 25th pct of [500, 1500]


def test_smaller_target_recovers_throughput() -> None:
    # A fire that can only absorb 40 at any cap is dead at target 1500 but alive at a
    # smaller target IF the deliverable clears the min economic notional. Here flow
    # 5_000 * 0.10 = 500 caps every target at 500, so target 50/100/300 deliver the
    # full (sub-cap) target and target 1500 is flow-capped to 500.
    fires = [_fire(5_000.0)]
    curve = capacity_curve(
        "P-SHAPE",
        0.25,
        fires,
        targets=(50.0, 100.0, 300.0, 1500.0),
        cap=0.10,
        min_economic_notional_usd=50.0,
    )
    delivered = {cp.target_notional_usd: cp.delivered_median_usd for cp in curve}
    assert delivered[50.0] == 50.0
    assert delivered[100.0] == 100.0
    assert delivered[300.0] == 300.0
    assert delivered[1500.0] == 500.0  # capped by 0.10 * 5000


def test_empty_fire_set_yields_none_rates() -> None:
    cp = capacity_at_target("P-MAG", 0.25, [], target_notional_usd=1500.0)
    assert cp.n_fires == 0
    assert cp.n_tradeable == 0
    assert cp.tradeable_rate is None
    assert cp.delivered_median_usd is None
