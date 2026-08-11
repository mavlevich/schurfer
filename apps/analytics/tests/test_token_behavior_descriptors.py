from datetime import UTC, datetime, timedelta
from math import log
from statistics import pstdev

from schurfer_analytics.token_behavior_descriptors import (
    ONE_DAY_MS,
    DailyBar,
    HistoricalSpike,
    RecoveryResult,
    SpikeHistory,
    days_since_last_spike_recovery,
    detect_historical_spikes,
    historical_volatility,
    listing_age_days,
    prior_spike_count,
)

# Deliberately intraday, not UTC-midnight-aligned -- most real decisions are.
DECISION_TS = datetime(2026, 8, 9, 14, 30, tzinfo=UTC)
DAY0 = datetime(2026, 8, 9, 0, 0, tzinfo=UTC)  # the UTC day decision_ts falls on


def _bar(days_before_decision: float, close: float, high: float | None = None) -> DailyBar:
    at = DECISION_TS - timedelta(days=days_before_decision)
    return DailyBar(ts_ms=int(at.timestamp() * 1000), close=close, high=high or close)


def _day_bar(days_before_day0: int, close: float, high: float | None = None) -> DailyBar:
    """A bar aligned exactly ONE_DAY_MS apart from its neighbors, anchored to
    the UTC midnight of decision_ts's own day. Spike detection needs exact
    day-grid alignment to find "the immediately preceding day"."""
    ts_ms = int(DAY0.timestamp() * 1000) - days_before_day0 * ONE_DAY_MS
    return DailyBar(ts_ms=ts_ms, close=close, high=high or close)


# --- listing_age_days ---


def test_listing_age_days_computes_calendar_days_between_onboarding_and_decision() -> None:
    onboarded_at = DECISION_TS - timedelta(days=45, hours=6)
    assert listing_age_days(decision_ts=DECISION_TS, onboarded_at=onboarded_at) == 45.25


# --- historical_volatility ---


def test_historical_volatility_returns_none_with_fewer_than_min_returns() -> None:
    bars = (_bar(5, 100.0),)
    assert (
        historical_volatility(bars=bars, decision_ts=DECISION_TS, lookback_days=30, min_returns=1)
        is None
    )


def test_historical_volatility_requires_the_frozen_minimum() -> None:
    bars = (_bar(3, 100.0), _bar(2, 101.0), _bar(1, 99.0))  # 2 returns available
    assert (
        historical_volatility(bars=bars, decision_ts=DECISION_TS, lookback_days=30, min_returns=29)
        is None
    )


def test_historical_volatility_excludes_bars_at_or_after_decision_ts() -> None:
    bars = (_bar(3, 100.0), _bar(2, 101.0), _bar(1, 99.0), _bar(0, 500.0))
    with_leak = (*bars, _bar(-1, 900.0))
    without_leak_result = historical_volatility(
        bars=bars, decision_ts=DECISION_TS, lookback_days=30, min_returns=1
    )
    with_leak_result = historical_volatility(
        bars=with_leak, decision_ts=DECISION_TS, lookback_days=30, min_returns=1
    )
    assert without_leak_result == with_leak_result
    assert without_leak_result is not None


def test_historical_volatility_excludes_a_bar_whose_day_has_not_fully_elapsed() -> None:
    established_history = (_bar(5, 100.0), _bar(4, 101.0), _bar(3, 99.0), _bar(2, 102.0))
    same_day_bar = _bar(0.5, 5000.0)  # started only 12h before decision_ts
    without_same_day = historical_volatility(
        bars=established_history, decision_ts=DECISION_TS, lookback_days=30, min_returns=1
    )
    with_same_day = historical_volatility(
        bars=(*established_history, same_day_bar),
        decision_ts=DECISION_TS,
        lookback_days=30,
        min_returns=1,
    )
    assert without_same_day == with_same_day


def test_historical_volatility_computes_chronological_not_price_sorted_returns() -> None:
    bars = (_bar(3, 100.0), _bar(2, 80.0), _bar(1, 200.0))
    expected = pstdev([log(80.0 / 100.0), log(200.0 / 80.0)])
    actual = historical_volatility(
        bars=bars, decision_ts=DECISION_TS, lookback_days=30, min_returns=1
    )
    assert actual == expected


def test_historical_volatility_is_zero_for_constant_prices() -> None:
    bars = (_bar(3, 100.0), _bar(2, 100.0), _bar(1, 100.0))
    assert (
        historical_volatility(bars=bars, decision_ts=DECISION_TS, lookback_days=30, min_returns=1)
        == 0.0
    )


