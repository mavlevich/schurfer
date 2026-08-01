from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from schurfer_analytics.ohlcv import ONE_MINUTE_MS, Candle, next_timeframe_after
from schurfer_analytics.source_lead import (
    LONG_HORIZONS_MINUTES,
    SOURCE_LEAD_COHORT_START,
    SourceLeadCandidate,
    SourceLeadEvent,
    SourceLeadObservation,
    SourceLeadPath,
    build_source_lead_candidates,
    evaluate_source_lead_candidate,
    source_lead_path_bounds,
)
from schurfer_analytics.source_lead_report import (
    REPORT_VERSION,
    build_source_lead_report,
    render_json,
    render_markdown,
)


def _observation(
    exchange: str,
    at: datetime,
    *,
    unified_symbol: str | None = None,
    onboarded_at: datetime | None = None,
) -> SourceLeadObservation:
    return SourceLeadObservation(
        exchange=exchange,
        symbol="EDGEUSDT",
        identity_key="edge:usdt:swap",
        unified_symbol=unified_symbol or "EDGE/USDT:USDT",
        market_type="swap",
        base_asset="EDGE",
        quote_asset="USDT",
        settle_asset="USDT",
        onboarded_at=onboarded_at,
        identity_conflict=False,
        first_seen_at=at,
        first_change_pct=25,
        first_price=100,
        first_volume_24h_usd=1_000_000,
    )


def _event(*observations: SourceLeadObservation, event_id: int = 42) -> SourceLeadEvent:
    return SourceLeadEvent(
        event_id=event_id,
        base="EDGE",
        episode=1,
        first_seen_at=min(row.first_seen_at for row in observations),
        closed_at=None,
        observations=tuple(observations),
    )


def _candidate_and_path() -> tuple[SourceLeadCandidate, SourceLeadPath]:
    source_at = SOURCE_LEAD_COHORT_START + timedelta(hours=12, seconds=10)
    confirmation_at = source_at + timedelta(minutes=2, seconds=20)
    result = build_source_lead_candidates(
        (
            _event(
                _observation("mexc", source_at),
                _observation("binance", confirmation_at),
            ),
        ),
        until=confirmation_at + timedelta(hours=5),
    )
    candidate = result.candidates[0]
    start_ms, end_ms = source_lead_path_bounds(candidate)
    confirmation_entry_ms = next_timeframe_after(
        int(candidate.confirmation_at.timestamp() * 1000), ONE_MINUTE_MS
    )
    candles = []
    for timestamp in range(start_ms, end_ms, ONE_MINUTE_MS):
        open_price = 90 if timestamp < confirmation_entry_ms else 100
        close_price = 110 if timestamp >= confirmation_entry_ms else open_price
        candles.append(Candle(timestamp, open_price, 110, 89, close_price, 1))
    return candidate, SourceLeadPath(
        candidate.candidate_id,
        candidate.event_id,
        candidate.execution_exchange,
        candidate.exact_symbol,
        "complete",
        tuple(candles),
    )


def test_candidates_require_unique_earliest_source_and_later_identity_safe_target() -> None:
    source_at = SOURCE_LEAD_COHORT_START + timedelta(hours=1)
    target_at = source_at + timedelta(minutes=5)
    tied = _event(
        _observation("mexc", source_at),
        _observation("gate", source_at),
        _observation("binance", target_at),
        event_id=1,
    )
    valid = _event(
        _observation("mexc", source_at),
        _observation("binance", target_at, unified_symbol="1000EDGE/USDT:USDT"),
        event_id=2,
    )

    result = build_source_lead_candidates(
        (tied, valid),
        until=target_at + timedelta(hours=5),
    )

    assert [row.event_id for row in result.candidates] == [2]
    assert result.candidates[0].exact_symbol == "1000EDGE/USDT:USDT"
    assert ("tied_first_source", 1) in result.event_statuses


def test_candidate_rejects_target_that_was_not_listed_at_source_time() -> None:
    source_at = SOURCE_LEAD_COHORT_START + timedelta(hours=1)
    target_at = source_at + timedelta(minutes=5)
    event = _event(
        _observation("mexc", source_at),
        _observation("binance", target_at, onboarded_at=source_at + timedelta(minutes=1)),
    )

    result = build_source_lead_candidates(
        (event,),
        until=target_at + timedelta(hours=5),
    )

    assert result.candidates == ()
    assert ("mexc", "binance", "target_not_onboarded_at_source", 1) in result.route_statuses


