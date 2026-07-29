from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.exit_policy_discovery import (
    EXIT_DISCOVERY_ATR_BARS,
    EXIT_DISCOVERY_VARIANTS,
    WIDER_STOP_EXIT_DISCOVERY_VARIANT,
    exit_discovery_path_bounds,
)
from schurfer_analytics.ohlcv import TIMEFRAME_MS, Candle
from schurfer_analytics.replay import (
    ReplayDataset,
    ReplayDecision,
    ReplayFilters,
    ReplayOutcome,
    build_replay_dataset,
)
from schurfer_analytics.virtual_exit_discovery_report import (
    EXIT_DISCOVERY_COHORT_START,
    EXIT_DISCOVERY_STRATEGY_VERSIONS,
    build_exit_discovery_report,
    build_parser,
    render_json,
    render_markdown,
)
from schurfer_analytics.virtual_market import DecisionMarketPath
from schurfer_analytics.virtual_strategy import MarketPath


def _inputs() -> tuple[ReplayDataset, ReplayFilters, DecisionMarketPath]:
    decision_at = EXIT_DISCOVERY_COHORT_START + timedelta(hours=2, minutes=1)
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
        reason="score 5 < threshold 6",
        score=5,
        pump_pct=40,
        price=99,
        strategy_version=EXIT_DISCOVERY_STRATEGY_VERSIONS[0],
        features={
            "signal": {"computed_at": decision_at.timestamp()},
            "config": {
                "require_market_quality": True,
                "signal_position_usd": 50,
            },
        },
        liquidity={
            "status": "sampled",
            "bid_impact_bps": {"100": 3},
            "ask_impact_bps": {"100": 4},
            "quality": {
                "allowed": True,
                "reason": "ok",
                "depth_target_usd": 100,
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
                mae_pct=10,
                short_return_pct=10,
                coverage_ratio=1,
            ),
        ),
    )
    filters = ReplayFilters(
        since=EXIT_DISCOVERY_COHORT_START,
        until=EXIT_DISCOVERY_COHORT_START + timedelta(days=2),
        strategy_versions=EXIT_DISCOVERY_STRATEGY_VERSIONS,
        required_horizons=(480,),
    )
    dataset = build_replay_dataset([decision], filters)
    start_ms, end_ms = exit_discovery_path_bounds(decision)
    entry_ms = start_ms + (EXIT_DISCOVERY_ATR_BARS + 1) * TIMEFRAME_MS
    candles = tuple(
        [
            *(
                Candle(timestamp, 100, 101, 99, 100, 1)
                for timestamp in range(start_ms, entry_ms, TIMEFRAME_MS)
            ),
            Candle(entry_ms, 100, 109, 100, 108, 1),
            *(
                Candle(timestamp, 90, 90, 90, 90, 1)
                for timestamp in range(entry_ms + TIMEFRAME_MS, end_ms, TIMEFRAME_MS)
            ),
        ]
    )
    path = DecisionMarketPath(
        decision.decision_id or "",
        MarketPath(42, "binance", "ERA", "complete", candles),
    )
    return dataset, filters, path


def test_report_is_discovery_only_and_fully_paired() -> None:
    dataset, filters, path = _inputs()

    report = build_exit_discovery_report(
        dataset,
        filters,
        (path,),
        generated_at=datetime(2026, 7, 30, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
        bootstrap_iterations=100,
    )
    payload = json.loads(render_json(report))
    markdown = render_markdown(report)

    assert report.manifest.interpretation == "discovery_only_no_promotion"
    assert report.manifest.primary_metric == "risk_normalized_net_return_pct"
    assert report.matched_episodes == 1
    assert len(report.variant_metrics) == len(EXIT_DISCOVERY_VARIANTS)
    assert all(row.episodes == 1 for row in report.variant_metrics)
    wider = next(
        row
        for row in report.variant_metrics
        if row.variant_key == WIDER_STOP_EXIT_DISCOVERY_VARIANT.key
    )
    assert wider.mean_position_scale_pct == pytest.approx(100 * 2 / 3)
    assert payload["manifest"]["working_tree_dirty"] is False
    assert payload["manifest"]["selection_policy"] == (
        "first_recorded_market_quality_allowed_decision"
    )
    assert "Discovery only" in markdown
    assert "Risk-normalized" in markdown
    assert "not an exchange liquidation calculation" in markdown


def test_report_rejects_wrong_scope_and_duplicate_decision_paths() -> None:
    dataset, filters, path = _inputs()
    wrong_since = ReplayFilters(
        since=EXIT_DISCOVERY_COHORT_START + timedelta(minutes=5),
        until=filters.until,
        strategy_versions=filters.strategy_versions,
        required_horizons=filters.required_horizons,
    )
    wrong_strategy = ReplayFilters(
        since=filters.since,
        until=filters.until,
        strategy_versions=("other",),
        required_horizons=filters.required_horizons,
    )

    with pytest.raises(ValueError, match="locked discovery cohort"):
        build_exit_discovery_report(
            dataset,
            wrong_since,
            (path,),
            generated_at=datetime(2026, 7, 30, tzinfo=UTC),
            code_revision="abc123",
            working_tree_dirty=False,
            bootstrap_iterations=100,
        )
    with pytest.raises(ValueError, match="locked strategy cohort"):
        build_exit_discovery_report(
            dataset,
            wrong_strategy,
            (path,),
            generated_at=datetime(2026, 7, 30, tzinfo=UTC),
            code_revision="abc123",
            working_tree_dirty=False,
            bootstrap_iterations=100,
        )
    with pytest.raises(ValueError, match="duplicate market paths"):
        build_exit_discovery_report(
            dataset,
            filters,
            (path, path),
            generated_at=datetime(2026, 7, 30, tzinfo=UTC),
            code_revision="abc123",
            working_tree_dirty=False,
            bootstrap_iterations=100,
        )


def test_parser_defaults_to_discovery_cohort_and_requires_tree_state() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--code-revision", "abc123"])

    args = build_parser().parse_args(["--code-revision", "abc123", "--no-working-tree-dirty"])

    assert args.since == EXIT_DISCOVERY_COHORT_START
    assert args.working_tree_dirty is False
