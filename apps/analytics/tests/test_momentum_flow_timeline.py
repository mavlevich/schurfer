from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import log
from statistics import pstdev

import pytest
from schurfer_analytics.momentum_flow_protocol import (
    FLOW_AVAILABLE,
    FLOW_GAP_EXCLUDED,
    FLOW_PARTIAL_COVERAGE,
    FLOW_UNAVAILABLE_PRE_CAPTURE,
    MOMENTUM_FLOW_BARS_AVAILABLE_FROM,
)
from schurfer_analytics.momentum_flow_timeline import (
    EventTimeline,
    FlowBar,
    PriceBar,
    TimelinePoint,
    build_event_timeline,
)

# Well after the real capture start, so flow bars are in-window for these
# tests. Deliberately minute-aligned (unlike MOMENTUM_FLOW_BARS_AVAILABLE_
# FROM itself, which carries a real sub-minute fraction): real flow bars'
# bucket_start always lands on a true UTC-minute boundary, and these
# fixtures need to match that grid for _expected_minute_buckets's own
# rounding to be a no-op, or coverage-fraction math would be off by
# whatever fraction of a minute the trigger itself carried.
TRIGGER_AT = datetime(2026, 8, 12, 12, 0, 0, tzinfo=UTC)
TRIGGER_MS = int(TRIGGER_AT.timestamp() * 1000)
_PRICE_DURATION_MS = 5 * 60_000


def _price_bar(
    offset_minutes: int, close: float, duration_ms: int = _PRICE_DURATION_MS
) -> PriceBar:
    """A price bar that CLOSES (becomes known) exactly at `offset_minutes`
    -- i.e. it opened `duration_ms` earlier. This is what lets a lookback
    point at `offset_minutes` actually use it under the known-at rule."""
    target_ms = TRIGGER_MS + offset_minutes * 60_000
    return PriceBar(ts_ms=target_ms - duration_ms, close=close, duration_ms=duration_ms)


def _flow_bar(
    offset_minutes: int,
    *,
    buy: float,
    sell: float,
    oi: float | None = None,
    oi_value: float | None = None,
    complete: bool = True,
    ticker_observed_this_minute: bool = True,
    oi_observed_in_own_bucket: bool = True,
) -> FlowBar:
    """A flow bar covering the ONE-MINUTE bucket ending exactly at
    `offset_minutes` (bucket_start = offset - 1 minute), matching the
    known-at rule (bucket_start + 60_000 == offset's own target).
    `oi_observed_in_own_bucket=True` (default) makes this bar's OI a
    genuinely fresh observation (`open_interest_observed_at_ms` set to
    its own `bucket_start_ms`); `False` simulates a carried-forward
    reading by dating the observation 10 minutes before this bucket even
    opened."""
    target_ms = TRIGGER_MS + offset_minutes * 60_000
    bucket_start_ms = target_ms - 60_000
    observed_at_ms = (
        (bucket_start_ms if oi_observed_in_own_bucket else bucket_start_ms - 600_000)
        if oi is not None
        else None
    )
    return FlowBar(
        bucket_start_ms=bucket_start_ms,
        close_price=None,
        open_interest=oi,
        open_interest_value=oi_value,
        open_interest_event_at_ms=observed_at_ms,
        open_interest_observed_at_ms=observed_at_ms,
        open_interest_value_event_at_ms=(observed_at_ms if oi_value is not None else None),
        open_interest_value_observed_at_ms=(observed_at_ms if oi_value is not None else None),
        buy_total_notional_usd=buy,
        sell_total_notional_usd=sell,
        ticker_observed_this_minute=ticker_observed_this_minute,
        complete=complete,
    )


