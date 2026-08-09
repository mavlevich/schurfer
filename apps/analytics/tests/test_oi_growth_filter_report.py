from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from schurfer_analytics.challenger_inference import (
    DEFAULT_INFERENCE_SETTINGS,
    ChallengerFormalResult,
    ChallengerInference,
    InferenceReadiness,
    PairedInference,
    StrategyInference,
)
from schurfer_analytics.clustered_inference import BootstrapEstimate
from schurfer_analytics.decision_quality import ComponentSnapshot
from schurfer_analytics.ohlcv import TIMEFRAME_MS, Candle
from schurfer_analytics.oi_growth_filter_report import (
    OI_GROWTH_FILTER_COHORT_START,
    OI_GROWTH_MIN_PROFIT_FACTOR,
    OiGrowthEpisodeResult,
    WeekSensitivity,
    _final_verdict,
    _is_canonical_run,
    _metrics,
    _week_sensitivity,
    evaluate_oi_growth_episode,
    oi_growth_reason,
)
from schurfer_analytics.replay import ReplayDecision, ReplayEpisode, ReplayFilters, ReplayOutcome
from schurfer_analytics.virtual_strategy import (
    DEFAULT_COSTS,
    MarketPath,
    VirtualTrade,
    expected_path_bounds,
)

T0 = OI_GROWTH_FILTER_COHORT_START

# Default "growth"/"decline"/"neutral" raw values consistent with the frozen
# +-5% threshold, keyed by the points they must independently agree with.
_VALUE_BY_POINTS = {0: 10.0, 2: -10.0, 1: 0.0}


def _trade(net_return_pct: float | None, net_pnl_usd: float | None) -> VirtualTrade:
    return VirtualTrade(
        pump_event_id=1,
        cluster_key="base:ERA",
        base="ERA",
        exchange="binance",
        decision_id="dec-1",
        decision_at=datetime(2026, 8, 10, tzinfo=UTC),
        taken=True,
        selection_reason="test",
        status="complete",
        classification="short",
        exit_reason="max_hold",
        ambiguity_resolution=None,
        entry_at=datetime(2026, 8, 10, tzinfo=UTC),
        exit_at=datetime(2026, 8, 10, 8, tzinfo=UTC),
        entry_price=1.0,
        exit_price=0.9,
        entry_delay_seconds=0.0,
        duration_minutes=480.0,
        position_usd=50.0,
        gross_return_pct=net_return_pct,
        net_return_pct=net_return_pct,
        gross_pnl_usd=net_pnl_usd,
        net_pnl_usd=net_pnl_usd,
        fee_cost_bps=0.0,
        funding_cost_bps=0.0,
        slippage_cost_bps=0.0,
        mfe_pct=0.0,
        mae_pct=0.0,
        captured_move_pct=0.0,
    )


# --- oi_growth_reason (pure, over ComponentSnapshot) -------------------------


def _oi(
    points: int, *, data_available: bool | None, value: float | None = None
) -> ComponentSnapshot:
    resolved_value = _VALUE_BY_POINTS[points] if value is None else value
    return ComponentSnapshot("oi_trend", points, 2, resolved_value, data_available)


def test_oi_growth_reason_confirmed_missing_fails_closed() -> None:
    assert oi_growth_reason(_oi(0, data_available=False)) == "oi_data_confirmed_missing"


def test_oi_growth_reason_unknown_quality_fails_closed() -> None:
    assert oi_growth_reason(_oi(0, data_available=None)) == "oi_data_quality_unknown"


def test_oi_growth_reason_declining() -> None:
    assert oi_growth_reason(_oi(2, data_available=True)) == "oi_confirmed_declining"


def test_oi_growth_reason_neutral() -> None:
    assert oi_growth_reason(_oi(1, data_available=True)) == "oi_confirmed_neutral"


def test_oi_growth_reason_confirmed_growth() -> None:
    assert oi_growth_reason(_oi(0, data_available=True)) == "oi_confirmed_growth"


