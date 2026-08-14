from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.momentum_flow_episode_study_report import (
    REQUIRED_PRE_WINDOW,
    _anchor_flow_notional,
    build_episode_study_report,
    render_json,
    render_markdown,
)
from schurfer_analytics.momentum_flow_event_repository import MeasurementEvent
from schurfer_analytics.momentum_flow_protocol import LOOKBACK_OFFSETS_MINUTES
from schurfer_analytics.momentum_flow_timeline import EventTimeline, FlowBar, TimelinePoint
from schurfer_analytics.momentum_flow_watch_linkage_repository import WatchLinkage

CAPTURE_START = datetime(2026, 8, 14, 12, 4, 47, tzinfo=UTC)


def _event(
    event_id: int,
    *,
    base: str = "EDGE",
    exchange: str = "bybit",
    market_id: str | None = None,
    trigger_at: datetime,
) -> MeasurementEvent:
    return MeasurementEvent(
        pump_event_id=event_id,
        base=base,
        exchange=exchange,
        identity_key=f"{exchange}:{base}",
        market_id=market_id or f"{base}USDT",
        unified_symbol=f"{base}/USDT:USDT",
        market_type="swap",
        onboarded_at=CAPTURE_START - timedelta(days=365),
        trigger_at=trigger_at,
    )


def _dense_bars(
    *,
    start_ms: int,
    end_ms: int,
    price: float = 100.0,
    buy: float = 1_000.0,
    sell: float = 800.0,
) -> tuple[FlowBar, ...]:
    """A complete, gapless, complete-flagged one-minute bar sequence -- the
    only shape `_flow_window`'s 100%-coverage gate accepts as FLOW_AVAILABLE."""
    bars = []
    ts = start_ms
    while ts <= end_ms:
        bars.append(
            FlowBar(
                bucket_start_ms=ts,
                close_price=price,
                open_interest=1_000_000.0,
                open_interest_value=100_000_000.0,
                open_interest_event_at_ms=ts + 100,
                open_interest_observed_at_ms=ts + 200,
                open_interest_value_event_at_ms=ts + 100,
                open_interest_value_observed_at_ms=ts + 200,
                buy_total_notional_usd=buy,
                sell_total_notional_usd=sell,
                ticker_observed_this_minute=True,
                complete=True,
            )
        )
        ts += 60_000
    return tuple(bars)


def _full_window_bars(
    trigger_at: datetime, *, buy: float = 1_000.0, sell: float = 800.0
) -> tuple[FlowBar, ...]:
    trigger_ms = int(trigger_at.timestamp() * 1000)
    # One extra bar before the window's own start: a price anchor at the
    # timeline's first offset needs a CLOSE already known_at that instant
    # (known_at_ms = ts_ms + 60_000), which only a bar starting one minute
    # earlier can provide -- see momentum_flow_timeline.py's point-in-time
    # known-at rule.
    start_ms = trigger_ms + LOOKBACK_OFFSETS_MINUTES[0] * 60_000 - 60_000
    end_ms = trigger_ms + LOOKBACK_OFFSETS_MINUTES[-1] * 60_000
    return _dense_bars(start_ms=start_ms, end_ms=end_ms, buy=buy, sell=sell)


def _point(
    offset_minutes: int,
    *,
    buy_notional_usd: float | None,
    sell_notional_usd: float | None,
    flow_availability: str = "available",
) -> TimelinePoint:
    return TimelinePoint(
        offset_minutes=offset_minutes,
        at_ms=offset_minutes * 60_000,
        price_available=True,
        price_change_pct=0.0,
        realized_volatility=None,
        flow_availability=flow_availability,
        flow_coverage_pct=1.0 if buy_notional_usd is not None else 0.0,
        oi_change_pct=None,
        oi_value_change_pct=None,
        buy_notional_usd=buy_notional_usd,
        sell_notional_usd=sell_notional_usd,
        net_flow_notional_usd=(
            buy_notional_usd - sell_notional_usd
            if buy_notional_usd is not None and sell_notional_usd is not None
            else None
        ),
    )


