from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from schurfer_analytics.momentum_flow_event_repository import MeasurementEvent
from schurfer_analytics.momentum_flow_event_study_report import (
    EVENT_STUDY_REPORT_VERSION,
    FETCH_STATUS_EMPTY_RESULT,
    FETCH_STATUS_FETCHED,
    FETCH_STATUS_IMMATURE,
    EventExclusionReason,
    EventStudyReport,
    PriceFetchResult,
    _fetch_price_bars_by_event,
    _flow_input_fingerprint,
    aggregate_lookback,
    build_momentum_flow_event_study_report,
    render_json,
    render_markdown,
)
from schurfer_analytics.momentum_flow_protocol import (
    CALIBRATION_WINDOW_UNTIL,
    FLOW_AVAILABLE,
    FLOW_GAP_EXCLUDED,
    FLOW_PARTIAL_COVERAGE,
    FLOW_UNAVAILABLE_PRE_CAPTURE,
    LOOKBACK_OFFSETS_MINUTES,
    MOMENTUM_FLOW_BARS_AVAILABLE_FROM,
)
from schurfer_analytics.momentum_flow_timeline import (
    EventTimeline,
    FlowBar,
    PriceBar,
    TimelinePoint,
    build_event_timeline,
)
from schurfer_analytics.ohlcv import TIMEFRAME_MS

_PRICE_DURATION_MS = 5 * 60_000
_CCXT_VERSION = "test-ccxt-4.0.0"
# Minute-aligned (unlike MOMENTUM_FLOW_BARS_AVAILABLE_FROM itself, which
# carries a real sub-minute fraction) -- see test_momentum_flow_timeline.
# py's own TRIGGER_AT for the full reasoning. Comfortably mature against
# CALIBRATION_WINDOW_UNTIL (days of margin).
POST_CAPTURE_TRIGGER = datetime(2026, 8, 11, 12, 0, 0, tzinfo=UTC)
PRE_CAPTURE_TRIGGER = MOMENTUM_FLOW_BARS_AVAILABLE_FROM - timedelta(days=5)


def _event(
    pump_event_id: int,
    base: str,
    trigger_at: datetime,
    exchange: str = "binance",
    unified_symbol: str | None = None,
) -> MeasurementEvent:
    return MeasurementEvent(
        pump_event_id=pump_event_id,
        base=base,
        exchange=exchange,
        identity_key=f"{exchange}:swap:{base}USDT:v1",
        market_id=f"{base}USDT",
        unified_symbol=unified_symbol or f"{base}/USDT:USDT",
        market_type="swap",
        onboarded_at=trigger_at - timedelta(days=30),
        trigger_at=trigger_at,
    )


def _price_bar(ts_ms: int, close: float) -> PriceBar:
    return PriceBar(ts_ms=ts_ms, close=close, duration_ms=_PRICE_DURATION_MS)


def _fetched(bars: tuple[PriceBar, ...]) -> PriceFetchResult:
    return PriceFetchResult(status=FETCH_STATUS_FETCHED, bars=bars)


def _flow_bar(bucket_start_ms: int, *, buy: float = 1.0, sell: float = 0.5) -> FlowBar:
    return FlowBar(
        bucket_start_ms=bucket_start_ms,
        close_price=100.0,
        open_interest=1000.0,
        open_interest_value=50_000.0,
        open_interest_event_at_ms=bucket_start_ms,
        open_interest_observed_at_ms=bucket_start_ms,
        open_interest_value_event_at_ms=bucket_start_ms,
        open_interest_value_observed_at_ms=bucket_start_ms,
        buy_total_notional_usd=buy,
        sell_total_notional_usd=sell,
        ticker_observed_this_minute=True,
        complete=True,
    )


# --- aggregate_lookback ---


