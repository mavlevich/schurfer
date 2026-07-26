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
from schurfer_analytics.virtual_strategy import MarketPath, exit_parameters
from schurfer_analytics.virtual_strategy_report import (
    build_parser,
    build_virtual_report,
    render_json,
    render_markdown,
)


def _inputs() -> tuple[ReplayDataset, ReplayFilters, MarketPath]:
    since = datetime(2026, 7, 26, tzinfo=UTC)
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
    start_ms = ((int(decision_at.timestamp() * 1000) + TIMEFRAME_MS - 1) // TIMEFRAME_MS) * (
        TIMEFRAME_MS
    )
    count = exit_parameters(decision.pump_pct).max_hold_min // 5
    candles = tuple(
        Candle(
            start_ms + index * TIMEFRAME_MS,
            100 if index == 0 else 90,
            100 if index == 0 else 90,
            100 if index == 0 else 90,
            100 if index == 0 else 90,
            1,
        )
        for index in range(count)
    )
    path = MarketPath(42, "binance", "ERA", "complete", candles)
    return dataset, filters, path


def test_report_exposes_versioned_models_costs_and_episode_result() -> None:
    dataset, filters, path = _inputs()
    generated = datetime(2026, 7, 27, tzinfo=UTC)

    report = build_virtual_report(
        dataset,
        filters,
        (path,),
        generated_at=generated,
        code_revision="abc123",
        working_tree_dirty=False,
    )
    markdown = render_markdown(report)
    payload = json.loads(render_json(report))

    assert report.health.completed_replays == 1
    assert report.metrics.mean_net_return_pct == pytest.approx(9.73125)
    assert report.classifications[0].name == "skipped_would_have_won"
    assert "Descriptive baseline replay only" in markdown
    assert "conservative_stop_first" in markdown
    assert payload["manifest"]["entry_model_version"] == "next_complete_5m_open_v1"
    assert payload["manifest"]["replay_engine_version"] == "episode_replay_foundation_v2"
    assert payload["manifest"]["report_version"] == "virtual_strategy_report_v2"
    assert payload["manifest"]["working_tree_dirty"] is False
    assert payload["trades"][0]["classification"] == "skipped_would_have_won"
    assert len(payload["market_paths"][0]["candles"]) == 36


def test_report_keeps_unresolved_paths_visible_and_out_of_metrics() -> None:
    dataset, filters, path = _inputs()
    failed = MarketPath(path.pump_event_id, path.exchange, path.base, "fetch_failed", (), "boom")

    report = build_virtual_report(
        dataset,
        filters,
        (failed,),
        generated_at=datetime(2026, 7, 27, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=True,
    )

    assert report.health.unresolved_replays == 1
    assert report.metrics.mean_net_return_pct is None
    assert report.unresolved_reasons[0].count == 1
    assert report.trades[0].classification == "unresolved"


def test_report_surfaces_input_exclusion_reasons() -> None:
    dataset, filters, path = _inputs()
    source = dataset.decisions[0]
    missing_exchange = replace(
        source,
        row_id=2,
        decision_id="00000000-0000-0000-0000-000000000002",
        pump_event_id=43,
        event_base="BANK",
        base="BANK",
        exchange="",
    )
    dataset_with_exclusion = build_replay_dataset(
        [source, missing_exchange],
        filters,
    )

    report = build_virtual_report(
        dataset_with_exclusion,
        filters,
        (path,),
        generated_at=datetime(2026, 7, 27, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
    )
    markdown = render_markdown(report)
    payload = json.loads(render_json(report))

    assert report.health.eligible_episodes == 1
    assert report.health.excluded_episodes == 1
    assert report.input_exclusion_reasons[0].name == "missing_exchange"
    assert "| missing_exchange | 1 |" in markdown
    assert payload["input_exclusion_reasons"][0] == {
        "count": 1,
        "name": "missing_exchange",
    }


def test_parser_requires_explicit_working_tree_state() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--code-revision", "abc123"])

    args = build_parser().parse_args(["--code-revision", "abc123", "--no-working-tree-dirty"])

    assert args.taker_fee_bps_per_side == 10
    assert args.funding_cost_bps_per_8h == 5
    assert args.working_tree_dirty is False


def test_report_rejects_missing_revision() -> None:
    dataset, filters, path = _inputs()

    with pytest.raises(ValueError, match="revision"):
        build_virtual_report(
            dataset,
            filters,
            (path,),
            generated_at=datetime(2026, 7, 27, tzinfo=UTC),
            code_revision=" ",
            working_tree_dirty=False,
        )


def test_report_rejects_duplicate_market_paths() -> None:
    dataset, filters, path = _inputs()

    with pytest.raises(ValueError, match="duplicate market paths"):
        build_virtual_report(
            dataset,
            filters,
            (path, path),
            generated_at=datetime(2026, 7, 27, tzinfo=UTC),
            code_revision="abc123",
            working_tree_dirty=False,
        )
