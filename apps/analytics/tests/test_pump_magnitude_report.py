from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.ohlcv import TIMEFRAME_MS, Candle
from schurfer_analytics.pump_magnitude_report import (
    PUMP_MAGNITUDE_COHORT_START,
    PUMP_MAGNITUDE_FLOORS_PCT,
    PUMP_MAGNITUDE_REQUIRED_HORIZONS,
    PUMP_MAGNITUDE_STRATEGY_VERSIONS,
    build_parser,
    build_pump_magnitude_report,
    render_json,
    render_markdown,
)
from schurfer_analytics.replay import (
    ReplayDecision,
    ReplayFilters,
    ReplayOutcome,
    build_replay_dataset,
)
from schurfer_analytics.virtual_market import DecisionMarketPath
from schurfer_analytics.virtual_strategy import MarketPath, expected_path_bounds


def _decision(row_id: int, pump_pct: float, exchange: str) -> ReplayDecision:
    ts = PUMP_MAGNITUDE_COHORT_START + timedelta(minutes=row_id)
    strategy_version = (
        "pump_short_measurement_v1" if row_id == 1 else "pump_short_v1_market_quality"
    )
    return ReplayDecision(
        row_id=row_id,
        decision_id=f"00000000-0000-0000-0000-{row_id:012d}",
        pump_event_id=42,
        event_base="ERA",
        event_first_seen_at=PUMP_MAGNITUDE_COHORT_START,
        event_closed_at=PUMP_MAGNITUDE_COHORT_START + timedelta(hours=8),
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
        outcomes=tuple(
            ReplayOutcome(
                horizon_minutes=horizon,
                status="complete",
                anchor_exchange=exchange,
                source_exchange=exchange,
                entry_price=100,
                forward_price=90,
                mfe_pct=10,
                mae_pct=1,
                short_return_pct=10,
                coverage_ratio=1,
            )
            for horizon in PUMP_MAGNITUDE_REQUIRED_HORIZONS
        ),
    )


def _path(decision: ReplayDecision) -> DecisionMarketPath:
    start_ms, end_ms = expected_path_bounds(decision)
    candles = tuple(
        Candle(
            timestamp,
            100 if timestamp == start_ms else 95,
            100 if timestamp == start_ms else 95,
            95,
            95,
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


def _inputs() -> (
    tuple[
        list[ReplayDecision],
        ReplayFilters,
        tuple[DecisionMarketPath, ...],
    ]
):
    decisions = [
        _decision(1, 20, "binance"),
        _decision(2, 75, "bybit"),
        _decision(3, 160, "okx"),
    ]
    filters = ReplayFilters(
        since=PUMP_MAGNITUDE_COHORT_START,
        until=PUMP_MAGNITUDE_COHORT_START + timedelta(days=2),
        strategy_versions=PUMP_MAGNITUDE_STRATEGY_VERSIONS,
        required_horizons=PUMP_MAGNITUDE_REQUIRED_HORIZONS,
    )
    return decisions, filters, tuple(_path(decision) for decision in decisions)


def test_surface_selects_point_in_time_crossings_and_cash() -> None:
    decisions, filters, paths = _inputs()
    report = build_pump_magnitude_report(
        build_replay_dataset(decisions, filters),
        filters,
        paths,
        generated_at=datetime(2026, 7, 30, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
    )
    results = {row.threshold_key: row for row in report.episode_results}

    assert results["floor_20"].selected_decision_id == decisions[0].decision_id
    assert results["floor_30"].selected_decision_id == decisions[1].decision_id
    assert results["floor_70"].selected_decision_id == decisions[1].decision_id
    assert results["floor_100"].selected_decision_id == decisions[2].decision_id
    assert results["floor_150"].selected_decision_id == decisions[2].decision_id
    assert results["floor_200"].status == "not_triggered"
    assert results["floor_200"].episode_net_return_pct == 0

    metrics = {row.threshold_key: row for row in report.metrics}
    assert metrics["floor_20"].triggered == 1
    assert metrics["floor_20"].fixed_240_resolved_episodes == 1
    assert metrics["floor_20"].fixed_240_episode_gross_return_pct == 10
    assert metrics["floor_200"].cash == 1
    assert metrics["floor_200"].fixed_240_episode_gross_return_pct == 0
    assert metrics["floor_200"].triggered_per_calendar_day == 0


def test_missing_path_is_unresolved_but_fixed_horizon_remains_descriptive() -> None:
    decisions, filters, paths = _inputs()
    report = build_pump_magnitude_report(
        build_replay_dataset(decisions, filters),
        filters,
        paths[1:],
        generated_at=datetime(2026, 7, 30, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=True,
    )
    metrics = {row.threshold_key: row for row in report.metrics}

    assert metrics["floor_20"].resolved_episodes == 0
    assert metrics["floor_20"].unresolved == 1
    assert metrics["floor_20"].fixed_240_resolved_episodes == 1
    assert metrics["floor_20"].fixed_240_episode_gross_return_pct == 10
    assert metrics["floor_200"].resolved_episodes == 1


def test_report_fails_closed_for_invalid_pump_and_duplicate_paths() -> None:
    decisions, filters, paths = _inputs()
    invalid = [replace(decisions[0], pump_pct=float("nan")), *decisions[1:]]
    report = build_pump_magnitude_report(
        build_replay_dataset(invalid, filters),
        filters,
        paths,
        generated_at=datetime(2026, 7, 30, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
    )
    assert all(row.status == "selection_unresolved" for row in report.episode_results)

    with pytest.raises(ValueError, match="duplicate market paths"):
        build_pump_magnitude_report(
            build_replay_dataset(decisions, filters),
            filters,
            (paths[0], paths[0]),
            generated_at=datetime(2026, 7, 30, tzinfo=UTC),
            code_revision="abc123",
            working_tree_dirty=False,
        )

    older_filters = replace(
        filters,
        since=PUMP_MAGNITUDE_COHORT_START - timedelta(minutes=1),
    )
    with pytest.raises(ValueError, match="pre-measurement-split"):
        build_pump_magnitude_report(
            build_replay_dataset(decisions, older_filters),
            older_filters,
            paths,
            generated_at=datetime(2026, 7, 30, tzinfo=UTC),
            code_revision="abc123",
            working_tree_dirty=False,
        )


def test_manifest_and_renderers_lock_discovery_scope() -> None:
    decisions, filters, paths = _inputs()
    report = build_pump_magnitude_report(
        build_replay_dataset(decisions, filters),
        filters,
        paths,
        generated_at=datetime(2026, 7, 30, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
    )
    payload = json.loads(render_json(report))
    markdown = render_markdown(report)

    assert payload["manifest"]["floors_pct"] == list(PUMP_MAGNITUDE_FLOORS_PCT)
    assert payload["manifest"]["interpretation"] == "discovery_only_no_strategy_change"
    assert payload["manifest"]["fixed_horizon_minutes"] == 240
    assert "Discovery-only surface" in markdown
    assert "Fixed 240m episode gross" in markdown
    assert "floor_200" not in markdown
    assert "200.00%" in markdown


def test_parser_defaults_and_requires_tree_state() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--code-revision", "abc123"])
    args = build_parser().parse_args(["--code-revision", "abc123", "--no-working-tree-dirty"])
    assert args.since == PUMP_MAGNITUDE_COHORT_START
    assert args.working_tree_dirty is False