def _flow_bars_covering(
    start_offset_minutes: int, end_offset_minutes: int, *, buy_per_min: float, sell_per_min: float
) -> tuple[FlowBar, ...]:
    """One complete 1-minute bar per minute in (start_offset, end_offset]
    -- full coverage of that span, for FLOW_AVAILABLE scenarios."""
    return tuple(
        _flow_bar(offset, buy=buy_per_min, sell=sell_per_min)
        for offset in range(start_offset_minutes + 1, end_offset_minutes + 1)
    )


def _point(timeline: EventTimeline, offset_minutes: int) -> TimelinePoint:
    matches = [point for point in timeline.points if point.offset_minutes == offset_minutes]
    assert len(matches) == 1
    return matches[0]


# --- point-in-time known-at rule ---


def test_price_bar_not_known_until_its_own_period_elapses() -> None:
    # A bar OPENING exactly at target_ms (duration 5m) is not known until
    # target_ms + 5m -- using ts_ms alone would leak its close early.
    still_open_bar = PriceBar(ts_ms=TRIGGER_MS, close=999.0, duration_ms=_PRICE_DURATION_MS)
    timeline = build_event_timeline(
        pump_event_id=1,
        base="BTC",
        trigger_at=TRIGGER_AT,
        price_bars=(still_open_bar,),
        lookback_offsets_minutes=(0,),
    )
    assert _point(timeline, 0).price_available is False


def test_flow_bar_not_known_until_its_own_minute_elapses() -> None:
    still_open_flow_bar = FlowBar(
        bucket_start_ms=TRIGGER_MS,
        close_price=None,
        open_interest=1000.0,
        open_interest_value=50_000.0,
        open_interest_event_at_ms=TRIGGER_MS,
        open_interest_observed_at_ms=TRIGGER_MS,
        open_interest_value_event_at_ms=TRIGGER_MS,
        open_interest_value_observed_at_ms=TRIGGER_MS,
        buy_total_notional_usd=10.0,
        sell_total_notional_usd=5.0,
        ticker_observed_this_minute=True,
        complete=True,
    )
    timeline = build_event_timeline(
        pump_event_id=1,
        base="BTC",
        trigger_at=TRIGGER_AT,
        price_bars=(_price_bar(0, 1.0),),
        flow_bars=(still_open_flow_bar,),
        lookback_offsets_minutes=(0,),
    )
    # The bucket starting exactly at 0 is not known until +1 minute, so
    # the window at offset 0 sees no complete, known-at-time-safe bar.
    assert _point(timeline, 0).flow_availability == FLOW_GAP_EXCLUDED


# --- fixed price reference anchor ---


def test_price_change_pct_measured_against_the_earliest_offset_anchor() -> None:
    timeline = build_event_timeline(
        pump_event_id=1,
        base="BTC",
        trigger_at=TRIGGER_AT,
        price_bars=(
            _price_bar(-120, 100.0),
            _price_bar(-60, 105.0),
            _price_bar(0, 110.0),
            _price_bar(60, 120.0),
        ),
        lookback_offsets_minutes=(-120, -60, 0, 60),
    )
    assert timeline.reference_price == 100.0
    assert _point(timeline, -120).price_change_pct == pytest.approx(0.0)
    assert _point(timeline, -60).price_change_pct == pytest.approx(5.0)
    assert _point(timeline, 0).price_change_pct == pytest.approx(10.0)
    assert _point(timeline, 60).price_change_pct == pytest.approx(20.0)


def test_reference_unavailable_at_anchor_makes_every_point_unresolved() -> None:
    # No bar covers the -120 anchor at all; per the fixed-anchor rule this
    # must NOT silently fall back to -60 as a different, event-specific
    # anchor (that would make aggregates across events incomparable --
    # see momentum_flow_protocol.py). Every point's price_change_pct must
    # be None, even -60 and 0, which DO have their own price data.
    timeline = build_event_timeline(
        pump_event_id=1,
        base="BTC",
        trigger_at=TRIGGER_AT,
        price_bars=(_price_bar(-60, 105.0), _price_bar(0, 110.0)),
        lookback_offsets_minutes=(-120, -60, 0),
    )
    assert timeline.reference_price is None
    assert _point(timeline, -120).price_change_pct is None
    assert _point(timeline, -60).price_available is True
    assert _point(timeline, -60).price_change_pct is None
    assert _point(timeline, 0).price_change_pct is None


