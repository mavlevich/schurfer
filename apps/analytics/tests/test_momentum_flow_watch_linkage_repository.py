from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.momentum_flow_watch_linkage_repository import (
    PRE_TRIGGER_WINDOW_MINUTES,
    InstrumentWindow,
    build_watch_linkage,
    watch_cohort_started_at_statement,
    watch_evaluations_statement,
)
from sqlalchemy.dialects import postgresql

TRIGGER_AT = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _window(event_id: int = 1, *, trigger_at: datetime = TRIGGER_AT) -> InstrumentWindow:
    return InstrumentWindow(
        pump_event_id=event_id,
        exchange="bybit",
        market_type="linear",
        symbol="EDGEUSDT",
        trigger_at=trigger_at,
    )


def _row(
    status: str,
    decision_at: datetime,
    *,
    exchange: str = "bybit",
    market_type: str = "linear",
    symbol: str = "EDGEUSDT",
    bucket_start: datetime | None = None,
    quality_ready: bool = True,
) -> tuple[str, str, str, datetime, bool, str, datetime]:
    """`decision_at` is the row's own availability instant; `bucket_start`
    (the market minute the row actually covers) defaults to the SAME
    instant unless a test needs to separate them -- evaluator latency,
    catch-up, or a restart-driven backlog, see the module docstring on why
    these two timestamps are not interchangeable."""
    return (
        exchange,
        market_type,
        symbol,
        bucket_start if bucket_start is not None else decision_at,
        quality_ready,
        status,
        decision_at,
    )


def test_pre_trigger_window_matches_the_frozen_post_trigger_lookback_span() -> None:
    # Amended after colleague review: the search bound must come from the
    # frozen offset grid (240 minutes), not the WATCH evaluator's own
    # internal 60-minute feature lookback.
    assert PRE_TRIGGER_WINDOW_MINUTES == 240


def test_watch_cohort_statement_does_not_filter_by_worker_status() -> None:
    sql = str(
        watch_cohort_started_at_statement().compile(
            dialect=postgresql.dialect()  # type: ignore[no-untyped-call]
        )
    )
    assert "app.momentum_flow_watch_runs" in sql
    assert "momentum_flow_watch_runs.watch_version" in sql
    assert "momentum_flow_watch_runs.status" not in sql


def test_evaluations_statement_is_none_for_empty_windows() -> None:
    assert watch_evaluations_statement(()) is None


def test_evaluations_statement_bounds_pre_and_post_trigger_window() -> None:
    # Regression for the colleague-review mypy finding: watch_evaluations_
    # statement's return type is `Select[Any] | None` (see its own docstring
    # on why an empty windows tuple must return None rather than an
    # unbounded scan); calling `.compile` straight off the call result left
    # mypy unable to prove the non-empty-input branch actually returns a
    # statement. Assert it explicitly rather than relying on a bare index/
    # call to narrow the type implicitly.
    statement = watch_evaluations_statement((_window(),))
    assert statement is not None
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "timeseries.momentum_flow_watch_evaluations_1m" in sql
    assert "'2026-08-20 08:00:00" in sql  # trigger - 240m
    assert "'2026-08-20 12:15:00" in sql  # trigger + 15m
    assert "bucket_start" in sql
    assert "quality_ready" in sql


def test_linkage_finds_earliest_watch_before_trigger_and_computes_lead() -> None:
    window = _window()
    rows = [
        _row("rejected_signal", TRIGGER_AT - timedelta(minutes=50)),
        _row("watch", TRIGGER_AT - timedelta(minutes=30)),
        _row("watch", TRIGGER_AT - timedelta(minutes=10)),
    ]

    linkage = build_watch_linkage((window,), rows)[window.pump_event_id]

    assert linkage.watch_evaluations_in_window == 3
    assert linkage.earliest_watch_before_trigger_at == TRIGGER_AT - timedelta(minutes=30)
    assert linkage.lead_minutes == 30
    assert linkage.first_watch_at == TRIGGER_AT - timedelta(minutes=30)
    assert linkage.watch_arrived_only_after_trigger is False


def test_linkage_finds_watch_leading_by_more_than_an_hour() -> None:
    """The exact scenario the 60-minute cap used to lose."""
    window = _window()
    rows = [_row("watch", TRIGGER_AT - timedelta(minutes=180))]

    linkage = build_watch_linkage((window,), rows)[window.pump_event_id]

    assert linkage.earliest_watch_before_trigger_at == TRIGGER_AT - timedelta(minutes=180)
    assert linkage.lead_minutes == 180


