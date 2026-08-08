from __future__ import annotations

from datetime import timedelta

import pytest
import schurfer_analytics.pump_short_failure_attribution_report as report_module
from schurfer_analytics.decision_quality import SCORE_COMPONENTS, ComponentSnapshot
from schurfer_analytics.ohlcv import TIMEFRAME_MS, Candle
from schurfer_analytics.pump_short_failure_attribution_report import (
    FAILURE_ATTRIBUTION_DEFAULT_SINCE,
    MIN_CELL_OBSERVATIONS,
    BaselineRecord,
    SensitivityRow,
    VetoCandidateRow,
    _build_baseline_record,
    _component_calibration,
    _discovery_interpretation,
    _interaction_table,
    _loss_concentration,
    _price_extent_bucket_label,
    _veto_candidate,
    _vetoed,
    build_failure_attribution_report,
    render_json,
    render_markdown,
)
from schurfer_analytics.replay import (
    ReplayDecision,
    ReplayEpisode,
    ReplayFilters,
    ReplayOutcome,
    build_replay_dataset,
)
from schurfer_analytics.virtual_market import DecisionMarketPath
from schurfer_analytics.virtual_strategy import (
    DEFAULT_COSTS,
    MarketPath,
    VirtualTrade,
    expected_path_bounds,
)

# --- helpers -------------------------------------------------------------------


def _trade(
    pump_event_id: int = 1,
    *,
    net_return_pct: float | None = 1.0,
    net_pnl_usd: float | None = 0.5,
    status: str = "complete",
    exit_reason: str | None = "trailing_stop",
    exchange: str = "binance",
    fee_cost_bps: float | None = 5.0,
    funding_cost_bps: float | None = 1.0,
    slippage_cost_bps: float | None = 2.0,
    gross_return_pct: float | None = 1.2,
    mfe_pct: float | None = 3.0,
    mae_pct: float | None = 1.0,
    duration_minutes: float | None = 30.0,
) -> VirtualTrade:
    return VirtualTrade(
        pump_event_id=pump_event_id,
        cluster_key=f"base:B{pump_event_id}",
        base=f"B{pump_event_id}",
        exchange=exchange,
        decision_id=f"decision-{pump_event_id}",
        decision_at=FAILURE_ATTRIBUTION_DEFAULT_SINCE,
        taken=True,
        selection_reason="failure_attribution:baseline",
        status=status,
        classification="short",
        exit_reason=exit_reason,
        ambiguity_resolution=None,
        entry_at=FAILURE_ATTRIBUTION_DEFAULT_SINCE,
        exit_at=FAILURE_ATTRIBUTION_DEFAULT_SINCE + timedelta(minutes=30),
        entry_price=100.0,
        exit_price=99.0,
        entry_delay_seconds=60.0,
        duration_minutes=duration_minutes,
        position_usd=50.0,
        gross_return_pct=gross_return_pct,
        net_return_pct=net_return_pct,
        gross_pnl_usd=(net_pnl_usd or 0) + 0.1,
        net_pnl_usd=net_pnl_usd,
        fee_cost_bps=fee_cost_bps,
        funding_cost_bps=funding_cost_bps,
        slippage_cost_bps=slippage_cost_bps,
        mfe_pct=mfe_pct,
        mae_pct=mae_pct,
        captured_move_pct=2.0,
    )


def _components(
    points: dict[str, int],
    *,
    missing: tuple[str, ...] = (),
    unknown: tuple[str, ...] = (),
) -> tuple[ComponentSnapshot, ...]:
    snapshots = []
    for index, name in enumerate(SCORE_COMPONENTS):
        value = float(points.get(name, 0)) * 10 + index
        if name in missing:
            data_available: bool | None = False
        elif name in unknown:
            data_available = None
        else:
            data_available = True
        snapshots.append(ComponentSnapshot(name, points.get(name, 0), 2, value, data_available))
    return tuple(snapshots)