def test_oi_growth_reason_points_value_mismatch_fails_closed() -> None:
    # points=0 ("growth") but the raw value doesn't clear the frozen +5%
    # threshold — must not be silently trusted via points alone.
    assert oi_growth_reason(_oi(0, data_available=True, value=1.0)) == "oi_points_value_mismatch"


def test_oi_growth_reason_neutral_points_but_growth_value_mismatch() -> None:
    assert oi_growth_reason(_oi(1, data_available=True, value=10.0)) == "oi_points_value_mismatch"


def test_oi_growth_reason_boundary_value_is_neutral_not_growth() -> None:
    # Exactly at the threshold: handler.go's `>` / `<` comparisons are
    # strict, so +5.0 itself is neutral, not growth.
    assert oi_growth_reason(_oi(1, data_available=True, value=5.0)) == "oi_confirmed_neutral"


# --- evaluate_oi_growth_episode ----------------------------------------------


def _component_points(*, oi_points: int, other_points: int) -> dict[str, int]:
    """pump_age + price_extent + funding_rate + retrace_from_peak sum to
    `other_points` (default 6, so baseline score_6 always triggers regardless
    of the oi_trend contribution) and oi_trend is the only variable term."""
    names = ("pump_age", "price_extent", "funding_rate", "retrace_from_peak")
    per = other_points // len(names)
    remainder = other_points - per * len(names)
    points = {name: per + (1 if index < remainder else 0) for index, name in enumerate(names)}
    points["oi_trend"] = oi_points
    return points


def _components(oi_points: int, oi_value: float, *, other_points: int = 6) -> dict[str, object]:
    points = _component_points(oi_points=oi_points, other_points=other_points)
    return {
        name: {
            "value": (oi_value if name == "oi_trend" else 1.0),
            "points": score,
            "max": 2,
            "note": "",
        }
        for name, score in points.items()
    }


def _decision(
    row_id: int,
    *,
    oi_points: int,
    oi_value: float | None = None,
    oi_data_available: bool = True,
    other_points: int = 6,
    exchange: str = "binance",
) -> ReplayDecision:
    ts = T0 + timedelta(minutes=row_id)
    resolved_value = _VALUE_BY_POINTS[oi_points] if oi_value is None else oi_value
    components = _components(oi_points, resolved_value, other_points=other_points)
    score = sum(_component_points(oi_points=oi_points, other_points=other_points).values())
    return ReplayDecision(
        row_id=row_id,
        decision_id=f"00000000-0000-0000-0000-{row_id:012d}",
        pump_event_id=42,
        event_base="ERA",
        event_first_seen_at=T0,
        event_closed_at=T0 + timedelta(hours=7),
        ts=ts,
        base="ERA",
        exchange=exchange,
        action="skipped",
        reason="measurement",
        score=score,
        pump_pct=40,
        price=100,
        strategy_version="pump_short_v1_market_quality",
        features={
            "signal": {
                "computed_at": ts.timestamp(),
                "components": components,
                "data_quality": {"oi": oi_data_available, "funding": True},
            },
            "config": {
                "score_threshold": 6,
                "require_market_quality": True,
                "signal_position_usd": 50,
            },
        },
        liquidity={
            "status": "sampled",
            "bid_impact_bps": {"100": 2},
            "ask_impact_bps": {"100": 3},
            "quality": {"allowed": True, "depth_target_usd": 100},
        },
        outcomes=(
            ReplayOutcome(
                horizon_minutes=480,
                status="complete",
                anchor_exchange=exchange,
                source_exchange=exchange,
                entry_price=100,
                forward_price=90,
                mfe_pct=10,
                mae_pct=0,
                short_return_pct=10,
                coverage_ratio=1,
            ),
        ),
    )


def _episode(*decisions: ReplayDecision) -> ReplayEpisode:
    return ReplayEpisode(42, "ERA", "base:ERA", decisions, ())


def _complete_path(decision: ReplayDecision) -> MarketPath:
    start_ms, end_ms = expected_path_bounds(decision)
    candles = tuple(
        Candle(
            timestamp,
            100 if timestamp == start_ms else 90,
            100 if timestamp == start_ms else 90,
            90,
            90,
            1,
        )
        for timestamp in range(start_ms, end_ms, TIMEFRAME_MS)
    )
    return MarketPath(
        pump_event_id=42,
        exchange=decision.exchange,
        base=decision.base,
        status="complete",
        candles=candles,
    )