def test_linkage_flags_watch_arriving_only_after_trigger() -> None:
    window = _window()
    rows = [_row("watch", TRIGGER_AT + timedelta(minutes=5))]

    linkage = build_watch_linkage((window,), rows)[window.pump_event_id]

    assert linkage.earliest_watch_before_trigger_at is None
    assert linkage.lead_minutes is None
    assert linkage.first_watch_at == TRIGGER_AT + timedelta(minutes=5)
    assert linkage.watch_arrived_only_after_trigger is True


def test_linkage_matches_rows_to_the_correct_instrument_only() -> None:
    window = _window(event_id=1)
    other_window = InstrumentWindow(
        pump_event_id=2,
        exchange="bybit",
        market_type="linear",
        symbol="OTHERUSDT",
        trigger_at=TRIGGER_AT,
    )
    rows = [_row("watch", TRIGGER_AT - timedelta(minutes=5), symbol="OTHERUSDT")]

    linkage = build_watch_linkage((window, other_window), rows)

    assert linkage[1].watch_evaluations_in_window == 0
    assert linkage[1].earliest_watch_before_trigger_at is None
    assert linkage[2].watch_evaluations_in_window == 1
    assert linkage[2].earliest_watch_before_trigger_at == TRIGGER_AT - timedelta(minutes=5)


def test_linkage_does_not_leak_a_watch_decision_to_a_repeat_tokens_other_pump() -> None:
    """A repeat token: the same instrument pumps twice. The SQL layer
    returns the union of both windows' own ranges for that one instrument
    (see watch_evaluations_statement's own docstring) -- build_watch_linkage
    must still attribute each decision only to the window it actually falls
    inside, not to every window sharing that instrument."""
    early_trigger = TRIGGER_AT
    later_trigger = TRIGGER_AT + timedelta(days=5)
    early_window = _window(event_id=1, trigger_at=early_trigger)
    later_window = _window(event_id=2, trigger_at=later_trigger)
    rows = [
        # Belongs only to the early event's own [-240m, +15m] window.
        _row("watch", early_trigger - timedelta(minutes=20)),
        # Belongs only to the later event's own window, 5 days away.
        _row("watch", later_trigger - timedelta(minutes=10)),
    ]

    linkage = build_watch_linkage((early_window, later_window), rows)

    assert linkage[1].watch_evaluations_in_window == 1
    assert linkage[1].earliest_watch_before_trigger_at == early_trigger - timedelta(minutes=20)
    assert linkage[2].watch_evaluations_in_window == 1
    assert linkage[2].earliest_watch_before_trigger_at == later_trigger - timedelta(minutes=10)


def test_linkage_reports_no_evaluations_without_error() -> None:
    window = _window()
    linkage = build_watch_linkage((window,), [])[window.pump_event_id]
    assert linkage.watch_evaluations_in_window == 0
    assert linkage.first_watch_at is None
    assert linkage.watch_arrived_only_after_trigger is False
    # Regression for the second colleague review: zero evaluation rows is
    # zero coverage, not a resolved "no watch" reading.
    assert linkage.pre_trigger_evaluation_coverage_pct == 0.0
    assert linkage.watch_observable is False


def test_linkage_full_pre_trigger_coverage_is_observable() -> None:
    window = _window()
    rows = [
        _row(
            "rejected_signal",
            TRIGGER_AT - timedelta(minutes=PRE_TRIGGER_WINDOW_MINUTES) + timedelta(minutes=i),
        )
        for i in range(PRE_TRIGGER_WINDOW_MINUTES + 1)
    ]

    linkage = build_watch_linkage((window,), rows)[window.pump_event_id]

    assert linkage.pre_trigger_evaluation_coverage_pct == pytest.approx(1.0)
    assert linkage.watch_observable is True