def _record(
    pump_event_id: int,
    *,
    status: str = "triggered",
    week_key: str = "2026-W31",
    cluster_key: str | None = None,
    components: tuple[ComponentSnapshot, ...] = (),
    components_resolved: bool = True,
    trade: VirtualTrade | None = None,
    entry_bid_impact_bps: float | None = 4.0,
) -> BaselineRecord:
    return BaselineRecord(
        pump_event_id=pump_event_id,
        cluster_key=cluster_key or f"base:B{pump_event_id}",
        base=f"B{pump_event_id}",
        week_key=week_key,
        status=status,
        decision_id=f"decision-{pump_event_id}" if status == "triggered" else None,
        decision_at=FAILURE_ATTRIBUTION_DEFAULT_SINCE if status == "triggered" else None,
        exchange="binance" if status == "triggered" else None,
        entry_bid_impact_bps=entry_bid_impact_bps if status == "triggered" else None,
        components=components,
        components_resolved=components_resolved,
        trade=trade,
    )


def _veto_row(
    component: str,
    *,
    delta: float | None,
    affected_completed_trades: int = MIN_CELL_OBSERVATIONS,
    affected_assets: int = 2,
    affected_weeks: int = 2,
    retained_trades: int = 10,
    episode_cash_inclusive_net_pct: float | None = 1.0,
    conditional_net_return_pct: float | None = 1.0,
    profit_factor: float | None = 1.5,
) -> VetoCandidateRow:
    """A real VetoCandidateRow, defaulted to pass every gate in
    _is_robust_candidate -- using the actual dataclass (not a duck-typed
    fake) is what catches an API drift like a field that doesn't exist on
    it. Individual tests override just the field they mean to violate."""
    return VetoCandidateRow(
        component=component,
        prevented_losing_trades=0,
        prevented_loss_usd=0.0,
        missed_winners=0,
        missed_profit_usd=0.0,
        retained_trades=retained_trades,
        retained_trade_rate_pct=None,
        trades_per_calendar_week=None,
        conditional_win_rate_pct=None,
        conditional_net_return_pct=conditional_net_return_pct,
        episode_cash_inclusive_net_pct=episode_cash_inclusive_net_pct,
        profit_factor=profit_factor,
        max_sequential_drawdown_usd=None,
        initial_stop_rate_pct=None,
        paired_delta_vs_baseline_pct=delta,
        affected_completed_trades=affected_completed_trades,
        affected_assets=affected_assets,
        affected_weeks=affected_weeks,
    )


def _sensitivity_row(
    label: str,
    *,
    n_assets: int = 5,
    n_weeks: int = 5,
    min_loo_asset: float | None,
    min_loo_week: float | None,
) -> SensitivityRow:
    return SensitivityRow(
        label=label,
        n_episodes=n_assets * 2,
        n_assets=n_assets,
        n_weeks=n_weeks,
        largest_asset_share_pct=None,
        largest_week_share_pct=None,
        min_leave_one_asset_out_pct=min_loo_asset,
        min_leave_one_week_out_pct=min_loo_week,
    )


# --- _price_extent_bucket_label -------------------------------------------------


def test_price_extent_bucket_label_matches_registered_magnitude_floors() -> None:
    floors = report_module.PRICE_EXTENT_BUCKET_FLOORS_PCT
    assert _price_extent_bucket_label(10.0, floors) == "[0,20)"
    assert _price_extent_bucket_label(25.0, floors) == "[20,30)"
    assert _price_extent_bucket_label(250.0, floors) == "[200,300)"
    assert _price_extent_bucket_label(500.0, floors) == ">=300"


# --- _vetoed: fail-closed on unknown/missing data quality -----------------------


def test_vetoed_fires_only_on_confirmed_zero() -> None:
    confirmed = _record(1, components=_components({"funding_rate": 0}))
    assert _vetoed(confirmed, "funding_rate") is True


def test_vetoed_never_fires_on_unknown_data_quality() -> None:
    """A None (unknown) quality state must never vote to veto, even though its
    recorded points happen to be 0 -- fail-closed, not fail-open."""
    unknown = _record(1, components=_components({"funding_rate": 0}, unknown=("funding_rate",)))
    assert _vetoed(unknown, "funding_rate") is False


def test_vetoed_never_fires_on_confirmed_missing_data() -> None:
    missing = _record(1, components=_components({"funding_rate": 0}, missing=("funding_rate",)))
    assert _vetoed(missing, "funding_rate") is False


