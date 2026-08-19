from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.binance_watch_input_coverage_report import (
    WRITER_MATURITY_BUFFER_SECONDS,
    CoverageWindow,
    build_report,
    check_bucket_count,
    resolve_until,
)


def _window(**overrides: object) -> CoverageWindow:
    defaults: dict[str, object] = {
        "since": datetime(2026, 8, 17, 0, 0, tzinfo=UTC),
        "until": datetime(2026, 8, 17, 6, 0, tzinfo=UTC),
        "decision_delay_seconds": 90,
    }
    defaults.update(overrides)
    return CoverageWindow(**defaults)  # type: ignore[arg-type]


def test_coverage_window_rejects_since_after_until() -> None:
    with pytest.raises(ValueError, match="since must be earlier"):
        _window(
            since=datetime(2026, 8, 17, 6, 0, tzinfo=UTC),
            until=datetime(2026, 8, 17, 0, 0, tzinfo=UTC),
        )


def test_coverage_window_rejects_non_positive_decision_delay() -> None:
    with pytest.raises(ValueError, match="decision_delay_seconds"):
        _window(decision_delay_seconds=0)


def test_build_report_computes_overall_and_hourly_quality_ready_rate() -> None:
    window = _window()
    hour_a = datetime(2026, 8, 17, 0, 0, tzinfo=UTC)
    hour_b = datetime(2026, 8, 17, 1, 0, tzinfo=UTC)

    report = build_report(
        window,
        buckets_evaluated=4,
        quality_ready_flags=[True, True, False, True],
        reason_tuples=[(), (), ("missing_fresh_oi",), ()],
        hour_starts=[hour_a, hour_a, hour_b, hour_b],
    )

    assert report.buckets_evaluated == 4
    assert report.total_evaluations == 4
    assert report.quality_ready == 3
    assert report.quality_ready_pct == pytest.approx(75.0)

    by_hour = {row.hour_start: row for row in report.hourly}
    assert by_hour[hour_a].evaluations == 2
    assert by_hour[hour_a].quality_ready == 2
    assert by_hour[hour_a].quality_ready_pct == pytest.approx(100.0)
    assert by_hour[hour_b].evaluations == 2
    assert by_hour[hour_b].quality_ready == 1
    assert by_hour[hour_b].quality_ready_pct == pytest.approx(50.0)


def test_build_report_counts_multi_reason_evaluations_in_every_reason_row() -> None:
    # Regression: prepare_symbol_evaluation never short-circuits after the
    # first failing check (see its own doc comment), so one evaluation can
    # legitimately carry more than one reason -- each must be counted in
    # its own reason row, not just the first.
    window = _window()
    hour = datetime(2026, 8, 17, 0, 0, tzinfo=UTC)

    report = build_report(
        window,
        buckets_evaluated=1,
        quality_ready_flags=[False],
        reason_tuples=[("missing_price", "missing_fresh_oi", "stale_quote")],
        hour_starts=[hour],
    )

    reasons = {row.reason: row.count for row in report.reasons}
    assert reasons == {"missing_price": 1, "missing_fresh_oi": 1, "stale_quote": 1}
    # Each reason is 100% of the single evaluation -- the percentages are
    # deliberately allowed to sum past 100%, see render_markdown's own note.
    assert all(row.pct == pytest.approx(100.0) for row in report.reasons)


def test_build_report_sorts_reasons_by_count_descending_then_name() -> None:
    window = _window()
    hour = datetime(2026, 8, 17, 0, 0, tzinfo=UTC)

    report = build_report(
        window,
        buckets_evaluated=1,
        quality_ready_flags=[False, False, False],
        reason_tuples=[
            ("stale_quote",),
            ("missing_fresh_oi",),
            ("missing_fresh_oi",),
        ],
        hour_starts=[hour, hour, hour],
    )

    assert [row.reason for row in report.reasons] == ["missing_fresh_oi", "stale_quote"]
    assert report.reasons[0].count == 2
    assert report.reasons[1].count == 1


def test_build_report_handles_zero_evaluations_without_dividing_by_zero() -> None:
    window = _window()
    report = build_report(
        window, buckets_evaluated=0, quality_ready_flags=[], reason_tuples=[], hour_starts=[]
    )
    assert report.total_evaluations == 0
    assert report.quality_ready == 0
    assert report.quality_ready_pct == 0.0
    assert report.reasons == ()
    assert report.hourly == ()


def test_decision_delay_default_matches_module_constant() -> None:
    from schurfer_analytics.binance_watch_input_coverage_report import (
        DEFAULT_DECISION_DELAY_SECONDS,
        build_parser,
    )

    args = build_parser().parse_args(["--since", "2026-08-17T00:00:00Z"])
    assert args.decision_delay_seconds == DEFAULT_DECISION_DELAY_SECONDS


def test_until_defaults_to_none_and_is_resolved_by_run_not_the_parser() -> None:
    from schurfer_analytics.binance_watch_input_coverage_report import build_parser

    args = build_parser().parse_args(["--since", "2026-08-17T00:00:00Z"])
    assert args.until is None


def test_bucket_start_plus_decision_delay_lands_after_bucket_closes() -> None:
    # Sanity check on the constant itself: a bucket "closes" one minute
    # after its own bucket_start (1-minute bars); the default delay must
    # leave real margin past that close, not just barely clear it.
    from schurfer_analytics.binance_watch_input_coverage_report import (
        DEFAULT_DECISION_DELAY_SECONDS,
    )

    bucket_start = datetime(2026, 8, 17, 0, 0, tzinfo=UTC)
    evaluator_started_at = bucket_start + timedelta(seconds=DEFAULT_DECISION_DELAY_SECONDS)
    bucket_close = bucket_start + timedelta(minutes=1)
    assert evaluator_started_at > bucket_close


def test_resolve_until_trusts_an_explicit_value_with_no_margin_applied() -> None:
    explicit = datetime(2026, 8, 17, 3, 0, tzinfo=UTC)
    now = datetime(2026, 8, 17, 6, 0, tzinfo=UTC)
    assert resolve_until(explicit, decision_delay_seconds=90, now=now) == explicit


def test_resolve_until_pads_the_default_past_decision_delay_and_writer_flush() -> None:
    # Regression: a code-review finding caught --until defaulting straight
    # to now() with no margin, which would replay buckets before capture
    # had actually finished writing/completing them -- not a real
    # quality-gate failure, just real time not having caught up.
    now = datetime(2026, 8, 17, 6, 0, tzinfo=UTC)
    resolved = resolve_until(None, decision_delay_seconds=90, now=now)
    assert resolved == now - timedelta(seconds=90 + WRITER_MATURITY_BUFFER_SECONDS)
    assert resolved < now - timedelta(seconds=90)


def test_check_bucket_count_passes_within_the_limit() -> None:
    check_bucket_count(100, max_buckets=100)  # inclusive boundary, must not raise


def test_check_bucket_count_fails_loudly_over_the_limit() -> None:
    # Regression: a code-review finding on the earlier, unbounded design --
    # a huge window must be rejected outright, not silently truncated into
    # a report that mislabels what it actually covered.
    with pytest.raises(ValueError, match="over --max-buckets"):
        check_bucket_count(101, max_buckets=100)