def test_anchor_flow_notional_is_frozen_to_offset_zero_not_first_available() -> None:
    """Regression for the second colleague review: taking whichever offset
    happened to resolve FIRST could compare the event's own partial
    accumulation at one offset against the control's own partial
    accumulation at a completely different offset -- two structurally
    different periods. The anchor must always read offset 0 (the full
    [-24h, trigger) accumulation), never "whichever point resolved
    first"."""
    timeline = EventTimeline(
        pump_event_id=1,
        base="EDGE",
        trigger_at_ms=0,
        reference_price=100.0,
        reference_oi=None,
        reference_oi_value=None,
        points=(
            # An EARLIER offset resolves first in point order but must be
            # ignored -- 999 + 1 = 1000, not the value this function must
            # return.
            _point(-720, buy_notional_usd=999.0, sell_notional_usd=1.0),
            _point(0, buy_notional_usd=50.0, sell_notional_usd=50.0),
        ),
    )

    assert _anchor_flow_notional(timeline) == pytest.approx(100.0)


def test_anchor_flow_notional_is_none_when_offset_zero_itself_is_unresolved() -> None:
    """Even when an earlier offset resolved, an unresolved offset-0 point
    must not fall back to that earlier, shorter-period reading."""
    timeline = EventTimeline(
        pump_event_id=1,
        base="EDGE",
        trigger_at_ms=0,
        reference_price=100.0,
        reference_oi=None,
        reference_oi_value=None,
        points=(
            _point(-720, buy_notional_usd=999.0, sell_notional_usd=1.0),
            _point(
                0,
                buy_notional_usd=None,
                sell_notional_usd=None,
                flow_availability="gap_excluded",
            ),
        ),
    )

    assert _anchor_flow_notional(timeline) is None


def _report_kwargs(**overrides: object) -> dict[str, object]:
    trigger_at = CAPTURE_START + timedelta(days=10)
    until = trigger_at + timedelta(days=20)
    base = {
        "capture_epoch_started_at": CAPTURE_START,
        "watch_cohort_started_at": CAPTURE_START,
        "dataset_since": CAPTURE_START + REQUIRED_PRE_WINDOW,
        "until": until,
        "generated_at": until,
        "code_revision": "abc123",
        "working_tree_dirty": False,
        "event_input_fingerprint": "a" * 64,
        "bars_input_fingerprint": "b" * 64,
        "control_max_search_days": 28,
    }
    base.update(overrides)
    return base


def test_cross_venue_events_are_counted_as_secondary_not_processed() -> None:
    trigger_at = CAPTURE_START + timedelta(days=10)
    event = _event(1, exchange="gate", trigger_at=trigger_at)

    report = build_episode_study_report(
        (event,),
        {},
        {},
        **_report_kwargs(),  # type: ignore[arg-type]
    )

    assert report.complete_episodes == 0
    reasons = {row.name: row.count for row in report.exclusion_reasons}
    assert reasons["cross_venue_secondary"] == 1
    assert report.episode_results == ()  # excluded before per-episode processing


def test_immature_event_is_excluded_and_recorded() -> None:
    until = CAPTURE_START + timedelta(days=20)
    trigger_at = until - timedelta(hours=1)  # its own +240m window reaches past `until`
    event = _event(1, trigger_at=trigger_at)

    report = build_episode_study_report(
        (event,),
        {},
        {},
        **_report_kwargs(until=until, dataset_since=CAPTURE_START + REQUIRED_PRE_WINDOW),  # type: ignore[arg-type]
    )

    reasons = {row.name: row.count for row in report.exclusion_reasons}
    assert reasons["immature"] == 1
    assert report.episode_results[0].status == "immature"


def test_event_without_any_bars_is_flow_unavailable() -> None:
    trigger_at = CAPTURE_START + timedelta(days=10)
    event = _event(1, trigger_at=trigger_at)

    report = build_episode_study_report(
        (event,),
        {},  # no bars for this symbol at all
        {},
        **_report_kwargs(),  # type: ignore[arg-type]
    )

    reasons = {row.name: row.count for row in report.exclusion_reasons}
    assert reasons["event_flow_unavailable"] == 1