# --- _component_calibration: three-way split ------------------------------------


def test_component_calibration_splits_missing_from_unknown_from_confirmed() -> None:
    records = [
        _record(
            1, components=_components({"funding_rate": 2}), trade=_trade(1, net_return_pct=5.0)
        ),
        _record(
            2,
            components=_components({"funding_rate": 0}, missing=("funding_rate",)),
            trade=_trade(2, net_return_pct=-3.0),
        ),
        _record(
            3,
            components=_components({"funding_rate": 0}, unknown=("funding_rate",)),
            trade=_trade(3, net_return_pct=-7.0),
        ),
        _record(4, status="cash"),
    ]

    rows = _component_calibration(tuple(records))
    funding_rows = {row.bucket: row for row in rows if row.component == "funding_rate"}

    assert funding_rows["missing_data"].n == 1
    assert funding_rows["missing_data"].mean_net_return_pct == -3.0
    assert funding_rows["unknown_data_quality"].n == 1
    assert funding_rows["unknown_data_quality"].mean_net_return_pct == -7.0
    assert funding_rows["points=2"].n == 1
    assert funding_rows["points=2"].mean_net_return_pct == 5.0
    # Cash (never-triggered) episodes must never contribute to component calibration.
    assert sum(row.n for row in funding_rows.values()) == 3


def test_component_calibration_flags_small_cells() -> None:
    records = [
        _record(i, components=_components({"pump_age": 1}), trade=_trade(i, net_return_pct=1.0))
        for i in range(MIN_CELL_OBSERVATIONS - 1)
    ]
    rows = _component_calibration(tuple(records))
    pump_age_row = next(row for row in rows if row.component == "pump_age")
    assert pump_age_row.n == MIN_CELL_OBSERVATIONS - 1
    assert pump_age_row.insufficient_cell is True


# --- _veto_candidate ------------------------------------------------------------


def test_veto_candidate_prevents_losses_and_counts_missed_winners() -> None:
    records = (
        _record(
            1,
            components=_components({"pump_age": 0}),
            trade=_trade(1, net_return_pct=-4.0, net_pnl_usd=-2.0),
        ),
        _record(
            2,
            components=_components({"pump_age": 0}),
            trade=_trade(2, net_return_pct=6.0, net_pnl_usd=3.0),
        ),
        _record(
            3,
            components=_components({"pump_age": 2}),
            trade=_trade(3, net_return_pct=2.0, net_pnl_usd=1.0),
        ),
        _record(4, status="cash"),
    )

    row = _veto_candidate(records, "pump_age", n_weeks=1)

    assert row.prevented_losing_trades == 1
    assert row.prevented_loss_usd == pytest.approx(2.0)
    assert row.missed_winners == 1
    assert row.missed_profit_usd == pytest.approx(3.0)
    assert row.retained_trades == 1
    assert row.conditional_net_return_pct == pytest.approx(2.0)
    assert row.episode_cash_inclusive_net_pct == pytest.approx((0.0 + 0.0 + 2.0 + 0.0) / 4)


def test_veto_never_searches_for_a_different_entry() -> None:
    record = _record(
        1,
        components=_components({"oi_trend": 0}),
        trade=_trade(1, net_return_pct=-9.0, net_pnl_usd=-4.5),
    )
    row = _veto_candidate((record,), "oi_trend", n_weeks=1)
    assert row.retained_trades == 0
    assert row.episode_cash_inclusive_net_pct == pytest.approx(0.0)


def test_veto_candidate_does_not_fire_on_unknown_data_quality() -> None:
    """A component whose data quality is unknown (None) must never be treated
    as a confirmed zero -- the episode passes through unvetoed."""
    record = _record(
        1,
        components=_components({"funding_rate": 0}, unknown=("funding_rate",)),
        trade=_trade(1, net_return_pct=-9.0, net_pnl_usd=-4.5),
    )
    row = _veto_candidate((record,), "funding_rate", n_weeks=1)
    assert row.prevented_losing_trades == 0
    assert row.retained_trades == 1


# --- _loss_concentration ---------------------------------------------------------


