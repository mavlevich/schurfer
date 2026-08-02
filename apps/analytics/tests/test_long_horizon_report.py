from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.long_horizon_funding_repository import (
    FundingSample,
    FundingSeries,
)
from schurfer_analytics.long_horizon_report import (
    LONG_HORIZON_COHORT_START,
    LONG_HORIZON_REPORT_VERSION,
    LONG_HORIZON_STRATEGY_VERSIONS,
    LONG_HORIZONS,
    SHORT_FUNDING_SIGN_CONVENTION,
    build_long_horizon_dataset,
    build_long_horizon_report,
    build_parser,
    render_json,
    render_markdown,
    signed_funding_for_window,
)
from schurfer_analytics.ohlcv import ceil_to_timeframe
from schurfer_analytics.outcomes import EXTENDED_HORIZON_STRATEGY_VERSIONS
from schurfer_analytics.replay import (
    ReplayDecision,
    ReplayFilters,
    ReplayOutcome,
    build_replay_dataset,
)


def _decision() -> ReplayDecision:
    decision_at = LONG_HORIZON_COHORT_START + timedelta(minutes=1)
    outcomes = tuple(
        ReplayOutcome(
            horizon_minutes=horizon,
            status="complete",
            anchor_exchange="binance",
            source_exchange="binance",
            entry_price=100,
            forward_price=95,
            mfe_pct=6,
            mae_pct={1_440: 7, 4_320: 9, 10_080: 11}[horizon],
            short_return_pct=5,
            coverage_ratio=1,
        )
        for horizon in LONG_HORIZONS
    )
    return ReplayDecision(
        row_id=1,
        decision_id="00000000-0000-0000-0000-000000000001",
        pump_event_id=42,
        event_base="ERA",
        event_first_seen_at=LONG_HORIZON_COHORT_START,
        event_closed_at=LONG_HORIZON_COHORT_START + timedelta(hours=2),
        ts=decision_at,
        base="ERA",
        exchange="binance",
        action="opened_dry_run",
        reason="paper",
        score=6,
        pump_pct=40,
        price=100,
        strategy_version=LONG_HORIZON_STRATEGY_VERSIONS[0],
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
            "bid_impact_bps": {"100": 3},
            "ask_impact_bps": {"100": 4},
            "quality": {
                "allowed": True,
                "depth_target_usd": 100,
            },
        },
        outcomes=outcomes,
    )


def _funding(decision: ReplayDecision) -> FundingSeries:
    entry_ms = ceil_to_timeframe(int(decision.ts.timestamp() * 1000))
    entry = datetime.fromtimestamp(entry_ms / 1000, tz=UTC)
    points = (
        (entry, 0.5),
        (entry + timedelta(hours=8), 0.001),
        (entry + timedelta(hours=24), -0.0005),
        (entry + timedelta(hours=72), 0.002),
        (entry + timedelta(days=7), -0.001),
    )
    return FundingSeries(
        event_id=42,
        exchange="binance",
        status="sampled",
        requested_since=entry - timedelta(days=1),
        requested_until=entry + timedelta(days=7),
        error=None,
        samples=tuple(
            FundingSample(
                source_at=source_at,
                sample_key=str(index),
                payload={"fundingRate": rate},
            )
            for index, (source_at, rate) in enumerate(points)
        ),
    )


def _inputs() -> tuple[ReplayDecision, ReplayFilters]:
    decision = _decision()
    filters = ReplayFilters(
        since=LONG_HORIZON_COHORT_START,
        until=LONG_HORIZON_COHORT_START + timedelta(days=8),
        strategy_versions=LONG_HORIZON_STRATEGY_VERSIONS,
        required_horizons=LONG_HORIZONS,
    )
    return decision, filters


def test_signed_funding_excludes_entry_boundary_and_preserves_sign() -> None:
    decision = _decision()
    series = _funding(decision)
    entry_ms = ceil_to_timeframe(int(decision.ts.timestamp() * 1000))
    entry = datetime.fromtimestamp(entry_ms / 1000, tz=UTC)

    count, return_pct, error = signed_funding_for_window(
        series,
        entry_at=entry,
        exit_at=entry + timedelta(hours=24),
    )

    assert error is None
    assert count == 2
    assert return_pct == pytest.approx(0.05)


