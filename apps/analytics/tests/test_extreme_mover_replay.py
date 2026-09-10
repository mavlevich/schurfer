from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.extreme_mover_replay import (
    DISCOVERY_END,
    HORIZONS_MINUTES,
    Report,
    build_episodes,
    build_report,
    evaluate_episode,
)
from schurfer_analytics.replay import ReplayDecision, ReplayOutcome

T0 = datetime(2026, 8, 27, tzinfo=UTC)


def _outcome(
    horizon: int,
    *,
    exchange: str = "binance",
    status: str = "complete",
    forward_price: float = 110.0,
) -> ReplayOutcome:
    return ReplayOutcome(
        horizon_minutes=horizon,
        status=status,
        anchor_exchange=exchange,
        source_exchange=exchange,
        entry_price=100.0,
        forward_price=forward_price,
        mfe_pct=5.0,
        mae_pct=12.0,
        short_return_pct=(100.0 - forward_price) / 100.0 * 100,
        coverage_ratio=1.0,
    )


def _decision(
    row_id: int,
    event_id: int,
    *,
    at: datetime | None = None,
    exchange: str = "binance",
    strategy_version: str = "pump_short_measurement_v1",
    quality_allowed: bool = True,
    liquidity_status: str = "sampled",
    outcomes: tuple[ReplayOutcome, ...] | None = None,
) -> ReplayDecision:
    ts = at or T0 + timedelta(minutes=row_id)
    return ReplayDecision(
        row_id=row_id,
        decision_id=f"00000000-0000-0000-0000-{row_id:012d}",
        pump_event_id=event_id,
        event_base=f"ASSET{event_id % 31}",
        event_first_seen_at=ts,
        event_closed_at=ts + timedelta(hours=1),
        ts=ts,
        base=f"ASSET{event_id % 31}",
        exchange=exchange,
        action="skipped",
        reason="measurement",
        score=5,
        pump_pct=50.0,
        price=100.0,
        strategy_version=strategy_version,
        features={"signal": {"computed_at": ts.timestamp()}, "config": {}},
        liquidity={
            "status": liquidity_status,
            "quality": {"allowed": quality_allowed},
            "bid_impact_bps": {"100": 4.0},
            "ask_impact_bps": {"100": 6.0},
        },
        outcomes=outcomes
        if outcomes is not None
        else tuple(_outcome(horizon, exchange=exchange) for horizon in HORIZONS_MINUTES),
    )


def _report(decisions: tuple[ReplayDecision, ...]) -> Report:
    return build_report(
        decisions,
        dataset_since=T0,
        dataset_until_exclusive=DISCOVERY_END,
        database_snapshot_at=DISCOVERY_END + timedelta(hours=4),
        generated_at=DISCOVERY_END + timedelta(hours=4),
        code_revision="abc123",
        working_tree_dirty=False,
        bootstrap_iterations=100,
        bootstrap_seed=7,
    )


def test_groups_supported_decisions_by_episode_and_orders_before_selection() -> None:
    later = _decision(2, 42, at=T0 + timedelta(minutes=2))
    first = _decision(1, 42, at=T0 + timedelta(minutes=1), quality_allowed=False)
    unrelated = replace(_decision(3, 43), strategy_version="early_momentum_v4")

    episodes = build_episodes((later, unrelated, first))

    assert len(episodes) == 1
    assert episodes[0].pump_event_id == 42
    assert [row.row_id for row in episodes[0].decisions] == [1, 2]


def test_first_and_first_quality_anchors_are_selected_before_outcomes() -> None:
    first = _decision(1, 42, quality_allowed=False, outcomes=())
    quality = _decision(2, 42, quality_allowed=True)

    rows = evaluate_episode(build_episodes((quality, first))[0])
    first_15_long = next(
        row
        for row in rows
        if row.anchor == "first_decision" and row.direction == "long" and row.horizon_minutes == 15
    )
    quality_15_long = next(
        row
        for row in rows
        if row.anchor == "first_quality" and row.direction == "long" and row.horizon_minutes == 15
    )

    assert first_15_long.status == "unresolved"
    assert first_15_long.decision_id == first.decision_id
    assert quality_15_long.status == "complete"
    assert quality_15_long.decision_id == quality.decision_id


