from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import pytest
import schurfer_analytics.liquid_taker_wider_stop_report as wider_report
from schurfer_analytics.liquid_taker_report import (
    LIQUID_TAKER_COHORT_START,
    build_liquid_taker_report,
)
from schurfer_analytics.liquid_taker_wider_stop import (
    LIQUID_TAKER_BASELINE_KEY,
    LIQUID_TAKER_WIDER_KEY,
)
from schurfer_analytics.liquid_taker_wider_stop_report import (
    LIQUID_TAKER_WIDER_COHORT_START,
    LIQUID_TAKER_WIDER_CONTRACT_VERSION,
    LIQUID_TAKER_WIDER_STRATEGY_VERSIONS,
    build_parser,
    build_report,
    render_json,
    render_markdown,
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
    event_day: int = 0,
    score: int = 6,
    score_threshold: int = 6,
    bid: float | None = 3,
    ask: float | None = 4,
) -> ReplayDecision:
    event_at = LIQUID_TAKER_WIDER_COHORT_START + timedelta(days=event_day)
    ts = event_at + timedelta(minutes=row_id)
    return ReplayDecision(
        row_id=row_id,
        decision_id=f"00000000-0000-0000-0000-{row_id:012d}",
        pump_event_id=event_id,
        event_base=base,
        event_first_seen_at=event_at,
        event_closed_at=event_at + timedelta(hours=7),
        ts=ts,
        base=base,
        exchange="binance",
        action="skipped",
        reason="measurement",
        score=score,
        pump_pct=40,
        price=100,
        strategy_version=LIQUID_TAKER_WIDER_STRATEGY_VERSIONS[0],
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
            "bid_impact_bps": {"50": bid, "100": 6},
            "ask_impact_bps": {"50": ask, "100": 7},
            "quality": {
                "allowed": True,
                "reason": "ok",
                "depth_target_usd": 50,
            },
        },
        outcomes=(
            ReplayOutcome(
                horizon_minutes=480,
                status="complete",
                anchor_exchange="binance",
                source_exchange="binance",
                entry_price=100,
                forward_price=90,
                mfe_pct=10,
                mae_pct=9,
                short_return_pct=10,
                coverage_ratio=1,
            ),
        ),
    )


def _path(decision: ReplayDecision) -> DecisionMarketPath:
    start_ms, end_ms = expected_path_bounds(decision)
    candles = []
    for index, timestamp in enumerate(range(start_ms, end_ms, TIMEFRAME_MS)):
        if index == 0:
            candles.append(Candle(timestamp, 100, 109, 100, 108, 1))
        else:
            candles.append(Candle(timestamp, 90, 90, 90, 90, 1))
    return DecisionMarketPath(
        decision.decision_id or "",
        MarketPath(
            pump_event_id=decision.pump_event_id or 0,
            exchange=decision.exchange,
            base=decision.base,
            status="complete",
            candles=tuple(candles),
        ),
    )


def _inputs(
    decisions: tuple[ReplayDecision, ...],
) -> tuple[ReplayDataset, ReplayFilters]:
    filters = ReplayFilters(
        since=LIQUID_TAKER_WIDER_COHORT_START,
        until=LIQUID_TAKER_WIDER_COHORT_START + timedelta(days=30),
        strategy_versions=LIQUID_TAKER_WIDER_STRATEGY_VERSIONS,
    )
    return build_replay_dataset(list(decisions), filters), filters


def test_wider_stop_preserves_fixed_dollar_risk_and_rescues_baseline_stop() -> None:
    decision = _decision(1)
    dataset, filters = _inputs((decision,))

    report = build_report(
        dataset,
        filters,
        (_path(decision),),
        generated_at=filters.until,
        code_revision="abc123",
        working_tree_dirty=False,
        bootstrap_iterations=100,
    )
    episode = report.episode_results[0]

    assert episode.baseline.variant_key == LIQUID_TAKER_BASELINE_KEY
    assert episode.challenger.variant_key == LIQUID_TAKER_WIDER_KEY
    assert episode.baseline.effective_initial_sl_pct == 8
    assert episode.challenger.effective_initial_sl_pct == 12
    assert episode.challenger.position_scale == pytest.approx(2 / 3)
    assert episode.challenger.simple_3x_liquidation_buffer_pct == pytest.approx(100 / 3 - 12)
    assert episode.baseline.trade is not None
    assert episode.challenger.trade is not None
    assert episode.baseline.trade.exit_reason == "initial_sl"
    assert episode.challenger.trade.exit_reason != "initial_sl"
    assert episode.challenger.risk_normalized_net_return_pct is not None
    assert episode.challenger.risk_normalized_net_return_pct > 0
    assert episode.baseline.round_trip_impact_bps == 7
    assert episode.baseline.measured_capacity_floor_usd == 100
    assert report.paired_comparison.rescued_initial_stops == 1
    assert report.opportunities_per_calendar_day == pytest.approx(1 / 30)
    assert report.median_measured_capacity_floor_usd == 100


