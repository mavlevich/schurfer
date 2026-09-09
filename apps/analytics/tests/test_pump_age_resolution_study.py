"""Coverage for HYP-027, the surviving claim from HYP-023's withdrawal.

The tests that matter are the ones the withdrawal implies: that the cutoffs are
absolute and never derived from the data in front of them, that the groups
compared are ones production scores identically, that the floors bind on the
thinner side rather than the pool, and that the direction graded is the one
declared in advance.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.pump_age_resolution_study import (
    GROUP_A,
    GROUP_B,
    assign_group,
    run_study,
    select_one_per_episode,
)
from schurfer_analytics.research_contract import ResearchContract
from schurfer_analytics.score_component_study import horizon_cost_pct, within_window

_SINCE = datetime(2026, 8, 25, tzinfo=UTC)
_UNTIL = datetime(2026, 9, 30, tzinfo=UTC)


def _contract(**over: object) -> ResearchContract:
    defaults: dict[str, object] = {
        "hypothesis_id": "HYP-027",
        "contract_version": "v1",
        "window_since": _SINCE,
        "window_until": _UNTIL,
        "strategy_versions": ("pump_short_v1_market_quality",),
        "allow_fallback": False,
        "outcome_horizon_minutes": 60,
        "metric": "median",
        "comparison": "difference_of_metric",
        "baseline_policy": GROUP_B,
        "challenger_policies": (GROUP_A,),
        "cost_model_version": "conservative_costs_v1",
        "minimum_completed_trades": 3,
        "minimum_clusters": 2,
        "candidate_margin": 1.0,
        "rejection_margin": -1.0,
    }
    defaults.update(over)
    return ResearchContract(**defaults)  # type: ignore[arg-type]


def _row(
    episode: int,
    *,
    age_minutes: float,
    ret: float,
    minutes: int = 0,
    action: str = "skipped",
    base: str | None = None,
) -> dict[str, object]:
    return {
        "pump_event_id": episode,
        "base": base or f"TOK{episode}",
        "ts": _SINCE + timedelta(minutes=minutes),
        "action": action,
        "components": {
            "pump_age": {"value": age_minutes / 60, "points": 0, "max": 2, "note": ""},
        },
        "short_return_pct": ret,
        "mfe_pct": 1.0,
        "mae_pct": -1.0,
    }


# --- the cutoffs ------------------------------------------------------------


def test_the_group_boundary_is_absolute_and_inclusive_below() -> None:
    """Not a quantile. The withdrawn HYP-023 result came from boundaries derived
    from the data in front of them, which put a cut through 368 identical
    values; these are fixed by the contract and shared by every window."""
    assert assign_group(0.0) == GROUP_A
    assert assign_group(0.6) == GROUP_A
    assert assign_group(0.61) == GROUP_B
    assert assign_group(60.0) == GROUP_B


def test_an_episode_production_actually_scores_is_excluded_not_reported() -> None:
    """Above an hour production awards 1 point, above four hours 2. Those are the
    episodes the score does distinguish, and they are a different question with
    its own window. Reporting them descriptively here would spend their data
    before that contract exists."""
    assert assign_group(60.01) is None
    assert assign_group(600.0) is None


def test_a_negative_age_is_out_of_scope_rather_than_youngest() -> None:
    assert assign_group(-1.0) is None


def test_both_compared_groups_score_zero_in_production() -> None:
    """The premise of the whole hypothesis, asserted so a later edit to the
    cutoffs cannot quietly break it: production's first threshold is one hour,
    so every episode in either group contributes the same zero points."""
    production_zero_point_ceiling_minutes = 60.0
    for age in (0.0, 0.6, 0.61, 30.0, 59.9):
        assert assign_group(age) is not None
        assert age <= production_zero_point_ceiling_minutes


# --- the comparison ---------------------------------------------------------


def _cohort(*, young_return: float, older_return: float, count: int = 8) -> list[dict[str, object]]:
    rows = [
        _row(index, age_minutes=0.6, ret=young_return, base=f"YOUNG{index}")
        for index in range(1, count + 1)
    ]
    rows += [
        _row(count + index, age_minutes=5.0, ret=older_return, base=f"OLD{index}")
        for index in range(1, count + 1)
    ]
    return rows


def test_a_difference_in_the_declared_direction_confirms() -> None:
    result = run_study(
        select_one_per_episode(_cohort(young_return=3.0, older_return=0.0)), _contract()
    )
    assert result.difference_pct == pytest.approx(3.0)
    assert result.verdict == "candidate"


def test_a_difference_the_other_way_refutes_rather_than_being_reported_as_signal() -> None:
    """The direction was taken from discovery, so this test is one-sided. A large
    difference the wrong way is a refutation and must not come back as a finding
    with a sign attached."""
    result = run_study(
        select_one_per_episode(_cohort(young_return=0.0, older_return=3.0)), _contract()
    )
    assert result.difference_pct == pytest.approx(-3.0)
    assert result.verdict == "rejected"


def test_a_small_difference_is_inconclusive() -> None:
    result = run_study(
        select_one_per_episode(_cohort(young_return=1.0, older_return=0.7)), _contract()
    )
    assert result.verdict == "inconclusive"


def test_the_cost_model_is_applied_to_both_sides() -> None:
    result = run_study(
        select_one_per_episode(_cohort(young_return=2.0, older_return=1.0)), _contract()
    )
    assert result.groups[GROUP_A].median_net_pct == pytest.approx(2.0 - horizon_cost_pct(60))
    # A difference of medians cancels the cost, which is why the cost model
    # cannot flatter this comparison in either direction.
    assert result.difference_pct == pytest.approx(1.0)


# --- floors bind on the thinner side ----------------------------------------


def test_the_episode_floor_binds_on_the_thinner_group_not_the_pool() -> None:
    """The defect this forestalls: 254 young episodes and 119 older ones pool to
    373, which clears a 150 floor while the comparison rests on 119. A comparison
    is only as well evidenced as its thinner side."""
    rows = [_row(index, age_minutes=0.6, ret=3.0, base=f"YOUNG{index}") for index in range(1, 200)]
    rows += [
        _row(500 + index, age_minutes=5.0, ret=0.0, base=f"OLD{index}") for index in range(1, 40)
    ]
    result = run_study(
        select_one_per_episode(rows), _contract(minimum_completed_trades=150, minimum_clusters=30)
    )
    assert result.verdict == "inconclusive"
    assert "39 in the thinner group" in result.detail


def test_the_cluster_floor_binds_the_same_way() -> None:
    """Two hundred episodes of one token that pumped repeatedly is one
    observation about that token."""
    rows = [_row(index, age_minutes=0.6, ret=3.0, base="SAME") for index in range(1, 60)]
    rows += [
        _row(500 + index, age_minutes=5.0, ret=0.0, base=f"OLD{index}") for index in range(1, 60)
    ]
    result = run_study(
        select_one_per_episode(rows), _contract(minimum_completed_trades=10, minimum_clusters=30)
    )
    assert result.verdict == "inconclusive"
    assert "cluster floor" in result.detail


def test_an_empty_group_is_inconclusive_rather_than_a_one_sided_claim() -> None:
    rows = [_row(index, age_minutes=0.6, ret=3.0) for index in range(1, 10)]
    result = run_study(select_one_per_episode(rows), _contract())
    assert result.difference_pct is None
    assert result.verdict == "inconclusive"


# --- the shared family rules still apply ------------------------------------


def test_one_observation_per_episode() -> None:
    rows = [_row(1, age_minutes=0.6, ret=1.0, minutes=index) for index in range(200)]
    assert len(select_one_per_episode(rows)) == 1


def test_an_opened_decision_represents_its_episode() -> None:
    rows = [
        _row(1, age_minutes=0.6, ret=1.0, minutes=0, action="skipped"),
        _row(1, age_minutes=9.0, ret=2.0, minutes=5, action="opened"),
    ]
    selected = select_one_per_episode(rows)
    assert len(selected) == 1
    assert selected[0].age_minutes == pytest.approx(9.0)


def test_an_outcome_reaching_past_the_window_is_dropped() -> None:
    inside = int((_UNTIL - _SINCE).total_seconds() / 60) - 61
    straddling = int((_UNTIL - _SINCE).total_seconds() / 60) - 40
    observations = select_one_per_episode(
        [
            _row(1, age_minutes=0.6, ret=1.0, minutes=inside),
            _row(2, age_minutes=0.6, ret=1.0, minutes=straddling),
        ]
    )
    kept = within_window(observations, _contract())
    assert {item.pump_event_id for item in kept} == {1}


def test_an_episode_with_no_recorded_age_is_dropped_rather_than_called_young() -> None:
    """Absent and zero are different things, and defaulting a missing age to zero
    would put every unrecorded episode in the group under test."""
    rows = [_row(1, age_minutes=0.6, ret=1.0)]
    rows.append(
        {
            "pump_event_id": 2,
            "base": "TOK2",
            "ts": _SINCE,
            "action": "skipped",
            "components": {"pump_age": {"points": 0, "max": 2, "note": "no value"}},
            "short_return_pct": 1.0,
            "mfe_pct": None,
            "mae_pct": None,
        }
    )
    assert {item.pump_event_id for item in select_one_per_episode(rows)} == {1}