def test_historical_volatility_min_returns_is_achievable_for_an_intraday_decision() -> None:
    # Regression test for the known_at_ms-window fix: a naive ts_ms-filtered
    # 30-day window anchored to an intraday decision_ts systematically
    # undercounts by one bar versus a genuinely-covered 30-day history.
    # Anchoring to known_at_ms instead must be able to see all 30 fully
    # closed days -> 29 returns, even though decision_ts is not UTC midnight.
    bars = tuple(_day_bar(days_before_day0=n, close=100.0 + n) for n in range(1, 32))
    result = historical_volatility(
        bars=bars, decision_ts=DECISION_TS, lookback_days=30, min_returns=29
    )
    assert result is not None


# --- detect_historical_spikes ---


def test_detect_historical_spikes_finds_a_single_qualifying_day() -> None:
    bars = (
        _day_bar(3, close=100.0),
        _day_bar(2, close=100.0, high=140.0),  # 40% spike day
        _day_bar(1, close=105.0),
    )
    result = detect_historical_spikes(
        bars=bars, decision_ts=DECISION_TS, lookback_days=90, threshold_pct=30.0
    )
    assert result.coverage_ok is False  # not enough history for a 90d window in this test
    assert len(result.spikes) == 1
    spike = result.spikes[0]
    assert spike.first_day_ts_ms == spike.last_day_ts_ms
    assert spike.pre_spike_close == 100.0


def test_detect_historical_spikes_merges_consecutive_qualifying_days() -> None:
    bars = (
        _day_bar(4, close=100.0),
        _day_bar(3, close=140.0, high=145.0),  # day 1 of episode
        _day_bar(2, close=150.0, high=200.0),  # day 2, still qualifies vs previous close
        _day_bar(1, close=155.0),
    )
    result = detect_historical_spikes(
        bars=bars, decision_ts=DECISION_TS, lookback_days=90, threshold_pct=30.0
    )
    assert len(result.spikes) == 1
    spike = result.spikes[0]
    assert spike.first_day_ts_ms != spike.last_day_ts_ms
    assert spike.pre_spike_close == 100.0


def test_detect_historical_spikes_a_gap_breaks_the_run_into_two_episodes() -> None:
    bars = (
        _day_bar(5, close=100.0),
        _day_bar(4, close=140.0, high=145.0),  # spike day 1
        # day 3 missing entirely -- breaks the run
        _day_bar(
            2, close=200.0, high=280.0
        ),  # would qualify vs day 3's close, but day 3 is missing
        _day_bar(1, close=205.0),
    )
    result = detect_historical_spikes(
        bars=bars, decision_ts=DECISION_TS, lookback_days=90, threshold_pct=30.0
    )
    # day -4 qualifies (episode of 1). day -2 has no previous-day bar (day -3
    # missing) so it cannot be evaluated at all and does not qualify.
    assert len(result.spikes) == 1
    assert result.spikes[0].first_day_ts_ms == result.spikes[0].last_day_ts_ms


def test_detect_historical_spikes_current_pump_day_is_structurally_excluded() -> None:
    # The day decision_ts itself falls on is never fully closed at decision
    # time, so even an enormous same-day move cannot appear as a spike.
    bars = (
        _day_bar(1, close=100.0),
        _day_bar(0, close=100.0, high=900.0),  # today -- decision_ts's own day
    )
    result = detect_historical_spikes(
        bars=bars, decision_ts=DECISION_TS, lookback_days=90, threshold_pct=30.0
    )
    assert result.spikes == ()


def test_detect_historical_spikes_coverage_ok_requires_full_lookback_plus_boundary_day() -> None:
    # 90-day lookback needs a bar at day -91 (window_start - 1 day) to judge
    # the oldest in-window day's own previous close.
    insufficient = tuple(_day_bar(n, close=100.0) for n in range(1, 91))  # only back to day -90
    sufficient = tuple(_day_bar(n, close=100.0) for n in range(1, 92))  # back to day -91
    assert (
        detect_historical_spikes(
            bars=insufficient, decision_ts=DECISION_TS, lookback_days=90, threshold_pct=30.0
        ).coverage_ok
        is False
    )
    assert (
        detect_historical_spikes(
            bars=sufficient, decision_ts=DECISION_TS, lookback_days=90, threshold_pct=30.0
        ).coverage_ok
        is True
    )


def test_detect_historical_spikes_below_threshold_does_not_qualify() -> None:
    bars = (
        _day_bar(2, close=100.0),
        _day_bar(1, close=100.0, high=125.0),  # only 25% < 30% threshold
    )
    result = detect_historical_spikes(
        bars=bars, decision_ts=DECISION_TS, lookback_days=90, threshold_pct=30.0
    )
    assert result.spikes == ()


# --- prior_spike_count ---


def test_prior_spike_count_is_unresolved_when_coverage_is_insufficient() -> None:
    history = SpikeHistory(spikes=(), coverage_ok=False)
    assert prior_spike_count(spike_history=history) is None


def test_prior_spike_count_returns_the_real_count_when_coverage_is_sufficient() -> None:
    spike = HistoricalSpike(first_day_ts_ms=0, last_day_ts_ms=0, pre_spike_close=100.0)
    history = SpikeHistory(spikes=(spike, spike), coverage_ok=True)
    assert prior_spike_count(spike_history=history) == 2


