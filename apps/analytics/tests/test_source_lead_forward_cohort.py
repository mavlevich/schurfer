"""Frozen-contract and pure-evaluator tests for research/gate-source-lead-
forward-cohort-v1.

No tuning after start: the constants here are not supposed to ever change
once real data starts accumulating against them. A frozen-value test
failure means someone edited the contract, which this module's own
docstring says never to do -- these assertions exist to make that change
loud, not to validate business logic.

resolve_episode/formal_verdict are pure (no I/O, no DB, no network) --
exercised here with synthetic inputs, per colleague review (2026-08-30/31):
the resolution contract must be frozen and testable before a single real
qualified capture exists, not designed after the fact against real data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from schurfer_analytics.ohlcv import Candle
from schurfer_analytics.source_lead_contract import IDENTITY_REGISTRY_V3_START
from schurfer_analytics.source_lead_forward_cohort import (
    COSTS,
    ENTRY_DELAY_MINUTES,
    ESTIMAND_VERSION,
    EVIDENCE_FLOOR,
    EXIT_SLIPPAGE_BPS_ASSUMED,
    HYPOTHESIS_ORIGIN,
    MAX_EXIT_BAR_GAP_MINUTES,
    MAX_SINGLE_ASSET_EPISODE_SHARE,
    MAX_SINGLE_WEEK_EPISODE_SHARE,
    OUTCOME_HORIZON_MINUTES,
    QUALIFICATION_STATUS,
    QUALIFICATION_VERSION,
    SOURCE_LEAD_FORWARD_COHORT_START,
    VERDICT_CANDIDATE,
    VERDICT_FAIL,
    VERDICT_INSUFFICIENT_DATA,
    EpisodeInputs,
    formal_verdict,
    resolve_episode,
)
from schurfer_performance import DEFAULT_COSTS


def _bar(close: float) -> Candle:
    return Candle(ts_ms=0, open=close, high=close, low=close, close=close, volume=None)


# --- frozen values --------------------------------------------------------


def test_cohort_start_is_aliased_to_the_registry_cutover_not_copied() -> None:
    assert SOURCE_LEAD_FORWARD_COHORT_START == IDENTITY_REGISTRY_V3_START
    assert datetime(2026, 9, 3, tzinfo=UTC) == SOURCE_LEAD_FORWARD_COHORT_START


def test_candidate_set_matches_the_live_qualification_contract() -> None:
    assert QUALIFICATION_STATUS == "qualified"
    assert QUALIFICATION_VERSION == "source_lead_qualified_capture_v3"


def test_estimand_is_narrower_than_and_linked_to_hyp_012_not_a_replication() -> None:
    """Colleague review: this contract must not claim to replicate HYP-012's
    original paired/Holm-corrected 4-route family -- the registry currently
    verifies only one of those four routes."""
    assert ESTIMAND_VERSION == "standalone_early_entry_net_return_v1"
    assert HYPOTHESIS_ORIGIN == "HYP-012"


def test_entry_and_outcome_are_frozen() -> None:
    assert ENTRY_DELAY_MINUTES == 0
    assert OUTCOME_HORIZON_MINUTES == 30


def test_exit_mechanics_are_frozen_before_any_real_data_exists() -> None:
    assert MAX_EXIT_BAR_GAP_MINUTES == 2.0
    assert EXIT_SLIPPAGE_BPS_ASSUMED == 15.0


def test_costs_reuse_the_shared_conservative_model() -> None:
    assert COSTS is DEFAULT_COSTS


def test_evidence_floor_and_concentration_caps_fit_the_real_candidate_universe() -> None:
    """The usual 30-cluster floor used elsewhere in this codebase is
    unreachable here by construction (14-asset universe) -- registered
    instead as an explicit small-universe floor with concentration caps
    doing the job the 30-cluster floor exists to do (colleague review,
    second round)."""
    assert EVIDENCE_FLOOR == {
        "min_resolved_episodes": 100,
        "min_distinct_asset_clusters": 7,
        "min_distinct_utc_weeks": 4,
    }
    assert MAX_SINGLE_ASSET_EPISODE_SHARE == 0.35
    assert MAX_SINGLE_WEEK_EPISODE_SHARE == 0.45


def test_verdict_states_are_distinct() -> None:
    verdicts = {VERDICT_CANDIDATE, VERDICT_FAIL, VERDICT_INSUFFICIENT_DATA}
    assert len(verdicts) == 3


# --- resolve_episode: pure, synthetic inputs -------------------------------


def test_resolve_episode_missing_exit_bar_is_unresolved() -> None:
    result = resolve_episode(
        EpisodeInputs(base="ABC", entry_price=1.0, exit_bar=None, exit_bar_gap_minutes=None)
    )
    assert result.resolved is False
    assert result.unresolved_reason == "missing_exit_bar"
    assert result.net_return_pct is None


def test_resolve_episode_exit_bar_gap_exceeded_is_unresolved() -> None:
    result = resolve_episode(
        EpisodeInputs(
            base="ABC",
            entry_price=1.0,
            exit_bar=_bar(1.0),
            exit_bar_gap_minutes=MAX_EXIT_BAR_GAP_MINUTES + 0.01,
        )
    )
    assert result.resolved is False
    assert result.unresolved_reason == "exit_bar_gap_exceeded"


def test_resolve_episode_gap_exactly_at_the_boundary_is_resolved() -> None:
    """The boundary itself is inclusive -- only strictly exceeding it fails closed."""
    result = resolve_episode(
        EpisodeInputs(
            base="ABC",
            entry_price=1.0,
            exit_bar=_bar(1.0),
            exit_bar_gap_minutes=MAX_EXIT_BAR_GAP_MINUTES,
        )
    )
    assert result.resolved is True


def test_resolve_episode_applies_the_conservative_exit_slippage_haircut() -> None:
    """A flat close (no price move at all) must still net negative -- the
    slippage haircut plus round-trip fees are real costs, not zero."""
    result = resolve_episode(
        EpisodeInputs(base="ABC", entry_price=1.0, exit_bar=_bar(1.0), exit_bar_gap_minutes=0.0)
    )
    assert result.resolved is True
    assert result.net_return_pct is not None
    assert result.net_return_pct < 0


def test_resolve_episode_charges_round_trip_taker_fees() -> None:
    """A price move exactly equal to the round-trip fee (before slippage)
    must still net negative once the slippage haircut is added on top."""
    fee_pct = COSTS.taker_fee_bps_per_side * 2 / 100
    entry_price = 1.0
    exit_close = entry_price * (1 + fee_pct / 100)
    result = resolve_episode(
        EpisodeInputs(
            base="ABC", entry_price=entry_price, exit_bar=_bar(exit_close), exit_bar_gap_minutes=0.0
        )
    )
    assert result.net_return_pct is not None
    assert result.net_return_pct < 0


def test_resolve_episode_a_large_favorable_move_still_nets_positive_after_costs() -> None:
    result = resolve_episode(
        EpisodeInputs(base="ABC", entry_price=1.0, exit_bar=_bar(1.05), exit_bar_gap_minutes=0.0)
    )
    assert result.resolved is True
    assert result.net_return_pct is not None
    assert result.net_return_pct > 0


# --- formal_verdict: pure, synthetic aggregates ----------------------------


def _aggregate(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "resolved_episodes": EVIDENCE_FLOOR["min_resolved_episodes"],
        "distinct_asset_clusters": EVIDENCE_FLOOR["min_distinct_asset_clusters"],
        "distinct_utc_weeks": EVIDENCE_FLOOR["min_distinct_utc_weeks"],
        "max_single_asset_share": 0.1,
        "max_single_week_share": 0.1,
        "ci_lower_bound_pct": 0.05,
    }
    base.update(overrides)
    return base


def test_formal_verdict_insufficient_data_below_episode_floor() -> None:
    assert (
        formal_verdict(**_aggregate(resolved_episodes=EVIDENCE_FLOOR["min_resolved_episodes"] - 1))
        == VERDICT_INSUFFICIENT_DATA
    )


def test_formal_verdict_insufficient_data_below_cluster_floor() -> None:
    assert (
        formal_verdict(
            **_aggregate(distinct_asset_clusters=EVIDENCE_FLOOR["min_distinct_asset_clusters"] - 1)
        )
        == VERDICT_INSUFFICIENT_DATA
    )


def test_formal_verdict_insufficient_data_below_week_floor() -> None:
    weeks = EVIDENCE_FLOOR["min_distinct_utc_weeks"] - 1
    assert formal_verdict(**_aggregate(distinct_utc_weeks=weeks)) == VERDICT_INSUFFICIENT_DATA


def test_formal_verdict_insufficient_data_when_one_asset_dominates() -> None:
    """The concentration cap this codebase's usual 30-cluster floor exists
    to enforce elsewhere -- explicit here since the cluster floor alone
    (7) cannot (colleague review: seven distinct assets can still mean one
    asset contributes the overwhelming majority of episodes)."""
    assert (
        formal_verdict(**_aggregate(max_single_asset_share=MAX_SINGLE_ASSET_EPISODE_SHARE + 0.01))
        == VERDICT_INSUFFICIENT_DATA
    )


def test_formal_verdict_insufficient_data_when_one_week_dominates() -> None:
    assert (
        formal_verdict(**_aggregate(max_single_week_share=MAX_SINGLE_WEEK_EPISODE_SHARE + 0.01))
        == VERDICT_INSUFFICIENT_DATA
    )


def test_formal_verdict_insufficient_data_when_ci_could_not_be_computed() -> None:
    assert formal_verdict(**_aggregate(ci_lower_bound_pct=None)) == VERDICT_INSUFFICIENT_DATA


def test_formal_verdict_fail_when_floor_met_and_ci_not_positive() -> None:
    assert formal_verdict(**_aggregate(ci_lower_bound_pct=0.0)) == VERDICT_FAIL
    assert formal_verdict(**_aggregate(ci_lower_bound_pct=-0.5)) == VERDICT_FAIL


def test_formal_verdict_candidate_when_floor_met_and_ci_strictly_positive() -> None:
    assert formal_verdict(**_aggregate(ci_lower_bound_pct=0.01)) == VERDICT_CANDIDATE