def test_realized_volatility_none_below_three_closes_in_window() -> None:
    timeline = build_event_timeline(
        pump_event_id=1,
        base="BTC",
        trigger_at=TRIGGER_AT,
        price_bars=(_price_bar(-60, 100.0), _price_bar(0, 105.0)),
        lookback_offsets_minutes=(-60, 0),
    )
    assert _point(timeline, -60).realized_volatility is None
    assert _point(timeline, 0).realized_volatility is None


def test_realized_volatility_computed_once_three_closes_are_in_window() -> None:
    closes = [100.0, 105.0, 110.0, 120.0]
    timeline = build_event_timeline(
        pump_event_id=1,
        base="BTC",
        trigger_at=TRIGGER_AT,
        price_bars=tuple(
            _price_bar(offset, close)
            for offset, close in zip((-120, -60, 0, 60), closes, strict=True)
        ),
        lookback_offsets_minutes=(-120, -60, 0, 60),
    )
    expected_at_0 = pstdev([log(105.0 / 100.0), log(110.0 / 105.0)])
    expected_at_60 = pstdev([log(105.0 / 100.0), log(110.0 / 105.0), log(120.0 / 110.0)])
    assert _point(timeline, -60).realized_volatility is None
    assert _point(timeline, 0).realized_volatility == expected_at_0
    assert _point(timeline, 60).realized_volatility == expected_at_60


# --- flow availability / completeness / coverage ---


def test_flow_unavailable_pre_capture_regardless_of_flow_bars_passed() -> None:
    pre_capture_trigger = MOMENTUM_FLOW_BARS_AVAILABLE_FROM - timedelta(days=5)
    stray_bucket_start_ms = int(pre_capture_trigger.timestamp() * 1000) - 60_000
    stray_flow_bar = FlowBar(
        bucket_start_ms=stray_bucket_start_ms,
        close_price=None,
        open_interest=1000.0,
        open_interest_value=50_000.0,
        open_interest_event_at_ms=stray_bucket_start_ms,
        open_interest_observed_at_ms=stray_bucket_start_ms,
        open_interest_value_event_at_ms=stray_bucket_start_ms,
        open_interest_value_observed_at_ms=stray_bucket_start_ms,
        buy_total_notional_usd=100.0,
        sell_total_notional_usd=50.0,
        ticker_observed_this_minute=True,
        complete=True,
    )
    timeline = build_event_timeline(
        pump_event_id=1,
        base="BTC",
        trigger_at=pre_capture_trigger,
        price_bars=(
            PriceBar(
                ts_ms=int(pre_capture_trigger.timestamp() * 1000) - _PRICE_DURATION_MS,
                close=100.0,
                duration_ms=_PRICE_DURATION_MS,
            ),
        ),
        flow_bars=(stray_flow_bar,),
        lookback_offsets_minutes=(0,),
    )
    point = _point(timeline, 0)
    assert point.flow_availability == FLOW_UNAVAILABLE_PRE_CAPTURE
    assert point.buy_notional_usd is None
    assert point.sell_notional_usd is None
    assert point.oi_change_pct is None
    assert timeline.reference_oi is None


