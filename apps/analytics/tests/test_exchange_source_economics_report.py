from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from schurfer_analytics.exchange_coverage_report import SourceObservation
from schurfer_analytics.exchange_source_economics_report import (
    REPORT_VERSION,
    SOURCE_ECONOMICS_COHORT_START,
    SOURCE_ECONOMICS_HORIZONS,
    build_exchange_source_economics_report,
    build_source_attributions,
    render_json,
    render_markdown,
    source_input_fingerprint,
)
from schurfer_analytics.liquid_taker_report import LIQUID_TAKER_STRATEGY_VERSIONS
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
    event_id: int,
    base: str,
    score: int = 6,
    exchange: str = "binance",
) -> ReplayDecision:
    decision_at = SOURCE_ECONOMICS_COHORT_START + timedelta(minutes=10 + row_id)
    outcomes = tuple(
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
        for horizon in SOURCE_ECONOMICS_HORIZONS
    )
    return ReplayDecision(
        row_id=row_id,
        decision_id=f"00000000-0000-0000-0000-{row_id:012d}",
        pump_event_id=event_id,
        event_base=base,
        event_first_seen_at=SOURCE_ECONOMICS_COHORT_START,
        event_closed_at=SOURCE_ECONOMICS_COHORT_START + timedelta(hours=10),
        ts=decision_at,
        base=base,
        exchange=exchange,
        action="skipped",
        reason="measurement",
        score=score,
        pump_pct=40,
        price=100,
        strategy_version=LIQUID_TAKER_STRATEGY_VERSIONS[0],
        features={
            "signal": {"computed_at": decision_at.timestamp()},
            "config": {
                "score_threshold": 6,
                "require_market_quality": True,
                "signal_position_usd": 50,
            },
        },
        liquidity={
            "status": "sampled",
            "bid_impact_bps": {"100": 3, "500": 8},
            "ask_impact_bps": {"100": 4, "500": 9},
            "quality": {"allowed": True, "depth_target_usd": 100},
        },
        outcomes=outcomes,
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
        decision.decision_id or "",
        MarketPath(
            pump_event_id=decision.pump_event_id or 0,
            exchange=decision.exchange,
            base=decision.base,
            status="complete",
            candles=candles,
        ),
    )


def _inputs(
    decisions: tuple[ReplayDecision, ...],
) -> tuple[ReplayDataset, ReplayFilters]:
    filters = ReplayFilters(
        since=SOURCE_ECONOMICS_COHORT_START,
        until=SOURCE_ECONOMICS_COHORT_START + timedelta(days=1),
        strategy_versions=LIQUID_TAKER_STRATEGY_VERSIONS,
        required_horizons=SOURCE_ECONOMICS_HORIZONS,
    )
    return build_replay_dataset(list(decisions), filters), filters


def test_source_attribution_is_order_independent_and_preserves_ties() -> None:
    start = SOURCE_ECONOMICS_COHORT_START
    rows = [
        SourceObservation(42, "Bybit", start + timedelta(seconds=5)),
        SourceObservation(42, "mexc", start),
        SourceObservation(42, "BINANCE", start),
    ]

    attribution = build_source_attributions(rows)[42]

    assert attribution.first_source_key == "binance+mexc"
    assert attribution.first_sources == ("binance", "mexc")
    assert attribution.source_count == 3
    assert attribution.next_confirmation_delay_seconds == 5
    assert source_input_fingerprint(rows) == source_input_fingerprint(list(reversed(rows)))
    normalized = [replace(row, exchange=row.exchange.strip().lower()) for row in rows]
    assert source_input_fingerprint(rows) == source_input_fingerprint(normalized)


def test_source_attribution_rejects_duplicate_event_exchange() -> None:
    row = SourceObservation(42, "mexc", SOURCE_ECONOMICS_COHORT_START)

    with pytest.raises(ValueError, match="duplicate source attribution"):
        build_source_attributions([row, replace(row, first_seen_at=row.first_seen_at)])


