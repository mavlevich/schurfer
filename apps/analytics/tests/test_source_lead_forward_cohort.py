"""Frozen-contract and pure-evaluator tests for research/gate-source-lead-
forward-cohort-v1.

No tuning after start: the constants here are not supposed to ever change
once real data starts accumulating against them. A frozen-value test
failure means someone edited the contract, which this module's own
docstring says never to do -- these assertions exist to make that change
loud, not to validate business logic.

resolve_episode/formal_verdict/primary_sensitivity_ci are pure (no I/O, no
DB, no network) -- exercised here with synthetic inputs, per colleague
review: the resolution contract must be frozen and testable before a
single real qualified capture exists, not designed after the fact against
real data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from schurfer_analytics.clustered_inference import ClusterObservation
from schurfer_analytics.ohlcv import Candle
from schurfer_analytics.source_lead_contract import IDENTITY_REGISTRY_V3_START
from schurfer_analytics.source_lead_forward_cohort import (
    BOOTSTRAP_ITERATIONS,
    BOOTSTRAP_SEED,
    CONFIDENCE_LEVEL,
    COSTS,
    ENTRY_DELAY_MINUTES,
    ESTIMAND_VERSION,
    EVIDENCE_FLOOR,
    EXIT_BAR_TIMEFRAME_MS,
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
    episode_is_matured,
    expected_exit_boundary_ms,
    find_earliest_checkpoint_prefix_length,
    formal_verdict,
    primary_sensitivity_ci,
    resolve_episode,
    resolve_episode_at_exit_slippage,
)
from schurfer_performance import DEFAULT_COSTS, calculate_performance

_ENTRY_AT = datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)
_ENTRY_NOTIONAL = 50.0

# entry_at + 30m, ceil-aligned to a 1-minute boundary -- entry is already
# minute-aligned here, so the boundary is exactly entry + 30m.
_EXPECTED_BOUNDARY_MS = int(_ENTRY_AT.timestamp() * 1000) + OUTCOME_HORIZON_MINUTES * 60_000


def _bar(close: float, *, ts_ms: int = _EXPECTED_BOUNDARY_MS) -> Candle:
    return Candle(ts_ms=ts_ms, open=close, high=close, low=close, close=close, volume=None)


def _inputs(*, exit_bar: Candle | None, entry_price: float = 1.0) -> EpisodeInputs:
    return EpisodeInputs(
        base="ABC",
        entry_at=_ENTRY_AT,
        entry_price=entry_price,
        entry_notional_usd=_ENTRY_NOTIONAL,
        exit_bar=exit_bar,
    )


# --- frozen values --------------------------------------------------------


def test_cohort_start_is_aliased_to_the_registry_cutover_not_copied() -> None:
    assert SOURCE_LEAD_FORWARD_COHORT_START == IDENTITY_REGISTRY_V3_START
    assert datetime(2026, 9, 3, tzinfo=UTC) == SOURCE_LEAD_FORWARD_COHORT_START


def test_candidate_set_matches_the_live_qualification_contract() -> None:
    assert QUALIFICATION_STATUS == "qualified"
    assert QUALIFICATION_VERSION == "source_lead_qualified_capture_v3"


def test_estimand_is_narrower_than_and_linked_to_hyp_012_not_a_replication() -> None:
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


def test_bootstrap_parameters_are_frozen_from_the_shared_module() -> None:
    """Colleague review, third round: bootstrap method/seed/iterations/
    confidence level must not be a choice left for whoever writes the
    eventual evaluator -- this codebase's shared clustered_inference
    defaults, not bespoke ones."""
    assert BOOTSTRAP_ITERATIONS == 10_000
    assert BOOTSTRAP_SEED == 20_260_729
    assert CONFIDENCE_LEVEL == 0.95


def test_evidence_floor_and_concentration_caps_fit_the_real_candidate_universe() -> None:
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


# --- resolve_episode: exit-bar boundary/gap, computed not trusted ---------


def test_resolve_episode_missing_exit_bar_is_unresolved() -> None:
    result = resolve_episode(_inputs(exit_bar=None))
    assert result.resolved is False
    assert result.unresolved_reason == "missing_exit_bar"
    assert result.net_return_pct is None


def test_resolve_episode_rejects_a_bar_before_the_ceil_boundary() -> None:
    """Colleague review, third round: a floor-aligned (or any pre-boundary)
    bar must never resolve -- it would mean inspecting the outcome before
    it is fully known."""
    result = resolve_episode(_inputs(exit_bar=_bar(1.0, ts_ms=_EXPECTED_BOUNDARY_MS - 60_000)))
    assert result.resolved is False
    assert result.unresolved_reason == "exit_bar_before_boundary"


def test_resolve_episode_rejects_a_zero_timestamp_bar() -> None:
    """The exact scenario named in review: a caller bug that supplies
    ts_ms=0 must not silently resolve."""
    result = resolve_episode(_inputs(exit_bar=_bar(1.0, ts_ms=0)))
    assert result.resolved is False
    assert result.unresolved_reason == "exit_bar_before_boundary"


def test_resolve_episode_exit_bar_gap_exceeded_is_unresolved() -> None:
    gap_ms = int((MAX_EXIT_BAR_GAP_MINUTES + 1) * 60_000)
    result = resolve_episode(_inputs(exit_bar=_bar(1.0, ts_ms=_EXPECTED_BOUNDARY_MS + gap_ms)))
    assert result.resolved is False
    assert result.unresolved_reason == "exit_bar_gap_exceeded"


def test_resolve_episode_gap_exactly_at_the_boundary_is_resolved() -> None:
    """The boundary itself is inclusive -- only strictly exceeding it fails closed."""
    gap_ms = int(MAX_EXIT_BAR_GAP_MINUTES * 60_000)
    result = resolve_episode(_inputs(exit_bar=_bar(1.0, ts_ms=_EXPECTED_BOUNDARY_MS + gap_ms)))
    assert result.resolved is True


def test_resolve_episode_exact_boundary_with_no_gap_is_resolved() -> None:
    result = resolve_episode(_inputs(exit_bar=_bar(1.0)))
    assert result.resolved is True


# --- resolve_episode: price validation and cost model ----------------------


def test_resolve_episode_rejects_non_finite_entry_price() -> None:
    """Colleague review, third round: NaN/infinite/non-positive market
    inputs must fail closed as a data-integrity problem, never silently
    resolve into a polluted net_return_pct."""
    result = resolve_episode(_inputs(entry_price=float("nan"), exit_bar=_bar(1.0)))
    assert result.resolved is False
    assert result.unresolved_reason == "invalid_market_data"


def test_resolve_episode_rejects_non_positive_entry_price() -> None:
    result = resolve_episode(_inputs(entry_price=0.0, exit_bar=_bar(1.0)))
    assert result.resolved is False
    assert result.unresolved_reason == "invalid_market_data"


def test_resolve_episode_rejects_non_finite_exit_close() -> None:
    result = resolve_episode(_inputs(exit_bar=_bar(float("inf"))))
    assert result.resolved is False
    assert result.unresolved_reason == "invalid_market_data"


def test_resolve_episode_applies_the_conservative_exit_slippage_haircut() -> None:
    """A flat close (no price move at all) must still net negative -- the
    slippage haircut plus round-trip fees are real costs, not zero."""
    result = resolve_episode(_inputs(exit_bar=_bar(1.0)))
    assert result.resolved is True
    assert result.net_return_pct is not None
    assert result.net_return_pct < 0


def test_resolve_episode_a_large_favorable_move_still_nets_positive_after_costs() -> None:
    result = resolve_episode(_inputs(exit_bar=_bar(1.05)))
    assert result.resolved is True
    assert result.net_return_pct is not None
    assert result.net_return_pct > 0


def test_resolve_episode_charges_prorated_funding_not_zero() -> None:
    """Colleague review, third round: an earlier version claimed a
    30-minute hold never crosses an 8h funding settlement, which is false
    (entry can occur minutes before one) -- calculate_performance's
    proration must actually be in effect, not skipped."""
    with_funding = resolve_episode(_inputs(exit_bar=_bar(1.0)))
    no_funding_costs = COSTS.__class__(
        taker_fee_bps_per_side=COSTS.taker_fee_bps_per_side, funding_cost_bps_per_8h=0.0
    )
    without_funding = resolve_episode(_inputs(exit_bar=_bar(1.0)), costs=no_funding_costs)
    assert with_funding.net_return_pct is not None
    assert without_funding.net_return_pct is not None
    assert with_funding.net_return_pct < without_funding.net_return_pct
    # Sanity: calculate_performance itself agrees funding is nonzero here.
    accounting = calculate_performance(
        position_usd=_ENTRY_NOTIONAL,
        entry_price=1.0,
        exit_price=1.0,
        side="long",
        duration_minutes=OUTCOME_HORIZON_MINUTES,
        entry_slippage_bps=0.0,
        exit_slippage_bps=EXIT_SLIPPAGE_BPS_ASSUMED,
        costs=COSTS,
    )
    assert accounting.funding_cost_bps > 0


# --- primary_sensitivity_ci: frozen bootstrap wiring -----------------------


def test_primary_sensitivity_ci_is_deterministic() -> None:
    observations = tuple(ClusterObservation(cluster_key=f"asset-{i}", value=0.1) for i in range(10))
    first = primary_sensitivity_ci(observations)
    second = primary_sensitivity_ci(observations)
    assert first == second


def test_primary_sensitivity_ci_low_variance_positive_mean_gives_positive_bound() -> None:
    observations = tuple(ClusterObservation(cluster_key=f"asset-{i}", value=1.0) for i in range(20))
    assert primary_sensitivity_ci(observations) > 0


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
    episodes = EVIDENCE_FLOOR["min_resolved_episodes"] - 1
    assert formal_verdict(**_aggregate(resolved_episodes=episodes)) == VERDICT_INSUFFICIENT_DATA


def test_formal_verdict_insufficient_data_below_cluster_floor() -> None:
    clusters = EVIDENCE_FLOOR["min_distinct_asset_clusters"] - 1
    assert (
        formal_verdict(**_aggregate(distinct_asset_clusters=clusters)) == VERDICT_INSUFFICIENT_DATA
    )


def test_formal_verdict_insufficient_data_below_week_floor() -> None:
    weeks = EVIDENCE_FLOOR["min_distinct_utc_weeks"] - 1
    assert formal_verdict(**_aggregate(distinct_utc_weeks=weeks)) == VERDICT_INSUFFICIENT_DATA


def test_formal_verdict_insufficient_data_when_one_asset_dominates() -> None:
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


def test_formal_verdict_insufficient_data_when_ci_is_not_finite() -> None:
    verdict = formal_verdict(**_aggregate(ci_lower_bound_pct=float("nan")))
    assert verdict == VERDICT_INSUFFICIENT_DATA


def test_formal_verdict_fail_when_floor_met_and_ci_not_positive() -> None:
    assert formal_verdict(**_aggregate(ci_lower_bound_pct=0.0)) == VERDICT_FAIL
    assert formal_verdict(**_aggregate(ci_lower_bound_pct=-0.5)) == VERDICT_FAIL


def test_formal_verdict_candidate_when_floor_met_and_ci_strictly_positive() -> None:
    assert formal_verdict(**_aggregate(ci_lower_bound_pct=0.01)) == VERDICT_CANDIDATE


# --- episode_is_matured (colleague review, 2026-09-03) --------------------


def test_episode_is_matured_false_well_before_the_exit_boundary() -> None:
    database_now = _ENTRY_AT + timedelta(minutes=1)
    assert episode_is_matured(_ENTRY_AT, database_now) is False


def test_episode_is_matured_false_exactly_at_the_boundary() -> None:
    """The exit bar OPENS at the boundary -- it has not closed yet, so this
    must not be treated as matured (the exact bug the colleague review
    caught: a flat entry_at + OUTCOME_HORIZON_MINUTES cutoff would say True
    here)."""
    boundary_ms = expected_exit_boundary_ms(_ENTRY_AT)
    database_now = datetime.fromtimestamp(boundary_ms / 1000, tz=UTC)
    assert episode_is_matured(_ENTRY_AT, database_now) is False


def test_episode_is_matured_false_one_ms_before_bar_close() -> None:
    boundary_ms = expected_exit_boundary_ms(_ENTRY_AT)
    database_now = datetime.fromtimestamp((boundary_ms + EXIT_BAR_TIMEFRAME_MS - 1) / 1000, tz=UTC)
    assert episode_is_matured(_ENTRY_AT, database_now) is False


def test_episode_is_matured_true_exactly_when_the_bar_closes() -> None:
    boundary_ms = expected_exit_boundary_ms(_ENTRY_AT)
    database_now = datetime.fromtimestamp((boundary_ms + EXIT_BAR_TIMEFRAME_MS) / 1000, tz=UTC)
    assert episode_is_matured(_ENTRY_AT, database_now) is True


def test_episode_is_matured_true_well_after_the_bar_closes() -> None:
    database_now = _ENTRY_AT + timedelta(minutes=OUTCOME_HORIZON_MINUTES + 10)
    assert episode_is_matured(_ENTRY_AT, database_now) is True


# --- find_earliest_checkpoint_prefix_length (STOPPING_RULE, literal) ------


def test_find_earliest_checkpoint_prefix_length_empty_sequence_is_none() -> None:
    assert find_earliest_checkpoint_prefix_length(()) is None


def test_find_earliest_checkpoint_prefix_length_none_when_floor_never_reached() -> None:
    # Far below both floors: 5 resolved episodes, 1 distinct week.
    outcomes = [("2026-W36", True) for _ in range(5)]
    assert find_earliest_checkpoint_prefix_length(outcomes) is None


def test_find_earliest_checkpoint_prefix_length_none_when_only_episode_floor_met() -> None:
    # 100 resolved episodes, but all in the same single UTC week.
    outcomes = [("2026-W36", True) for _ in range(EVIDENCE_FLOOR["min_resolved_episodes"])]
    assert find_earliest_checkpoint_prefix_length(outcomes) is None


def test_find_earliest_checkpoint_prefix_length_none_when_only_week_floor_met() -> None:
    # 4 distinct weeks, but only 1 resolved episode per week (well below 100).
    outcomes = [(f"2026-W{36 + i}", True) for i in range(EVIDENCE_FLOOR["min_distinct_utc_weeks"])]
    assert find_earliest_checkpoint_prefix_length(outcomes) is None


def test_find_earliest_checkpoint_prefix_length_stops_at_the_earliest_point() -> None:
    """The whole point of STOPPING_RULE: reaching the floor early and then
    having MORE matured episodes appended afterward must not move the
    checkpoint boundary -- this is what makes the checkpoint reproducible
    run to run as more data becomes available."""
    floor_episodes = EVIDENCE_FLOOR["min_resolved_episodes"]
    floor_weeks = EVIDENCE_FLOOR["min_distinct_utc_weeks"]
    # Exactly enough resolved episodes across exactly enough distinct weeks
    # to cross both floors at the very last of these entries.
    checkpoint_prefix = [(f"2026-W{36 + (i % floor_weeks)}", True) for i in range(floor_episodes)]
    extra_after_checkpoint = [("2026-W99", True) for _ in range(50)]
    with_extra = checkpoint_prefix + extra_after_checkpoint

    without_extra_result = find_earliest_checkpoint_prefix_length(checkpoint_prefix)
    with_extra_result = find_earliest_checkpoint_prefix_length(with_extra)

    assert without_extra_result == floor_episodes
    assert with_extra_result == floor_episodes  # unchanged despite 50 more episodes


def test_find_earliest_checkpoint_prefix_length_unresolved_entries_do_not_count() -> None:
    floor_episodes = EVIDENCE_FLOOR["min_resolved_episodes"]
    floor_weeks = EVIDENCE_FLOOR["min_distinct_utc_weeks"]
    # Interleave unresolved (week_key=None, per the report's own convention
    # for an unresolved episode) entries that must not advance either
    # counter.
    outcomes: list[tuple[str | None, bool]] = []
    for i in range(floor_episodes):
        outcomes.append((None, False))  # unresolved noise
        outcomes.append((f"2026-W{36 + (i % floor_weeks)}", True))
    prefix_length = find_earliest_checkpoint_prefix_length(outcomes)
    assert prefix_length is not None
    # The prefix must include exactly floor_episodes resolved entries --
    # i.e. it stops right after the floor_episodes-th resolved entry, not
    # earlier (which would mean unresolved entries were miscounted as
    # resolved) and not with room to spare (which would mean the boundary
    # wasn't the EARLIEST one).
    resolved_in_prefix = sum(1 for _week, resolved in outcomes[:prefix_length] if resolved)
    assert resolved_in_prefix == floor_episodes


# --- resolve_episode_at_exit_slippage (REQUIRE_EXIT_SLIPPAGE_SENSITIVITY) -


def test_resolve_episode_at_exit_slippage_matches_resolve_episode_at_the_primary_bps() -> None:
    bar = _bar(1.1)
    inputs = _inputs(exit_bar=bar)
    assert resolve_episode(inputs) == resolve_episode_at_exit_slippage(
        inputs, exit_slippage_bps=EXIT_SLIPPAGE_BPS_ASSUMED
    )


def test_resolve_episode_at_exit_slippage_higher_slippage_means_lower_net_return() -> None:
    bar = _bar(1.1)
    inputs = _inputs(exit_bar=bar)
    at_zero = resolve_episode_at_exit_slippage(inputs, exit_slippage_bps=0.0)
    at_primary = resolve_episode_at_exit_slippage(
        inputs, exit_slippage_bps=EXIT_SLIPPAGE_BPS_ASSUMED
    )
    at_double = resolve_episode_at_exit_slippage(
        inputs, exit_slippage_bps=2 * EXIT_SLIPPAGE_BPS_ASSUMED
    )
    assert at_zero.resolved and at_primary.resolved and at_double.resolved
    assert at_zero.net_return_pct is not None
    assert at_primary.net_return_pct is not None
    assert at_double.net_return_pct is not None
    assert at_zero.net_return_pct > at_primary.net_return_pct > at_double.net_return_pct


def test_resolve_episode_at_exit_slippage_still_unresolved_for_a_missing_bar() -> None:
    inputs = _inputs(exit_bar=None)
    result = resolve_episode_at_exit_slippage(inputs, exit_slippage_bps=0.0)
    assert result.resolved is False
    assert result.unresolved_reason == "missing_exit_bar"
