from datetime import UTC, datetime, timedelta

from schurfer_analytics.momentum_flow_protocol import (
    CALIBRATION_WINDOW_UNTIL,
    EXIT_HORIZONS_MINUTES,
    FLOW_AVAILABLE,
    FLOW_FULL_COVERAGE_FRACTION,
    FLOW_GAP_EXCLUDED,
    FLOW_PARTIAL_COVERAGE,
    FLOW_UNAVAILABLE_PRE_CAPTURE,
    LANE_DISTRIBUTION_SHORT,
    LANE_EARLY_LONG,
    LANE_PUMP_SHORT_FLOW_VETO,
    LOOKBACK_OFFSETS_MINUTES,
    MOMENTUM_FLOW_BARS_AVAILABLE_FROM,
    MOMENTUM_FLOW_FAMILY,
    MOMENTUM_FLOW_LANES,
    event_is_mature,
    flow_bars_available_at,
)


def test_lanes_are_exactly_the_three_registered_names_in_order() -> None:
    assert MOMENTUM_FLOW_LANES == (
        LANE_EARLY_LONG,
        LANE_DISTRIBUTION_SHORT,
        LANE_PUMP_SHORT_FLOW_VETO,
    )


def test_family_name_is_frozen() -> None:
    assert MOMENTUM_FLOW_FAMILY == "momentum_flow_state_v1"


def test_lookback_offsets_are_sorted_unique_and_include_trigger() -> None:
    assert list(LOOKBACK_OFFSETS_MINUTES) == sorted(LOOKBACK_OFFSETS_MINUTES)
    assert len(set(LOOKBACK_OFFSETS_MINUTES)) == len(LOOKBACK_OFFSETS_MINUTES)
    assert 0 in LOOKBACK_OFFSETS_MINUTES


def test_lookback_offsets_span_pre_and_post_trigger() -> None:
    assert min(LOOKBACK_OFFSETS_MINUTES) < 0
    assert max(LOOKBACK_OFFSETS_MINUTES) > 0


def test_exit_horizons_are_exactly_the_positive_lookbacks() -> None:
    assert tuple(m for m in LOOKBACK_OFFSETS_MINUTES if m > 0) == EXIT_HORIZONS_MINUTES
    assert all(m > 0 for m in EXIT_HORIZONS_MINUTES)


def test_calibration_window_until_is_after_bars_available_from() -> None:
    assert CALIBRATION_WINDOW_UNTIL > MOMENTUM_FLOW_BARS_AVAILABLE_FROM


def test_flow_bars_available_at_before_capture_start() -> None:
    assert flow_bars_available_at(MOMENTUM_FLOW_BARS_AVAILABLE_FROM - timedelta(seconds=1)) is False


def test_flow_bars_available_at_exactly_capture_start() -> None:
    assert flow_bars_available_at(MOMENTUM_FLOW_BARS_AVAILABLE_FROM) is True


def test_flow_bars_available_at_after_capture_start() -> None:
    assert flow_bars_available_at(MOMENTUM_FLOW_BARS_AVAILABLE_FROM + timedelta(days=1)) is True


def test_flow_availability_sentinels_are_distinct() -> None:
    assert (
        len(
            {
                FLOW_UNAVAILABLE_PRE_CAPTURE,
                FLOW_GAP_EXCLUDED,
                FLOW_PARTIAL_COVERAGE,
                FLOW_AVAILABLE,
            }
        )
        == 4
    )


def test_full_coverage_fraction_is_exactly_one() -> None:
    # v0 requires 100% coverage for FLOW_AVAILABLE -- see the amendment
    # note next to this constant. Not configurable, deliberately.
    assert FLOW_FULL_COVERAGE_FRACTION == 1.0


def test_event_is_mature_when_full_lookback_span_fits_before_until() -> None:
    trigger_at = CALIBRATION_WINDOW_UNTIL - timedelta(hours=6)
    assert event_is_mature(trigger_at, CALIBRATION_WINDOW_UNTIL) is True


def test_event_is_mature_false_when_post_trigger_horizon_would_exceed_until() -> None:
    # The furthest post-trigger offset is +240 minutes; a trigger only 1
    # hour before `until` cannot fit that.
    trigger_at = CALIBRATION_WINDOW_UNTIL - timedelta(hours=1)
    assert event_is_mature(trigger_at, CALIBRATION_WINDOW_UNTIL) is False


def test_event_is_mature_exactly_at_the_boundary() -> None:
    max_offset = LOOKBACK_OFFSETS_MINUTES[-1]
    trigger_at = CALIBRATION_WINDOW_UNTIL - timedelta(minutes=max_offset)
    assert event_is_mature(trigger_at, CALIBRATION_WINDOW_UNTIL) is True


def test_timezone_aware_constants() -> None:
    assert MOMENTUM_FLOW_BARS_AVAILABLE_FROM.tzinfo is UTC
    assert CALIBRATION_WINDOW_UNTIL.tzinfo is UTC


def test_reference_timestamp_is_now_in_the_past_relative_to_current_test_run() -> None:
    # Sanity guard, not a business rule: this test file is written well
    # after both constants, so both must already be real, non-placeholder
    # instants rather than e.g. datetime.min.
    assert datetime(2020, 1, 1, tzinfo=UTC) < MOMENTUM_FLOW_BARS_AVAILABLE_FROM