def test_loss_concentration_sorts_most_negative_first_and_ignores_winners() -> None:
    records = (
        _record(1, trade=_trade(1, net_return_pct=-1.0, net_pnl_usd=-1.0)),
        _record(2, trade=_trade(2, net_return_pct=-5.0, net_pnl_usd=-5.0), cluster_key="base:B2"),
        _record(3, trade=_trade(3, net_return_pct=3.0, net_pnl_usd=3.0)),
    )
    rows = _loss_concentration(records)
    assert [row.cluster_key for row in rows] == ["base:B2", "base:B1"]
    assert rows[0].total_loss_usd == pytest.approx(-5.0)
    assert sum(row.share_of_total_loss_pct for row in rows) == pytest.approx(100.0)


# --- _interaction_table -----------------------------------------------------------


def test_interaction_table_flags_insufficient_cell_and_skips_missing_data() -> None:
    records = [
        _record(
            i,
            components=_components({"price_extent": 1, "retrace_from_peak": 2}),
            trade=_trade(i, net_return_pct=1.0),
        )
        for i in range(2)
    ]
    records.append(
        _record(
            100,
            components=_components(
                {"price_extent": 1, "retrace_from_peak": 2}, missing=("retrace_from_peak",)
            ),
            trade=_trade(100, net_return_pct=1.0),
        )
    )
    records.append(
        _record(
            101,
            components=_components(
                {"price_extent": 1, "retrace_from_peak": 2}, unknown=("retrace_from_peak",)
            ),
            trade=_trade(101, net_return_pct=1.0),
        )
    )
    cells = _interaction_table(tuple(records), "price_extent", "retrace_from_peak")
    assert len(cells) == 1
    assert cells[0].n == 2  # neither the missing-data nor the unknown-data row may enter a cell
    assert cells[0].insufficient_cell is True


# --- _discovery_interpretation (real dataclasses, not duck-typed fakes) --------------


def test_discovery_interpretation_rejects_more_than_one_apparent_candidate() -> None:
    vetoes = (_veto_row("pump_age", delta=1.0), _veto_row("price_extent", delta=1.0))
    sensitivity = (
        _sensitivity_row("veto:pump_age", min_loo_asset=0.5, min_loo_week=0.5),
        _sensitivity_row("veto:price_extent", min_loo_asset=0.5, min_loo_week=0.5),
    )
    verdict, rationale = _discovery_interpretation(vetoes, sensitivity)
    assert verdict == "no_existing_feature_separation"
    assert "2 of 5" in rationale


def test_discovery_interpretation_accepts_exactly_one_robust_candidate() -> None:
    """Regression: this must exercise the REAL VetoCandidateRow/SensitivityRow
    dataclasses end to end -- an earlier version of this test used duck-typed
    fake objects and did not catch a real AttributeError on a field that
    doesn't exist on VetoCandidateRow, masked by short-circuit evaluation."""
    vetoes = (_veto_row("pump_age", delta=2.0),)
    sensitivity = (_sensitivity_row("veto:pump_age", min_loo_asset=1.0, min_loo_week=1.0),)
    verdict, rationale = _discovery_interpretation(vetoes, sensitivity)
    assert verdict == "candidate_veto_found"
    assert "pump_age" in rationale


def test_discovery_interpretation_rejects_a_candidate_whose_loo_delta_flips_sign() -> None:
    """A positive average paired delta that turns negative when one asset is
    excluded must not be promoted to candidate_veto_found -- it must require
    the minimum leave-one-out delta to be POSITIVE, not merely present."""
    vetoes = (_veto_row("pump_age", delta=2.0),)
    sensitivity = (_sensitivity_row("veto:pump_age", min_loo_asset=-0.1, min_loo_week=1.0),)
    verdict, _rationale = _discovery_interpretation(vetoes, sensitivity)
    assert verdict == "no_existing_feature_separation"


def _robust_sensitivity(component: str) -> tuple[SensitivityRow, ...]:
    return (_sensitivity_row(f"veto:{component}", min_loo_asset=1.0, min_loo_week=1.0),)


