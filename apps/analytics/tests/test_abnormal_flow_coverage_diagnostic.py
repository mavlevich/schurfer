from __future__ import annotations

from datetime import UTC, datetime

import pytest
from schurfer_analytics.abnormal_flow_coverage_diagnostic import summarize_coverage
from schurfer_analytics.abnormal_flow_replay import DecisionFeatures
from schurfer_analytics.abnormal_flow_snapshots import decision_id


def _decision(exchange: str, symbol: str, week: str, minute: int) -> DecisionFeatures:
    return DecisionFeatures(
        exchange=exchange,
        market_type="linear",
        native_market_id=symbol,
        capture_version="v1",
        symbol=symbol,
        canonical_asset=f"asset:{symbol}",
        decision_at=datetime(2026, 1, 1, 0, minute, tzinfo=UTC),
        oi_growth_pct=10.0 + minute,
        buy_pressure=0.7,
        containment=0.02,
        oi_native_amount=100.0,
        oi_native_value_usd=10_000.0,
        decision_price=100.0,
        pre_decision_turnover_usd=1_000.0,
        iso_week=week,
    )


def test_coverage_is_split_by_role_venue_and_week() -> None:
    first = _decision("bybit", "A", "2026-W01", 0)
    second = _decision("binance", "B", "2026-W02", 1)
    first_control = _decision("bybit", "C", "2026-W01", 2)
    second_control = _decision("binance", "D", "2026-W02", 3)
    summary = summarize_coverage(
        [first, second],
        {decision_id(first): [first_control], decision_id(second): [second_control]},
        {first.route_key(), first_control.route_key()},
    )

    assert summary["primary"]["overall"] == {
        "total": 2,
        "resolved": 1,
        "unresolved": 1,
        "resolved_fraction": 0.5,
    }
    assert summary["controls"]["overall"]["resolved_fraction"] == 0.5
    assert summary["controls"]["primary_episode_coverage_fraction"] == 0.5
    assert summary["controls"]["resolved_primary_episode_coverage_fraction"] == 1.0
    by_exchange = summary["primary"]["by_exchange"]
    assert by_exchange[0]["group"] == ["binance"]
    assert by_exchange[0]["resolved"] == 0
    assert by_exchange[1]["group"] == ["bybit"]
    assert by_exchange[1]["resolved"] == 1
    feature = summary["primary"]["feature_comparison"]["oi_growth_pct"]
    assert feature["resolved"]["mean"] == pytest.approx(10.0)
    assert feature["unresolved"]["mean"] == pytest.approx(11.0)
