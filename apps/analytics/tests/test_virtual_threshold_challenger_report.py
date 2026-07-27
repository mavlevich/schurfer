from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
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
from schurfer_analytics.virtual_threshold_challenger_report import (
    ENTRY_THRESHOLD_BASELINE_KEY,
    ENTRY_THRESHOLD_STRATEGY_VERSIONS,
    build_entry_threshold_report,
    build_parser,
    render_json,
    render_markdown,
)
from schurfer_analytics.virtual_threshold_challengers import (
    ENTRY_THRESHOLD_COHORT_START,
    ENTRY_THRESHOLD_VARIANTS,
)


def _decision(
    row_id: int,
    pump_pct: float,
    exchange: str,
    strategy_version: str,
) -> ReplayDecision:
    ts = ENTRY_THRESHOLD_COHORT_START + timedelta(minutes=row_id)
    return ReplayDecision(
        row_id=row_id,
        decision_id=f"00000000-0000-0000-0000-{row_id:012d}",
        pump_event_id=42,
        event_base="ERA",
        event_first_seen_at=ENTRY_THRESHOLD_COHORT_START,
        event_closed_at=ENTRY_THRESHOLD_COHORT_START + timedelta(hours=7),
        ts=ts,
        base="ERA",
        exchange=exchange,
        action="skipped",
        reason="measurement",
        score=7,
        pump_pct=pump_pct,
        price=100,
        strategy_version=strategy_version,
        features={
            "signal": {"computed_at": ts.timestamp()},
            "measurement_only": strategy_version == "pump_short_measurement_v1",
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
            "quality": {
                "allowed": True,
                "depth_target_usd": 100,
            },
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
            pump_event_id=42,
            exchange=decision.exchange,
            base=decision.base,
            status="complete",
            candles=candles,
        ),
    )


def _inputs(
    *,
    include_entry_crossing: bool = True,
) -> tuple[
    ReplayDataset,
    ReplayFilters,
    tuple[ReplayDecision, ...],
    tuple[DecisionMarketPath, ...],
]:
    measurement = _decision(1, 20, "binance", "pump_short_measurement_v1")
    decisions = [measurement]
    if include_entry_crossing:
        decisions.append(_decision(2, 35, "bybit", "pump_short_v1_market_quality"))
    filters = ReplayFilters(
        since=ENTRY_THRESHOLD_COHORT_START,
        until=ENTRY_THRESHOLD_COHORT_START + timedelta(days=1),
        strategy_versions=ENTRY_THRESHOLD_STRATEGY_VERSIONS,
    )
    dataset = build_replay_dataset(decisions, filters)
    frozen = tuple(decisions)
    return dataset, filters, frozen, tuple(_path(decision) for decision in frozen)


