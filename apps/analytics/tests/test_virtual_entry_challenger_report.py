from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.ohlcv import TIMEFRAME_MS, Candle, ceil_to_timeframe
from schurfer_analytics.replay import (
    ReplayDataset,
    ReplayDecision,
    ReplayFilters,
    ReplayOutcome,
    build_replay_dataset,
)
from schurfer_analytics.virtual_entry_challenger_report import (
    ENTRY_CHALLENGER_COHORT_START,
    build_entry_challenger_report,
    build_parser,
    render_json,
    render_markdown,
)
from schurfer_analytics.virtual_entry_challengers import (
    ENTRY_VARIANTS,
    challenger_path_bounds,
)
from schurfer_analytics.virtual_strategy import MarketPath


def _inputs(*, confirmed: bool = False) -> tuple[ReplayDataset, ReplayFilters, MarketPath]:
    since = ENTRY_CHALLENGER_COHORT_START
    decision_at = since + timedelta(minutes=1)
    decision = ReplayDecision(
        row_id=1,
        decision_id="00000000-0000-0000-0000-000000000001",
        pump_event_id=42,
        event_base="ERA",
        event_first_seen_at=since,
        event_closed_at=since + timedelta(hours=7),
        ts=decision_at,
        base="ERA",
        exchange="binance",
        action="skipped",
        reason="score 5 < threshold 6",
        score=5,
        pump_pct=40,
        price=100,
        strategy_version="pump_short_v1_market_quality",
        features={
            "signal": {"computed_at": decision_at.timestamp()},
            "config": {"signal_position_usd": 50},
        },
        liquidity={
            "status": "sampled",
            "bid_impact_bps": {"100": 2},
            "ask_impact_bps": {"100": 3},
            "quality": {"depth_target_usd": 100},
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
                mae_pct=0,
                short_return_pct=10,
                coverage_ratio=1,
            ),
        ),
    )
    filters = ReplayFilters(since=since, until=since + timedelta(days=1))
    dataset = build_replay_dataset([decision], filters)
    start_ms, end_ms = challenger_path_bounds(decision)
    baseline_entry_ms = ceil_to_timeframe(int(decision_at.timestamp() * 1000))
    candles = [
        Candle(timestamp, 100, 100, 99, 100, 1)
        for timestamp in range(start_ms, end_ms, TIMEFRAME_MS)
    ]
    if confirmed:
        last_safe_ms = baseline_entry_ms - 2 * TIMEFRAME_MS
        index = next(index for index, candle in enumerate(candles) if candle.ts_ms == last_safe_ms)
        candles[index] = replace(candles[index], open=100, high=100, low=98, close=98)
    path = MarketPath(42, "binance", "ERA", "complete", tuple(candles))
    return dataset, filters, path


def test_report_treats_no_confirmation_as_zero_return_cash_episode() -> None:
    dataset, filters, path = _inputs()

    report = build_entry_challenger_report(
        dataset,
        filters,
        (path,),
        generated_at=datetime(2026, 7, 28, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
    )

    assert len(report.variant_metrics) == 3
    for metrics, comparison in zip(
        report.variant_metrics,
        report.paired_comparisons,
        strict=True,
    ):
        assert metrics.not_confirmed == 1
        assert metrics.avoided_losing_entries == 1
        assert metrics.missed_winning_entries == 0
        assert metrics.completed_trades == 0
        assert metrics.mean_confirmed_wait_minutes is None
        assert metrics.mean_effective_wait_minutes == 60
        assert metrics.mean_episode_net_return_pct == 0
        assert comparison.episodes == 1
        assert comparison.mean_challenger_net_return_pct == 0
        assert comparison.mean_delta_pct is not None
        assert comparison.mean_delta_pct > 0
        assert comparison.improved_episodes == 1


def test_report_registers_exact_family_and_serializes_confirmation() -> None:
    dataset, filters, path = _inputs(confirmed=True)

    report = build_entry_challenger_report(
        dataset,
        filters,
        (path,),
        generated_at=datetime(2026, 7, 28, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
    )
    payload = json.loads(render_json(report))
    markdown = render_markdown(report)

    assert tuple(variant["key"] for variant in payload["manifest"]["variants"]) == tuple(
        variant.key for variant in ENTRY_VARIANTS
    )
    assert payload["manifest"]["working_tree_dirty"] is False
    assert payload["manifest"]["report_scope"] == "descriptive_paired_no_statistical_verdict"
    assert (
        payload["manifest"]["liquidity_slippage_policy"]
        == "baseline_decision_snapshot_held_constant"
    )
    assert payload["manifest"]["eligibility_policy"] == "baseline_episode_eligibility_held_constant"
    assert all(variant["execution_gap_bars"] == 1 for variant in payload["manifest"]["variants"])
    assert payload["challenger_results"][0]["confirmation"]["status"] == "confirmed"
    assert all(row.completed_trades == 1 for row in report.variant_metrics)
    assert "Descriptive paired replay only" in markdown
    assert "red_candle_retrace_1_5" in markdown
    assert "baseline_decision_snapshot_held_constant" in markdown
    assert "Holm correction" in markdown


def test_report_keeps_missing_market_path_visible() -> None:
    dataset, filters, path = _inputs()
    missing = replace(path, status="fetch_failed", candles=(), error="venue timeout")

    report = build_entry_challenger_report(
        dataset,
        filters,
        (missing,),
        generated_at=datetime(2026, 7, 28, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=True,
    )

    assert all(row.unresolved == 1 for row in report.variant_metrics)
    assert all(row.paired_resolved == 0 for row in report.variant_metrics)
    assert all(result.error == "venue timeout" for result in report.challenger_results)


def test_confirmed_entry_with_incomplete_exit_path_remains_unresolved() -> None:
    dataset, filters, path = _inputs(confirmed=True)
    baseline_entry_ms = ceil_to_timeframe(
        int(dataset.eligible_episodes[0].decisions[0].ts.timestamp() * 1000)
    )
    truncated = replace(
        path,
        candles=tuple(
            candle for candle in path.candles if candle.ts_ms < baseline_entry_ms + 30 * 60 * 1000
        ),
    )

    report = build_entry_challenger_report(
        dataset,
        filters,
        (truncated,),
        generated_at=datetime(2026, 7, 28, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
    )

    assert all(row.confirmed_entries == 1 for row in report.variant_metrics)
    assert all(row.completed_trades == 0 for row in report.variant_metrics)
    assert all(row.unresolved == 1 for row in report.variant_metrics)
    assert all(result.status == "incomplete_market_path" for result in report.challenger_results)


def test_parser_defaults_to_preregistered_cohort_and_requires_tree_state() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--code-revision", "abc123"])

    args = build_parser().parse_args(["--code-revision", "abc123", "--no-working-tree-dirty"])

    assert args.since == ENTRY_CHALLENGER_COHORT_START
    assert args.working_tree_dirty is False


def test_report_requires_explicit_since_and_rejects_duplicate_paths() -> None:
    dataset, filters, path = _inputs()
    no_since = ReplayFilters(until=filters.until)

    with pytest.raises(ValueError, match="explicit cohort start"):
        build_entry_challenger_report(
            dataset,
            no_since,
            (path,),
            generated_at=datetime(2026, 7, 28, tzinfo=UTC),
            code_revision="abc123",
            working_tree_dirty=False,
        )

    with pytest.raises(ValueError, match="duplicate market paths"):
        build_entry_challenger_report(
            dataset,
            filters,
            (path, path),
            generated_at=datetime(2026, 7, 28, tzinfo=UTC),
            code_revision="abc123",
            working_tree_dirty=False,
        )
