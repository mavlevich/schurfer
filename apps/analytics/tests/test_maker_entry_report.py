from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.maker_entry_report import (
    MAKER_ENTRY_COHORT_START,
    MAKER_ENTRY_PROSPECTIVE_COHORT_START,
    MAKER_ENTRY_STRATEGY_VERSIONS,
    build_maker_entry_report,
    build_parser,
    render_json,
    render_markdown,
)
from schurfer_analytics.ohlcv import ONE_MINUTE_MS, TIMEFRAME_MS, Candle
from schurfer_analytics.replay import (
    ReplayDataset,
    ReplayDecision,
    ReplayFilters,
    ReplayOutcome,
    build_replay_dataset,
)
from schurfer_analytics.virtual_market import MakerDecisionPaths, maker_path_bounds
from schurfer_analytics.virtual_strategy import MarketPath


def _inputs(
    cohort_start: datetime = MAKER_ENTRY_COHORT_START,
) -> tuple[ReplayDataset, ReplayFilters, MakerDecisionPaths]:
    decision_at = cohort_start + timedelta(hours=2, minutes=1)
    decision = ReplayDecision(
        row_id=1,
        decision_id="00000000-0000-0000-0000-000000000001",
        pump_event_id=42,
        event_base="ERA",
        event_first_seen_at=decision_at - timedelta(hours=1),
        event_closed_at=decision_at + timedelta(hours=8),
        ts=decision_at,
        base="ERA",
        exchange="binance",
        action="skipped",
        reason="score",
        score=5,
        pump_pct=40,
        price=100,
        strategy_version=MAKER_ENTRY_STRATEGY_VERSIONS[0],
        features={
            "signal": {"computed_at": decision_at.timestamp()},
            "config": {"require_market_quality": True, "signal_position_usd": 50},
        },
        liquidity={
            "status": "sampled",
            "best_bid": 99.9,
            "best_ask": 100.1,
            "bid_impact_bps": {"100": 3},
            "ask_impact_bps": {"100": 4},
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
                10,
                10,
                1,
            ),
        ),
    )
    filters = ReplayFilters(
        since=cohort_start,
        until=cohort_start + timedelta(days=2),
        strategy_versions=MAKER_ENTRY_STRATEGY_VERSIONS,
        required_horizons=(480,),
    )
    dataset = build_replay_dataset([decision], filters)

    def path(timeframe_ms: int) -> MarketPath:
        start_ms, end_ms = maker_path_bounds(decision, timeframe_ms)
        candles = tuple(
            Candle(timestamp, 100, 101, 90, 90, 1)
            for timestamp in range(start_ms, end_ms, timeframe_ms)
        )
        return MarketPath(42, "binance", "ERA", "complete", candles)

    paths = MakerDecisionPaths(
        decision.decision_id or "",
        path(ONE_MINUTE_MS),
        path(TIMEFRAME_MS),
    )
    return dataset, filters, paths