def test_evaluate_baseline_not_triggered_is_cash_for_both() -> None:
    # Total score forced below 6 (below the baseline threshold).
    decision = _decision(1, oi_points=0, other_points=2)
    episode = _episode(decision)
    result = evaluate_oi_growth_episode(episode, {}, DEFAULT_COSTS)
    assert result.status == "not_triggered"
    assert result.oi_selection_reason == "baseline_not_triggered"
    assert result.baseline_net_return_pct == 0.0
    assert result.challenger_net_return_pct == 0.0
    assert result.baseline_triggered is False
    assert result.challenger_triggered is False


def test_evaluate_confirmed_growth_triggers_challenger_same_as_baseline() -> None:
    decision = _decision(1, oi_points=0, oi_data_available=True)
    episode = _episode(decision)
    path_by_decision = {decision.decision_id or "": _complete_path(decision)}
    result = evaluate_oi_growth_episode(episode, path_by_decision, DEFAULT_COSTS)
    assert result.status == "triggered"
    assert result.oi_selection_reason == "oi_confirmed_growth"
    assert result.baseline_triggered is True
    assert result.challenger_triggered is True
    assert result.challenger_net_return_pct == result.baseline_net_return_pct


def test_evaluate_declining_oi_is_cash_for_challenger_only() -> None:
    decision = _decision(1, oi_points=2, oi_data_available=True)
    episode = _episode(decision)
    path_by_decision = {decision.decision_id or "": _complete_path(decision)}
    result = evaluate_oi_growth_episode(episode, path_by_decision, DEFAULT_COSTS)
    assert result.oi_selection_reason == "oi_confirmed_declining"
    assert result.baseline_triggered is True
    assert result.baseline_net_return_pct is not None
    assert result.challenger_net_return_pct == 0.0
    assert result.challenger_triggered is False


def test_evaluate_neutral_oi_is_cash_for_challenger() -> None:
    decision = _decision(1, oi_points=1, oi_data_available=True)
    episode = _episode(decision)
    path_by_decision = {decision.decision_id or "": _complete_path(decision)}
    result = evaluate_oi_growth_episode(episode, path_by_decision, DEFAULT_COSTS)
    assert result.oi_selection_reason == "oi_confirmed_neutral"
    assert result.challenger_net_return_pct == 0.0


def test_evaluate_confirmed_missing_oi_data_fails_closed_to_cash() -> None:
    # points=0 would normally mean "growth", but data_quality.oi=False means
    # the reading is not confirmed — must fail closed, never confirm growth.
    decision = _decision(1, oi_points=0, oi_data_available=False)
    episode = _episode(decision)
    path_by_decision = {decision.decision_id or "": _complete_path(decision)}
    result = evaluate_oi_growth_episode(episode, path_by_decision, DEFAULT_COSTS)
    assert result.oi_selection_reason == "oi_data_confirmed_missing"
    assert result.challenger_net_return_pct == 0.0
    assert result.challenger_triggered is False
    # Baseline itself is unaffected by OI data-quality — it still trades and
    # is still "triggered" (selection-based), even though the challenger
    # never fires on this episode.
    assert result.baseline_triggered is True


def test_evaluate_points_value_mismatch_is_unresolved_not_cash() -> None:
    # points=0 claims growth, but the recorded value contradicts it against
    # this filter's own frozen threshold — must be unresolved, not folded
    # into either "growth" or plain cash.
    decision = _decision(1, oi_points=0, oi_value=1.0, oi_data_available=True)
    episode = _episode(decision)
    path_by_decision = {decision.decision_id or "": _complete_path(decision)}
    result = evaluate_oi_growth_episode(episode, path_by_decision, DEFAULT_COSTS)
    assert result.status == "selection_unresolved"
    assert result.oi_selection_reason == "oi_points_value_mismatch"
    assert result.baseline_net_return_pct is None
    assert result.baseline_triggered is False
    assert result.challenger_net_return_pct is None
    assert result.challenger_triggered is False


