from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.candle_anomaly_features import (
    CANDLE_ANOMALY_COHORT_START,
    FORMATION_BARS,
    WARMUP_BARS,
    candle_anomaly_path_bounds,
    feature_window_bounds,
)
from schurfer_analytics.candle_anomaly_report import (
    CANDLE_ANOMALY_BUCKETS,
    CANDLE_ANOMALY_STRATEGY_VERSIONS,
    ReportWindowNotStartedError,
    build_candle_anomaly_report,
    build_parser,
    render_json,
    render_markdown,
    resolve_report_until,
)
from schurfer_analytics.ohlcv import TIMEFRAME_MS, Candle
from schurfer_analytics.replay import (
    ReplayDataset,
    ReplayDecision,
    ReplayFilters,
    ReplayOutcome,
    build_replay_dataset,
)
from schurfer_analytics.virtual_strategy import MarketPath, expected_path_bounds


def _decision() -> ReplayDecision:
    ts = CANDLE_ANOMALY_COHORT_START + timedelta(hours=12, minutes=3)
    return ReplayDecision(
        row_id=1,
        decision_id="00000000-0000-0000-0000-000000000001",
        pump_event_id=42,
        event_base="ERA",
        event_first_seen_at=ts - timedelta(hours=1),
        event_closed_at=ts + timedelta(hours=7),
        ts=ts,
        base="ERA",
        exchange="binance",
        action="skipped",
        reason="score 5 < threshold 6",
        score=5,
        pump_pct=40,
        price=100,
        strategy_version="pump_short_v1_market_quality",
        features={
            "signal": {"computed_at": ts.timestamp()},
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
            "quality": {"allowed": True, "depth_target_usd": 100},
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


def _filters() -> ReplayFilters:
    return ReplayFilters(
        since=CANDLE_ANOMALY_COHORT_START,
        until=CANDLE_ANOMALY_COHORT_START + timedelta(days=1),
        strategy_versions=CANDLE_ANOMALY_STRATEGY_VERSIONS,
    )


def _path(decision: ReplayDecision) -> MarketPath:
    start_ms, end_ms = candle_anomaly_path_bounds(decision)
    feature_start_ms, feature_end_ms = feature_window_bounds(decision)
    closes = [100.0] * (WARMUP_BARS + FORMATION_BARS)
    first_jump = WARMUP_BARS + 100
    second_jump = first_jump + 1
    closes[first_jump] = 110
    closes[second_jump:] = [120] * (len(closes) - second_jump)
    closes[-1] = 110
    feature_by_timestamp: dict[int, Candle] = {}
    previous_close = 100.0
    for index, close in enumerate(closes):
        open_ = previous_close
        feature_by_timestamp[feature_start_ms + index * TIMEFRAME_MS] = Candle(
            feature_start_ms + index * TIMEFRAME_MS,
            open_,
            max(open_, close) + 0.5,
            min(open_, close) - 0.5,
            close,
            1,
        )
        previous_close = close
    assert max(feature_by_timestamp) + TIMEFRAME_MS == feature_end_ms

    entry_start_ms, _ = expected_path_bounds(decision)
    candles: list[Candle] = []
    for timestamp in range(start_ms, end_ms, TIMEFRAME_MS):
        candle = feature_by_timestamp.get(timestamp)
        if candle is not None:
            candles.append(candle)
        elif timestamp >= entry_start_ms:
            candles.append(Candle(timestamp, 100, 100, 90, 90, 1))
        else:
            candles.append(Candle(timestamp, 100, 100.5, 99.5, 100, 1))
    return MarketPath(42, "binance", "ERA", "complete", tuple(candles))


def _inputs() -> tuple[ReplayDataset, ReplayFilters, ReplayDecision, MarketPath]:
    decision = _decision()
    filters = _filters()
    dataset = build_replay_dataset([decision], filters)
    return dataset, filters, decision, _path(decision)


def test_report_joins_predecision_features_with_locked_virtual_trade() -> None:
    dataset, filters, decision, path = _inputs()

    report = build_candle_anomaly_report(
        dataset,
        filters,
        (path,),
        generated_at=datetime(2026, 7, 30, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
    )
    row = report.episodes[0]
    bucket = next(item for item in report.buckets if item.bucket == row.features.bucket)

    assert row.decision_id == decision.decision_id
    assert row.features.bucket == "blow_off__strong_reversal"
    assert row.trade_status == "complete"
    assert row.net_return_pct is not None
    assert bucket.feature_episodes == 1
    assert bucket.resolved_trades == 1
    assert bucket.asset_clusters == 1
    assert bucket.largest_cluster_share_pct == 100
    assert tuple(item.bucket for item in report.buckets) == CANDLE_ANOMALY_BUCKETS


def test_missing_predecision_candle_does_not_invalidate_forward_trade() -> None:
    dataset, filters, _, path = _inputs()
    feature_start_ms, _ = feature_window_bounds(_decision())
    missing_context = replace(
        path,
        candles=tuple(candle for candle in path.candles if candle.ts_ms != feature_start_ms),
    )

    report = build_candle_anomaly_report(
        dataset,
        filters,
        (missing_context,),
        generated_at=datetime(2026, 7, 30, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
    )

    assert report.episodes[0].trade_status == "complete"
    assert report.episodes[0].features.status == "unresolved"
    assert "missing required candle" in (report.episodes[0].features.error or "")
    assert all(bucket.feature_episodes == 0 for bucket in report.buckets)


def test_manifest_and_renderers_expose_descriptive_boundary() -> None:
    dataset, filters, _, path = _inputs()

    report = build_candle_anomaly_report(
        dataset,
        filters,
        (path,),
        generated_at=datetime(2026, 7, 30, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
    )
    payload = json.loads(render_json(report))
    markdown = render_markdown(report)

    assert payload["manifest"]["feature_version"] == "candle_anomaly_features_v1"
    assert payload["manifest"]["formation_bars"] == 288
    assert payload["manifest"]["report_scope"] == "descriptive_feature_research_no_promotion"
    assert payload["baseline"]["manifest"]["working_tree_dirty"] is False
    assert "Descriptive feature research only" in markdown
    assert "blow_off__strong_reversal" in markdown
    assert "Top-2 share" in markdown


def test_report_rejects_changed_cohort_or_strategy() -> None:
    dataset, filters, _, path = _inputs()
    changed_since = replace(
        filters,
        since=CANDLE_ANOMALY_COHORT_START + timedelta(minutes=1),
    )
    changed_strategy = replace(filters, strategy_versions=("other",))

    with pytest.raises(ValueError, match="registered cohort start"):
        build_candle_anomaly_report(
            dataset,
            changed_since,
            (path,),
            generated_at=datetime(2026, 7, 30, tzinfo=UTC),
            code_revision="abc123",
            working_tree_dirty=False,
        )
    with pytest.raises(ValueError, match="registered strategy cohort"):
        build_candle_anomaly_report(
            dataset,
            changed_strategy,
            (path,),
            generated_at=datetime(2026, 7, 30, tzinfo=UTC),
            code_revision="abc123",
            working_tree_dirty=False,
        )


def test_parser_defaults_to_registered_cohort_and_requires_tree_state() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--code-revision", "abc123"])

    args = build_parser().parse_args(["--code-revision", "abc123", "--no-working-tree-dirty"])

    assert args.since == CANDLE_ANOMALY_COHORT_START
    assert args.working_tree_dirty is False


def test_report_window_rejects_cutoff_before_registered_cohort() -> None:
    before_start = CANDLE_ANOMALY_COHORT_START - timedelta(seconds=1)

    with pytest.raises(ReportWindowNotStartedError, match="cohort starts"):
        resolve_report_until(None, before_start)
    with pytest.raises(ReportWindowNotStartedError, match="cohort starts"):
        resolve_report_until(CANDLE_ANOMALY_COHORT_START, before_start)

    after_start = CANDLE_ANOMALY_COHORT_START + timedelta(seconds=1)
    assert resolve_report_until(None, after_start) == after_start