def test_discovery_interpretation_rejects_too_few_affected_trades() -> None:
    """A population-level robust-looking delta driven by fewer than
    MIN_CELL_OBSERVATIONS actually-vetoed trades must not be promoted -- two
    lucky trades diluted across a large denominator can otherwise satisfy
    the leave-one-out checks alone."""
    vetoes = (_veto_row("pump_age", delta=2.0, affected_completed_trades=2),)
    verdict, _rationale = _discovery_interpretation(vetoes, _robust_sensitivity("pump_age"))
    assert verdict == "no_existing_feature_separation"


def test_discovery_interpretation_rejects_single_asset_effect() -> None:
    """Even with enough affected trades, if they all come from one asset the
    'effect' is one coincidence repeated, not a real feature."""
    vetoes = (_veto_row("pump_age", delta=2.0, affected_assets=1),)
    verdict, _rationale = _discovery_interpretation(vetoes, _robust_sensitivity("pump_age"))
    assert verdict == "no_existing_feature_separation"


def test_discovery_interpretation_rejects_single_week_effect() -> None:
    vetoes = (_veto_row("pump_age", delta=2.0, affected_weeks=1),)
    verdict, _rationale = _discovery_interpretation(vetoes, _robust_sensitivity("pump_age"))
    assert verdict == "no_existing_feature_separation"


def test_discovery_interpretation_rejects_non_positive_post_veto_economics() -> None:
    """A veto that merely makes baseline 'less negative' (e.g. -1.0% ->
    -0.9%) must not be promoted -- the resulting strategy must actually be
    net positive, not just improved."""
    vetoes = (_veto_row("pump_age", delta=2.0, episode_cash_inclusive_net_pct=-0.1),)
    verdict, _rationale = _discovery_interpretation(vetoes, _robust_sensitivity("pump_age"))
    assert verdict == "no_existing_feature_separation"


def test_discovery_interpretation_rejects_profit_factor_at_or_below_one() -> None:
    vetoes = (_veto_row("pump_age", delta=2.0, profit_factor=1.0),)
    verdict, _rationale = _discovery_interpretation(vetoes, _robust_sensitivity("pump_age"))
    assert verdict == "no_existing_feature_separation"


def test_discovery_interpretation_rejects_zero_retained_trades() -> None:
    """A veto that eliminates every trade leaves nothing to compute a
    conditional economics read from -- must not be promoted regardless of
    what the (degenerate) paired delta says."""
    vetoes = (
        _veto_row(
            "pump_age",
            delta=2.0,
            retained_trades=0,
            conditional_net_return_pct=None,
            profit_factor=None,
        ),
    )
    verdict, _rationale = _discovery_interpretation(vetoes, _robust_sensitivity("pump_age"))
    assert verdict == "no_existing_feature_separation"


def test_discovery_interpretation_never_auto_assigns_execution_quality_only() -> None:
    """This report's code has no reliable, venue-specific signal (its cost
    figures are a uniform model, not observed fills) -- it may only ever
    auto-produce one of two verdicts, regardless of how expensive an
    aggregated exit_reason bucket looks."""
    verdict, _rationale = _discovery_interpretation((), ())
    assert verdict in ("candidate_veto_found", "no_existing_feature_separation")
    assert verdict != "execution_quality_only"


# --- _build_baseline_record: status/component-resolution edge cases -------------


def _episode_with_decision(decision: ReplayDecision, base: str = "ERA") -> ReplayEpisode:
    return ReplayEpisode(decision.pump_event_id or 0, base, f"base:{base}", (decision,), ())


def _minimal_decision(
    row_id: int,
    *,
    score: int = 6,
    components: dict[str, dict[str, object]] | None = None,
) -> ReplayDecision:
    ts = FAILURE_ATTRIBUTION_DEFAULT_SINCE + timedelta(minutes=row_id)
    signal: dict[str, object] = {"computed_at": ts.timestamp()}
    if components is not None:
        signal["components"] = components
        signal["data_quality"] = {"oi": True, "funding": True}
    return ReplayDecision(
        row_id=row_id,
        decision_id=f"00000000-0000-0000-0000-{row_id:012d}",
        pump_event_id=row_id,
        event_base="ERA",
        event_first_seen_at=FAILURE_ATTRIBUTION_DEFAULT_SINCE,
        event_closed_at=FAILURE_ATTRIBUTION_DEFAULT_SINCE + timedelta(hours=7),
        ts=ts,
        base="ERA",
        exchange="binance",
        action="skipped",
        reason="measurement",
        score=score,
        pump_pct=40,
        price=100,
        strategy_version="pump_short_v1_market_quality",
        features={
            "signal": signal,
            "config": {"require_market_quality": True, "signal_position_usd": 50},
        },
        liquidity={
            "status": "sampled",
            "bid_impact_bps": {"100": 2},
            "ask_impact_bps": {"100": 3},
            "quality": {"allowed": True, "depth_target_usd": 100},
        },
        outcomes=(),
    )