def test_candidate_keeps_venue_local_identity_keys_separate() -> None:
    source_at = SOURCE_LEAD_COHORT_START + timedelta(hours=1)
    target_at = source_at + timedelta(minutes=5)
    source = replace(_observation("mexc", source_at), identity_key="mexc:swap:EDGE_USDT:1")
    target = replace(
        _observation("binance", target_at),
        identity_key="binance:swap:EDGEUSDT:2",
    )

    result = build_source_lead_candidates(
        (_event(source, target),),
        until=target_at + timedelta(hours=5),
    )

    assert len(result.candidates) == 1


def test_candidate_is_right_censored_until_full_240m_window_matures() -> None:
    source_at = SOURCE_LEAD_COHORT_START + timedelta(hours=1)
    target_at = source_at + timedelta(minutes=5)
    event = _event(
        _observation("gate", source_at),
        _observation("bybit", target_at),
    )

    result = build_source_lead_candidates(
        (event,),
        until=target_at + timedelta(minutes=max(LONG_HORIZONS_MINUTES) - 1),
    )

    assert result.candidates == ()
    assert ("gate", "bybit", "right_censored", 1) in result.route_statuses


def test_early_and_confirmation_entries_share_one_exit_endpoint() -> None:
    candidate, path = _candidate_and_path()

    outcomes = evaluate_source_lead_candidate(candidate, path)
    primary = next(row for row in outcomes if row.delay_minutes == 0 and row.horizon_minutes == 30)

    assert primary.status == "complete"
    assert primary.early_traded is True
    assert primary.early_long_gross_pct == pytest.approx((110 - 90) / 90 * 100)
    assert primary.confirmation_long_gross_pct == pytest.approx(10)
    assert primary.paired_long_delta_pct == pytest.approx(
        primary.early_long_net_pct - primary.confirmation_long_net_pct  # type: ignore[operator]
    )
    assert primary.early_holding_minutes is not None
    assert primary.early_holding_minutes > primary.control_holding_minutes  # type: ignore[operator]


def test_entry_after_confirmation_is_cash_not_a_retrospective_fill() -> None:
    candidate, path = _candidate_and_path()

    outcomes = evaluate_source_lead_candidate(candidate, path)
    delayed = next(row for row in outcomes if row.delay_minutes == 5 and row.horizon_minutes == 30)

    assert delayed.status == "missed_lead_cash"
    assert delayed.early_traded is False
    assert delayed.early_long_net_pct == 0
    assert delayed.paired_long_delta_pct == pytest.approx(-delayed.confirmation_long_net_pct)  # type: ignore[operator]


def test_path_gap_fails_every_lane_closed() -> None:
    candidate, path = _candidate_and_path()
    incomplete = replace(path, candles=path.candles[1:])

    outcomes = evaluate_source_lead_candidate(candidate, incomplete)

    assert len(outcomes) == 18
    assert {row.status for row in outcomes} == {"path_gap"}
    assert all(row.early_long_net_pct is None for row in outcomes)


def test_report_reconciles_candidates_and_keeps_long_and_short_books_separate() -> None:
    candidate, path = _candidate_and_path()
    source = _observation("mexc", candidate.source_at)
    target = _observation("binance", candidate.confirmation_at)
    events = (_event(source, target),)
    until = candidate.confirmation_at + timedelta(hours=5)
    candidate_build = build_source_lead_candidates(events, until=until)

    report = build_source_lead_report(
        events,
        candidate_build,
        (path,),
        since=SOURCE_LEAD_COHORT_START,
        until=until,
        generated_at=until,
        code_revision="abc123",
        working_tree_dirty=False,
        bootstrap_iterations=100,
    )

    assert report.manifest.report_version == REPORT_VERSION
    assert {row.lane for row in report.lane_metrics} == {
        "early_long",
        "confirmation_short",
    }
    assert report.primary_inference[0].resolved == 1
    assert "canonical cross-venue token address mapping" in render_markdown(report)
    assert json.loads(render_json(report))["manifest"]["report_scope"].startswith(
        "post_hoc_discovery_only"
    )


def test_report_rejects_candidate_build_from_different_cutoff() -> None:
    candidate, path = _candidate_and_path()
    events = (
        _event(
            _observation("mexc", candidate.source_at),
            _observation("binance", candidate.confirmation_at),
        ),
    )
    until = candidate.confirmation_at + timedelta(hours=5)
    wrong = build_source_lead_candidates(
        events,
        until=candidate.confirmation_at + timedelta(minutes=30),
    )

    with pytest.raises(ValueError, match="reconcile"):
        build_source_lead_report(
            events,
            wrong,
            (path,),
            since=SOURCE_LEAD_COHORT_START,
            until=until,
            generated_at=until,
            code_revision="abc123",
            working_tree_dirty=False,
            bootstrap_iterations=100,
        )