def _point(
    offset: int,
    *,
    price_change: float | None,
    flow_availability: str,
    oi_change: float | None = None,
    oi_value_change: float | None = None,
    buy: float | None = None,
    sell: float | None = None,
    net: float | None = None,
) -> TimelinePoint:
    return TimelinePoint(
        offset_minutes=offset,
        at_ms=0,
        price_available=price_change is not None,
        price_change_pct=price_change,
        realized_volatility=None,
        flow_availability=flow_availability,
        flow_coverage_pct=1.0 if flow_availability == FLOW_AVAILABLE else None,
        oi_change_pct=oi_change,
        oi_value_change_pct=oi_value_change,
        buy_notional_usd=buy,
        sell_notional_usd=sell,
        net_flow_notional_usd=net,
    )


def _timeline(event_id: int, points: tuple[TimelinePoint, ...]) -> EventTimeline:
    return EventTimeline(
        pump_event_id=event_id,
        base="ERA",
        trigger_at_ms=0,
        reference_price=100.0,
        reference_oi=None,
        reference_oi_value=None,
        points=points,
    )


def test_aggregate_lookback_computes_means_and_availability_counts() -> None:
    timelines = (
        _timeline(
            1,
            (
                _point(
                    -60,
                    price_change=10.0,
                    flow_availability=FLOW_AVAILABLE,
                    oi_change=5.0,
                    oi_value_change=6.0,
                    buy=100.0,
                    sell=40.0,
                    net=60.0,
                ),
            ),
        ),
        _timeline(
            2,
            (_point(-60, price_change=20.0, flow_availability=FLOW_GAP_EXCLUDED),),
        ),
        _timeline(
            3,
            (_point(-60, price_change=None, flow_availability=FLOW_UNAVAILABLE_PRE_CAPTURE),),
        ),
        _timeline(
            4,
            # PARTIAL flow coverage must NOT exclude an otherwise-resolved
            # OI reading -- OI is independent of buy/sell coverage.
            (
                _point(
                    -60,
                    price_change=30.0,
                    flow_availability=FLOW_PARTIAL_COVERAGE,
                    oi_change=7.0,
                    oi_value_change=8.0,
                ),
            ),
        ),
    )
    aggregate = aggregate_lookback(-60, timelines)
    assert aggregate.event_count == 4
    assert aggregate.price_available_count == 3
    assert aggregate.mean_price_change_pct == pytest.approx(20.0)  # 10, 20, 30
    assert aggregate.median_price_change_pct == pytest.approx(20.0)
    assert aggregate.flow_available_count == 1
    assert aggregate.flow_partial_coverage_count == 1
    assert aggregate.flow_gap_excluded_count == 1
    assert aggregate.flow_unavailable_pre_capture_count == 1
    # OI mean includes BOTH the FLOW_AVAILABLE point (5.0) and the
    # FLOW_PARTIAL_COVERAGE point (7.0) -- gated only by oi_change_pct
    # itself being resolved, never by flow coverage.
    assert aggregate.oi_available_count == 2
    assert aggregate.mean_oi_change_pct == pytest.approx(6.0)
    assert aggregate.oi_value_available_count == 2
    assert aggregate.mean_oi_value_change_pct == pytest.approx(7.0)
    assert aggregate.mean_net_flow_notional_usd == pytest.approx(60.0)


def test_aggregate_lookback_empty_timelines_all_none() -> None:
    aggregate = aggregate_lookback(-60, ())
    assert aggregate.event_count == 0
    assert aggregate.mean_price_change_pct is None
    assert aggregate.mean_oi_change_pct is None
    assert aggregate.oi_available_count == 0


# --- build_momentum_flow_event_study_report ---


def test_build_report_rejects_until_not_equal_to_calibration_window() -> None:
    with pytest.raises(ValueError, match="calibration window"):
        build_momentum_flow_event_study_report(
            (),
            price_fetch_by_event={},
            flow_bars_by_event={},
            since=None,
            until=CALIBRATION_WINDOW_UNTIL + timedelta(days=1),
            generated_at=datetime.now(UTC),
            code_revision="deadbeef",
            working_tree_dirty=False,
            ccxt_version=_CCXT_VERSION,
        )