def test_missing_market_path_becomes_unresolved_path_not_triggered() -> None:
    """A selected decision whose market path never loaded must not be counted
    as a clean trigger -- it is a real attempt that could not be priced."""
    decision = _minimal_decision(1)
    episode = _episode_with_decision(decision)
    record = _build_baseline_record(episode, {}, DEFAULT_COSTS)
    assert record.status == "unresolved_path"
    assert record.episode_net_return_pct is None


def test_incomplete_component_vector_is_tracked_not_silently_dropped() -> None:
    """A baseline decision selected with an invalid/partial component vector
    must still resolve economically, but must be flagged as
    components_resolved=False so attribution tables and coverage can exclude
    it visibly instead of it vanishing without a trace."""
    decision = _minimal_decision(1, components=None)  # no signal.components at all
    episode = _episode_with_decision(decision)
    record = _build_baseline_record(episode, {}, DEFAULT_COSTS)
    assert record.components == ()
    assert record.components_resolved is False


# --- end-to-end wiring -------------------------------------------------------------


def _decision(
    row_id: int,
    pump_event_id: int,
    points: dict[str, int],
    *,
    action: str = "skipped",
) -> ReplayDecision:
    ts = FAILURE_ATTRIBUTION_DEFAULT_SINCE + timedelta(minutes=row_id)
    components = {
        name: {
            "value": float(points.get(name, 0)) * 10,
            "points": points.get(name, 0),
            "max": 2,
            "note": "",
        }
        for name in SCORE_COMPONENTS
    }
    return ReplayDecision(
        row_id=row_id,
        decision_id=f"00000000-0000-0000-0000-{row_id:012d}",
        pump_event_id=pump_event_id,
        event_base=f"B{pump_event_id}",
        event_first_seen_at=FAILURE_ATTRIBUTION_DEFAULT_SINCE,
        event_closed_at=FAILURE_ATTRIBUTION_DEFAULT_SINCE + timedelta(hours=7),
        ts=ts,
        base=f"B{pump_event_id}",
        exchange="binance",
        action=action,
        reason="measurement",
        score=sum(points.values()),
        pump_pct=40,
        price=100,
        strategy_version="pump_short_v1_market_quality",
        features={
            "signal": {
                "computed_at": ts.timestamp(),
                "components": components,
                "data_quality": {"oi": True, "funding": True},
            },
            "config": {"require_market_quality": True, "signal_position_usd": 50},
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
                anchor_exchange="binance",
                source_exchange="binance",
                entry_price=100,
                forward_price=99,
                mfe_pct=3,
                mae_pct=1,
                short_return_pct=1,
                coverage_ratio=1,
            ),
        ),
    )


def _path(decision: ReplayDecision) -> DecisionMarketPath:
    start_ms, end_ms = expected_path_bounds(decision)
    candles = tuple(
        Candle(timestamp, 100, 100, 90, 90, 1)
        for timestamp in range(start_ms, end_ms, TIMEFRAME_MS)
    )
    return DecisionMarketPath(
        decision_id=decision.decision_id or "",
        path=MarketPath(
            pump_event_id=decision.pump_event_id or 0,
            exchange=decision.exchange,
            base=decision.base,
            status="complete",
            candles=candles,
        ),
    )


