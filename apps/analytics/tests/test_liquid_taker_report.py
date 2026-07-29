from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import pytest
import schurfer_analytics.liquid_taker_report as liquid_report
from schurfer_analytics.liquid_taker_report import (
    LIQUID_TAKER_CANDIDATE_VERSION,
    LIQUID_TAKER_COHORT_START,
    LIQUID_TAKER_STRATEGY_VERSIONS,
    build_liquid_taker_report,
    build_parser,
    measured_capacity_floor_usd,
    render_json,
    render_markdown,
    select_liquid_taker_decision,
)
from schurfer_analytics.ohlcv import TIMEFRAME_MS, Candle
from schurfer_analytics.replay import (
    ReplayDataset,
    ReplayDecision,
    ReplayFilters,
    ReplayOutcome,
    build_replay_dataset,
)
from schurfer_analytics.virtual_market import DecisionMarketPath
from schurfer_analytics.virtual_strategy import MarketPath, expected_path_bounds


def _decision(
    row_id: int,
    *,
    event_id: int = 42,
    base: str = "ERA",
    score: int = 6,
    score_threshold: int = 6,
    quality_allowed: bool = True,
    bid: float | None = 3,
    ask: float | None = 4,
    exchange: str = "binance",
) -> ReplayDecision:
    ts = LIQUID_TAKER_COHORT_START + timedelta(minutes=row_id)
    return ReplayDecision(
        row_id=row_id,
        decision_id=f"00000000-0000-0000-0000-{row_id:012d}",
        pump_event_id=event_id,
        event_base=base,
        event_first_seen_at=LIQUID_TAKER_COHORT_START,
        event_closed_at=LIQUID_TAKER_COHORT_START + timedelta(hours=7),
        ts=ts,
        base=base,
        exchange=exchange,
        action="skipped",
        reason="measurement",
        score=score,
        pump_pct=40,
        price=100,
        strategy_version=LIQUID_TAKER_STRATEGY_VERSIONS[0],
        features={
            "signal": {"computed_at": ts.timestamp()},
            "config": {
                "score_threshold": score_threshold,
                "require_market_quality": True,
                "signal_position_usd": 50,
            },
        },
        liquidity={
            "status": "sampled",
            "bid_impact_bps": {"100": bid, "500": 8, "1000": 15},
            "ask_impact_bps": {"100": ask, "500": 9, "1000": 15},
            "quality": {"allowed": quality_allowed, "depth_target_usd": 100},
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


def _path(decision: ReplayDecision) -> DecisionMarketPath:
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


def _inputs(
    decisions: tuple[ReplayDecision, ...],
) -> tuple[ReplayDataset, ReplayFilters]:
    filters = ReplayFilters(
        since=LIQUID_TAKER_COHORT_START,
        until=LIQUID_TAKER_COHORT_START + timedelta(days=1),
        strategy_versions=LIQUID_TAKER_STRATEGY_VERSIONS,
    )
    return build_replay_dataset(list(decisions), filters), filters


def test_selection_waits_for_first_complete_recorded_gate_crossing() -> None:
    low_score = _decision(1, score=5, bid=None, ask=None)
    wide = _decision(2, bid=12, ask=9)
    selected = _decision(3, bid=6, ask=7)
    dataset, _ = _inputs((low_score, wide, selected))

    result = select_liquid_taker_decision(dataset.eligible_episodes[0])

    assert result.status == "selected"
    assert result.decision == selected
    assert result.bid_impact_bps == 6
    assert result.ask_impact_bps == 7


def test_candidate_decision_with_missing_impact_fails_closed() -> None:
    dataset, _ = _inputs((_decision(1, bid=None),))

    result = select_liquid_taker_decision(dataset.eligible_episodes[0])

    assert result.status == "unresolved"
    assert result.error == "missing_configured_notional_impact"


def test_capacity_is_largest_observed_target_below_round_trip_limit() -> None:
    decision = _decision(1)

    assert measured_capacity_floor_usd(decision) == 500
    assert (
        measured_capacity_floor_usd(
            replace(
                decision,
                liquidity={
                    **(decision.liquidity or {}),
                    "ask_impact_bps": {"100": None, "500": None, "1000": None},
                },
            )
        )
        is None
    )


def test_report_replays_selected_trade_and_keeps_non_trigger_as_cash() -> None:
    selected = _decision(1)
    cash = _decision(2, event_id=43, base="CASH", score=5)
    dataset, filters = _inputs((selected, cash))

    report = build_liquid_taker_report(
        dataset,
        filters,
        (_path(selected),),
        generated_at=filters.until,
        code_revision="abc123",
        working_tree_dirty=False,
        bootstrap_iterations=100,
    )
    by_event = {result.pump_event_id: result for result in report.episode_results}

    assert by_event[42].status == "complete"
    assert by_event[42].episode_net_return_pct is not None
    assert by_event[43].status == "not_triggered"
    assert by_event[43].episode_net_return_pct == 0
    assert report.metrics.selected == 1
    assert report.metrics.cash == 1
    assert report.metrics.opportunities_per_calendar_day == 1
    assert report.metrics.median_measured_capacity_floor_usd == 500


def test_missing_exact_path_is_unresolved_and_blocks_formal_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = _decision(1)
    dataset, filters = _inputs((selected,))
    monkeypatch.setattr(liquid_report, "FORMAL_EPISODES", 1)
    monkeypatch.setattr(liquid_report, "MIN_FORMAL_CLUSTERS", 1)
    monkeypatch.setattr(liquid_report, "FORMAL_WEEKS", 1)

    report = build_liquid_taker_report(
        dataset,
        filters,
        (),
        generated_at=filters.until,
        code_revision="abc123",
        working_tree_dirty=True,
        bootstrap_iterations=100,
    )

    assert report.episode_results[0].status == "market_path_unavailable"
    assert report.formal_inference.status == "awaiting_complete_resolution"
    assert report.formal_inference.verdict == "withheld"


def test_manifest_and_rendering_preserve_registered_guardrails() -> None:
    selected = _decision(1)
    dataset, filters = _inputs((selected,))
    report = build_liquid_taker_report(
        dataset,
        filters,
        (_path(selected),),
        generated_at=filters.until,
        code_revision="abc123",
        working_tree_dirty=False,
        bootstrap_iterations=100,
    )

    payload = json.loads(render_json(report))
    markdown = render_markdown(report)

    assert payload["manifest"]["candidate_version"] == LIQUID_TAKER_CANDIDATE_VERSION
    assert payload["manifest"]["maximum_round_trip_impact_bps"] == 20
    assert payload["manifest"]["exact_venue_only"] is True
    assert "shadow-only" in markdown
    assert "measured floor, not unlimited executable size" in markdown


def test_formal_inference_requires_positive_week_and_asset_sensitivity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(liquid_report, "FORMAL_EPISODES", 6)
    monkeypatch.setattr(liquid_report, "MIN_FORMAL_CLUSTERS", 6)
    monkeypatch.setattr(liquid_report, "FORMAL_WEEKS", 2)
    results = tuple(
        liquid_report.LiquidTakerResult(
            pump_event_id=index,
            cluster_key=f"base:ASSET{index}",
            base=f"ASSET{index}",
            episode_at=LIQUID_TAKER_COHORT_START + timedelta(days=index * 2),
            episode_week=("2026-W31" if index < 3 else "2026-W32"),
            status="not_triggered",
            selected_decision_id=None,
            selected_at=None,
            exchange=None,
            recorded_score=None,
            recorded_score_threshold=None,
            bid_impact_bps=None,
            ask_impact_bps=None,
            round_trip_impact_bps=None,
            measured_capacity_floor_usd=None,
            episode_net_return_pct=1,
            trade=None,
        )
        for index in range(6)
    )

    inference = liquid_report._formal_inference(
        results,
        bootstrap_iterations=100,
        bootstrap_seed=7,
    )

    assert inference.status == "ready"
    assert inference.lower_95_pct == 1
    assert inference.excluding_busiest_week_pct == 1
    assert inference.minimum_top_asset_exclusion_pct == 1
    assert inference.verdict == "shadow_candidate"


def test_formal_inference_uses_worst_top_asset_exclusion_not_average(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(liquid_report, "FORMAL_EPISODES", 6)
    monkeypatch.setattr(liquid_report, "MIN_FORMAL_CLUSTERS", 6)
    monkeypatch.setattr(liquid_report, "FORMAL_WEEKS", 2)
    returns = (-5.0, -5.0, -5.0, 50.0, -5.0, -5.0)
    results = tuple(
        liquid_report.LiquidTakerResult(
            pump_event_id=index,
            cluster_key=f"base:ASSET{index}",
            base=f"ASSET{index}",
            episode_at=LIQUID_TAKER_COHORT_START + timedelta(days=index * 2),
            episode_week="2026-W31" if index < 3 else "2026-W32",
            status="not_triggered",
            selected_decision_id=None,
            selected_at=None,
            exchange=None,
            recorded_score=None,
            recorded_score_threshold=None,
            bid_impact_bps=None,
            ask_impact_bps=None,
            round_trip_impact_bps=None,
            measured_capacity_floor_usd=None,
            episode_net_return_pct=return_pct,
            trade=None,
        )
        for index, return_pct in enumerate(returns)
    )

    inference = liquid_report._formal_inference(
        results,
        bootstrap_iterations=100,
        bootstrap_seed=7,
    )

    # Removing ASSET3 leaves five -5% episodes. The average of all five
    # leave-one-out means is positive, so this specifically pins the worst case.
    assert inference.minimum_top_asset_exclusion_pct == -5
    assert inference.verdict == "do_not_promote"


def test_report_rejects_changed_cohort_strategy_fallback_and_duplicate_paths() -> None:
    selected = _decision(1)
    dataset, filters = _inputs((selected,))
    path = _path(selected)

    def build(
        changed_filters: ReplayFilters,
        paths: tuple[DecisionMarketPath, ...],
    ) -> None:
        build_liquid_taker_report(
            dataset,
            changed_filters,
            paths,
            generated_at=filters.until,
            code_revision="abc123",
            working_tree_dirty=False,
            bootstrap_iterations=100,
        )

    with pytest.raises(ValueError, match="registered cohort start"):
        build(
            replace(filters, since=LIQUID_TAKER_COHORT_START + timedelta(minutes=1)),
            (path,),
        )
    with pytest.raises(ValueError, match="registered strategy cohort"):
        build(
            replace(filters, strategy_versions=("other",)),
            (path,),
        )
    with pytest.raises(ValueError, match="exact venue outcomes"):
        build(
            replace(filters, allow_fallback=True),
            (path,),
        )
    with pytest.raises(ValueError, match="duplicate market paths"):
        build(filters, (path, path))


def test_parser_requires_dirty_state_and_locks_default_cohort() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--code-revision", "abc123"])

    args = build_parser().parse_args(["--code-revision", "abc123", "--no-working-tree-dirty"])

    assert args.since == LIQUID_TAKER_COHORT_START
    assert args.format == "markdown"