def test_build_report_end_to_end_splits_pre_and_post_capture_events() -> None:
    post_event = _event(100, "ERA", POST_CAPTURE_TRIGGER)
    pre_event = _event(200, "BEAT", PRE_CAPTURE_TRIGGER)
    events = (post_event, pre_event)

    post_trigger_ms = int(POST_CAPTURE_TRIGGER.timestamp() * 1000)
    price_fetch_by_event = {
        100: _fetched(
            tuple(
                _price_bar(
                    post_trigger_ms + offset * 60_000 - _PRICE_DURATION_MS, 100.0 + offset / 100
                )
                for offset in LOOKBACK_OFFSETS_MINUTES
            )
        ),
        200: _fetched(
            (_price_bar(int(PRE_CAPTURE_TRIGGER.timestamp() * 1000) - _PRICE_DURATION_MS, 1.0),)
        ),
    }
    # Every point's cumulative window reaches back to LOOKBACK_OFFSETS_
    # MINUTES[0] (-1440), so a genuine FLOW_AVAILABLE anywhere needs
    # complete coverage of that whole span.
    flow_bars_by_event: dict[int, tuple[FlowBar, ...]] = {
        100: tuple(
            _flow_bar(post_trigger_ms + minute * 60_000)
            for minute in range(LOOKBACK_OFFSETS_MINUTES[0], 0)
        ),
    }

    report = build_momentum_flow_event_study_report(
        events,
        price_fetch_by_event=price_fetch_by_event,
        flow_bars_by_event=flow_bars_by_event,
        since=None,
        until=CALIBRATION_WINDOW_UNTIL,
        generated_at=datetime.now(UTC),
        code_revision="deadbeef",
        working_tree_dirty=False,
        ccxt_version=_CCXT_VERSION,
    )
    assert report.cohort_events == 2
    assert report.events_with_timeline == 2
    assert report.events_with_any_flow == 1
    assert report.events_entirely_pre_capture == 1
    assert report.manifest.report_version == EVENT_STUDY_REPORT_VERSION
    assert report.manifest.ccxt_version == _CCXT_VERSION
    assert report.manifest.event_cohort_fingerprint
    assert report.manifest.price_input_fingerprint
    assert report.manifest.flow_input_fingerprint
    assert len(report.lookback_aggregates) == len(LOOKBACK_OFFSETS_MINUTES)
    assert len(report.event_records) == 2
    record_by_event = {record.pump_event_id: record for record in report.event_records}
    assert record_by_event[100].exchange == "binance"
    assert record_by_event[100].price_fetch_status == FETCH_STATUS_FETCHED
    assert record_by_event[200].any_complete_flow_bar is False


def test_build_report_skips_events_with_no_price_bars_fetched() -> None:
    events = (_event(100, "ERA", POST_CAPTURE_TRIGGER),)

    report = build_momentum_flow_event_study_report(
        events,
        price_fetch_by_event={100: PriceFetchResult(status=FETCH_STATUS_EMPTY_RESULT)},
        flow_bars_by_event={},
        since=None,
        until=CALIBRATION_WINDOW_UNTIL,
        generated_at=datetime.now(UTC),
        code_revision="deadbeef",
        working_tree_dirty=False,
        ccxt_version=_CCXT_VERSION,
    )
    assert report.events_with_timeline == 0
    assert report.event_exclusion_reasons == (EventExclusionReason(FETCH_STATUS_EMPTY_RESULT, 1),)


