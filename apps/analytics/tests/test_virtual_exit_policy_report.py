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
from schurfer_analytics.virtual_exit_policy_report import (
    EXIT_POLICY_COHORT_START,
    EXIT_POLICY_INFERENCE_VERSION,
    EXIT_POLICY_STRATEGY_VERSIONS,
    build_exit_policy_report,
    build_parser,
    render_json,
    render_markdown,
)
from schurfer_analytics.virtual_strategy import (
    EXIT_POLICIES,
    MarketPath,
    exit_policy_family_path_bounds,
)


def _inputs() -> tuple[ReplayDataset, ReplayFilters, MarketPath]:
    since = EXIT_POLICY_COHORT_START
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
        action="opened_dry_run",
        reason="dry_run",
        score=6,
        pump_pct=40,
        price=100,
        strategy_version=EXIT_POLICY_STRATEGY_VERSIONS[0],
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
    filters = ReplayFilters(
        since=since,
        until=since + timedelta(days=1),
        strategy_versions=EXIT_POLICY_STRATEGY_VERSIONS,
    )
    dataset = build_replay_dataset([decision], filters)
    start_ms, end_ms = exit_policy_family_path_bounds(decision)
    candles = tuple(
        Candle(
            ts_ms,
            100 if ts_ms == start_ms else 90,
            100 if ts_ms == start_ms else 90,
            100 if ts_ms == start_ms else 90,
            100 if ts_ms == start_ms else 90,
            1,
        )
        for ts_ms in range(start_ms, end_ms, TIMEFRAME_MS)
    )
    return dataset, filters, MarketPath(42, "binance", "ERA", "complete", candles)


def test_report_registers_family_and_compares_same_episode() -> None:
    dataset, filters, path = _inputs()

    report = build_exit_policy_report(
        dataset,
        filters,
        (path,),
        generated_at=datetime(2026, 7, 30, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
    )
    payload = json.loads(render_json(report))
    markdown = render_markdown(report)

    assert [row.policy_key for row in report.policy_metrics] == [
        policy.key for policy in EXIT_POLICIES
    ]
    assert all(row.completed_trades == 1 for row in report.policy_metrics)
    assert all(row.episodes == 1 for row in report.paired_comparisons)
    assert report.inference.inference_version == EXIT_POLICY_INFERENCE_VERSION
    assert report.inference.readiness.status == "collecting"
    assert report.inference.formal_sample_event_ids == (42,)
    assert payload["manifest"]["working_tree_dirty"] is False
    assert payload["manifest"]["path_policy"] == (
        "longest_registered_window_required_for_paired_family"
    )
    assert payload["manifest"]["baseline"]["key"] == "baseline"
    assert len(payload["manifest"]["challengers"]) == 4
    assert "Formal inference status: `collecting`" in markdown
    assert "breakeven_after_activation" in markdown
    assert "Holm family alpha" in markdown
    assert "never changes production exits" in markdown


def test_incomplete_longest_path_makes_whole_family_unresolved() -> None:
    dataset, filters, path = _inputs()
    decision = dataset.eligible_episodes[0].decisions[0]
    baseline_minutes = 180
    truncated = replace(
        path,
        candles=path.candles[: baseline_minutes // 5],
    )

    report = build_exit_policy_report(
        dataset,
        filters,
        (truncated,),
        generated_at=datetime(2026, 7, 30, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
    )

    assert decision.pump_pct == 40
    assert all(row.resolved_episodes == 0 for row in report.policy_metrics)
    assert all(row.unresolved_episodes == 1 for row in report.policy_metrics)
    assert all(row.trade.status == "market_path_unavailable" for row in report.policy_trades)
    assert all(
        row.trade.error == "missing one or more bars in the longest registered exit-policy window"
        for row in report.policy_trades
    )
    assert report.inference.readiness.completely_paired_episodes == 0


def test_report_rejects_wrong_cohort_strategy_and_duplicate_paths() -> None:
    dataset, filters, path = _inputs()
    wrong_since = ReplayFilters(
        since=filters.since + timedelta(minutes=5) if filters.since else None,
        until=filters.until,
        strategy_versions=filters.strategy_versions,
    )
    wrong_strategy = ReplayFilters(
        since=filters.since,
        until=filters.until,
        strategy_versions=("other",),
    )
    with pytest.raises(ValueError, match="registered cohort start"):
        build_exit_policy_report(
            dataset,
            wrong_since,
            (path,),
            generated_at=datetime(2026, 7, 30, tzinfo=UTC),
            code_revision="abc123",
            working_tree_dirty=False,
        )
    with pytest.raises(ValueError, match="registered strategy cohort"):
        build_exit_policy_report(
            dataset,
            wrong_strategy,
            (path,),
            generated_at=datetime(2026, 7, 30, tzinfo=UTC),
            code_revision="abc123",
            working_tree_dirty=False,
        )
    with pytest.raises(ValueError, match="duplicate market paths"):
        build_exit_policy_report(
            dataset,
            filters,
            (path, path),
            generated_at=datetime(2026, 7, 30, tzinfo=UTC),
            code_revision="abc123",
            working_tree_dirty=False,
        )


def test_parser_defaults_to_registered_cohort_and_requires_tree_state() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--code-revision", "abc123"])

    args = build_parser().parse_args(["--code-revision", "abc123", "--no-working-tree-dirty"])

    assert args.since == EXIT_POLICY_COHORT_START
    assert args.working_tree_dirty is False
