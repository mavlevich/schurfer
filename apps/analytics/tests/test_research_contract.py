"""Coverage for the executable research contract.

Every test here is a mistake that actually happened on 2026-09-08 and was found
by a human reading two documents against each other rather than by anything
failing. The point of the module under test is that these stop being things
someone has to notice.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from schurfer_analytics.research_contract import (
    ContractViolationError,
    ResearchContract,
    compare,
    crosses_window_boundary,
    freeze_or_verify_sample,
    load_contract,
    validate_configuration,
    verdict,
)

if TYPE_CHECKING:
    from pathlib import Path

_SINCE = datetime(2026, 7, 29, tzinfo=UTC)
_UNTIL = datetime(2026, 8, 25, tzinfo=UTC)


def _contract(**over: object) -> ResearchContract:
    defaults: dict[str, object] = {
        "hypothesis_id": "HYP-022",
        "contract_version": "v1",
        "window_since": _SINCE,
        "window_until": _UNTIL,
        "strategy_versions": ("pump_short_v1_market_quality",),
        "allow_fallback": False,
        "outcome_horizon_minutes": 60,
        "metric": "median",
        "comparison": "difference_of_metric",
        "baseline_policy": "baseline",
        "challenger_policies": ("scaled_p25",),
        "cost_model_version": "conservative_costs_v1",
        "minimum_completed_trades": 200,
        "minimum_clusters": 30,
        "candidate_margin": 1.0,
        "rejection_margin": -1.0,
    }
    defaults.update(over)
    return ResearchContract(**defaults)  # type: ignore[arg-type]


def _valid_configuration(contract: ResearchContract) -> dict[str, object]:
    return {
        "since": contract.window_since,
        "until": contract.window_until,
        "metric": contract.metric,
        "comparison": contract.comparison,
        "baseline_policy": contract.baseline_policy,
        "challenger_policies": contract.challenger_policies,
        "cost_model_version": contract.cost_model_version,
        "strategy_versions": contract.strategy_versions,
        "allow_fallback": contract.allow_fallback,
    }


# --- the window ------------------------------------------------------------


def test_a_run_past_the_registered_window_is_refused() -> None:
    """What actually happened: the report's --until defaults to the run's own
    start time, so the run covered 2026-07-29 to 2026-09-08 while the contract
    said 2026-07-29 to 2026-08-25. Nothing objected."""
    contract = _contract()
    configuration = _valid_configuration(contract)
    configuration["until"] = datetime(2026, 9, 8, 14, 20, tzinfo=UTC)
    with pytest.raises(ContractViolationError, match="window ends"):
        validate_configuration(contract, **configuration)  # type: ignore[arg-type]


def test_the_window_check_names_both_bounds() -> None:
    contract = _contract()
    configuration = _valid_configuration(contract)
    configuration["since"] = datetime(2026, 7, 1, tzinfo=UTC)
    with pytest.raises(ContractViolationError, match="window starts"):
        validate_configuration(contract, **configuration)  # type: ignore[arg-type]


# --- the metric ------------------------------------------------------------


def test_swapping_mean_for_median_is_refused() -> None:
    """The other half of what happened: the contract's metric was the median and
    the result was reported in means, compared against a margin written for
    medians."""
    contract = _contract()
    configuration = _valid_configuration(contract)
    configuration["metric"] = "mean"
    with pytest.raises(ContractViolationError, match="metric is 'mean'"):
        validate_configuration(contract, **configuration)  # type: ignore[arg-type]


def test_difference_of_metric_is_not_metric_of_differences() -> None:
    """These are different numbers, and the contract has to say which. On this
    sample the difference of medians is +1.0 while the median of the paired
    differences is 0.0 -- the same disagreement the real run produced."""
    # Paired element-wise. Only the middle episode moves, which shifts the
    # median level by a point while leaving the median per-episode difference at
    # zero -- the shape the real run produced: 237 worse, 61 unchanged, 258
    # better, with the middle of that distribution sitting in the zero block.
    baseline = [-5.0, -1.0, 0.0, 1.0, 5.0]
    challenger = [-5.0, -1.0, 2.0, 1.0, 5.0]
    assert compare(_contract(comparison="difference_of_metric"), baseline, challenger) == 1.0
    assert compare(_contract(comparison="metric_of_differences"), baseline, challenger) == 0.0


def test_paired_comparison_refuses_unequal_samples() -> None:
    with pytest.raises(ContractViolationError, match="paired samples"):
        compare(_contract(comparison="metric_of_differences"), [1.0, 2.0], [1.0])


# --- the verdict -----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "trades", "clusters", "formal", "expected"),
    [
        (1.5, 200, 30, True, "candidate"),
        (1.0, 200, 30, True, "candidate"),
        (0.99, 200, 30, True, "inconclusive"),
        (-1.0, 200, 30, True, "rejected"),
        # The floors are checked before the margins: a striking number on a thin
        # sample must not become a candidate.
        (5.0, 199, 30, True, "inconclusive"),
        (5.0, 200, 29, True, "inconclusive"),
        # And a withheld formal interval overrides a passing margin, which is
        # exactly the condition HYP-021 and HYP-022 both landed on.
        (5.0, 200, 30, False, "inconclusive"),
        (None, 200, 30, True, "inconclusive"),
    ],
)
def test_verdict_follows_the_contract(
    value: float | None, trades: int, clusters: int, formal: bool, expected: str
) -> None:
    assert (
        verdict(
            _contract(),
            value=value,
            completed_trades=trades,
            clusters=clusters,
            formal_inference_available=formal,
        )
        == expected
    )


# --- episodes that straddle the boundary ------------------------------------


def test_a_decision_whose_outcome_leaves_the_window_is_flagged() -> None:
    """A decision twenty minutes before the window closes has a 60-minute
    outcome measured on data outside it. In a discovery/holdout split those
    minutes belong to the other side, so the two windows silently overlap."""
    contract = _contract()
    inside = _UNTIL - timedelta(minutes=61)
    straddling = _UNTIL - timedelta(minutes=20)
    assert not crosses_window_boundary(contract, inside)
    assert crosses_window_boundary(contract, straddling)


def test_the_boundary_rule_follows_the_registered_horizon() -> None:
    at_240 = _contract(outcome_horizon_minutes=240)
    decision = _UNTIL - timedelta(minutes=120)
    assert not crosses_window_boundary(_contract(), decision)
    assert crosses_window_boundary(at_240, decision)


# --- the contract's own identity --------------------------------------------


def test_a_contract_edited_after_the_fact_is_refused(tmp_path: Path) -> None:
    """The checksum is not about an adversary. It is about a registration edited
    once a result is known, which otherwise leaves no trace at all."""
    path = tmp_path / "contract.json"
    path.write_text(_contract().to_json())
    payload = json.loads(path.read_text())
    payload["candidate_margin"] = 0.5
    path.write_text(json.dumps(payload))
    with pytest.raises(ContractViolationError, match="checksum"):
        load_contract(path)


def test_an_unedited_contract_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "contract.json"
    original = _contract()
    path.write_text(original.to_json())
    assert load_contract(path) == original


# --- the sample -------------------------------------------------------------


def test_the_first_run_freezes_the_sample_and_a_rerun_must_match(tmp_path: Path) -> None:
    """Without this, "read once" is a sentence a report prints about itself: a
    second run picks up whatever accumulated since and says it again."""
    contract = _contract()
    path = tmp_path / "sample.json"
    first = freeze_or_verify_sample(contract, [3, 1, 2], path)
    assert first.episode_ids == (1, 2, 3)
    # Order does not matter; membership does.
    freeze_or_verify_sample(contract, [2, 3, 1], path)
    with pytest.raises(ContractViolationError, match="different sample"):
        freeze_or_verify_sample(contract, [1, 2, 3, 4], path)


def test_a_rerun_after_the_contract_changed_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "sample.json"
    freeze_or_verify_sample(_contract(), [1, 2, 3], path)
    with pytest.raises(ContractViolationError, match="contract changed"):
        freeze_or_verify_sample(_contract(candidate_margin=0.5), [1, 2, 3], path)


def test_a_sample_from_another_hypothesis_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "sample.json"
    freeze_or_verify_sample(_contract(), [1, 2, 3], path)
    with pytest.raises(ContractViolationError, match="belongs to"):
        freeze_or_verify_sample(_contract(hypothesis_id="HYP-999"), [1, 2, 3], path)


# --- contracts that cannot mean anything ------------------------------------


def test_an_empty_or_reversed_window_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="non-empty and ordered"):
        _contract(window_until=_SINCE)


def test_a_naive_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _contract(window_since=datetime(2026, 7, 29))


def test_margins_that_overlap_are_rejected() -> None:
    with pytest.raises(ValueError, match="candidate margin must exceed"):
        _contract(candidate_margin=0.0, rejection_margin=0.0)


def test_the_baseline_cannot_also_be_a_challenger() -> None:
    with pytest.raises(ValueError, match="cannot also be a challenger"):
        _contract(challenger_policies=("baseline", "scaled_p25"))