def test_baseline_reconciles_with_existing_liquid_taker_report() -> None:
    decision = _decision(1)
    dataset, filters = _inputs((decision,))
    path = _path(decision)

    baseline_report = build_liquid_taker_report(
        dataset,
        replace(filters, since=LIQUID_TAKER_COHORT_START),
        (path,),
        generated_at=filters.until,
        code_revision="abc123",
        working_tree_dirty=False,
        bootstrap_iterations=100,
    )
    combined = build_report(
        dataset,
        filters,
        (path,),
        generated_at=filters.until,
        code_revision="abc123",
        working_tree_dirty=False,
        bootstrap_iterations=100,
    )
    expected = baseline_report.episode_results[0].trade
    actual = combined.episode_results[0].baseline.trade

    assert actual is not None
    assert expected is not None
    assert actual == expected
    assert (
        combined.episode_results[0].baseline.risk_normalized_net_return_pct
        == baseline_report.episode_results[0].episode_net_return_pct
    )


def test_no_trigger_is_paired_cash_and_missing_path_is_unresolved() -> None:
    cash = _decision(1, event_id=42, score=5)
    selected = _decision(2, event_id=43, base="MISS")
    dataset, filters = _inputs((cash, selected))

    report = build_report(
        dataset,
        filters,
        (),
        generated_at=filters.until,
        code_revision="abc123",
        working_tree_dirty=True,
        bootstrap_iterations=100,
    )
    by_event = {row.pump_event_id: row for row in report.episode_results}

    assert by_event[42].status == "cash"
    assert by_event[42].baseline.risk_normalized_net_return_pct == 0
    assert by_event[42].challenger.risk_normalized_net_return_pct == 0
    assert by_event[43].status == "unresolved"
    assert report.cash_episodes == 1
    assert report.unresolved_episodes == 1


def test_formal_verdict_requires_positive_absolute_and_paired_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wider_report, "FORMAL_EPISODES", 4)
    monkeypatch.setattr(wider_report, "MIN_FORMAL_CLUSTERS", 4)
    monkeypatch.setattr(wider_report, "FORMAL_WEEKS", 2)
    decisions = tuple(
        _decision(
            index,
            event_id=100 + index,
            base=f"ASSET{index}",
            event_day=index * 3,
        )
        for index in range(1, 5)
    )
    dataset, filters = _inputs(decisions)

    report = build_report(
        dataset,
        filters,
        tuple(_path(decision) for decision in decisions),
        generated_at=filters.until,
        code_revision="abc123",
        working_tree_dirty=False,
        bootstrap_iterations=100,
        bootstrap_seed=7,
    )

    assert report.formal_inference.status == "ready"
    assert report.formal_inference.challenger is not None
    assert report.formal_inference.challenger.lower_bound > 0
    assert report.formal_inference.paired_delta is not None
    assert report.formal_inference.paired_delta.lower_bound > 0
    assert report.formal_inference.verdict == "shadow_candidate"


def test_manifest_rendering_and_scope_guards_are_locked() -> None:
    decision = _decision(1)
    dataset, filters = _inputs((decision,))
    path = _path(decision)
    report = build_report(
        dataset,
        filters,
        (path,),
        generated_at=filters.until,
        code_revision="abc123",
        working_tree_dirty=False,
        bootstrap_iterations=100,
    )
    payload = json.loads(render_json(report))
    markdown = render_markdown(report)

    assert payload["manifest"]["contract_version"] == LIQUID_TAKER_WIDER_CONTRACT_VERSION
    assert payload["manifest"]["maximum_round_trip_impact_bps"] == 20
    assert payload["manifest"]["exact_venue_only"] is True
    assert payload["manifest"]["variants"][1]["initial_stop_multiplier"] == 1.5
    assert payload["manifest"]["variants"][1]["position_scale"] == pytest.approx(2 / 3)
    assert "shadow-only" in markdown
    assert "complete pairing" in markdown

    with pytest.raises(ValueError, match="registered cohort start"):
        build_report(
            dataset,
            replace(
                filters,
                since=LIQUID_TAKER_WIDER_COHORT_START + timedelta(minutes=1),
            ),
            (path,),
            generated_at=filters.until,
            code_revision="abc123",
            working_tree_dirty=False,
            bootstrap_iterations=100,
        )
    with pytest.raises(ValueError, match="registered strategy cohort"):
        build_report(
            dataset,
            replace(filters, strategy_versions=("other",)),
            (path,),
            generated_at=filters.until,
            code_revision="abc123",
            working_tree_dirty=False,
            bootstrap_iterations=100,
        )
    with pytest.raises(ValueError, match="exact venue outcomes"):
        build_report(
            dataset,
            replace(filters, allow_fallback=True),
            (path,),
            generated_at=filters.until,
            code_revision="abc123",
            working_tree_dirty=False,
            bootstrap_iterations=100,
        )
    with pytest.raises(ValueError, match="duplicate market paths"):
        build_report(
            dataset,
            filters,
            (path, path),
            generated_at=filters.until,
            code_revision="abc123",
            working_tree_dirty=False,
            bootstrap_iterations=100,
        )


def test_parser_requires_dirty_state_and_defaults_to_future_cohort() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--code-revision", "abc123"])

    args = build_parser().parse_args(["--code-revision", "abc123", "--no-working-tree-dirty"])

    assert args.since == LIQUID_TAKER_WIDER_COHORT_START
    assert args.working_tree_dirty is False
    assert args.format == "markdown"