def test_full_coverage_gives_flow_available_and_correct_cumulative_sums() -> None:
    # Offsets 5 minutes apart; full coverage needs a complete bar for
    # every one of the 10 one-minute buckets spanning -10..0.
    flow_bars = _flow_bars_covering(-10, 0, buy_per_min=10.0, sell_per_min=4.0)
    timeline = build_event_timeline(
        pump_event_id=1,
        base="BTC",
        trigger_at=TRIGGER_AT,
        price_bars=(_price_bar(-10, 1.0), _price_bar(-5, 1.0), _price_bar(0, 1.0)),
        flow_bars=flow_bars,
        lookback_offsets_minutes=(-10, -5, 0),
    )
    at_minus_5 = _point(timeline, -5)
    assert at_minus_5.flow_availability == FLOW_AVAILABLE
    assert at_minus_5.flow_coverage_pct == pytest.approx(1.0)
    assert at_minus_5.buy_notional_usd == pytest.approx(50.0)  # 5 minutes x 10.0
    assert at_minus_5.sell_notional_usd == pytest.approx(20.0)  # 5 minutes x 4.0

    at_0 = _point(timeline, 0)
    assert at_0.flow_availability == FLOW_AVAILABLE
    assert at_0.buy_notional_usd == pytest.approx(100.0)  # 10 minutes x 10.0
    assert at_0.sell_notional_usd == pytest.approx(40.0)


def test_partial_coverage_excludes_neither_zero_fills_nor_silently_passes_as_available() -> None:
    # 5-minute window, only 3 of the 5 expected one-minute bars present.
    flow_bars = (
        _flow_bar(-4, buy=10.0, sell=4.0),
        _flow_bar(-3, buy=10.0, sell=4.0),
        _flow_bar(-1, buy=10.0, sell=4.0),
        # -2 and 0 (bucket -1..0) missing/incomplete entirely.
    )
    timeline = build_event_timeline(
        pump_event_id=1,
        base="BTC",
        trigger_at=TRIGGER_AT,
        price_bars=(_price_bar(-5, 1.0), _price_bar(0, 1.0)),
        flow_bars=flow_bars,
        lookback_offsets_minutes=(-5, 0),
    )
    at_0 = _point(timeline, 0)
    assert at_0.flow_availability == FLOW_PARTIAL_COVERAGE
    assert at_0.flow_coverage_pct == pytest.approx(3 / 5)
    # Partial-coverage points still expose their (undercounted) sums for
    # diagnostics, but callers must gate on flow_availability before
    # trusting them as a complete total.
    assert at_0.buy_notional_usd == pytest.approx(30.0)


def test_incomplete_flow_bar_excluded_from_cumulative_sums() -> None:
    # Offsets[0] = -3 makes the achievable window at target=0 exactly the
    # 3 one-minute buckets ending at -2, -1, and 0.
    flow_bars = (
        _flow_bar(-2, buy=100.0, sell=40.0, oi=1000.0, oi_value=50_000.0, complete=True),
        _flow_bar(-1, buy=999.0, sell=999.0, complete=False),
        _flow_bar(0, buy=50.0, sell=60.0, oi=1100.0, oi_value=54_000.0, complete=True),
    )
    timeline = build_event_timeline(
        pump_event_id=1,
        base="BTC",
        trigger_at=TRIGGER_AT,
        price_bars=(_price_bar(-3, 1.0), _price_bar(0, 1.0)),
        flow_bars=flow_bars,
        lookback_offsets_minutes=(-3, 0),
    )
    # The incomplete bucket ending at -1 is missing from the 3-bucket
    # window at offset 0, so coverage is 2/3 -> PARTIAL, not AVAILABLE,
    # and the buy/sell total excludes the decoy 999/999 values.
    at_0 = _point(timeline, 0)
    assert at_0.flow_availability == FLOW_PARTIAL_COVERAGE
    assert at_0.flow_coverage_pct == pytest.approx(2 / 3)
    assert at_0.buy_notional_usd == pytest.approx(150.0)
    assert at_0.sell_notional_usd == pytest.approx(100.0)