@pytest.mark.parametrize(
    ("series", "expected"),
    [
        (None, "funding_run_missing"),
        (
            FundingSeries(
                42,
                "binance",
                "no_data",
                LONG_HORIZON_COHORT_START,
                LONG_HORIZON_COHORT_START + timedelta(days=8),
                "no rows",
                (),
            ),
            "funding_run_status:no_data",
        ),
    ],
)
def test_signed_funding_fails_closed_when_unavailable(
    series: FundingSeries | None,
    expected: str,
) -> None:
    count, return_pct, error = signed_funding_for_window(
        series,
        entry_at=LONG_HORIZON_COHORT_START,
        exit_at=LONG_HORIZON_COHORT_START + timedelta(days=1),
    )

    assert count is None
    assert return_pct is None
    assert error == expected


def test_signed_funding_rejects_duplicate_settlement_timestamps() -> None:
    decision = _decision()
    series = _funding(decision)
    duplicate = replace(
        series,
        samples=(
            *series.samples,
            replace(series.samples[1], sample_key="duplicate"),
        ),
    )
    entry_ms = ceil_to_timeframe(int(decision.ts.timestamp() * 1000))
    entry = datetime.fromtimestamp(entry_ms / 1000, tz=UTC)

    count, return_pct, error = signed_funding_for_window(
        duplicate,
        entry_at=entry,
        exit_at=entry + timedelta(days=1),
    )

    assert count is None
    assert return_pct is None
    assert error == "duplicate_funding_settlement"


def test_report_calculates_signed_net_stop_survival_and_capacity() -> None:
    decision, filters = _inputs()
    dataset = build_replay_dataset([decision], filters)

    report = build_long_horizon_report(
        dataset,
        filters,
        (_funding(decision),),
        generated_at=filters.until,
        code_revision="abc123",
        working_tree_dirty=False,
        taker_fee_bps_per_side=10,
    )

    day = next(row for row in report.results if row.horizon_minutes == 1_440)
    assert day.funding_settlements == 2
    assert day.exact_venue_path is True
    assert day.signed_funding_return_pct == pytest.approx(0.05)
    assert day.modeled_signed_funding_cash_usd == pytest.approx(0.025)
    assert day.funding_direction == "credit"
    assert day.execution_cost_bps == 27
    assert day.net_fixed_horizon_return_pct == pytest.approx(4.78)
    assert day.survived_initial_stop is True
    three_days = next(row for row in report.results if row.horizon_minutes == 4_320)
    assert three_days.survived_initial_stop is False
    metrics = report.horizon_metrics[0]
    assert metrics.initial_stop_survival_rate_pct == 100
    assert metrics.expected_concurrent_positions_upper_bound == pytest.approx(0.125)
    assert metrics.expected_occupied_notional_usd_upper_bound == pytest.approx(6.25)
    assert report.manifest.funding_resolver_version == "long_horizon_funding_v1"
    assert report.manifest.funding_sign_convention == SHORT_FUNDING_SIGN_CONVENTION
    buffer = next(
        row
        for row in report.margin_buffer_metrics
        if row.horizon_minutes == 1_440 and row.collateral_to_notional_pct == 25
    )
    assert buffer.exact_paths == 1
    assert buffer.crossed_price_distance == 0
    assert buffer.price_distance_survival_rate_pct == 100
    assert buffer.mean_collateral_usd == pytest.approx(12.5)
    assert buffer.expected_occupied_collateral_usd_upper_bound == pytest.approx(1.5625)
    assert buffer.survivor_mean_return_on_collateral_pct == pytest.approx(19.12)
    assert report.manifest.report_version == "long_horizon_signed_funding_report_v2"
    assert LONG_HORIZON_REPORT_VERSION == "long_horizon_signed_funding_report_v2"
    assert LONG_HORIZON_STRATEGY_VERSIONS is EXTENDED_HORIZON_STRATEGY_VERSIONS
    assert SHORT_FUNDING_SIGN_CONVENTION in render_markdown(report)
    assert "Collateral buffer path screen" in render_markdown(report)
    assert "Descriptive discovery only" in render_markdown(report)
    assert '"signed_funding_return_pct": 0.05' in render_json(report)