def test_bars_lookup_uses_the_exact_market_id_not_a_reconstructed_symbol() -> None:
    """Regression for the third colleague review: MeasurementEvent already
    carries Bybit's own EXACT market_id for a bybit_native event -- the
    report must look up bars by it directly rather than reconstructing a
    `{base}USDT` symbol, which would silently diverge from the real traded
    market id on an unusual case (e.g. a relisting)."""
    trigger_at = CAPTURE_START + timedelta(days=10)
    until = trigger_at + timedelta(days=20)
    exact_market_id = "1000EDGEUSDT"  # deliberately NOT base + "USDT"
    event = _event(1, base="EDGE", market_id=exact_market_id, trigger_at=trigger_at)

    report = build_episode_study_report(
        (event,),
        # Bars are supplied ONLY under the exact market id -- a
        # reconstructed "EDGEUSDT" lookup would find nothing here.
        {exact_market_id: _full_window_bars(trigger_at)},
        {},
        **_report_kwargs(until=until),  # type: ignore[arg-type]
    )

    row = report.episode_results[0]
    assert row.status != "event_flow_unavailable"
    assert row.event_timeline is not None
    assert row.event_timeline.any_flow_available is True


def test_complete_episode_resolves_control_and_watch_linkage() -> None:
    trigger_at = CAPTURE_START + timedelta(days=10)
    event = _event(1, trigger_at=trigger_at)
    until = trigger_at + timedelta(days=20)

    # A control candidate resolves at +2 days (see matched_controls tests: the
    # nearest candidate skips +-1 day self-exclusion). Bars must cover both
    # the event's own window and that control's window.
    control_at = trigger_at + timedelta(days=2)
    event_bars = _full_window_bars(trigger_at)
    control_bars = _full_window_bars(control_at)
    all_bars = tuple({bar.bucket_start_ms: bar for bar in (*event_bars, *control_bars)}.values())
    symbol = "EDGEUSDT"

    watch = WatchLinkage(
        pump_event_id=1,
        watch_evaluations_in_window=2,
        pre_trigger_evaluation_coverage_pct=1.0,
        watch_observable=True,
        earliest_watch_before_trigger_at=trigger_at - timedelta(minutes=20),
        lead_minutes=20.0,
        first_watch_at=trigger_at - timedelta(minutes=20),
        watch_arrived_only_after_trigger=False,
    )

    report = build_episode_study_report(
        (event,),
        {symbol: all_bars},
        {1: watch},
        **_report_kwargs(until=until),  # type: ignore[arg-type]
    )

    assert report.complete_episodes == 1
    row = report.episode_results[0]
    assert row.status == "complete"
    assert row.control_at is not None
    assert row.balance is not None and row.balance.balanced is True
    assert row.watch is watch

    assert report.watch_recall.denominator_events == 1
    assert report.watch_recall.watch_before_trigger == 1
    assert report.watch_recall.recall_pct == 100.0
    assert report.watch_recall.median_lead_minutes == 20.0

    # Both event and control price were flat (100.0 throughout the synthetic
    # fixture); the primary lookback row must reflect that, not a crash.
    primary = next(r for r in report.lookback_comparison if r.offset_minutes == 0)
    assert primary.mean_event_price_change_pct == pytest.approx(0.0)
    assert primary.mean_control_price_change_pct == pytest.approx(0.0)


def test_repeat_token_flag_is_set_on_the_second_event_for_the_same_base() -> None:
    first_at = CAPTURE_START + timedelta(days=5)
    second_at = CAPTURE_START + timedelta(days=15)
    until = second_at + timedelta(days=10)
    events = (
        _event(1, trigger_at=first_at),
        _event(2, trigger_at=second_at),
    )

    report = build_episode_study_report(
        events,
        {},
        {},
        **_report_kwargs(until=until),  # type: ignore[arg-type]
    )

    results = {row.pump_event_id: row for row in report.episode_results}
    assert results[1].repeat_token is False
    assert results[2].repeat_token is True


def test_event_before_dataset_since_raises() -> None:
    """Regression for colleague-review item (d): the repository's own query
    already filters to `since=dataset_since` (see `_run`), so an event
    before that instant reaching this function means the caller's own
    scoping is broken, not a normal, reachable production state. It must
    fail loud rather than be silently counted as an ordinary exclusion
    reason -- the previous soft-exclusion path here was dead code in every
    real CLI run and misleadingly implied otherwise."""
    dataset_since = CAPTURE_START + REQUIRED_PRE_WINDOW + timedelta(days=1)
    too_early = _event(1, trigger_at=dataset_since - timedelta(hours=1))
    until = dataset_since + timedelta(days=20)

    with pytest.raises(ValueError, match="before dataset_since"):
        build_episode_study_report(
            (too_early,),
            {},
            {},
            **_report_kwargs(until=until, dataset_since=dataset_since),  # type: ignore[arg-type]
        )