def test_report_exposes_upper_bound_limits_and_separate_fallback() -> None:
    dataset, filters, paths = _inputs()
    report = build_maker_entry_report(
        dataset,
        filters,
        (paths,),
        generated_at=datetime(2026, 7, 29, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
        bootstrap_iterations=100,
    )
    payload = json.loads(render_json(report))
    markdown = render_markdown(report)

    assert report.metrics.potential_fills == 1
    assert report.metrics.primary_1m == 1
    assert report.metrics.fallback_5m == 0
    assert report.metrics.mean_fee_cost_bps == pytest.approx(10)
    assert report.metrics.median_filled_trade_net_return_pct is not None
    assert report.metrics.taker_1m_matched_episodes == 1
    assert report.metrics.taker_1m_matched_mean_net_return_pct is not None
    assert report.metrics.maker_vs_taker_1m_mean_delta_pct is not None
    assert report.metrics.resolved_clusters == 1
    assert report.metrics.largest_cluster == "base:ERA"
    assert report.timeframe_metrics[0].timeframe == "1m_primary"
    assert report.timeframe_metrics[0].episodes == 1
    assert report.timeframe_metrics[1].timeframe == "5m_fallback"
    assert report.timeframe_metrics[1].episodes == 0
    assert payload["manifest"]["unfilled_policy"] == "zero_return_cash"
    assert payload["manifest"]["comparison_interpretation"] == ("not_an_entry_only_causal_delta")
    assert payload["manifest"]["fill_evidence_version"].endswith("_v2")
    assert report.fill_evidence_metrics[2].evidence == "crossed_intrabar"
    assert report.fill_evidence_metrics[2].fills == 1
    assert report.sensitivity_metrics[0].potential_fills == 1
    assert report.sensitivity_metrics[1].potential_fills == 1
    assert "does not prove post-only acceptance" in markdown
    assert "same-resolution" in markdown
    assert "marketable_on_activation" in markdown
    assert "Fixed fill sensitivities" in markdown
    assert "Discovery-only optimistic upper bound" in markdown


def test_report_rejects_scope_drift_and_duplicate_paths() -> None:
    dataset, filters, paths = _inputs()
    wrong = ReplayFilters(
        since=MAKER_ENTRY_COHORT_START + timedelta(minutes=1),
        until=filters.until,
        strategy_versions=filters.strategy_versions,
        required_horizons=filters.required_horizons,
    )

    with pytest.raises(ValueError, match="registered cohort start"):
        build_maker_entry_report(
            dataset,
            wrong,
            (paths,),
            generated_at=datetime(2026, 7, 29, tzinfo=UTC),
            code_revision="abc123",
            working_tree_dirty=False,
            bootstrap_iterations=100,
        )
    with pytest.raises(ValueError, match="duplicate maker paths"):
        build_maker_entry_report(
            dataset,
            filters,
            (paths, paths),
            generated_at=datetime(2026, 7, 29, tzinfo=UTC),
            code_revision="abc123",
            working_tree_dirty=False,
            bootstrap_iterations=100,
        )


def test_prospective_cohort_is_accepted_and_labeled() -> None:
    """The prospective confirmation cohort (frozen 2026-08-24, see
    docs/research/pump-short-maker-entry-prospective-v1.md) must be
    accepted alongside the original discovery cohort, and the report must
    label which one it read -- these must never be silently conflated."""
    dataset, filters, paths = _inputs(cohort_start=MAKER_ENTRY_PROSPECTIVE_COHORT_START)
    report = build_maker_entry_report(
        dataset,
        filters,
        (paths,),
        generated_at=MAKER_ENTRY_PROSPECTIVE_COHORT_START + timedelta(days=3),
        code_revision="abc123",
        working_tree_dirty=False,
        bootstrap_iterations=100,
    )
    assert report.manifest.scope == "prospective_confirmation_v1"
    assert "Cohort: prospective_confirmation_v1" in render_markdown(report)


def test_discovery_cohort_is_labeled_distinctly_from_prospective() -> None:
    dataset, filters, paths = _inputs()
    report = build_maker_entry_report(
        dataset,
        filters,
        (paths,),
        generated_at=datetime(2026, 7, 29, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
        bootstrap_iterations=100,
    )
    assert report.manifest.scope == "discovery_upper_bound_only"
    assert "Cohort: discovery_upper_bound_only" in render_markdown(report)


def test_activation_marketable_sensitivity_turns_fill_into_cash() -> None:
    dataset, filters, paths = _inputs()
    first = paths.one_minute.candles[0]
    marketable_one = tuple(
        (
            Candle(
                candle.ts_ms,
                101 if index == 0 else candle.open,
                candle.high,
                candle.low,
                candle.close,
                candle.volume,
            )
            if index == 0
            else candle
        )
        for index, candle in enumerate(paths.one_minute.candles)
    )
    assert first.ts_ms == marketable_one[0].ts_ms
    marketable_paths = replace(
        paths,
        one_minute=replace(paths.one_minute, candles=marketable_one),
    )
    report = build_maker_entry_report(
        dataset,
        filters,
        (marketable_paths,),
        generated_at=datetime(2026, 7, 29, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
        bootstrap_iterations=100,
    )

    optimistic, strict, strict_touch = report.sensitivity_metrics
    assert optimistic.potential_fills == 1
    assert strict.potential_fills == 0
    assert strict.mean_episode_net_return_pct == 0
    assert strict_touch.potential_fills == 0


def test_cli_requires_dirty_provenance() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([])
    args = parser.parse_args(["--no-working-tree-dirty"])
    assert args.working_tree_dirty is False
