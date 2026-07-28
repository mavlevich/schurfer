from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import timedelta

import pytest
import schurfer_analytics.virtual_score_challenger_report as score_report
from schurfer_analytics.ohlcv import TIMEFRAME_MS, Candle
from schurfer_analytics.replay import (
    ReplayDataset,
    ReplayDecision,
    ReplayFilters,
    ReplayOutcome,
    build_replay_dataset,
)
from schurfer_analytics.reporting import ReportWindowNotStartedError
from schurfer_analytics.virtual_market import DecisionMarketPath
from schurfer_analytics.virtual_score_challenger_report import (
    SCORE_THRESHOLD_COHORT_START,
    SCORE_THRESHOLD_INFERENCE_VERSION,
    SCORE_THRESHOLD_STRATEGY_VERSIONS,
    build_parser,
    build_score_threshold_report,
    render_json,
    render_markdown,
)
from schurfer_analytics.virtual_strategy import MarketPath, expected_path_bounds


def _decision(
    row_id: int,
    score: int,
    exchange: str,
    *,
    action: str = "skipped",
) -> ReplayDecision:
    ts = SCORE_THRESHOLD_COHORT_START + timedelta(minutes=row_id)
    return ReplayDecision(
        row_id=row_id,
        decision_id=f"00000000-0000-0000-0000-{row_id:012d}",
        pump_event_id=42,
        event_base="ERA",
        event_first_seen_at=SCORE_THRESHOLD_COHORT_START,
        event_closed_at=SCORE_THRESHOLD_COHORT_START + timedelta(hours=7),
        ts=ts,
        base="ERA",
        exchange=exchange,
        action=action,
        reason="measurement",
        score=score,
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
    include_baseline: bool = True,
) -> tuple[
    ReplayDataset,
    ReplayFilters,
    tuple[ReplayDecision, ...],
    tuple[DecisionMarketPath, ...],
]:
    low = _decision(1, 4, "binance")
    baseline = _decision(
        2,
        6,
        "bybit",
    )
    decisions = (low, baseline) if include_baseline else (low,)
    filters = ReplayFilters(
        since=SCORE_THRESHOLD_COHORT_START,
        until=SCORE_THRESHOLD_COHORT_START + timedelta(days=1),
        strategy_versions=SCORE_THRESHOLD_STRATEGY_VERSIONS,
    )
    dataset = build_replay_dataset(list(decisions), filters)
    return dataset, filters, decisions, tuple(_path(decision) for decision in decisions)


def test_report_selects_point_in_time_score_crossings() -> None:
    dataset, filters, decisions, paths = _inputs()

    report = build_score_threshold_report(
        dataset,
        filters,
        paths,
        generated_at=SCORE_THRESHOLD_COHORT_START + timedelta(days=1),
        code_revision="abc123",
        working_tree_dirty=False,
    )
    by_key = {result.policy_key: result for result in report.episode_results}

    assert by_key["score_4"].selected_decision_id == decisions[0].decision_id
    assert by_key["score_4"].exchange == "binance"
    assert by_key["score_5"].selected_decision_id == decisions[1].decision_id
    assert by_key["score_6"].selected_decision_id == decisions[1].decision_id
    assert by_key["score_6"].exchange == "bybit"
    assert report.inference.readiness.status == "collecting"
    assert all(row.episodes == 1 for row in report.paired_comparisons)


def test_threshold_never_reached_is_zero_return_cash() -> None:
    dataset, filters, _, paths = _inputs(include_baseline=False)

    report = build_score_threshold_report(
        dataset,
        filters,
        paths,
        generated_at=SCORE_THRESHOLD_COHORT_START + timedelta(days=1),
        code_revision="abc123",
        working_tree_dirty=False,
    )
    by_key = {result.policy_key: result for result in report.episode_results}

    assert by_key["score_4"].status == "complete"
    assert by_key["score_5"].status == "not_triggered"
    assert by_key["score_5"].episode_net_return_pct == 0
    assert by_key["score_6"].status == "not_triggered"
    assert by_key["score_6"].episode_net_return_pct == 0


def test_missing_selected_path_is_unresolved_without_changing_cash() -> None:
    dataset, filters, _, _ = _inputs(include_baseline=False)

    report = build_score_threshold_report(
        dataset,
        filters,
        (),
        generated_at=SCORE_THRESHOLD_COHORT_START + timedelta(days=1),
        code_revision="abc123",
        working_tree_dirty=True,
    )
    by_key = {result.policy_key: result for result in report.episode_results}

    assert by_key["score_4"].status == "market_path_unavailable"
    assert by_key["score_4"].episode_net_return_pct is None
    assert by_key["score_5"].status == "not_triggered"
    assert by_key["score_5"].episode_net_return_pct == 0


def test_manifest_locks_family_and_renders_guardrails() -> None:
    dataset, filters, _, paths = _inputs()
    report = build_score_threshold_report(
        dataset,
        filters,
        paths,
        generated_at=SCORE_THRESHOLD_COHORT_START + timedelta(days=1),
        code_revision="abc123",
        working_tree_dirty=False,
    )

    payload = json.loads(render_json(report))
    markdown = render_markdown(report)

    assert payload["manifest"]["baseline"] == {"key": "score_6", "min_score": 6}
    assert [row["min_score"] for row in payload["manifest"]["challengers"]] == [4, 5]
    assert payload["manifest"]["inference_version"] == SCORE_THRESHOLD_INFERENCE_VERSION
    assert payload["manifest"]["working_tree_dirty"] is False
    assert "Formal inference status: `collecting`" in markdown
    assert "never changes production score settings" in markdown


def test_report_rejects_changed_cohort_strategy_and_duplicate_paths() -> None:
    dataset, filters, _, paths = _inputs()

    with pytest.raises(ValueError, match="registered cohort start"):
        build_score_threshold_report(
            dataset,
            replace(filters, since=SCORE_THRESHOLD_COHORT_START + timedelta(minutes=1)),
            paths,
            generated_at=SCORE_THRESHOLD_COHORT_START + timedelta(days=1),
            code_revision="abc123",
            working_tree_dirty=False,
        )
    with pytest.raises(ValueError, match="registered strategy cohort"):
        build_score_threshold_report(
            dataset,
            replace(filters, strategy_versions=("pump_short_measurement_v1",)),
            paths,
            generated_at=SCORE_THRESHOLD_COHORT_START + timedelta(days=1),
            code_revision="abc123",
            working_tree_dirty=False,
        )
    with pytest.raises(ValueError, match="duplicate market paths"):
        build_score_threshold_report(
            dataset,
            filters,
            (paths[0], paths[0]),
            generated_at=SCORE_THRESHOLD_COHORT_START + timedelta(days=1),
            code_revision="abc123",
            working_tree_dirty=False,
        )


def test_parser_defaults_and_main_render_precohort_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--code-revision", "abc123"])

    args = build_parser().parse_args(["--code-revision", "abc123", "--no-working-tree-dirty"])
    assert args.since == SCORE_THRESHOLD_COHORT_START
    assert args.working_tree_dirty is False

    async def fail(_args: object) -> str:
        raise ReportWindowNotStartedError("cohort starts later")

    monkeypatch.setattr(score_report, "_run", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "virtual-score-challenger-report",
            "--code-revision",
            "abc123",
            "--no-working-tree-dirty",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        score_report.main()

    captured = capsys.readouterr()
    assert exc.value.code == 2
    assert "cohort starts later" in captured.err
    assert "Traceback" not in captured.err
