from datetime import UTC, datetime

import pytest
from schurfer_analytics.measurement_report import (
    CohortRow,
    CoverageRow,
    DatasetHealth,
    MeasurementReport,
    PerformanceRow,
    QualityReasonRow,
    ReportFilters,
    parse_datetime,
    render_json,
    render_markdown,
)


def _report() -> MeasurementReport:
    generated = datetime(2026, 7, 22, 18, 0, tzinfo=UTC)
    return MeasurementReport(
        generated_at=generated,
        filters=ReportFilters(
            since=datetime(2026, 7, 22, tzinfo=UTC),
            strategy_versions=("pump_short_v1_market_quality",),
        ),
        health=DatasetHealth(
            total_decisions=10,
            first_decision_at=datetime(2026, 7, 22, 10, 0, tzinfo=UTC),
            last_decision_at=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
            observation_hours=2.0,
            decisions_per_hour=5.0,
            unique_episodes=3,
            direct_episode_ids_present_pct=90.0,
            decision_ids_present_pct=100.0,
            prices_present_pct=90.0,
            features_present_pct=100.0,
            signal_present_pct=90.0,
            liquidity_present_pct=100.0,
            liquidity_sampled_pct=80.0,
            sampled_contract_size_present_pct=100.0,
            liquidity_fetch_failed_pct=10.0,
            liquidity_no_exchange_pct=10.0,
            quality_present_pct=80.0,
            signal_lag_samples=9,
            signal_lag_avg_seconds=2.2,
            signal_lag_p50_seconds=2.0,
            signal_lag_p95_seconds=4.5,
        ),
        cohorts=(
            CohortRow(
                strategy_version="pump_short_v1_market_quality",
                decisions=10,
                episodes=3,
                taken=2,
                skipped=8,
                first_decision_at=datetime(2026, 7, 22, 10, 0, tzinfo=UTC),
                last_decision_at=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
            ),
        ),
        quality_reasons=(
            QualityReasonRow("pump_short_v1_market_quality", "ok", 7),
            QualityReasonRow(
                "pump_short_v1_market_quality",
                "market_quality_spread_too_wide",
                3,
            ),
        ),
        coverage=(
            CoverageRow("pump_short_v1_market_quality", 60, "complete", 8),
            CoverageRow("pump_short_v1_market_quality", 60, "unresolved", 2),
        ),
        performance=(
            PerformanceRow(
                strategy_version="pump_short_v1_market_quality",
                horizon_minutes=60,
                segment="taken",
                exchange=None,
                decisions=2,
                episodes=2,
                exact_venue=2,
                fallback_venue=0,
                avg_short_return_pct=4.25,
                median_short_return_pct=4.0,
                win_rate_pct=100.0,
                avg_mfe_pct=7.0,
                avg_mae_pct=2.0,
            ),
        ),
        exchange_performance=(
            PerformanceRow(
                strategy_version="pump_short_v1_market_quality",
                horizon_minutes=60,
                segment="taken",
                exchange="gate",
                decisions=2,
                episodes=2,
                exact_venue=2,
                fallback_venue=0,
                avg_short_return_pct=4.25,
                median_short_return_pct=4.0,
                win_rate_pct=100.0,
                avg_mfe_pct=7.0,
                avg_mae_pct=2.0,
            ),
        ),
    )


def test_markdown_report_labels_descriptive_episode_level_limit() -> None:
    rendered = render_markdown(_report())

    assert "# Decision Measurement Report" in rendered
    assert "Decisions inside one pump episode are" in rendered
    assert "pump_short_v1_market_quality" in rendered
    assert "market_quality_spread_too_wide" in rendered
    assert "Sampled snapshots with contract_size" in rendered
    assert "2026-07-22T10:00:00+00:00 — 2026-07-22T12:00:00+00:00" in rendered
    assert "| pump_short_v1_market_quality | 1h | taken | 2 | 2 |" in rendered
    assert "## Exchange view at 1h" in rendered


def test_json_report_serializes_nested_datetimes() -> None:
    rendered = render_json(_report())

    assert '"generated_at": "2026-07-22T18:00:00+00:00"' in rendered
    assert '"strategy_versions": [' in rendered
    assert '"exchange": "gate"' in rendered


def test_parse_datetime_normalizes_naive_and_zulu_values_to_utc() -> None:
    assert parse_datetime("2026-07-22").tzinfo == UTC
    assert parse_datetime("2026-07-22T12:00:00Z") == datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def test_report_filters_reject_inverted_window() -> None:
    with pytest.raises(ValueError, match="earlier"):
        ReportFilters(
            since=datetime(2026, 7, 23, tzinfo=UTC),
            until=datetime(2026, 7, 22, tzinfo=UTC),
        )


def test_report_filters_reject_unknown_exchange_horizon() -> None:
    with pytest.raises(ValueError, match="one of"):
        ReportFilters(exchange_horizon=90)


def test_report_filters_reject_empty_resolver_version() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        ReportFilters(resolver_version=" ")