def test_gap_excluded_when_window_has_no_complete_flow_bar_at_all() -> None:
    flow_bars = (_flow_bar(-60, buy=100.0, sell=40.0, oi=1000.0, oi_value=50_000.0),)
    timeline = build_event_timeline(
        pump_event_id=1,
        base="BTC",
        trigger_at=TRIGGER_AT,
        price_bars=(_price_bar(-90, 1.0),),
        flow_bars=flow_bars,
        lookback_offsets_minutes=(-90, -60),
    )
    # -90 is the timeline start; no flow bar exists at or before it yet.
    at_minus_90 = _point(timeline, -90)
    assert at_minus_90.flow_availability == FLOW_GAP_EXCLUDED
    assert at_minus_90.buy_notional_usd is None


# --- fixed OI reference anchor ---


def test_oi_reference_anchored_to_earliest_requested_offset() -> None:
    # offsets[0] == -5 is the fixed OI anchor -- same offset the price
    # anchor uses, per momentum_flow_protocol.py's amended feature-set
    # section. Each bar here is fresh at its own bucket (the _flow_bar
    # default), so the anchor and the offset-0 point each resolve to
    # whichever single bar closes exactly at their own instant.
    flow_bars = tuple(
        _flow_bar(offset, buy=1.0, sell=1.0, oi=1000.0, oi_value=50_000.0) for offset in (-5,)
    ) + tuple(
        _flow_bar(offset, buy=1.0, sell=1.0, oi=1100.0, oi_value=54_000.0)
        for offset in (-4, -3, -2, -1, 0)
    )
    timeline = build_event_timeline(
        pump_event_id=1,
        base="BTC",
        trigger_at=TRIGGER_AT,
        price_bars=(_price_bar(-5, 1.0), _price_bar(0, 1.0)),
        flow_bars=flow_bars,
        lookback_offsets_minutes=(-5, 0),
    )
    assert timeline.reference_oi == 1000.0
    assert timeline.reference_oi_value == 50_000.0
    assert _point(timeline, -5).oi_change_pct == pytest.approx(0.0)
    assert _point(timeline, 0).oi_change_pct == pytest.approx((1100.0 / 1000.0 - 1) * 100.0)


def test_carried_forward_oi_not_treated_as_a_fresh_observation() -> None:
    # A bar whose open_interest_observed_at_ms falls BEFORE its own
    # bucket (oi_observed_in_own_bucket=False) is a carry-forward of an
    # earlier reading (see FlowBar's own docstring) -- it must never
    # resolve as if it were a genuine observation, even though it is
    # `complete`, has `ticker_observed_this_minute=True`, and has a
    # non-null open_interest. This is the exact colleague-review scenario
    # (2026-08-12, second amendment): ticker activity without a fresh OI
    # update in the same message.
    stale_bar = _flow_bar(-5, buy=1.0, sell=1.0, oi=1000.0, oi_observed_in_own_bucket=False)
    timeline = build_event_timeline(
        pump_event_id=1,
        base="BTC",
        trigger_at=TRIGGER_AT,
        price_bars=(_price_bar(-5, 1.0),),
        flow_bars=(stale_bar,),
        lookback_offsets_minutes=(-5,),
    )
    assert timeline.reference_oi is None
    assert _point(timeline, -5).oi_change_pct is None


def test_fresh_oi_observation_used_once_a_genuine_reading_lands() -> None:
    fresh_bar = _flow_bar(-5, buy=1.0, sell=1.0, oi=1000.0, oi_observed_in_own_bucket=True)
    timeline = build_event_timeline(
        pump_event_id=1,
        base="BTC",
        trigger_at=TRIGGER_AT,
        price_bars=(_price_bar(-5, 1.0),),
        flow_bars=(fresh_bar,),
        lookback_offsets_minutes=(-5,),
    )
    assert timeline.reference_oi == 1000.0