def test_upstream_exclusion_reasons_are_merged_into_the_funnel() -> None:
    """Regression for colleague-review item (d): the event cohort's own
    identity funnel (`MeasurementCohort.exclusion_reasons`, from
    `MomentumFlowEventRepository.load`) must surface in this report's own
    coverage funnel, not be silently dropped between the two."""
    report = build_episode_study_report(
        (),
        {},
        {},
        **_report_kwargs(),  # type: ignore[arg-type]
        upstream_exclusion_reasons=(("no_identity_ready_earliest_source", 4),),
    )

    reasons = {row.name: row.count for row in report.exclusion_reasons}
    assert reasons["no_identity_ready_earliest_source"] == 4


def test_contamination_beyond_primary_cohort_excludes_a_control_candidate() -> None:
    """Regression for colleague-review blocker #5: the contamination-
    exclusion set for matched controls must be wider than the primary
    `events` cohort passed to this call -- an independently-known Bybit
    pump instant for the same base must still exclude a control candidate
    even when that other pump never appears in `events` itself (e.g.
    cross-venue-first, identity-excluded, or simply outside this report's
    own [dataset_since, until) scope)."""
    trigger_at = CAPTURE_START + timedelta(days=10)
    until = trigger_at + timedelta(days=20)
    event = _event(1, trigger_at=trigger_at)
    symbol = "EDGEUSDT"

    # +-1 day is always excluded by self-exclusion against the trigger
    # itself (exactly the 24h exclusion boundary -- see momentum_flow_
    # matched_controls tests); -2 days is the nearest candidate normally
    # reachable at all.
    nearest_candidate_at = trigger_at - timedelta(days=2)
    next_candidate_at = trigger_at + timedelta(days=2)

    event_bars = _full_window_bars(trigger_at)
    nearest_bars = _full_window_bars(nearest_candidate_at)
    next_bars = _full_window_bars(next_candidate_at)
    all_bars = tuple(
        {bar.bucket_start_ms: bar for bar in (*event_bars, *nearest_bars, *next_bars)}.values()
    )

    # This base pumped again at nearest_candidate_at, but that OTHER pump
    # event is deliberately absent from `events` -- reachable only through
    # the wider contamination query.
    contamination: dict[str, tuple[datetime, ...]] = {"EDGE": (trigger_at, nearest_candidate_at)}

    report = build_episode_study_report(
        (event,),
        {symbol: all_bars},
        {},
        **_report_kwargs(until=until),  # type: ignore[arg-type]
        contamination_instants_by_base=contamination,
    )

    row = report.episode_results[0]
    assert row.status == "complete"
    assert row.control_offset_days == 2  # -2 days excluded by the wider contamination set


def test_control_selection_does_not_hunt_for_a_balanced_candidate() -> None:
    """Regression for colleague-review items (a)/(b): the nearest candidate
    whose own flow window resolves at all must be taken as-is, with its
    balance reported once -- never skipped in favor of a more convenient
    later candidate, even when a later candidate would in fact balance
    better. Using balance to keep searching would make it a de facto
    ranking input despite being documented as diagnostic-only."""
    trigger_at = CAPTURE_START + timedelta(days=10)
    until = trigger_at + timedelta(days=20)
    event = _event(1, trigger_at=trigger_at)
    symbol = "EDGEUSDT"

    # +-1 day is always excluded by self-exclusion against the trigger
    # itself (exactly the 24h exclusion boundary); -2 days is the nearest
    # candidate normally reachable, +2 days the next one searched after it.
    nearest_control_at = trigger_at - timedelta(days=2)  # searched first
    balanced_control_at = trigger_at + timedelta(days=2)  # searched second

    event_bars = _full_window_bars(trigger_at, buy=100_000.0, sell=80_000.0)
    imbalanced_control_bars = _full_window_bars(nearest_control_at, buy=100.0, sell=80.0)
    balanced_control_bars = _full_window_bars(balanced_control_at, buy=100_000.0, sell=80_000.0)
    all_bars = tuple(
        {
            bar.bucket_start_ms: bar
            for bar in (*event_bars, *imbalanced_control_bars, *balanced_control_bars)
        }.values()
    )

    report = build_episode_study_report(
        (event,),
        {symbol: all_bars},
        {},
        **_report_kwargs(until=until),  # type: ignore[arg-type]
    )

    row = report.episode_results[0]
    assert row.status == "control_unbalanced"
    assert row.control_offset_days == -2  # took the nearest candidate, not the balanced one
    assert row.balance is not None
    assert row.balance.balanced is False


