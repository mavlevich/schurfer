from __future__ import annotations

import pytest
from schurfer_analytics.clustered_inference import (
    ClusterObservation,
    cluster_bootstrap_mean,
    cluster_bootstrap_mean_null_p_value,
    confidence_interval,
    derived_seed,
    holm_step_down,
    leave_one_cluster_out_means,
)


def test_cluster_bootstrap_resamples_whole_clusters_deterministically() -> None:
    observations = (
        ClusterObservation("A", 100),
        ClusterObservation("A", -100),
        ClusterObservation("B", 1),
    )

    first = cluster_bootstrap_mean(observations, iterations=500, seed=7)
    second = cluster_bootstrap_mean(observations, iterations=500, seed=7)

    assert first == second
    assert first.estimate.point_estimate == pytest.approx(1 / 3)
    assert {round(value, 6) for value in first.samples} <= {
        0.0,
        round(1 / 3, 6),
        1.0,
    }


def test_confidence_interval_uses_interpolated_two_sided_percentiles() -> None:
    lower, upper = confidence_interval(
        (0.0, 10.0, 20.0, 30.0, 40.0),
        confidence_level=0.8,
    )

    assert lower == pytest.approx(4)
    assert upper == pytest.approx(36)


def test_holm_step_down_stops_after_first_non_rejection() -> None:
    decisions = {row.key: row for row in holm_step_down({"a": 0.01, "b": 0.03, "c": 0.04})}

    assert decisions["a"].rank == 1
    assert decisions["a"].critical_alpha == pytest.approx(0.05 / 3)
    assert decisions["a"].adjusted_p_value == pytest.approx(0.03)
    assert decisions["a"].rejected is True
    assert decisions["b"].adjusted_p_value == pytest.approx(0.06)
    assert decisions["b"].rejected is False
    assert decisions["c"].rejected is False


def test_null_bootstrap_centers_the_distribution_before_testing() -> None:
    observations = tuple(ClusterObservation(f"C{index}", 2 + index / 10) for index in range(30))

    p_value = cluster_bootstrap_mean_null_p_value(
        observations,
        iterations=500,
        seed=9,
    )

    assert p_value < 0.05


def test_leave_one_cluster_out_keeps_all_other_episodes() -> None:
    observations = (
        ClusterObservation("A", 1),
        ClusterObservation("A", 3),
        ClusterObservation("B", 10),
        ClusterObservation("C", 20),
    )

    result = dict(leave_one_cluster_out_means(observations, ("A", "B")))

    assert result["A"] == pytest.approx(15)
    assert result["B"] == pytest.approx(8)


def test_seed_derivation_is_stable_and_label_specific() -> None:
    assert derived_seed(123, "baseline") == derived_seed(123, "baseline")
    assert derived_seed(123, "baseline") != derived_seed(123, "challenger")


def test_empty_bootstrap_input_fails_closed() -> None:
    with pytest.raises(ValueError):
        cluster_bootstrap_mean((), iterations=500)


def test_non_finite_observation_fails_closed() -> None:
    with pytest.raises(ValueError):
        ClusterObservation("A", float("nan"))