def test_oi_value_freshness_is_independent_from_amount_freshness() -> None:
    fresh_amount = _flow_bar(-5, buy=1.0, sell=1.0, oi=1000.0, oi_value=50_000.0)
    stale_value = replace(
        fresh_amount,
        open_interest_value_event_at_ms=fresh_amount.bucket_start_ms - 60_000,
        open_interest_value_observed_at_ms=fresh_amount.bucket_start_ms - 60_000,
    )
    timeline = build_event_timeline(
        pump_event_id=1,
        base="BTC",
        trigger_at=TRIGGER_AT,
        price_bars=(),
        flow_bars=(stale_value,),
        lookback_offsets_minutes=(-5,),
    )
    assert timeline.reference_oi == 1000.0
    assert timeline.reference_oi_value is None
    assert _point(timeline, -5).oi_change_pct == pytest.approx(0.0)
    assert _point(timeline, -5).oi_value_change_pct is None


def test_oi_reference_ignores_buy_sell_coverage_gaps_entirely() -> None:
    # OI resolution is fully independent of the buy/sell cumulative
    # coverage-fraction gate (see _closest_known_oi_at_or_before's own
    # docstring): a genuinely fresh, single OI observation at the anchor
    # resolves reference_oi even though most of the surrounding one-
    # minute buckets have no flow bar at all (which would leave buy/sell
    # coverage at FLOW_GAP_EXCLUDED/PARTIAL for the same points).
    sparse_flow_bars = (_flow_bar(-10, buy=1.0, sell=1.0, oi=1000.0),)
    timeline = build_event_timeline(
        pump_event_id=1,
        base="BTC",
        trigger_at=TRIGGER_AT,
        price_bars=(),
        flow_bars=sparse_flow_bars,
        lookback_offsets_minutes=(-10, -5),
    )
    assert timeline.reference_oi == 1000.0


def test_oi_candidate_too_stale_relative_to_the_query_point_is_unresolved() -> None:
    # The only OI-bearing bar is genuinely fresh at ITS OWN bucket, but
    # that bucket is well beyond MOMENTUM_FLOW_OI_MAX_STALENESS_MINUTES
    # (30) before the point being evaluated -- too old to represent "now"
    # for that specific lookback point, even though nothing else is wrong
    # with it.
    old_bar = _flow_bar(-60, buy=1.0, sell=1.0, oi=1000.0)  # fresh at -60
    timeline = build_event_timeline(
        pump_event_id=1,
        base="BTC",
        trigger_at=TRIGGER_AT,
        price_bars=(),
        flow_bars=(old_bar,),
        lookback_offsets_minutes=(-60, 0),
    )
    # Anchor is -60 itself, so reference_oi DOES resolve there (distance 0).
    assert timeline.reference_oi == 1000.0
    # But the point at offset 0 is 60 minutes away from that observation
    # -- past the 30-minute staleness bound -- so it must NOT reuse it.
    assert _point(timeline, 0).oi_change_pct is None


# --- misc ---


def test_any_flow_available_property() -> None:
    # A single-offset timeline is always structurally zero-width (see
    # _flow_window's own docstring) -- need at least two offsets for a
    # genuinely coverable window.
    flow_bars = _flow_bars_covering(-5, 0, buy_per_min=10.0, sell_per_min=5.0)
    timeline_with_flow = build_event_timeline(
        pump_event_id=1,
        base="BTC",
        trigger_at=TRIGGER_AT,
        price_bars=(_price_bar(-5, 1.0), _price_bar(0, 1.0)),
        flow_bars=flow_bars,
        lookback_offsets_minutes=(-5, 0),
    )
    assert timeline_with_flow.any_flow_available is True

    timeline_without_flow = build_event_timeline(
        pump_event_id=1,
        base="BTC",
        trigger_at=TRIGGER_AT,
        price_bars=(_price_bar(-5, 1.0), _price_bar(0, 1.0)),
        lookback_offsets_minutes=(-5, 0),
    )
    assert timeline_without_flow.any_flow_available is False


def test_empty_lookback_offsets_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        build_event_timeline(
            pump_event_id=1,
            base="BTC",
            trigger_at=TRIGGER_AT,
            price_bars=(),
            lookback_offsets_minutes=(),
        )