def test_build_failure_attribution_report_runs_end_to_end_and_renders() -> None:
    decisions = []
    for i in range(1, 7):  # pump_event_id/row_id must be > 0 to be a valid episode
        points = {
            "pump_age": 2,
            "price_extent": 1 if i % 2 == 0 else 2,
            "oi_trend": 1,
            "funding_rate": 1,
            "retrace_from_peak": 0 if i % 3 == 0 else 1,
        }
        decisions.append(_decision(i, i, points))
    below_threshold = _decision(100, 100, {"pump_age": 1, "price_extent": 1})
    decisions.append(below_threshold)

    filters = ReplayFilters(
        since=FAILURE_ATTRIBUTION_DEFAULT_SINCE,
        until=FAILURE_ATTRIBUTION_DEFAULT_SINCE + timedelta(days=1),
    )
    dataset = build_replay_dataset(list(decisions), filters)
    paths = tuple(_path(decision) for decision in decisions if decision.pump_event_id != 100)

    report = build_failure_attribution_report(
        dataset,
        filters,
        paths,
        generated_at=FAILURE_ATTRIBUTION_DEFAULT_SINCE + timedelta(days=1),
        code_revision="abc123",
        working_tree_dirty=False,
    )

    assert report.eligible_episodes == 7
    # i=6's points sum to 5 (below the score_6 baseline threshold), so it and
    # the deliberately below-threshold pump_event_id=100 both end up cash.
    assert report.baseline_economics.triggered == 5
    assert report.baseline_economics.cash == 2
    assert report.discovery_verdict in ("candidate_veto_found", "no_existing_feature_separation")
    assert report.manifest.report_scope == "historical_discovery_only_no_strategy_change"
    assert report.manifest.canonical_run is True

    # The whole point of this report: these tables must actually be populated,
    # not merely present-but-empty while rendering happens not to crash.
    assert len(report.component_calibration) > 0
    assert len(report.fixed_veto_candidates) == len(SCORE_COMPONENTS)
    assert any(row.n > 0 for row in report.component_calibration)
    assert len(report.cluster_and_week_sensitivity) == len(SCORE_COMPONENTS) + 1

    markdown = render_markdown(report)
    assert "Pump Short Failure Attribution" in markdown
    assert report.discovery_verdict in markdown
    json_text = render_json(report)
    assert '"report_scope": "historical_discovery_only_no_strategy_change"' in json_text


def test_build_failure_attribution_report_caps_non_canonical_runs() -> None:
    """A run with any override (here: --allow-fallback) must never report
    candidate_veto_found, however the underlying numbers look -- it is
    capped at sensitivity_only_no_candidate and manifest.canonical_run is
    False, so a favorable override can never manufacture a headline."""
    decisions = []
    for i in range(1, 7):
        points = {
            "pump_age": 2,
            "price_extent": 1 if i % 2 == 0 else 2,
            "oi_trend": 1,
            "funding_rate": 1,
            "retrace_from_peak": 0 if i % 3 == 0 else 1,
        }
        decisions.append(_decision(i, i, points))

    filters = ReplayFilters(
        since=FAILURE_ATTRIBUTION_DEFAULT_SINCE,
        until=FAILURE_ATTRIBUTION_DEFAULT_SINCE + timedelta(days=1),
        allow_fallback=True,  # the override under test
    )
    dataset = build_replay_dataset(list(decisions), filters)
    paths = tuple(_path(decision) for decision in decisions)

    report = build_failure_attribution_report(
        dataset,
        filters,
        paths,
        generated_at=FAILURE_ATTRIBUTION_DEFAULT_SINCE + timedelta(days=1),
        code_revision="abc123",
        working_tree_dirty=False,
    )

    assert report.manifest.canonical_run is False
    assert report.discovery_verdict == "sensitivity_only_no_candidate"


def test_build_failure_attribution_report_requires_explicit_since() -> None:
    filters = ReplayFilters(until=FAILURE_ATTRIBUTION_DEFAULT_SINCE + timedelta(days=1))
    assert filters.since is None
    dataset = build_replay_dataset([], filters)
    with pytest.raises(ValueError, match="explicit since"):
        build_failure_attribution_report(
            dataset,
            filters,
            (),
            generated_at=FAILURE_ATTRIBUTION_DEFAULT_SINCE,
            code_revision="abc123",
            working_tree_dirty=False,
        )