def test_linkage_partial_pre_trigger_coverage_is_not_observable() -> None:
    """Regression for the second colleague review: partial pre-trigger
    evaluation coverage (worker gap, or not running yet) must not be
    silently read as a confirmed absence of WATCH -- it must be
    unresolved/unobservable, not a negative."""
    window = _window()
    rows = [
        _row(
            "rejected_signal",
            TRIGGER_AT - timedelta(minutes=PRE_TRIGGER_WINDOW_MINUTES) + timedelta(minutes=i),
        )
        for i in range(PRE_TRIGGER_WINDOW_MINUTES + 1)
        if i % 2 == 0  # every other minute only -- a genuine gap
    ]

    linkage = build_watch_linkage((window,), rows)[window.pump_event_id]

    assert linkage.pre_trigger_evaluation_coverage_pct < 1.0
    assert linkage.watch_observable is False


def test_coverage_uses_bucket_start_not_decision_at_during_a_catchup_burst() -> None:
    """Regression for the THIRD colleague review: during a catch-up burst
    after a restart/backlog, many DIFFERENT market minutes (bucket_start)
    can all have their own decisions become available within the SAME
    wall-clock minute (decision_at). Deduplicating by decision_at would
    collapse all of them into a single "observed" minute, drastically
    undercounting coverage even though every one of those market minutes
    really was processed. Deduplicating by bucket_start (the fix) counts
    each real market minute once, correctly."""
    window = _window()
    # Every row's own decision became available in the SAME wall-clock
    # minute (a catch-up burst), one minute before the trigger -- still
    # comfortably `<= window.trigger_at`, so the availability gate is not
    # what's being tested here.
    burst_decision_at = TRIGGER_AT - timedelta(minutes=1)
    rows = [
        _row(
            "rejected_signal",
            burst_decision_at,
            bucket_start=(
                TRIGGER_AT - timedelta(minutes=PRE_TRIGGER_WINDOW_MINUTES) + timedelta(minutes=i)
            ),
        )
        for i in range(PRE_TRIGGER_WINDOW_MINUTES + 1)
    ]

    linkage = build_watch_linkage((window,), rows)[window.pump_event_id]

    # Under the OLD (decision_at-keyed) coverage logic this would have
    # floored to ONE single minute across all 241 rows, reading as ~0.4%
    # coverage instead of the correct 100%.
    assert linkage.pre_trigger_evaluation_coverage_pct == pytest.approx(1.0)
    assert linkage.watch_observable is True


def test_coverage_excludes_a_bucket_whose_decision_arrived_after_the_trigger() -> None:
    """Regression for the THIRD colleague review: a bucket whose own market
    minute falls before the trigger but whose DECISION only became
    available after the trigger (evaluator backlog) could not have informed
    a live strategy before the pump -- it must not count toward pre-trigger
    coverage."""
    window = _window()
    rows = [
        _row(
            "rejected_signal",
            TRIGGER_AT - timedelta(minutes=PRE_TRIGGER_WINDOW_MINUTES) + timedelta(minutes=i),
        )
        for i in range(PRE_TRIGGER_WINDOW_MINUTES)  # every minute except the last
    ]
    # The final pre-trigger minute's own decision only became available
    # after the trigger (a backlog spike right before the pump).
    rows.append(
        _row(
            "rejected_signal",
            TRIGGER_AT + timedelta(minutes=1),
            bucket_start=TRIGGER_AT,
        )
    )

    linkage = build_watch_linkage((window,), rows)[window.pump_event_id]

    assert linkage.pre_trigger_evaluation_coverage_pct < 1.0
    assert linkage.watch_observable is False


def test_coverage_excludes_quality_rejected_buckets() -> None:
    """Regression for the THIRD colleague review: the registered validation
    plan's own recall denominator is `pumps_with_complete_pre_window` --
    only windows whose pre-trigger span was QUALITY-ready throughout. A
    `quality_ready=False` bucket (decision_status="rejected_quality") was
    processed but never reached a real watch/no-watch call, so it must not
    count toward coverage even though a row for it exists."""
    window = _window()
    rows = [
        _row(
            "rejected_signal",
            TRIGGER_AT - timedelta(minutes=PRE_TRIGGER_WINDOW_MINUTES) + timedelta(minutes=i),
        )
        for i in range(PRE_TRIGGER_WINDOW_MINUTES)
    ]
    rows.append(
        _row(
            "rejected_quality",
            TRIGGER_AT,
            quality_ready=False,
        )
    )

    linkage = build_watch_linkage((window,), rows)[window.pump_event_id]

    assert linkage.pre_trigger_evaluation_coverage_pct < 1.0
    assert linkage.watch_observable is False