def test_margin_screen_excludes_cross_venue_fallback_paths() -> None:
    decision, filters = _inputs()
    filters = replace(filters, allow_fallback=True)
    fallback_outcomes = tuple(
        replace(outcome, status="complete_fallback", source_exchange="bybit")
        for outcome in decision.outcomes
    )
    fallback_decision = replace(decision, outcomes=fallback_outcomes)
    dataset = build_replay_dataset([fallback_decision], filters)

    report = build_long_horizon_report(
        dataset,
        filters,
        (_funding(fallback_decision),),
        generated_at=filters.until,
        code_revision="abc123",
        working_tree_dirty=False,
    )

    day = next(row for row in report.results if row.horizon_minutes == 1_440)
    buffer = next(
        row
        for row in report.margin_buffer_metrics
        if row.horizon_minutes == 1_440 and row.collateral_to_notional_pct == 25
    )
    assert day.exact_venue_path is False
    assert buffer.exact_paths == 0
    assert buffer.price_distance_survival_rate_pct is None


def test_report_models_negative_funding_as_short_debit() -> None:
    decision, filters = _inputs()
    dataset = build_replay_dataset([decision], filters)
    series = _funding(decision)
    debit_series = replace(
        series,
        samples=tuple(
            replace(sample, payload={"fundingRate": -0.001}) for sample in series.samples
        ),
    )

    report = build_long_horizon_report(
        dataset,
        filters,
        (debit_series,),
        generated_at=filters.until,
        code_revision="abc123",
        working_tree_dirty=False,
        taker_fee_bps_per_side=10,
    )

    day = next(row for row in report.results if row.horizon_minutes == 1_440)
    assert day.signed_funding_return_pct == pytest.approx(-0.2)
    assert day.modeled_signed_funding_cash_usd == pytest.approx(-0.1)
    assert day.funding_direction == "debit"


def test_dataset_requires_long_outcomes_only_on_selected_decision() -> None:
    decision, filters = _inputs()
    later_at = decision.ts + timedelta(minutes=1)
    later = replace(
        decision,
        row_id=2,
        decision_id="00000000-0000-0000-0000-000000000002",
        ts=later_at,
        action="skipped",
        outcomes=(),
        features={
            **(decision.features or {}),
            "signal": {"computed_at": later_at.timestamp()},
        },
    )

    dataset = build_long_horizon_dataset([decision, later], filters)

    assert len(dataset.eligible_episodes) == 1
    assert dataset.eligible_episodes[0].decisions == (decision, later)


def test_dataset_still_excludes_missing_selected_long_outcome() -> None:
    decision, filters = _inputs()
    missing = replace(
        decision,
        outcomes=tuple(
            outcome for outcome in decision.outcomes if outcome.horizon_minutes != 10_080
        ),
    )

    dataset = build_long_horizon_dataset([missing], filters)

    assert dataset.eligible_episodes == ()
    assert dataset.episodes[0].exclusion_reasons == ("missing_outcome:10080",)


def test_report_does_not_turn_missing_funding_into_zero() -> None:
    decision, filters = _inputs()
    dataset = build_replay_dataset([decision], filters)

    report = build_long_horizon_report(
        dataset,
        filters,
        (),
        generated_at=filters.until,
        code_revision="abc123",
        working_tree_dirty=False,
    )

    assert {row.status for row in report.results} == {"unresolved"}
    assert {row.error for row in report.results} == {"funding_run_missing"}
    assert all(row.mean_signed_funding_return_pct is None for row in report.horizon_metrics)


def test_report_rejects_duplicate_funding_series() -> None:
    decision, filters = _inputs()
    dataset = build_replay_dataset([decision], filters)
    series = _funding(decision)

    with pytest.raises(ValueError, match="duplicate"):
        build_long_horizon_report(
            dataset,
            filters,
            (series, replace(series)),
            generated_at=filters.until,
            code_revision="abc123",
            working_tree_dirty=False,
        )


def test_cli_requires_explicit_dirty_state() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([])
    args = parser.parse_args(["--no-working-tree-dirty"])

    assert args.since == LONG_HORIZON_COHORT_START
    assert args.working_tree_dirty is False