def test_evaluate_confirmed_growth_with_missing_market_path_is_unresolved_not_cash() -> None:
    decision = _decision(1, oi_points=0, oi_data_available=True)
    episode = _episode(decision)
    # No path supplied at all -> _missing_path -> trade never resolves.
    result = evaluate_oi_growth_episode(episode, {}, DEFAULT_COSTS)
    assert result.status == "unresolved_path"
    # Selection still fired even though the market path never resolved —
    # "triggered" tracks selection, not resolution (see module docstring).
    assert result.baseline_triggered is True
    assert result.baseline_net_return_pct is None
    # Confirmed growth but baseline unresolved -> challenger is equally
    # unresolved, never fabricated to a cash 0.0.
    assert result.challenger_net_return_pct is None
    assert result.challenger_triggered is True


def test_evaluate_declining_oi_with_missing_market_path_stays_cash_not_triggered() -> None:
    # Even with an unresolved market path, a non-growth reason is *always*
    # cash for the challenger — never contingent on data availability.
    decision = _decision(1, oi_points=2, oi_data_available=True)
    episode = _episode(decision)
    result = evaluate_oi_growth_episode(episode, {}, DEFAULT_COSTS)
    assert result.status == "unresolved_path"
    assert result.baseline_triggered is True
    assert result.challenger_net_return_pct == 0.0
    assert result.challenger_triggered is False


def test_evaluate_baseline_selection_unresolved_propagates() -> None:
    decision = _decision(1, oi_points=0)
    # Corrupt the score so it no longer matches the summed component points,
    # forcing select_score_policy's underlying selection into "unresolved".
    broken = replace(decision, score=None)
    episode = _episode(broken)
    result = evaluate_oi_growth_episode(episode, {}, DEFAULT_COSTS)
    assert result.status == "selection_unresolved"
    assert result.baseline_net_return_pct is None
    assert result.challenger_net_return_pct is None


def test_evaluate_week_key_comes_from_decision_ts_not_episode_first_seen() -> None:
    decision = _decision(5, oi_points=0, oi_data_available=True)
    # Episode's own first-seen time deliberately differs from the decision's
    # ts (T0 + 5 minutes) by putting it a week earlier.
    episode = ReplayEpisode(42, "ERA", "base:ERA", (decision,), ())
    result = evaluate_oi_growth_episode(episode, {}, DEFAULT_COSTS)
    year, week, _ = decision.ts.isocalendar()
    assert result.week_key == f"{year}-W{week:02d}"


# --- _metrics ------------------------------------------------------------


def test_metrics_counts_cash_as_resolved_but_not_triggered() -> None:
    trade = _trade(5.0, 2.5)
    metrics = _metrics(
        "label",
        [0.0, 5.0, None],
        [False, True, False],
        [trade],
    )
    assert metrics.eligible_episodes == 3
    assert metrics.resolved_episodes == 2
    assert metrics.triggered == 1
    assert metrics.cash == 1
    assert metrics.unresolved == 1


def test_metrics_triggered_can_exceed_resolved_when_selection_is_unresolved() -> None:
    # A triggered-but-unresolved episode (None return) must not corrupt the
    # cash count or silently disappear from the triggered tally.
    metrics = _metrics("label", [None], [True], [])
    assert metrics.triggered == 1
    assert metrics.resolved_episodes == 0
    assert metrics.cash == 0
    assert metrics.unresolved == 1


# --- _week_sensitivity -----------------------------------------------------


def _result(
    event_id: int,
    week: str,
    baseline: float | None,
    challenger: float | None,
) -> OiGrowthEpisodeResult:
    return OiGrowthEpisodeResult(
        pump_event_id=event_id,
        cluster_key="base:ERA",
        base="ERA",
        week_key=week,
        status="triggered",
        oi_selection_reason="oi_confirmed_growth",
        oi_value_pct=-10.0,
        decision_id=f"dec-{event_id}",
        decision_ts=None,
        baseline_net_return_pct=baseline,
        baseline_triggered=True,
        challenger_net_return_pct=challenger,
        challenger_triggered=True,
        trade=None,
    )