def test_build_report_excludes_immature_events_from_the_funnel_not_silently() -> None:
    # Trigger only 1 hour before `until` -- cannot fit the +240m post-
    # trigger horizon. Must be dropped and counted, not fetched at all.
    immature_trigger = CALIBRATION_WINDOW_UNTIL - timedelta(hours=1)
    events = (_event(100, "ERA", immature_trigger),)

    report = build_momentum_flow_event_study_report(
        events,
        price_fetch_by_event={},
        flow_bars_by_event={},
        since=None,
        until=CALIBRATION_WINDOW_UNTIL,
        generated_at=datetime.now(UTC),
        code_revision="deadbeef",
        working_tree_dirty=False,
        ccxt_version=_CCXT_VERSION,
    )
    assert report.events_with_timeline == 0
    reasons = {row.reason: row.count for row in report.event_exclusion_reasons}
    assert reasons.get(FETCH_STATUS_IMMATURE) == 1
    assert report.event_records[0].price_fetch_status == FETCH_STATUS_IMMATURE


def test_price_fetch_error_surfaces_on_the_event_record() -> None:
    events = (_event(100, "ERA", POST_CAPTURE_TRIGGER),)
    report = build_momentum_flow_event_study_report(
        events,
        price_fetch_by_event={
            100: PriceFetchResult(status="fetch_failed", error="TimeoutError: boom")
        },
        flow_bars_by_event={},
        since=None,
        until=CALIBRATION_WINDOW_UNTIL,
        generated_at=datetime.now(UTC),
        code_revision="deadbeef",
        working_tree_dirty=False,
        ccxt_version=_CCXT_VERSION,
    )
    assert report.event_records[0].price_fetch_error == "TimeoutError: boom"


def test_event_record_flow_scoping_ignores_bars_outside_the_events_own_window() -> None:
    # A decoy bar far outside event 100's own -24h..+4h window (e.g. from
    # a DIFFERENT event on the same symbol/day) must not inflate this
    # event's own flow_bar_count/any_complete_flow_bar.
    events = (_event(100, "ERA", POST_CAPTURE_TRIGGER),)
    decoy_bar = _flow_bar(int(POST_CAPTURE_TRIGGER.timestamp() * 1000) + 10 * 24 * 3600 * 1000)
    report = build_momentum_flow_event_study_report(
        events,
        price_fetch_by_event={100: _fetched((_price_bar(0, 1.0),))},
        # Simulates the report already having windowed the flow bars per
        # event (as _run()'s own _window_flow_bars does) -- an EMPTY tuple
        # here because the decoy bar would have been filtered out before
        # ever reaching build_momentum_flow_event_study_report.
        flow_bars_by_event={100: ()},
        since=None,
        until=CALIBRATION_WINDOW_UNTIL,
        generated_at=datetime.now(UTC),
        code_revision="deadbeef",
        working_tree_dirty=False,
        ccxt_version=_CCXT_VERSION,
    )
    assert report.event_records[0].flow_bar_count == 0
    assert report.event_records[0].any_complete_flow_bar is False
    assert decoy_bar  # constructed only to document the scenario in-line


def test_pre_fetch_exclusions_fold_into_the_funnel_and_cohort_count() -> None:
    # An identity-selection-time exclusion (see momentum_flow_event_
    # repository.py -- an event with no non-conflicted, identity-
    # confirmed source) never becomes a MeasurementEvent at all, so it
    # must be threaded in explicitly to keep the funnel honest about the
    # TRUE starting population.
    report = build_momentum_flow_event_study_report(
        (),
        price_fetch_by_event={},
        flow_bars_by_event={},
        since=None,
        until=CALIBRATION_WINDOW_UNTIL,
        generated_at=datetime.now(UTC),
        code_revision="deadbeef",
        working_tree_dirty=False,
        ccxt_version=_CCXT_VERSION,
        pre_fetch_exclusions=(("no_identity_confirmed_source", 3),),
    )
    assert report.cohort_events == 3
    reasons = {row.reason: row.count for row in report.event_exclusion_reasons}
    assert reasons.get("no_identity_confirmed_source") == 3


# --- rendering ---