def test_report_selects_different_recorded_decisions_and_cash_for_higher_floors() -> None:
    dataset, filters, decisions, paths = _inputs()

    report = build_entry_threshold_report(
        dataset,
        filters,
        paths,
        generated_at=datetime(2026, 7, 28, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
    )
    by_key = {
        result.threshold_key: result
        for result in report.episode_results
        if result.pump_event_id == 42
    }

    assert by_key["floor_20"].selected_decision_id == decisions[0].decision_id
    assert by_key["floor_20"].exchange == "binance"
    assert by_key[ENTRY_THRESHOLD_BASELINE_KEY].selected_decision_id == decisions[1].decision_id
    assert by_key[ENTRY_THRESHOLD_BASELINE_KEY].exchange == "bybit"
    assert by_key["floor_35"].selected_decision_id == decisions[1].decision_id
    assert by_key["floor_40"].status == "not_triggered"
    assert by_key["floor_40"].episode_net_return_pct == 0
    assert by_key["floor_50"].status == "not_triggered"
    assert report.inference.readiness.status == "collecting"
    assert all(row.episodes == 1 for row in report.paired_comparisons)


def test_episode_that_never_reaches_baseline_remains_in_universe_as_cash() -> None:
    dataset, filters, _, paths = _inputs(include_entry_crossing=False)

    report = build_entry_threshold_report(
        dataset,
        filters,
        paths,
        generated_at=datetime(2026, 7, 28, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
    )
    by_key = {result.threshold_key: result for result in report.episode_results}

    assert by_key["floor_20"].status == "complete"
    assert by_key[ENTRY_THRESHOLD_BASELINE_KEY].status == "not_triggered"
    assert by_key[ENTRY_THRESHOLD_BASELINE_KEY].episode_net_return_pct == 0
    comparison = next(row for row in report.paired_comparisons if row.variant_key == "floor_20")
    assert comparison.episodes == 1


def test_missing_selected_decision_path_is_unresolved_without_affecting_cash() -> None:
    dataset, filters, _, paths = _inputs()

    report = build_entry_threshold_report(
        dataset,
        filters,
        paths[1:],
        generated_at=datetime(2026, 7, 28, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=True,
    )
    by_key = {result.threshold_key: result for result in report.episode_results}

    assert by_key["floor_20"].status == "market_path_unavailable"
    assert by_key["floor_20"].episode_net_return_pct is None
    assert by_key["floor_40"].status == "not_triggered"
    assert by_key["floor_40"].episode_net_return_pct == 0


def test_markdown_keeps_fail_closed_selection_reason_visible() -> None:
    _, filters, decisions, paths = _inputs()
    malformed = [replace(decisions[0], pump_pct=float("nan")), *decisions[1:]]
    dataset = build_replay_dataset(malformed, filters)

    report = build_entry_threshold_report(
        dataset,
        filters,
        paths,
        generated_at=datetime(2026, 7, 28, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
    )

    assert "invalid_pump_pct" in render_markdown(report)
    assert all(result.status == "selection_unresolved" for result in report.episode_results)


def test_report_manifest_locks_family_and_serializes_provenance() -> None:
    dataset, filters, _, paths = _inputs()

    report = build_entry_threshold_report(
        dataset,
        filters,
        paths,
        generated_at=datetime(2026, 7, 28, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
    )
    payload = json.loads(render_json(report))
    markdown = render_markdown(report)

    assert payload["manifest"]["baseline"]["min_pump_pct"] == 30
    assert [row["min_pump_pct"] for row in payload["manifest"]["variants"]] == [
        variant.min_pump_pct for variant in ENTRY_THRESHOLD_VARIANTS
    ]
    assert payload["manifest"]["strategy_versions"] == list(ENTRY_THRESHOLD_STRATEGY_VERSIONS)
    assert payload["manifest"]["working_tree_dirty"] is False
    assert payload["manifest"]["no_trigger_policy"] == "zero_return_cash_episode"
    assert "Formal inference status: `collecting`" in markdown
    assert "floor_50" in markdown


def test_report_rejects_changed_cohort_and_duplicate_decision_paths() -> None:
    dataset, filters, _, paths = _inputs()
    changed = replace(filters, since=ENTRY_THRESHOLD_COHORT_START + timedelta(minutes=1))

    with pytest.raises(ValueError, match="registered cohort start"):
        build_entry_threshold_report(
            dataset,
            changed,
            paths,
            generated_at=datetime(2026, 7, 28, tzinfo=UTC),
            code_revision="abc123",
            working_tree_dirty=False,
        )
    with pytest.raises(ValueError, match="duplicate market paths"):
        build_entry_threshold_report(
            dataset,
            filters,
            (paths[0], paths[0]),
            generated_at=datetime(2026, 7, 28, tzinfo=UTC),
            code_revision="abc123",
            working_tree_dirty=False,
        )

    wrong_strategies = replace(
        filters,
        strategy_versions=("pump_short_measurement_v1",),
    )
    with pytest.raises(ValueError, match="registered strategy cohorts"):
        build_entry_threshold_report(
            dataset,
            wrong_strategies,
            paths,
            generated_at=datetime(2026, 7, 28, tzinfo=UTC),
            code_revision="abc123",
            working_tree_dirty=False,
        )


def test_parser_defaults_to_registered_cohort_and_requires_tree_state() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--code-revision", "abc123"])

    args = build_parser().parse_args(["--code-revision", "abc123", "--no-working-tree-dirty"])

    assert args.since == ENTRY_THRESHOLD_COHORT_START
    assert args.working_tree_dirty is False
