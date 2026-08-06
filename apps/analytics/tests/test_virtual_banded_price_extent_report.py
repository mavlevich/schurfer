from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import timedelta

import pytest
import schurfer_analytics.virtual_banded_price_extent_report as banded_report
from schurfer_analytics.ohlcv import TIMEFRAME_MS, Candle
from schurfer_analytics.replay import (
    ReplayDataset,
    ReplayDecision,
    ReplayFilters,
    ReplayOutcome,
    build_replay_dataset,
)
from schurfer_analytics.reporting import ReportWindowNotStartedError
from schurfer_analytics.virtual_banded_price_extent_report import (
    BANDED_PRICE_EXTENT_COHORT_START,
    BANDED_PRICE_EXTENT_INFERENCE_VERSION,
    BANDED_PRICE_EXTENT_STRATEGY_VERSIONS,
    build_banded_price_extent_report,
    build_parser,
    render_json,
    render_markdown,
)
from schurfer_analytics.virtual_market import DecisionMarketPath
from schurfer_analytics.virtual_strategy import MarketPath, expected_path_bounds


def _components(price_extent_points: int, price_extent_value: float) -> dict[str, object]:
    names = ("pump_age", "price_extent", "oi_trend", "funding_rate", "retrace_from_peak")
    return {
        name: {
            "value": price_extent_value if name == "price_extent" else 1.0,
            "points": price_extent_points if name == "price_extent" else 1,
            "max": 2,
            "note": "",
        }
        for name in names
    }