# --- days_since_last_spike_recovery ---


def test_recovery_no_prior_spike_when_history_is_empty_but_covered() -> None:
    result = days_since_last_spike_recovery(
        bars=(),
        decision_ts=DECISION_TS,
        spike_history=SpikeHistory(spikes=(), coverage_ok=True),
        recovery_band_pct=10.0,
    )
    assert result == RecoveryResult(status="no_prior_spike")


def test_recovery_no_prior_spike_when_coverage_is_insufficient() -> None:
    # Insufficient coverage means we cannot even claim "no spike" with
    # confidence -- both cases collapse to the same unresolved outcome here.
    result = days_since_last_spike_recovery(
        bars=(),
        decision_ts=DECISION_TS,
        spike_history=SpikeHistory(spikes=(), coverage_ok=False),
        recovery_band_pct=10.0,
    )
    assert result.status == "no_prior_spike"


def test_recovery_missing_reference_price_is_distinct_from_no_prior_spike() -> None:
    # Defensive path: detect_historical_spikes should never actually produce
    # this, but the distinction must exist and be checked explicitly rather
    # than silently reusing "no_prior_spike" for a genuinely different cause.
    broken_spike = HistoricalSpike(first_day_ts_ms=0, last_day_ts_ms=0, pre_spike_close=0.0)
    result = days_since_last_spike_recovery(
        bars=(),
        decision_ts=DECISION_TS,
        spike_history=SpikeHistory(spikes=(broken_spike,), coverage_ok=True),
        recovery_band_pct=10.0,
    )
    assert result.status == "missing_reference_price"
    assert result.status != "no_prior_spike"


def test_recovery_found_within_band_measured_from_episode_end() -> None:
    spike = HistoricalSpike(
        first_day_ts_ms=int((DECISION_TS - timedelta(days=25)).timestamp() * 1000),
        last_day_ts_ms=int((DECISION_TS - timedelta(days=20)).timestamp() * 1000),
        pre_spike_close=100.0,
    )
    recovery_bar_ts_ms = spike.last_day_ts_ms + 8 * ONE_DAY_MS
    bars = (DailyBar(ts_ms=recovery_bar_ts_ms, close=108.0, high=108.0),)
    result = days_since_last_spike_recovery(
        bars=bars,
        decision_ts=DECISION_TS,
        spike_history=SpikeHistory(spikes=(spike,), coverage_ok=True),
        recovery_band_pct=10.0,
    )
    assert result.status == "recovered"
    assert result.recovered_in_days == 8.0
    # Measured from the episode's LAST day (20 days before decision), not
    # its first day (25 days before) -- recovery is from when the move
    # stopped, not when it started.
    assert result.observed_for_days == 20.0


def test_recovery_not_yet_recovered_by_decision_time() -> None:
    spike = HistoricalSpike(
        first_day_ts_ms=int((DECISION_TS - timedelta(days=20)).timestamp() * 1000),
        last_day_ts_ms=int((DECISION_TS - timedelta(days=20)).timestamp() * 1000),
        pre_spike_close=100.0,
    )
    still_far_bar = DailyBar(
        ts_ms=int((DECISION_TS - timedelta(days=1)).timestamp() * 1000), close=180.0, high=180.0
    )
    result = days_since_last_spike_recovery(
        bars=(still_far_bar,),
        decision_ts=DECISION_TS,
        spike_history=SpikeHistory(spikes=(spike,), coverage_ok=True),
        recovery_band_pct=10.0,
    )
    assert result.status == "not_yet_recovered_by_decision"
    assert result.recovered_in_days is None
    assert result.observed_for_days == 20.0


def test_recovery_uses_the_most_recent_of_multiple_prior_spikes() -> None:
    older = HistoricalSpike(
        first_day_ts_ms=int((DECISION_TS - timedelta(days=60)).timestamp() * 1000),
        last_day_ts_ms=int((DECISION_TS - timedelta(days=60)).timestamp() * 1000),
        pre_spike_close=50.0,
    )
    recent = HistoricalSpike(
        first_day_ts_ms=int((DECISION_TS - timedelta(days=20)).timestamp() * 1000),
        last_day_ts_ms=int((DECISION_TS - timedelta(days=20)).timestamp() * 1000),
        pre_spike_close=100.0,
    )
    recovery_bar = DailyBar(ts_ms=recent.last_day_ts_ms + 5 * ONE_DAY_MS, close=104.0, high=104.0)
    result = days_since_last_spike_recovery(
        bars=(recovery_bar,),
        decision_ts=DECISION_TS,
        spike_history=SpikeHistory(spikes=(older, recent), coverage_ok=True),
        recovery_band_pct=10.0,
    )
    assert result.status == "recovered"
    assert result.reference_spike == recent
    assert result.recovered_in_days == 5.0
