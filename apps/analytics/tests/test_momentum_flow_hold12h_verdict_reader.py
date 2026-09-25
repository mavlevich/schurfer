"""Formal-read guards of the hold12h verdict reader (no database needed)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from schurfer_analytics.momentum_flow_hold12h_verdict import Hold12hVerdictContract
from schurfer_analytics.momentum_flow_hold12h_verdict_reader import (
    HealthCheckpoint,
    claim_formal_output,
    formal_read_window,
    health_breaches,
    portfolio_summary,
)
from schurfer_analytics.momentum_flow_hold12h_verdict_report import PortfolioResult

if TYPE_CHECKING:
    from pathlib import Path

_START = datetime(2026, 10, 5, tzinfo=UTC)
_END = datetime(2026, 11, 2, tzinfo=UTC)
_FROZEN = dataclasses.replace(
    Hold12hVerdictContract(),
    cohort_start_iso=_START.isoformat(),
    decision_prefix_end_iso=_END.isoformat(),
)
_OPEN = _END + timedelta(hours=_FROZEN.min_read_delay_hours)


def test_formal_read_returns_the_frozen_bounds_once_open() -> None:
    assert formal_read_window(_FROZEN, registered=True, requested_prefix_end=_END, now=_OPEN) == (
        _START,
        _END,
    )


def test_formal_read_refuses_an_unregistered_contract() -> None:
    with pytest.raises(SystemExit, match="not registered"):
        formal_read_window(_FROZEN, registered=False, requested_prefix_end=_END, now=_OPEN)


def test_formal_read_refuses_missing_bounds() -> None:
    with pytest.raises(SystemExit, match="not frozen"):
        formal_read_window(
            Hold12hVerdictContract(), registered=True, requested_prefix_end=_END, now=_OPEN
        )


def test_formal_read_refuses_any_other_prefix() -> None:
    with pytest.raises(SystemExit, match="not the frozen"):
        formal_read_window(
            _FROZEN,
            registered=True,
            requested_prefix_end=_END + timedelta(days=7),
            now=_OPEN + timedelta(days=7),
        )


def test_formal_read_refuses_before_positions_and_funding_can_be_complete() -> None:
    with pytest.raises(SystemExit, match="too early"):
        formal_read_window(
            _FROZEN, registered=True, requested_prefix_end=_END, now=_OPEN - timedelta(seconds=1)
        )


def test_the_output_claim_allows_exactly_one_formal_read(tmp_path: Path) -> None:
    target = tmp_path / "formal" / "hold12h"
    claim_formal_output(target)
    assert target.is_dir()
    with pytest.raises(SystemExit, match="already claimed"):
        claim_formal_output(target)


def test_portfolio_summary_reports_capital_time() -> None:
    result = PortfolioResult(
        window_pnl_usd=None,
        adverse_from_entry_usd=3.0,
        longest_losing_streak=2,
        taken=40,
        skipped_slots_full=300,
        complete=False,
        incomplete_taken=1,
        slot_hours=336.0,
        window_pnl_zero_funding_sensitivity_usd=12.5,
    )
    summary = portfolio_summary(result, max_slots=6, window_hours=672.0)
    assert summary["occupancy_fraction"] == pytest.approx(336.0 / (6 * 672.0))
    assert summary["skipped_slots_full"] == 300
    assert summary["incomplete_taken_fraction"] == pytest.approx(1 / 40)
    assert summary["window_pnl_usd"] is None
    assert summary["window_pnl_zero_funding_sensitivity_usd"] == 12.5


def _checkpoint(
    eligible: int, baseline_lost: int, hold_lost: int, *, hold_unclaimed: int = 0
) -> HealthCheckpoint:
    return HealthCheckpoint(
        since=_START,
        until=_START + timedelta(hours=48),
        eligible_watches=eligible,
        baseline_unclaimed=0,
        baseline_stale=baseline_lost,
        hold12h_unclaimed=hold_unclaimed,
        hold12h_stale=hold_lost - hold_unclaimed,
        hold12h_claim_p50_seconds=12.0,
        hold12h_claim_p90_seconds=20.0,
        closed_positions_past_lag=100,
        funding_covered=99,
        accounting_complete=100,
    )


def test_health_rule_allows_two_points_of_excess_within_five_percent() -> None:
    assert health_breaches(_checkpoint(1000, 10, 30)) == []  # 1% vs 3%


def test_health_rule_flags_excess_over_baseline() -> None:
    (breach,) = health_breaches(_checkpoint(1000, 0, 25))  # 0% vs 2.5%
    assert "exceed baseline" in breach


def test_health_rule_flags_the_absolute_cap_even_when_baseline_is_also_high() -> None:
    (breach,) = health_breaches(_checkpoint(1000, 50, 60))  # 5% vs 6%
    assert "lost-entry fraction" in breach


def test_health_rule_counts_never_claimed_watches_as_lost() -> None:
    """A stopped hold12h worker leaves WATCH rows unclaimed; they must breach the rule."""
    assert len(health_breaches(_checkpoint(1000, 0, 400, hold_unclaimed=400))) == 2


def test_health_rule_without_eligible_watches_is_a_breach() -> None:
    assert health_breaches(_checkpoint(0, 0, 0)) == ["no eligible WATCH rows in the window"]