def test_control_with_zero_flow_reading_is_unresolved_not_unbalanced() -> None:
    """Regression for the second colleague review: a candidate whose own
    frozen offset-0 flow reading resolves to zero cannot be balance-
    COMPARED at all -- it must be `control_unresolved`, distinct from
    `control_unbalanced` (a candidate that WAS compared and found too
    different)."""
    trigger_at = CAPTURE_START + timedelta(days=10)
    until = trigger_at + timedelta(days=20)
    event = _event(1, trigger_at=trigger_at)
    symbol = "EDGEUSDT"

    nearest_control_at = trigger_at - timedelta(days=2)  # first candidate searched

    event_bars = _full_window_bars(trigger_at, buy=1_000.0, sell=800.0)
    zero_flow_control_bars = _full_window_bars(nearest_control_at, buy=0.0, sell=0.0)
    all_bars = tuple(
        {bar.bucket_start_ms: bar for bar in (*event_bars, *zero_flow_control_bars)}.values()
    )

    report = build_episode_study_report(
        (event,),
        {symbol: all_bars},
        {},
        **_report_kwargs(until=until),  # type: ignore[arg-type]
    )

    row = report.episode_results[0]
    assert row.status == "control_unresolved"
    assert row.balance is not None
    assert row.balance.reason == "non_positive_flow_notional"


def test_unobservable_watch_coverage_is_not_counted_as_a_watch_miss() -> None:
    """Regression for the second colleague review: an event whose own WATCH
    evaluation coverage was insufficient (worker not verifiably running, or
    with a gap) must not be silently counted as a WATCH miss -- it belongs
    in `unresolved_events`, not the recall denominator."""
    trigger_at = CAPTURE_START + timedelta(days=10)
    until = trigger_at + timedelta(days=20)
    event = _event(1, trigger_at=trigger_at)

    unobservable_watch = WatchLinkage(
        pump_event_id=1,
        watch_evaluations_in_window=0,
        pre_trigger_evaluation_coverage_pct=0.0,
        watch_observable=False,
        earliest_watch_before_trigger_at=None,
        lead_minutes=None,
        first_watch_at=None,
        watch_arrived_only_after_trigger=False,
    )

    report = build_episode_study_report(
        (event,),
        {},
        {1: unobservable_watch},
        **_report_kwargs(until=until),  # type: ignore[arg-type]
    )

    assert report.watch_recall.denominator_events == 0
    assert report.watch_recall.unresolved_events == 1
    assert report.watch_recall.recall_pct is None


def test_dataset_since_before_required_pre_window_fails_closed() -> None:
    with pytest.raises(ValueError, match="pre-trigger lookback"):
        build_episode_study_report(
            (),
            {},
            {},
            **_report_kwargs(dataset_since=CAPTURE_START),  # type: ignore[arg-type]
        )


def test_render_json_and_markdown_state_prerequisite_scope() -> None:
    trigger_at = CAPTURE_START + timedelta(days=10)
    event = _event(1, exchange="gate", trigger_at=trigger_at)
    report = build_episode_study_report(
        (event,),
        {},
        {},
        **_report_kwargs(),  # type: ignore[arg-type]
    )

    markdown = render_markdown(report)
    assert "Measurement prerequisites for HYP-014" in markdown
    assert "Does not confirm the family" in markdown
    # Regression for the third colleague review: the funnel's first row
    # must not claim "(all sources)" when its own count already excludes
    # upstream identity-funnel exclusions.
    assert "Identity-ready cohort events" in markdown
    assert "Dataset events (all sources)" not in markdown

    import json

    payload = json.loads(render_json(report))
    assert payload["manifest"]["interpretation"] == (
        "measurement_prerequisites_for_hyp_014_not_confirmation"
    )
