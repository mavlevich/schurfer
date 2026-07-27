from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.replay import ReplayDecision, ReplayEpisode
from schurfer_analytics.virtual_threshold_challengers import (
    ENTRY_THRESHOLD_VARIANTS,
    registered_thresholds,
    select_threshold_decision,
    selected_threshold_decisions,
)


def _decision(
    row_id: int,
    pump_pct: float,
    *,
    score: int = 7,
    score_threshold: object = 6,
    require_market_quality: object = True,
    quality_allowed: object = True,
) -> ReplayDecision:
    ts = datetime(2026, 7, 27, 7, tzinfo=UTC) + timedelta(minutes=row_id)
    return ReplayDecision(
        row_id=row_id,
        decision_id=f"00000000-0000-0000-0000-{row_id:012d}",
        pump_event_id=42,
        event_base="ERA",
        event_first_seen_at=datetime(2026, 7, 27, 7, tzinfo=UTC),
        event_closed_at=datetime(2026, 7, 28, tzinfo=UTC),
        ts=ts,
        base="ERA",
        exchange="binance",
        action="skipped",
        reason="measurement",
        score=score,
        pump_pct=pump_pct,
        price=100,
        strategy_version="pump_short_measurement_v1",
        features={
            "signal": {"computed_at": ts.timestamp()},
            "config": {
                "score_threshold": score_threshold,
                "require_market_quality": require_market_quality,
            },
        },
        liquidity={"quality": {"allowed": quality_allowed}},
        outcomes=(),
    )


def _episode(*decisions: ReplayDecision) -> ReplayEpisode:
    return ReplayEpisode(42, "ERA", "base:ERA", decisions, ())


def test_registered_thresholds_lock_baseline_and_five_challengers() -> None:
    assert registered_thresholds() == (30.0, 20.0, 25.0, 35.0, 40.0, 50.0)
    assert tuple(variant.key for variant in ENTRY_THRESHOLD_VARIANTS) == (
        "floor_20",
        "floor_25",
        "floor_35",
        "floor_40",
        "floor_50",
    )


def test_selection_is_inclusive_and_uses_first_gate_eligible_crossing() -> None:
    below = _decision(1, 24.99)
    score_failed = _decision(2, 25.0, score=5)
    quality_failed = _decision(3, 27.0, quality_allowed=False)
    selected = _decision(4, 28.0)

    result = select_threshold_decision(
        _episode(below, score_failed, quality_failed, selected),
        25.0,
    )

    assert result.status == "selected"
    assert result.decision is selected


def test_no_qualifying_crossing_is_zero_return_candidate_not_missing() -> None:
    result = select_threshold_decision(_episode(_decision(1, 29.9)), 30.0)

    assert result.status == "not_triggered"
    assert result.decision is None
    assert result.error is None


@pytest.mark.parametrize(
    ("decision", "error"),
    [
        (_decision(1, float("nan")), "invalid_pump_pct"),
        (replace(_decision(1, 30), score=None), "missing_score"),
        (_decision(1, 30, score_threshold="bad"), "invalid_score_threshold"),
        (_decision(1, 30, require_market_quality="yes"), "invalid_require_market_quality"),
        (_decision(1, 30, quality_allowed=None), "invalid_market_quality_allowed"),
    ],
)
def test_malformed_point_in_time_gate_data_fails_closed(
    decision: ReplayDecision,
    error: str,
) -> None:
    result = select_threshold_decision(_episode(decision), 30.0)

    assert result.status == "unresolved"
    assert result.error == error


def test_market_quality_snapshot_is_not_required_when_gate_was_disabled() -> None:
    decision = replace(
        _decision(1, 30, require_market_quality=False),
        liquidity={"status": "fetch_failed"},
    )

    result = select_threshold_decision(_episode(decision), 30.0)

    assert result.status == "selected"


def test_selected_decision_union_deduplicates_shared_crossings() -> None:
    first = _decision(1, 30)
    second = _decision(2, 50)

    selected = selected_threshold_decisions((_episode(first, second),))

    assert selected == (first, second)