def _minimal_report() -> EventStudyReport:
    return build_momentum_flow_event_study_report(
        (),
        price_fetch_by_event={},
        flow_bars_by_event={},
        since=None,
        until=CALIBRATION_WINDOW_UNTIL,
        generated_at=datetime.now(UTC),
        code_revision="deadbeef",
        working_tree_dirty=False,
        ccxt_version=_CCXT_VERSION,
    )


def test_render_markdown_includes_calibration_only_banner() -> None:
    markdown = render_markdown(_minimal_report())
    assert "calibration-only" in markdown.lower()
    assert "no p-value" in markdown.lower()
    assert "no promotion verdict" in markdown.lower()


def test_render_markdown_never_emits_promotion_machinery() -> None:
    # The banner legitimately DECLARES that no profit factor is computed
    # (see the assertion above); what must never appear is an actual
    # rendered promotion-style row, matching oi_growth_filter_report.py's
    # own vocabulary for a real statistical verdict.
    markdown = render_markdown(_minimal_report())
    assert "holm-adjusted" not in markdown.lower()
    assert "verdict (statistical only)" not in markdown.lower()
    assert "profit factor |" not in markdown.lower()


def test_render_markdown_includes_fingerprints_and_ccxt_version() -> None:
    markdown = render_markdown(_minimal_report())
    assert _CCXT_VERSION in markdown
    assert "Event cohort fingerprint" in markdown
    assert "Price input fingerprint" in markdown
    assert "Flow input fingerprint" in markdown


def test_render_json_round_trips_without_error() -> None:
    import json

    payload = render_json(_minimal_report())
    parsed = json.loads(payload)
    assert parsed["manifest"]["report_scope"] == "calibration_only_descriptive_no_promotion"
    assert parsed["manifest"]["ccxt_version"] == _CCXT_VERSION
    assert "event_records" in parsed


def test_flow_fingerprint_covers_oi_value_and_freshness_timestamps() -> None:
    bar = _flow_bar(1_700_000_000_000)
    changed_value = replace(bar, open_interest_value=50_001.0)
    changed_observed_at = replace(
        bar,
        open_interest_observed_at_ms=bar.bucket_start_ms + 1,
    )
    baseline = _flow_input_fingerprint({1: (bar,)})
    assert _flow_input_fingerprint({1: (changed_value,)}) != baseline
    assert _flow_input_fingerprint({1: (changed_observed_at,)}) != baseline


def test_full_timeline_engine_integration_smoke() -> None:
    """End-to-end sanity: the real timeline engine feeds real aggregation
    without any synthetic TimelinePoint shortcuts."""
    trigger_at = POST_CAPTURE_TRIGGER
    trigger_ms = int(trigger_at.timestamp() * 1000)
    price_bars = tuple(
        _price_bar(trigger_ms + offset * 60_000 - _PRICE_DURATION_MS, 100.0)
        for offset in LOOKBACK_OFFSETS_MINUTES
    )
    timeline = build_event_timeline(
        pump_event_id=1,
        base="ERA",
        trigger_at=trigger_at,
        price_bars=price_bars,
        lookback_offsets_minutes=LOOKBACK_OFFSETS_MINUTES,
    )
    aggregate = aggregate_lookback(0, (timeline,))
    assert aggregate.event_count == 1
    assert aggregate.price_available_count == 1
    assert aggregate.mean_price_change_pct == pytest.approx(0.0)


# --- _fetch_price_bars_by_event: real fetch -> real timeline anchor ---