def test_report_separates_source_and_execution_and_replays_full_exit() -> None:
    selected = _decision(1, event_id=42, base="EDGE")
    cash = _decision(2, event_id=43, base="CASH", score=5)
    dataset, filters = _inputs((selected, cash))
    sources = [
        SourceObservation(42, "mexc", SOURCE_ECONOMICS_COHORT_START),
        SourceObservation(
            42,
            "binance",
            SOURCE_ECONOMICS_COHORT_START + timedelta(minutes=5),
        ),
        SourceObservation(43, "gate", SOURCE_ECONOMICS_COHORT_START),
    ]

    report = build_exchange_source_economics_report(
        dataset,
        filters,
        sources,
        total_source_scope_episodes=2,
        paths=(_path(selected),),
        generated_at=filters.until,
        code_revision="abc123",
        working_tree_dirty=False,
        bootstrap_iterations=100,
    )
    by_source = {row.first_source: row for row in report.source_economics}
    mexc = by_source["mexc"]
    gate = by_source["gate"]

    assert report.manifest.report_version == REPORT_VERSION
    assert mexc.episodes == 1
    assert mexc.selected == 1
    assert mexc.completed_trades == 1
    assert mexc.fixed_240_mean_net_pct == pytest.approx(9.705)
    assert mexc.fixed_480_mean_net_pct == pytest.approx(9.68)
    assert mexc.mean_trade_net_pct is not None
    assert gate.cash == 1
    assert gate.mean_episode_net_pct == 0
    assert report.source_execution_routes[0].first_source == "mexc"
    assert report.source_execution_routes[0].execution_exchange == "binance"
    selected_result = next(row for row in report.episode_results if row.pump_event_id == 42)
    assert selected_result.sources_at_decision == 2
    assert selected_result.next_confirmation_delay_seconds == 300
    assert selected_result.source_status == "attributed"

    markdown = render_markdown(report)
    payload = json.loads(render_json(report))
    assert "Post-hoc discovery only" in markdown
    assert "Source to execution routes" in markdown
    assert payload["manifest"]["sole_source_policy"].startswith("coverage_counterfactual")


def test_report_rejects_left_censored_source_window() -> None:
    decision = _decision(1, event_id=42, base="EDGE")
    dataset, filters = _inputs((decision,))
    early_filters = replace(
        filters,
        since=SOURCE_ECONOMICS_COHORT_START - timedelta(seconds=1),
    )

    with pytest.raises(ValueError, match="left-censored"):
        build_exchange_source_economics_report(
            dataset,
            early_filters,
            [],
            1,
            (_path(decision),),
            generated_at=filters.until,
            code_revision="abc123",
            working_tree_dirty=False,
            bootstrap_iterations=100,
        )


def test_report_rejects_source_observation_at_exclusive_cutoff() -> None:
    decision = _decision(1, event_id=42, base="EDGE")
    dataset, filters = _inputs((decision,))

    with pytest.raises(ValueError, match="exclusive report cutoff"):
        build_exchange_source_economics_report(
            dataset,
            filters,
            [SourceObservation(42, "mexc", filters.until)],
            1,
            (_path(decision),),
            generated_at=filters.until,
            code_revision="abc123",
            working_tree_dirty=False,
            bootstrap_iterations=100,
        )


def test_report_rejects_too_few_bootstrap_iterations() -> None:
    decision = _decision(1, event_id=42, base="EDGE")
    dataset, filters = _inputs((decision,))

    with pytest.raises(ValueError, match="at least 100"):
        build_exchange_source_economics_report(
            dataset,
            filters,
            [],
            1,
            (_path(decision),),
            generated_at=filters.until,
            code_revision="abc123",
            working_tree_dirty=False,
            bootstrap_iterations=99,
        )


def test_report_quarantines_source_observed_after_selected_decision() -> None:
    decision = _decision(1, event_id=42, base="EDGE")
    dataset, filters = _inputs((decision,))
    late_source = SourceObservation(
        42,
        "mexc",
        decision.ts + timedelta(seconds=1),
    )

    report = build_exchange_source_economics_report(
        dataset,
        filters,
        [late_source],
        1,
        (_path(decision),),
        generated_at=filters.until,
        code_revision="abc123",
        working_tree_dirty=False,
        bootstrap_iterations=100,
    )

    assert report.episode_results[0].source_status == "source_after_decision"
    assert report.episode_results[0].first_source_key == "<source_after_decision>"
    assert all(row.first_source != "mexc" for row in report.source_economics)