def test_partial_and_cross_venue_outcomes_never_qualify() -> None:
    partial = _decision(
        1,
        1,
        outcomes=(_outcome(15, status="partial"),),
    )
    cross = _decision(
        2,
        2,
        outcomes=(_outcome(15, exchange="bybit"),),
    )

    partial_row = evaluate_episode(build_episodes((partial,))[0])[0]
    cross_row = evaluate_episode(build_episodes((cross,))[0])[0]

    assert partial_row.status == "unresolved"
    assert partial_row.reason == "outcome_status:partial"
    assert cross_row.status == "unresolved"
    assert cross_row.reason == "non_exact_venue_outcome"


def test_missing_entry_liquidity_is_cash_not_ticker_fill() -> None:
    decision = _decision(1, 1, liquidity_status="fetch_failed")

    row = evaluate_episode(build_episodes((decision,))[0])[0]

    assert row.status == "cash"
    assert row.reason == "entry_liquidity_unavailable"
    assert row.net_return_pct == 0
    assert row.net_pnl_usd == 0


def test_outcome_may_not_straddle_the_exclusive_dataset_end() -> None:
    decision = _decision(1, 1, at=T0 + timedelta(minutes=50))

    row = next(
        result
        for result in evaluate_episode(
            build_episodes((decision,))[0],
            dataset_until_exclusive=T0 + timedelta(hours=1),
        )
        if result.anchor == "first_decision"
        and result.direction == "long"
        and result.horizon_minutes == 15
    )

    assert row.status == "unresolved"
    assert row.reason == "outcome_straddles_window"


def test_long_and_short_use_correct_sides_and_costs() -> None:
    decision = _decision(1, 1)
    rows = evaluate_episode(build_episodes((decision,))[0])
    long_row = next(
        row
        for row in rows
        if row.anchor == "first_decision" and row.direction == "long" and row.horizon_minutes == 60
    )
    short_row = next(
        row
        for row in rows
        if row.anchor == "first_decision" and row.direction == "short" and row.horizon_minutes == 60
    )

    assert long_row.gross_return_pct == pytest.approx(10.0)
    assert short_row.gross_return_pct == pytest.approx(-10.0)
    assert long_row.entry_impact_bps == 6.0
    assert short_row.entry_impact_bps == 4.0
    assert long_row.net_return_pct == pytest.approx(10.0 - 0.2 - 0.06 - 0.15 - 0.00625)
    assert short_row.net_return_pct == pytest.approx(-10.0 - 0.2 - 0.04 - 0.15 - 0.00625)
    assert long_row.mfe_pct == 12.0
    assert long_row.mae_pct == 5.0
    assert short_row.mfe_pct == 5.0
    assert short_row.mae_pct == 12.0


def test_mature_positive_cross_asset_sample_routes_to_existing_data_candidate() -> None:
    decisions = tuple(
        _decision(
            index,
            index,
            at=T0 + timedelta(days=index % 14, minutes=index),
            outcomes=tuple(_outcome(horizon, forward_price=102.0) for horizon in HORIZONS_MINUTES),
        )
        for index in range(1, 111)
    )

    report = _report(decisions)

    long_verdict = next(row for row in report.verdicts if row.direction == "long")
    short_verdict = next(row for row in report.verdicts if row.direction == "short")
    assert long_verdict.verdict == "existing_data_candidate"
    assert long_verdict.selected_cell is not None
    assert short_verdict.verdict == "stop"
    assert report.manifest.input_fingerprint
    assert report.exchange_coverage[0].exact_outcomes_60m == 110


def test_small_sample_is_insufficient_and_json_manifest_is_deterministic() -> None:
    report = _report((_decision(1, 1),))

    assert {row.verdict for row in report.verdicts} == {"insufficient_discovery"}
    assert report.episodes == 1
    assert len(report.metrics) == 12


def test_report_counts_unassigned_decision_and_rejects_out_of_window_input() -> None:
    unassigned = replace(_decision(1, 1), pump_event_id=None)

    report = _report((unassigned,))

    assert report.episodes == 0
    assert ("missing_pump_event_id", 1) in {(row.name, row.count) for row in report.coverage}

    outside = _decision(2, 2, at=DISCOVERY_END)
    with pytest.raises(ValueError, match="outside the frozen window"):
        _report((outside,))


def test_episode_fails_closed_on_inconsistent_base_identity() -> None:
    inconsistent = replace(_decision(1, 1), event_base="DIFFERENT")

    with pytest.raises(ValueError, match="inconsistent base identity"):
        build_episodes((inconsistent,))