def _decision(
    row_id: int,
    *,
    price_extent_points: int,
    price_extent_value: float,
    exchange: str,
    action: str = "skipped",
) -> ReplayDecision:
    """A decision whose live score is exactly 6 (baseline-crossing), built from
    named components so both the live score_6 policy and the banded challenger can
    be evaluated deterministically from the same underlying raw price_extent value.
    """
    ts = BANDED_PRICE_EXTENT_COHORT_START + timedelta(minutes=row_id)
    # pump_age + oi_trend + funding_rate + retrace_from_peak are all fixed at 1
    # point each in _components(); price_extent is the only variable term.
    live_score = 4 + price_extent_points
    return ReplayDecision(
        row_id=row_id,
        decision_id=f"00000000-0000-0000-0000-{row_id:012d}",
        pump_event_id=42,
        event_base="ERA",
        event_first_seen_at=BANDED_PRICE_EXTENT_COHORT_START,
        event_closed_at=BANDED_PRICE_EXTENT_COHORT_START + timedelta(hours=7),
        ts=ts,
        base="ERA",
        exchange=exchange,
        action=action,
        reason="measurement",
        score=live_score,
        pump_pct=40,
        price=100,
        strategy_version="pump_short_v1_market_quality",
        features={
            "signal": {
                "computed_at": ts.timestamp(),
                "components": _components(price_extent_points, price_extent_value),
                "data_quality": {"oi": True, "funding": True},
            },
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
    decisions: tuple[ReplayDecision, ...],
) -> tuple[ReplayDataset, ReplayFilters, tuple[DecisionMarketPath, ...]]:
    filters = ReplayFilters(
        since=BANDED_PRICE_EXTENT_COHORT_START,
        until=BANDED_PRICE_EXTENT_COHORT_START + timedelta(days=1),
        strategy_versions=BANDED_PRICE_EXTENT_STRATEGY_VERSIONS,
    )
    dataset = build_replay_dataset(list(decisions), filters)
    return dataset, filters, tuple(_path(decision) for decision in decisions)


def test_banded_challenger_recomputes_score_from_raw_value() -> None:
    """A pump extended to 150% scores price_extent=2 live (score 6, crosses
    baseline) but banded_price_extent_points(150) == 0, so the challenger drops
    below 6 and should show not_triggered while baseline still selects it."""
    huge_pump = _decision(1, price_extent_points=2, price_extent_value=150.0, exchange="binance")
    dataset, filters, paths = _inputs((huge_pump,))

    report = build_banded_price_extent_report(
        dataset,
        filters,
        paths,
        generated_at=BANDED_PRICE_EXTENT_COHORT_START + timedelta(days=1),
        code_revision="abc123",
        working_tree_dirty=False,
    )
    by_key = {result.policy_key: result for result in report.episode_results}

    assert by_key["score_6"].selected_decision_id == huge_pump.decision_id
    assert by_key["score_6_with_banded_price_extent"].status == "not_triggered"


def test_banded_challenger_rescues_a_sweet_spot_pump_below_baseline() -> None:
    """A 30% pump only scores price_extent=1 live (score 5, does NOT cross
    baseline), but banded_price_extent_points(30) == 2, lifting the banded score
    to 6 — the challenger should trigger while baseline does not."""
    sweet_spot_pump = _decision(2, price_extent_points=1, price_extent_value=30.0, exchange="bybit")
    dataset, filters, paths = _inputs((sweet_spot_pump,))

    report = build_banded_price_extent_report(
        dataset,
        filters,
        paths,
        generated_at=BANDED_PRICE_EXTENT_COHORT_START + timedelta(days=1),
        code_revision="abc123",
        working_tree_dirty=False,
    )
    by_key = {result.policy_key: result for result in report.episode_results}

    assert by_key["score_6"].status == "not_triggered"
    assert (
        by_key["score_6_with_banded_price_extent"].selected_decision_id
        == sweet_spot_pump.decision_id
    )


def test_manifest_locks_bands_and_forward_cohort() -> None:
    dataset, filters, paths = _inputs(
        (_decision(1, price_extent_points=1, price_extent_value=30.0, exchange="bybit"),)
    )
    report = build_banded_price_extent_report(
        dataset,
        filters,
        paths,
        generated_at=BANDED_PRICE_EXTENT_COHORT_START + timedelta(days=1),
        code_revision="abc123",
        working_tree_dirty=False,
    )

    payload = json.loads(render_json(report))
    markdown = render_markdown(report)

    assert payload["manifest"]["baseline"] == {"key": "score_6", "min_score": 6}
    assert payload["manifest"]["challenger"] == {
        "key": "score_6_with_banded_price_extent",
        "min_score": 6,
    }
    assert payload["manifest"]["sweet_spot_low_pct"] == 25.0
    assert payload["manifest"]["sweet_spot_high_pct"] == 40.0
    assert payload["manifest"]["moderate_low_pct"] == 15.0
    assert payload["manifest"]["moderate_high_pct"] == 60.0
    assert payload["manifest"]["inference_version"] == BANDED_PRICE_EXTENT_INFERENCE_VERSION
    assert payload["manifest"]["dataset_since"].startswith("2026-08-06")
    assert "## Registered bands" in markdown
    assert "never changes production score settings" in markdown


def test_report_rejects_any_cohort_start_other_than_the_registered_forward_date() -> None:
    """The whole point of this report is that it cannot be pointed backward at the
    window the hypothesis was invented from — enforce an exact match, not merely
    'not earlier than', matching every other locked-cohort report in this repo."""
    dataset, filters, paths = _inputs(
        (_decision(1, price_extent_points=1, price_extent_value=30.0, exchange="bybit"),)
    )

    with pytest.raises(ValueError, match="registered forward cohort start"):
        build_banded_price_extent_report(
            dataset,
            replace(filters, since=BANDED_PRICE_EXTENT_COHORT_START - timedelta(days=1)),
            paths,
            generated_at=BANDED_PRICE_EXTENT_COHORT_START + timedelta(days=1),
            code_revision="abc123",
            working_tree_dirty=False,
        )
    with pytest.raises(ValueError, match="registered forward cohort start"):
        build_banded_price_extent_report(
            dataset,
            replace(filters, since=BANDED_PRICE_EXTENT_COHORT_START + timedelta(minutes=1)),
            paths,
            generated_at=BANDED_PRICE_EXTENT_COHORT_START + timedelta(days=1),
            code_revision="abc123",
            working_tree_dirty=False,
        )
    with pytest.raises(ValueError, match="registered strategy cohort"):
        build_banded_price_extent_report(
            dataset,
            replace(filters, strategy_versions=("pump_short_measurement_v1",)),
            paths,
            generated_at=BANDED_PRICE_EXTENT_COHORT_START + timedelta(days=1),
            code_revision="abc123",
            working_tree_dirty=False,
        )
    with pytest.raises(ValueError, match="duplicate market paths"):
        build_banded_price_extent_report(
            dataset,
            filters,
            (paths[0], paths[0]),
            generated_at=BANDED_PRICE_EXTENT_COHORT_START + timedelta(days=1),
            code_revision="abc123",
            working_tree_dirty=False,
        )


def test_parser_defaults_to_the_forward_cohort_and_main_renders_precohort_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--code-revision", "abc123"])

    args = build_parser().parse_args(["--code-revision", "abc123", "--no-working-tree-dirty"])
    assert args.since == BANDED_PRICE_EXTENT_COHORT_START
    assert args.working_tree_dirty is False

    async def fail(_args: object) -> str:
        raise ReportWindowNotStartedError("cohort starts later")

    monkeypatch.setattr(banded_report, "_run", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "virtual-banded-price-extent-report",
            "--code-revision",
            "abc123",
            "--no-working-tree-dirty",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        banded_report.main()

    captured = capsys.readouterr()
    assert exc.value.code == 2
    assert "cohort starts later" in captured.err
    assert "Traceback" not in captured.err
