from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.decision_quality import (
    SCORE_COMPONENTS,
    SCORE_POLICIES,
    ScorePolicy,
    banded_price_extent_points,
    component_snapshot,
    select_score_policy,
    selected_policy_decisions,
)
from schurfer_analytics.replay import ReplayDecision, ReplayEpisode


def _components(
    points: tuple[int, ...],
    *,
    price_extent_value: float | None = None,
) -> dict[str, object]:
    values = {name: float(index + 1) for index, name in enumerate(SCORE_COMPONENTS)}
    if price_extent_value is not None:
        values["price_extent"] = price_extent_value
    return {
        name: {"value": values[name], "points": score, "max": 2, "note": ""}
        for name, score in zip(SCORE_COMPONENTS, points, strict=True)
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
    price_extent_value: float | None = None,
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
                "components": _components(points, price_extent_value=price_extent_value),
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
        "score_6_with_banded_price_extent",
    )


@pytest.mark.parametrize(
    ("peak_pct", "expected_points"),
    [
        (10.0, 0),
        (15.0, 1),
        (24.9, 1),
        (25.0, 2),
        (39.9, 2),
        (40.0, 1),
        (59.9, 1),
        (60.0, 0),
        (200.0, 0),
    ],
)
def test_banded_price_extent_points_rewards_the_mid_range(
    peak_pct: float,
    expected_points: int,
) -> None:
    assert banded_price_extent_points(peak_pct) == expected_points


def test_banded_price_extent_policy_recomputes_effective_score_from_raw_value() -> None:
    """Registered hypothesis (2026-08-05): a memecoin that's already up 150% should
    score WORSE on this alternate component than one that's up 30%, even though the
    live price_extent component currently scores it higher."""
    huge_pump = _decision(1, (1, 2, 1, 1, 1), price_extent_value=150.0)
    sweet_spot_pump = _decision(2, (1, 1, 1, 1, 1), price_extent_value=30.0)
    policy = ScorePolicy("score_6_with_banded_price_extent", 6, use_banded_price_extent=True)

    huge_result = select_score_policy(_episode(huge_pump), policy)
    sweet_spot_result = select_score_policy(_episode(sweet_spot_pump), policy)

    # Live score_extent already counted 2 pts for the 150% pump; banding drops it
    # to 0, so 1+2+1+1+1=6 becomes 1+0+1+1+1=4 — below the threshold.
    assert huge_result.status == "not_triggered"
    # The 30% pump's live price_extent was only 1 pt; banding raises it to 2, so
    # 1+1+1+1+1=5 becomes 1+2+1+1+1=6 — now crosses the threshold.
    assert sweet_spot_result.status == "selected"
    assert sweet_spot_result.effective_score == 6


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