def test_week_sensitivity_none_when_no_paired_observations() -> None:
    results = (_result(1, "2026-W32", None, None),)
    assert _week_sensitivity(results, (1,)) is None


def test_week_sensitivity_single_week_does_not_crash() -> None:
    results = (
        _result(1, "2026-W32", 0.0, -2.0),
        _result(2, "2026-W32", 0.0, 4.0),
    )
    sensitivity = _week_sensitivity(results, (1, 2))
    assert sensitivity is not None
    assert sensitivity.distinct_weeks == 1
    assert sensitivity.minimum_leave_one_week_out_pct is None
    assert sensitivity.leave_one_week_out == ()


def test_week_sensitivity_computes_leave_one_week_out() -> None:
    results = (
        _result(1, "2026-W31", 0.0, -2.0),
        _result(2, "2026-W32", 0.0, 4.0),
    )
    sensitivity = _week_sensitivity(results, (1, 2))
    assert sensitivity is not None
    assert sensitivity.distinct_weeks == 2
    weeks = dict(sensitivity.leave_one_week_out)
    assert set(weeks) == {"2026-W31", "2026-W32"}
    # Without W31 (delta -2), only W32's +4 remains.
    assert weeks["2026-W31"] == 4.0
    # Without W32 (delta +4), only W31's -2 remains.
    assert weeks["2026-W32"] == -2.0
    assert sensitivity.minimum_leave_one_week_out_pct == -2.0


# --- _is_canonical_run -----------------------------------------------------


def _filters(**overrides: object) -> ReplayFilters:
    from schurfer_analytics.oi_growth_filter_report import OI_GROWTH_FILTER_STRATEGY_VERSIONS
    from schurfer_analytics.outcomes import RESOLVER_VERSION
    from schurfer_analytics.replay import DEFAULT_REPLAY_HORIZONS

    base = {
        "since": T0,
        "until": T0 + timedelta(days=1),
        "strategy_versions": OI_GROWTH_FILTER_STRATEGY_VERSIONS,
        "resolver_version": RESOLVER_VERSION,
        "required_horizons": DEFAULT_REPLAY_HORIZONS,
        "allow_fallback": False,
    }
    base.update(overrides)
    return ReplayFilters(**base)  # type: ignore[arg-type]


def test_is_canonical_run_true_for_registered_defaults() -> None:
    assert _is_canonical_run(_filters(), DEFAULT_COSTS) is True


def test_is_canonical_run_false_when_fallback_allowed() -> None:
    assert _is_canonical_run(_filters(allow_fallback=True), DEFAULT_COSTS) is False


def test_is_canonical_run_false_when_costs_overridden() -> None:
    overridden = replace(
        DEFAULT_COSTS, taker_fee_bps_per_side=DEFAULT_COSTS.taker_fee_bps_per_side + 1
    )
    assert _is_canonical_run(_filters(), overridden) is False


def test_is_canonical_run_false_when_strategy_versions_overridden() -> None:
    assert _is_canonical_run(_filters(strategy_versions=("other_v1",)), DEFAULT_COSTS) is False


# --- _final_verdict ----------------------------------------------------------


def _inference(*, status: str = "formal_sample_ready", verdict: str | None) -> ChallengerInference:
    readiness = InferenceReadiness(
        status=status,
        eligible_episodes=100,
        formal_sample_episodes=100,
        formal_sample_clusters=30,
        baseline_resolved=100,
        completely_paired_episodes=100,
    )
    if verdict is None:
        return ChallengerInference(
            inference_version="v",
            bootstrap_version="v",
            holm_version="v",
            seed_derivation="v",
            settings=DEFAULT_INFERENCE_SETTINGS,
            readiness=readiness,
            formal_sample_event_ids=(),
            cluster_concentration=(),
            baseline=None,
            challengers=(),
        )
    estimate = BootstrapEstimate(
        episodes=100, clusters=30, point_estimate=1.0, lower_bound=0.1, upper_bound=2.0
    )
    strategy = StrategyInference(
        strategy_key="confirmed_oi_growth",
        estimate=estimate,
        verdict="evidence_of_edge",
        minimum_leave_one_cluster_out_pct=0.5,
        leave_one_cluster_out=(),
    )
    paired = PairedInference(
        variant_key="confirmed_oi_growth",
        estimate=estimate,
        holm_rank=1,
        raw_p_value=0.01,
        holm_adjusted_p_value=0.01,
        holm_critical_alpha=0.05,
        holm_rejected=True,
        familywise_confidence_level=0.95,
        familywise_lower_bound=0.1,
        familywise_upper_bound=2.0,
    )
    result = ChallengerFormalResult(
        variant_key="confirmed_oi_growth", strategy=strategy, paired=paired, verdict=verdict
    )
    return ChallengerInference(
        inference_version="v",
        bootstrap_version="v",
        holm_version="v",
        seed_derivation="v",
        settings=DEFAULT_INFERENCE_SETTINGS,
        readiness=readiness,
        formal_sample_event_ids=(),
        cluster_concentration=(),
        baseline=strategy,
        challengers=(result,),
    )


