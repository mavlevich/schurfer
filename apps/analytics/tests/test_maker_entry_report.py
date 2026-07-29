from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.maker_entry_report import (
    MAKER_ENTRY_COHORT_START,
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


def _inputs() -> tuple[ReplayDataset, ReplayFilters, MakerDecisionPaths]:
    decision_at = MAKER_ENTRY_COHORT_START + timedelta(hours=2, minutes=1)
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
        since=MAKER_ENTRY_COHORT_START,
        until=MAKER_ENTRY_COHORT_START + timedelta(days=2),
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
    )
    payload = json.loads(render_json(report))
    markdown = render_markdown(report)

    assert report.metrics.potential_fills == 1
    assert report.metrics.primary_1m == 1
    assert report.metrics.fallback_5m == 0
    assert report.metrics.mean_fee_cost_bps == pytest.approx(10)
    assert report.timeframe_metrics[0].timeframe == "1m_primary"
    assert report.timeframe_metrics[0].episodes == 1
    assert report.timeframe_metrics[1].timeframe == "5m_fallback"
    assert report.timeframe_metrics[1].episodes == 0
    assert payload["manifest"]["unfilled_policy"] == "zero_return_cash"
    assert payload["manifest"]["comparison_interpretation"] == ("not_an_entry_only_causal_delta")
    assert "does not prove post-only acceptance" in markdown
    assert "not a pure causal estimate of entry execution alone" in markdown
    assert "Discovery-only optimistic upper bound" in markdown


def test_report_rejects_scope_drift_and_duplicate_paths() -> None:
    dataset, filters, paths = _inputs()
    wrong = ReplayFilters(
        since=MAKER_ENTRY_COHORT_START + timedelta(minutes=1),
        until=filters.until,
        strategy_versions=filters.strategy_versions,
        required_horizons=filters.required_horizons,
    )

    with pytest.raises(ValueError, match="locked discovery cohort"):
        build_maker_entry_report(
            dataset,
            wrong,
            (paths,),
            generated_at=datetime(2026, 7, 29, tzinfo=UTC),
            code_revision="abc123",
            working_tree_dirty=False,
        )
    with pytest.raises(ValueError, match="duplicate maker paths"):
        build_maker_entry_report(
            dataset,
            filters,
            (paths, paths),
            generated_at=datetime(2026, 7, 29, tzinfo=UTC),
            code_revision="abc123",
            working_tree_dirty=False,
        )


def test_cli_requires_dirty_provenance() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([])
    args = parser.parse_args(["--no-working-tree-dirty"])
    assert args.working_tree_dirty is False
