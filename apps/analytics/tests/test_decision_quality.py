from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.decision_quality import (
    SCORE_COMPONENTS,
    SCORE_POLICIES,
    ScorePolicy,
    component_snapshot,
    select_score_policy,
    selected_policy_decisions,
)
from schurfer_analytics.replay import ReplayDecision, ReplayEpisode


def _components(points: tuple[int, ...]) -> dict[str, object]:
    return {
        name: {"value": float(index + 1), "points": score, "max": 2, "note": ""}
        for index, (name, score) in enumerate(zip(SCORE_COMPONENTS, points, strict=True))
    }


def _data_quality(*, oi: bool = True, funding: bool = True) -> dict[str, bool]:
    return {"oi": oi, "funding": funding}


def _decision(
    row_id: int,
    points: tuple[int, ...],
    *,
    quality_allowed: object = True,
    require_market_quality: object = True,
    signal_data_quality: object | None = None,
) -> ReplayDecision:
    ts = datetime(2026, 7, 26, tzinfo=UTC) + timedelta(minutes=row_id)
    return ReplayDecision(
        row_id=row_id,
        decision_id=f"00000000-0000-0000-0000-{row_id:012d}",
        pump_event_id=42,
        event_base="ERA",
        event_first_seen_at=datetime(2026, 7, 26, tzinfo=UTC),
        event_closed_at=datetime(2026, 7, 27, tzinfo=UTC),
        ts=ts,
        base="ERA",
        exchange="binance",
        action="skipped",
        reason="measurement",
        score=sum(points),
        pump_pct=40,
        price=100,
        strategy_version="pump_short_v1_market_quality",
        features={
            "signal": {
                "computed_at": ts.timestamp(),
                "components": _components(points),
                "data_quality": (
                    _data_quality() if signal_data_quality is None else signal_data_quality
                ),
            },
            "config": {
                "require_market_quality": require_market_quality,
                "signal_position_usd": 50,
            },
        },
        liquidity={
            "status": "sampled",
            "quality": {"allowed": quality_allowed, "depth_target_usd": 100},
        },
        outcomes=(),
    )


def _episode(*decisions: ReplayDecision) -> ReplayEpisode:
    return ReplayEpisode(42, "ERA", "base:ERA", decisions, ())


def test_registered_policies_lock_control_thresholds_and_ablations() -> None:
    assert tuple(policy.key for policy in SCORE_POLICIES) == (
        "score_any",
        "score_4",
        "score_5",
        "score_6",
        "score_7",
        "score_8",
        "score_9",
        "score_6_without_pump_age",
        "score_6_without_price_extent",
        "score_6_without_oi_trend",
        "score_6_without_funding_rate",
        "score_6_without_retrace_from_peak",
    )


def test_score_policy_selects_first_quality_eligible_threshold_crossing() -> None:
    quality_failed = _decision(1, (2, 2, 1, 1, 0), quality_allowed=False)
    below = _decision(2, (1, 1, 1, 1, 1))
    selected = _decision(3, (2, 1, 1, 1, 1))

    result = select_score_policy(_episode(quality_failed, below, selected), ScorePolicy("s6", 6))

    assert result.status == "selected"
    assert result.decision is selected
    assert result.effective_score == 6


def test_policy_without_crossing_is_a_cash_candidate() -> None:
    result = select_score_policy(
        _episode(_decision(1, (1, 1, 1, 1, 1))),
        ScorePolicy("nine", 9),
    )

    assert result.status == "not_triggered"
    assert result.decision is None
    assert result.error is None


def test_stricter_policy_is_unresolved_after_recorded_open_censors_future_ticks() -> None:
    opened = replace(_decision(1, (2, 1, 1, 1, 1)), action="opened_dry_run")

    result = select_score_policy(_episode(opened), ScorePolicy("seven", 7))

    assert result.status == "unresolved"
    assert result.error == "right_censored_after_recorded_open"


def test_component_ablation_uses_recorded_points_without_recomputing_features() -> None:
    first = _decision(1, (2, 1, 1, 1, 1))
    second = _decision(2, (2, 2, 2, 1, 1))
    policy = ScorePolicy("without_age", 6, "pump_age")

    result = select_score_policy(_episode(first, second), policy)

    assert result.decision is second
    assert result.effective_score == 6


def test_component_schema_detects_score_mismatch() -> None:
    decision = replace(_decision(1, (1, 1, 1, 1, 1)), score=6)

    components, error = component_snapshot(decision)

    assert components is None
    assert error == "score_component_sum_mismatch"
    result = select_score_policy(
        _episode(decision),
        ScorePolicy("without_age", 6, "pump_age"),
    )
    assert result.status == "unresolved"
    assert result.error == "score_component_sum_mismatch"


def test_component_snapshot_separates_missing_oi_from_real_zero_points() -> None:
    decision = _decision(
        1,
        (1, 1, 0, 1, 1),
        signal_data_quality=_data_quality(oi=False),
    )

    components, error = component_snapshot(decision)

    assert error is None
    assert components is not None
    oi = next(item for item in components if item.name == "oi_trend")
    funding = next(item for item in components if item.name == "funding_rate")
    assert oi.points == 0
    assert oi.data_available is False
    assert funding.data_available is True


@pytest.mark.parametrize(
    ("decision", "error"),
    [
        (replace(_decision(1, (1, 1, 1, 1, 1)), score=None), "invalid_score"),
        (
            _decision(1, (1, 1, 1, 1, 1), require_market_quality="yes"),
            "invalid_require_market_quality",
        ),
        (
            _decision(1, (1, 1, 1, 1, 1), quality_allowed=None),
            "invalid_market_quality_allowed",
        ),
    ],
)
def test_invalid_point_in_time_data_fails_closed(
    decision: ReplayDecision,
    error: str,
) -> None:
    result = select_score_policy(_episode(decision), ScorePolicy("any", 0))

    assert result.status == "unresolved"
    assert result.error == error


def test_market_quality_snapshot_not_required_when_recorded_gate_was_disabled() -> None:
    decision = replace(
        _decision(1, (1, 1, 1, 1, 1), require_market_quality=False),
        liquidity={"status": "fetch_failed"},
    )

    result = select_score_policy(_episode(decision), ScorePolicy("any", 0))

    assert result.status == "selected"


def test_selected_policy_union_deduplicates_shared_decisions() -> None:
    first = _decision(1, (1, 1, 1, 1, 0))
    second = _decision(2, (2, 2, 1, 1, 1))

    selected = selected_policy_decisions(
        (_episode(first, second),),
        (ScorePolicy("any", 0), ScorePolicy("six", 6), ScorePolicy("seven", 7)),
    )

    assert selected == (first, second)
