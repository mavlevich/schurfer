from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from schurfer_analytics.exit_liquidity_calibration_report import (
    DECISION_SAMPLE_SIZE,
    DIRECTIONAL_SAMPLE_SIZE,
    EXIT_LIQUIDITY_COHORT_START,
    WIDE_SPREAD_UNRELIABLE_THRESHOLD_BPS,
    ExitLiquidityCalibrationReport,
    ExitLiquidityFilters,
    ExitLiquidityRow,
    build_exit_liquidity_calibration_report,
    build_parser,
    render_json,
    render_markdown,
)
from schurfer_analytics.exit_liquidity_calibration_repository import (
    exit_liquidity_statement,
    map_exit_liquidity_row,
)
from sqlalchemy.dialects import postgresql


def _row(
    trade_id: int,
    *,
    exchange: str = "binance",
    symbol: str = "COTI/USDT:USDT",
    modeled: float | None = 5,
    observed: float | None = 8,
    status: str | None = "sampled",
    observation: bool = True,
    observed_spread_bps: float = 4,
) -> ExitLiquidityRow:
    exit_at = EXIT_LIQUIDITY_COHORT_START + timedelta(hours=trade_id)
    return ExitLiquidityRow(
        trade_id=trade_id,
        symbol=symbol,
        exchange=exchange,
        size_usd=50,
        entry_at=exit_at - timedelta(hours=3),
        exit_at=exit_at,
        exit_reason="max_hold",
        modeled_exit_bps=modeled,
        observation_id=trade_id if observation else None,
        observed_at=exit_at - timedelta(seconds=1) if observation else None,
        observation_exchange=exchange if observation else None,
        observation_symbol=symbol if observation else None,
        observation_status=status if observation else None,
        requested_notional_usd=50 if observation else None,
        filled_notional_usd=50 if observation else None,
        observed_spread_bps=observed_spread_bps if observation else None,
        observed_exit_bps=observed if observation else None,
        latency_ms=120 if observation else None,
        error=None,
    )


def _filters(hours: int = 200) -> ExitLiquidityFilters:
    return ExitLiquidityFilters(
        since=EXIT_LIQUIDITY_COHORT_START,
        until=EXIT_LIQUIDITY_COHORT_START + timedelta(hours=hours),
    )


