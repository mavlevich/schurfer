from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.decision_quality import SCORE_COMPONENTS, selected_policy_decisions
from schurfer_analytics.decision_quality_report import (
    build_decision_quality_report,
    build_parser,
    render_json,
    render_markdown,
)
from schurfer_analytics.episode_replay import CONFIRMATION_COHORT_START
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


def _components(points: tuple[int, ...]) -> dict[str, object]:
    return {
        name: {"value": float(index + 1), "points": score, "max": 2, "note": ""}
        for index, (name, score) in enumerate(zip(SCORE_COMPONENTS, points, strict=True))
    }


def _decision(
    row_id: int,
    event_id: int,
    base: str,
    points: tuple[int, ...],
    *,
    oi_available: bool = True,
) -> ReplayDecision:
    ts = CONFIRMATION_COHORT_START + timedelta(minutes=row_id)
    return ReplayDecision(
        row_id=row_id,
        decision_id=f"00000000-0000-0000-0000-{row_id:012d}",
        pump_event_id=event_id,
        event_base=base,
        event_first_seen_at=CONFIRMATION_COHORT_START,
        event_closed_at=CONFIRMATION_COHORT_START + timedelta(hours=7),
        ts=ts,
        base=base,
        exchange="binance",
        action="skipped",
        reason="measurement",
        score=sum(points),
        pump_pct=40,
        price=100,
        strategy_version="pump_short_v1_market_quality",
        features={
            "signal": {
                "computed_at": ts.timestamp(),
                "components": _components(points),
                "data_quality": {"oi": oi_available, "funding": True},
            },
            "config": {
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
                480,
                "complete",
                "binance",
                "binance",
                100,
                90,
                10,
                0,
                10,
                1,
            ),
        ),
    )


def _path(decision: ReplayDecision, *, winning: bool) -> DecisionMarketPath:
    start_ms, end_ms = expected_path_bounds(decision)
    candles: list[Candle] = []
    for timestamp in range(start_ms, end_ms, TIMEFRAME_MS):
        if winning:
            candles.append(Candle(timestamp, 100 if not candles else 90, 100, 90, 90, 1))
        else:
            candles.append(Candle(timestamp, 100, 109, 100, 108, 1))
    return DecisionMarketPath(
        decision.decision_id or "",
        MarketPath(
            decision.pump_event_id or 0,
            decision.exchange,
            decision.base,
            "complete",
            tuple(candles),
        ),
    )


def _inputs() -> tuple[ReplayDataset, ReplayFilters, tuple[DecisionMarketPath, ...]]:
    low = _decision(1, 42, "ERA", (1, 1, 1, 1, 0), oi_available=False)
    high = _decision(2, 42, "ERA", (2, 2, 1, 1, 1))
    baseline = _decision(3, 43, "BANK", (2, 1, 1, 1, 1))
    filters = ReplayFilters(
        since=CONFIRMATION_COHORT_START,
        until=CONFIRMATION_COHORT_START + timedelta(days=1),
    )
    dataset = build_replay_dataset([low, high, baseline], filters)
    selected = selected_policy_decisions(dataset.eligible_episodes)
    paths = tuple(_path(decision, winning=decision.pump_event_id == 42) for decision in selected)
    return dataset, filters, paths


def test_report_compares_score_policies_and_component_ablations() -> None:
    dataset, filters, paths = _inputs()

    report = build_decision_quality_report(
        dataset,
        filters,
        paths,
        generated_at=datetime(2026, 7, 27, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
        bootstrap_iterations=100,
    )
    metrics = {row.policy_key: row for row in report.policy_metrics}

    assert metrics["score_any"].completed_trades == 2
    assert metrics["score_6"].completed_trades == 2
    assert metrics["score_8"].cash == 2
    assert metrics["score_6"].total_net_pnl_usd is not None
    assert metrics["score_6"].max_sequential_drawdown_usd is not None
    assert metrics["score_6"].clusters == 2
    assert metrics["score_6"].ci_95_lower_pct is not None
    assert {row.bucket for row in report.score_buckets} == {"4", "6"}
    assert {row.group for row in report.component_buckets} == set(SCORE_COMPONENTS)
    oi_buckets = {row.bucket for row in report.component_buckets if row.group == "oi_trend"}
    assert oi_buckets == {"1", "missing"}


def test_report_serializes_provenance_and_human_guardrails() -> None:
    dataset, filters, paths = _inputs()
    report = build_decision_quality_report(
        dataset,
        filters,
        paths,
        generated_at=datetime(2026, 7, 27, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=True,
        bootstrap_iterations=100,
    )

    payload = json.loads(render_json(report))
    markdown = render_markdown(report)

    assert payload["manifest"]["report_version"] == "decision_quality_report_v1"
    assert payload["manifest"]["baseline_policy"] == "score_6"
    assert payload["manifest"]["interpretation"] == "discovery_only"
    assert payload["manifest"]["working_tree_dirty"] is True
    assert "does not model account-level capital constraints" in markdown
    assert "score_6_without_pump_age" in markdown
    assert "Recorded score calibration" in markdown


def test_missing_path_and_invalid_components_remain_visible() -> None:
    dataset, filters, paths = _inputs()
    malformed = replace(
        dataset.decisions[0],
        features={
            **(dataset.decisions[0].features or {}),
            "signal": {"computed_at": dataset.decisions[0].ts.timestamp()},
        },
    )
    changed = build_replay_dataset([malformed, *dataset.decisions[1:]], filters)

    report = build_decision_quality_report(
        changed,
        filters,
        paths[1:],
        generated_at=datetime(2026, 7, 27, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
        bootstrap_iterations=100,
    )

    any_result = next(
        row
        for row in report.episode_results
        if row.pump_event_id == 42 and row.policy_key == "score_any"
    )
    ablation = next(
        row
        for row in report.episode_results
        if row.pump_event_id == 42 and row.policy_key == "score_6_without_pump_age"
    )
    assert any_result.status == "market_path_unavailable"
    assert ablation.status == "selection_unresolved"
    assert ablation.error == "missing_score_components"


def test_report_rejects_duplicate_paths_and_small_bootstrap() -> None:
    dataset, filters, paths = _inputs()

    with pytest.raises(ValueError, match="duplicate market paths"):
        build_decision_quality_report(
            dataset,
            filters,
            (*paths, paths[0]),
            generated_at=datetime(2026, 7, 27, tzinfo=UTC),
            code_revision="abc123",
            working_tree_dirty=False,
            bootstrap_iterations=100,
        )
    with pytest.raises(ValueError, match="at least 100"):
        build_decision_quality_report(
            dataset,
            filters,
            paths,
            generated_at=datetime(2026, 7, 27, tzinfo=UTC),
            code_revision="abc123",
            working_tree_dirty=False,
            bootstrap_iterations=99,
        )


def test_parser_requires_explicit_working_tree_state() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--code-revision", "abc123"])

    args = build_parser().parse_args(["--code-revision", "abc123", "--no-working-tree-dirty"])
    assert args.working_tree_dirty is False
