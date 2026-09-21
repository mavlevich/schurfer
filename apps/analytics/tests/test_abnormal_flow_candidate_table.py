"""Tests for the candidate-table power-floor selection (pure)."""

from __future__ import annotations

from schurfer_analytics.abnormal_flow_candidate_table import select_oi_percentile


def _rows() -> list[dict[str, object]]:
    # Higher OI percentile -> fewer, more-selective episodes.
    return [
        {"oi_percentile": 0.90, "projected_evaluation_episodes": 900.0},
        {"oi_percentile": 0.95, "projected_evaluation_episodes": 300.0},
        {"oi_percentile": 0.975, "projected_evaluation_episodes": 160.0},
        {"oi_percentile": 0.99, "projected_evaluation_episodes": 80.0},
    ]


def test_selects_highest_percentile_clearing_the_power_floor() -> None:
    # P99 (80) fails 150; P97.5 (160) clears -> pick the most selective that still clears.
    assert select_oi_percentile(_rows(), 150.0) == 0.975


def test_stricter_floor_pushes_selection_lower() -> None:
    # Floor 350: only P90 (900) and... P95 is 300 < 350, so P90.
    assert select_oi_percentile(_rows(), 350.0) == 0.90


def test_returns_none_when_nothing_clears() -> None:
    assert select_oi_percentile(_rows(), 10_000.0) is None