async def test_fetch_price_bars_includes_the_anchor_closing_candle_on_a_clean_grid() -> None:
    """Regression for the first colleague-review P1: fetching from
    exactly the anchor instant (rather than one bar duration earlier)
    silently dropped the one candle the fixed price anchor actually
    needs, making reference_price -- and every price_change_pct -- None
    for every event. Exercises the REAL fetch_symbol_candles()/
    closed_candles() boundary logic, not a synthetic PriceBar shortcut."""
    trigger_at = POST_CAPTURE_TRIGGER  # minute-aligned, :00 seconds
    trigger_ms = int(trigger_at.timestamp() * 1000)
    anchor_ms = trigger_ms + LOOKBACK_OFFSETS_MINUTES[0] * 60_000
    anchor_closing_candle_ts = anchor_ms - TIMEFRAME_MS

    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(
        return_value=[[anchor_closing_candle_ts, 100.0, 101.0, 99.0, 100.0, 1.0]]
    )
    exchange.close = AsyncMock()

    events = (_event(100, "ERA", trigger_at),)
    results = await _fetch_price_bars_by_event(events, {"binance": lambda: exchange})

    result = results[100]
    assert result.status == FETCH_STATUS_FETCHED
    assert any(bar.ts_ms == anchor_closing_candle_ts for bar in result.bars)

    timeline = build_event_timeline(
        pump_event_id=100,
        base="ERA",
        trigger_at=trigger_at,
        price_bars=result.bars,
        lookback_offsets_minutes=LOOKBACK_OFFSETS_MINUTES,
    )
    assert timeline.reference_price == 100.0


async def test_fetch_price_bars_includes_the_anchor_closing_candle_off_grid_trigger() -> None:
    """Regression for the SECOND colleague-review P1: the first fix
    (`anchor_ms - TIMEFRAME_MS`) only worked when trigger_at happened to
    already be aligned to the exchange's 5-minute candle grid. A real
    trigger has arbitrary seconds (e.g. :00:41) -- the needed candle
    (opening at the grid boundary BELOW the anchor, not one bar before the
    anchor's own arbitrary instant) was still silently dropped without
    flooring the anchor to the grid first."""
    trigger_at = datetime(2026, 8, 11, 12, 0, 41, tzinfo=UTC)  # NOT grid-aligned
    trigger_ms = int(trigger_at.timestamp() * 1000)
    anchor_ms = trigger_ms + LOOKBACK_OFFSETS_MINUTES[0] * 60_000
    grid_floor_ms = anchor_ms // TIMEFRAME_MS * TIMEFRAME_MS
    # The candle opening at grid_floor - TIMEFRAME_MS closes exactly at
    # grid_floor, which is <= anchor_ms (anchor has :41 seconds past the
    # grid mark) -- this is the bar the anchor must resolve to.
    needed_candle_ts = grid_floor_ms - TIMEFRAME_MS

    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(
        return_value=[[needed_candle_ts, 100.0, 101.0, 99.0, 100.0, 1.0]]
    )
    exchange.close = AsyncMock()

    events = (_event(100, "ERA", trigger_at),)
    results = await _fetch_price_bars_by_event(events, {"binance": lambda: exchange})

    result = results[100]
    assert result.status == FETCH_STATUS_FETCHED
    assert any(bar.ts_ms == needed_candle_ts for bar in result.bars)

    timeline = build_event_timeline(
        pump_event_id=100,
        base="ERA",
        trigger_at=trigger_at,
        price_bars=result.bars,
        lookback_offsets_minutes=LOOKBACK_OFFSETS_MINUTES,
    )
    assert timeline.reference_price == 100.0


async def test_fetch_price_bars_uses_the_confirmed_unified_symbol_not_a_guess() -> None:
    """Regression for the identity colleague-review finding: the fetch
    must target the event's own CONFIRMED unified_symbol, never a
    reconstructed `{base}/USDT:USDT` guess that could silently hit the
    wrong contract."""
    trigger_at = POST_CAPTURE_TRIGGER
    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(return_value=[])
    exchange.close = AsyncMock()

    events = (_event(100, "ERA", trigger_at, unified_symbol="ERA/USDT:USDT-26SEP26"),)
    await _fetch_price_bars_by_event(events, {"binance": lambda: exchange})

    called_symbol = exchange.fetch_ohlcv.await_args_list[0].args[0]
    assert called_symbol == "ERA/USDT:USDT-26SEP26"