def _week(*, distinct_weeks: int, minimum: float | None) -> WeekSensitivity:
    return WeekSensitivity(
        distinct_weeks=distinct_weeks, minimum_leave_one_week_out_pct=minimum, leave_one_week_out=()
    )


def test_final_verdict_collecting_when_no_challengers() -> None:
    verdict = _final_verdict(_inference(verdict=None), None, None, canonical_run=True)
    assert verdict == "collecting"


def test_final_verdict_no_go_short_circuits() -> None:
    week = _week(distinct_weeks=5, minimum=5.0)
    inference = _inference(verdict="no_go")
    assert _final_verdict(inference, week, 2.0, canonical_run=True) == "no_go"


def test_final_verdict_shadow_candidate_requires_all_gates() -> None:
    week = _week(distinct_weeks=4, minimum=0.1)
    inference = _inference(verdict="shadow_candidate")
    verdict = _final_verdict(inference, week, OI_GROWTH_MIN_PROFIT_FACTOR + 0.1, canonical_run=True)
    assert verdict == "shadow_candidate"


def test_final_verdict_inconclusive_when_week_sensitivity_negative() -> None:
    week = _week(distinct_weeks=4, minimum=-0.1)
    inference = _inference(verdict="shadow_candidate")
    verdict = _final_verdict(inference, week, 2.0, canonical_run=True)
    assert verdict == "inconclusive"


def test_final_verdict_inconclusive_when_fewer_than_four_weeks() -> None:
    week = _week(distinct_weeks=2, minimum=5.0)
    inference = _inference(verdict="shadow_candidate")
    verdict = _final_verdict(inference, week, 2.0, canonical_run=True)
    assert verdict == "inconclusive"


def test_final_verdict_inconclusive_when_week_sensitivity_uncomputable() -> None:
    week = _week(distinct_weeks=1, minimum=None)
    inference = _inference(verdict="shadow_candidate")
    verdict = _final_verdict(inference, week, 2.0, canonical_run=True)
    assert verdict == "inconclusive"


def test_final_verdict_inconclusive_when_profit_factor_at_or_below_one() -> None:
    week = _week(distinct_weeks=4, minimum=0.1)
    inference = _inference(verdict="shadow_candidate")
    verdict = _final_verdict(inference, week, OI_GROWTH_MIN_PROFIT_FACTOR, canonical_run=True)
    assert verdict == "inconclusive"


def test_final_verdict_inconclusive_when_statistical_verdict_is_inconclusive() -> None:
    week = _week(distinct_weeks=5, minimum=5.0)
    inference = _inference(verdict="inconclusive")
    assert _final_verdict(inference, week, 2.0, canonical_run=True) == "inconclusive"


def test_final_verdict_sensitivity_only_when_not_canonical() -> None:
    # Even a statistically perfect shadow_candidate must never emit a
    # promotion verdict on a non-canonical (overridden) run.
    week = _week(distinct_weeks=5, minimum=5.0)
    inference = _inference(verdict="shadow_candidate")
    verdict = _final_verdict(inference, week, 2.0, canonical_run=False)
    assert verdict == "sensitivity_only_no_promotion"