def _report(rows: tuple[ExitLiquidityRow, ...]) -> ExitLiquidityCalibrationReport:
    return build_exit_liquidity_calibration_report(
        rows,
        _filters(),
        generated_at=datetime(2026, 7, 30, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
    )


def test_missing_and_failed_observations_remain_in_denominator() -> None:
    report = _report(
        (
            _row(1),
            _row(2, observation=False),
            _row(3, status="fetch_failed", observed=None),
        )
    )

    assert report.readiness["closed_paper_shorts"] == 3
    assert report.readiness["observations"] == 2
    assert report.readiness["comparable_observations"] == 1
    assert report.readiness["capture_rate_pct"] == pytest.approx(200 / 3)
    assert report.observation_statuses == {
        "fetch_failed": 1,
        "missing_observation": 1,
        "sampled": 1,
    }
    assert report.exclusion_reasons["missing_observation"] == 1
    assert report.exclusion_reasons["status:fetch_failed"] == 1


def test_paired_delta_and_segments_use_only_complete_quotes() -> None:
    report = _report(
        (
            _row(1, modeled=5, observed=8),
            _row(2, exchange="bybit", symbol="AKE/USDT:USDT", modeled=10, observed=4),
        )
    )

    assert report.metrics is not None
    assert report.metrics.mean_modeled_exit_bps == 7.5
    assert report.metrics.mean_observed_exit_bps == 6
    assert report.metrics.mean_delta_bps == -1.5
    assert report.metrics.observed_worse_pct == 50
    assert report.metrics.asset_clusters == 2
    assert {(row.dimension, row.bucket) for row in report.segments} >= {
        ("exchange", "binance"),
        ("exchange", "bybit"),
        ("duration", "180-360m"),
        ("close_spread", "<10bps"),
        ("requested_depth", "<=50usd"),
        ("modeled_impact", "<10bps"),
        ("modeled_impact", "10-20bps"),
    }


def test_execution_cost_unreliable_flags_wide_close_spread_only() -> None:
    """Diagnostic only -- see the module's own docstring. A trade whose
    observed close spread was already at/above the discovery threshold is
    flagged; one comfortably below it is not."""
    report = _report(
        (
            _row(1, observed_spread_bps=WIDE_SPREAD_UNRELIABLE_THRESHOLD_BPS),
            _row(2, observed_spread_bps=WIDE_SPREAD_UNRELIABLE_THRESHOLD_BPS + 10),
            _row(3, observed_spread_bps=WIDE_SPREAD_UNRELIABLE_THRESHOLD_BPS - 1),
        )
    )

    flagged = {row.trade_id: row.execution_cost_unreliable for row in report.comparable_exits}
    assert flagged == {1: True, 2: True, 3: False}
    assert report.metrics is not None
    assert report.metrics.execution_cost_unreliable_count == 2
    assert report.metrics.execution_cost_unreliable_pct == pytest.approx(200 / 3)


def test_execution_cost_unreliable_never_excludes_a_row_from_calibration() -> None:
    """The flag is informational, not a filter -- a wide-spread trade must
    still count toward readiness/metrics/segments like any other
    comparable exit."""
    report = _report((_row(1, observed_spread_bps=WIDE_SPREAD_UNRELIABLE_THRESHOLD_BPS + 5),))

    assert report.readiness["comparable_observations"] == 1
    assert report.metrics is not None
    assert report.metrics.observations == 1


def test_execution_cost_unreliable_appears_in_markdown() -> None:
    report = _report((_row(1, observed_spread_bps=WIDE_SPREAD_UNRELIABLE_THRESHOLD_BPS),))
    markdown = render_markdown(report)

    assert "Execution cost unreliable" in markdown
    assert "never a trading rule" in markdown
    assert "1 of 1" in markdown


@pytest.mark.parametrize(
    ("changed", "reason"),
    [
        ({"observation_symbol": "WRONG/USDT:USDT"}, "identity_mismatch"),
        (
            {"observed_at": EXIT_LIQUIDITY_COHORT_START - timedelta(hours=1)},
            "quote_exit_time_mismatch",
        ),
        ({"requested_notional_usd": 40}, "requested_notional_mismatch"),
        ({"filled_notional_usd": 20}, "insufficient_visible_depth"),
        ({"modeled_exit_bps": float("nan")}, "missing_or_invalid_modeled_impact"),
    ],
)
def test_invalid_pairs_fail_closed(changed: dict[str, object], reason: str) -> None:
    report = _report((replace(_row(1), **cast("Any", changed)),))

    assert report.readiness["comparable_observations"] == 0
    assert report.exclusion_reasons == {reason: 1}


def test_readiness_thresholds_do_not_claim_strategy_change() -> None:
    directional = _report(tuple(_row(index) for index in range(1, DIRECTIONAL_SAMPLE_SIZE + 1)))
    decision = _report(tuple(_row(index) for index in range(1, DECISION_SAMPLE_SIZE + 1)))

    assert directional.readiness["state"] == "directional"
    assert directional.manifest["interpretation"] == "directional_only_no_strategy_change"
    assert decision.readiness["state"] == "decision_ready"
    assert decision.manifest["paper_quote_is_actual_fill"] is False


def test_manifest_and_renderers_state_quote_limit() -> None:
    report = _report((_row(1),))
    payload = json.loads(render_json(report))
    markdown = render_markdown(report)

    assert payload["manifest"]["contract"] == "exit_liquidity_calibration_v1"
    assert payload["manifest"]["delta_definition"].startswith("observed_close_quote")
    assert len(payload["manifest"]["input_fingerprint"]) == 64
    assert "not an actual fill" in markdown
    assert "Positive delta" in markdown


def test_manifest_records_execution_cost_unreliable_provenance() -> None:
    """A reader looking only at the JSON output (not markdown) must still be
    able to see that this diagnostic flag exists, what threshold it used,
    and that it is not a validated production filter."""
    report = _report((_row(1),))
    payload = json.loads(render_json(report))

    assert payload["manifest"]["execution_cost_unreliable_threshold_bps"] == pytest.approx(
        WIDE_SPREAD_UNRELIABLE_THRESHOLD_BPS
    )
    assert (
        payload["manifest"]["execution_cost_unreliable_status"]
        == "discovery_diagnostic_not_forward_validated"
    )
    assert (
        "never a production trading filter"
        in payload["manifest"]["execution_cost_unreliable_provenance"]
    )


def test_filter_contract_and_parser_fail_closed() -> None:
    with pytest.raises(ValueError, match="pre-capture"):
        ExitLiquidityFilters(
            since=EXIT_LIQUIDITY_COHORT_START - timedelta(seconds=1),
            until=EXIT_LIQUIDITY_COHORT_START + timedelta(hours=1),
        )
    with pytest.raises(ValueError, match="earlier"):
        ExitLiquidityFilters(
            since=EXIT_LIQUIDITY_COHORT_START,
            until=EXIT_LIQUIDITY_COHORT_START,
        )
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
    parsed = build_parser().parse_args(["--no-working-tree-dirty"])
    assert parsed.since == EXIT_LIQUIDITY_COHORT_START


def test_repository_statement_keeps_missing_observations_and_maps_row() -> None:
    statement = str(
        exit_liquidity_statement(_filters()).compile(
            dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "LEFT OUTER JOIN app.trade_exit_liquidity_observations" in statement
    assert "jsonb_extract_path_text" in statement
    assert "app.trades.side = 'short'" in statement

    source = {
        "trade_id": 1,
        "symbol": "COTI/USDT:USDT",
        "exchange": "binance",
        "size_usd": 50,
        "entry_at": EXIT_LIQUIDITY_COHORT_START,
        "exit_at": EXIT_LIQUIDITY_COHORT_START + timedelta(hours=3),
        "exit_reason": "max_hold",
        "modeled_exit_bps": 5,
        "observation_id": None,
        "observed_at": None,
        "observation_exchange": None,
        "observation_symbol": None,
        "observation_status": None,
        "requested_notional_usd": None,
        "filled_notional_usd": None,
        "observed_spread_bps": None,
        "observed_exit_bps": None,
        "latency_ms": None,
        "error": None,
    }
    assert map_exit_liquidity_row(source).observation_id is None


def test_malformed_modeled_exit_bps_falls_back_to_none_not_a_crash() -> None:
    """`modeled_exit_bps` comes from jsonb_extract_path_text -- raw JSON
    text, not a schema-validated numeric column. A malformed historical
    ask_impact_bps must not crash the whole report; it becomes None, which
    the report's own missing_or_invalid_modeled_impact exclusion already
    handles (colleague review, 2026-08-24)."""
    source = {
        "trade_id": 1,
        "symbol": "COTI/USDT:USDT",
        "exchange": "binance",
        "size_usd": 50,
        "entry_at": EXIT_LIQUIDITY_COHORT_START,
        "exit_at": EXIT_LIQUIDITY_COHORT_START + timedelta(hours=3),
        "exit_reason": "max_hold",
        "modeled_exit_bps": "not-a-number",
        "observation_id": None,
        "observed_at": None,
        "observation_exchange": None,
        "observation_symbol": None,
        "observation_status": None,
        "requested_notional_usd": None,
        "filled_notional_usd": None,
        "observed_spread_bps": None,
        "observed_exit_bps": None,
        "latency_ms": None,
        "error": None,
    }
    assert map_exit_liquidity_row(source).modeled_exit_bps is None


def test_duplicate_trade_rows_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate trade"):
        _report((_row(1), _row(1)))
