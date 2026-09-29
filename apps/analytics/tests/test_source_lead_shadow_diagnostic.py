"""The registered latency diagnostic never turns missing attempts into fast quotes."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.source_lead_shadow_diagnostic import (
    _ROWS,
    SHADOW_VERSION,
    ShadowRow,
    build_report,
    render_markdown,
)

S = datetime(2026, 9, 29, 0, 1, tzinfo=UTC)
FIRST_WEEK_END = datetime(2026, 10, 5, tzinfo=UTC)
SECOND_WEEK_END = datetime(2026, 10, 12, tzinfo=UTC)


def _row(
    capture_id: int,
    *,
    source_at: datetime = S,
    outcome: str | None = "shadow_recorded",
    quote_delay_s: int | None = 5,
    shadow_version: str | None = SHADOW_VERSION,
    late: bool = False,
    change_bps: float | None = 2.0,
) -> ShadowRow:
    observed = source_at + timedelta(seconds=1)
    qualified = source_at + timedelta(seconds=2)
    seen = source_at + timedelta(seconds=3)
    requested = source_at + timedelta(seconds=4)
    received = source_at + timedelta(seconds=quote_delay_s) if quote_delay_s else None
    return ShadowRow(
        capture_id=capture_id,
        source_first_observed_at=source_at,
        observed_at=observed,
        qualified_at=qualified,
        shadow_version=shadow_version,
        outcome=outcome if shadow_version else None,
        first_seen_at=seen if shadow_version else None,
        quote_requested_at=requested if shadow_version else None,
        quote_received_at=received if shadow_version else None,
        book_ts_ms=(round(received.timestamp() * 1000) - 300) if received else None,
        late=late if shadow_version else None,
        quote_change_bps=change_bps if shadow_version and received else None,
    )


def test_empty_and_missing_marks_are_explicit() -> None:
    missing = _row(1, shadow_version=None)
    report = build_report([missing], until=FIRST_WEEK_END)
    week = report["weekly"][0]
    assert week["qualified"] == 1
    assert week["outcomes"] == {"no_attempt": 1}
    assert week["quote_coverage"] == 0
    assert week["segments"]["capture_ms"]["n"] == 1
    assert week["segments"]["qualification_ms"]["n"] == 1
    assert week["segments"]["end_to_end_after_detection_ms"]["n"] == 0
    assert report["engineering_rule"]["status"] == "pending"
    assert "unavailable" in report["heartbeat_gap_history"]
    assert "no_attempt" in render_markdown(report)


def test_first_eligible_prefix_fixes_detection_branch() -> None:
    first_week = [_row(i) for i in range(1, 31)]
    # A later bad week must not change which first eligible prefix was read.
    second_week = [
        _row(31, source_at=S + timedelta(days=7), outcome="stale_book", quote_delay_s=90),
        _row(32, source_at=S + timedelta(days=7), shadow_version=None),
    ]
    report = build_report(first_week + second_week, until=SECOND_WEEK_END)
    rule = report["engineering_rule"]
    assert rule["status"] == "detection_next"
    assert rule["first_eligible_week_end_utc"] == FIRST_WEEK_END.isoformat()
    assert rule["shadow_recorded"] == 30
    assert rule["quote_coverage"] == 1
    assert report["weekly"][1]["outcomes"] == {"no_attempt": 1, "stale_book": 1}
    assert report["weekly"][1]["segments"]["end_to_end_after_detection_ms"]["p50"] == 90_000


def test_low_coverage_selects_pipeline_and_names_failure() -> None:
    rows = [_row(i) for i in range(1, 31)]
    rows.extend(_row(i, shadow_version=None) for i in range(31, 41))
    report = build_report(rows, until=FIRST_WEEK_END)
    rule = report["engineering_rule"]
    assert rule["status"] == "pipeline_first"
    assert rule["failed_conditions"] == ["quote_coverage_below_90pct"]
    assert rule["quote_coverage"] == 0.75


def test_missing_and_negative_segments_are_not_silently_dropped() -> None:
    rows = [
        _row(1, outcome="stale_book", quote_delay_s=2, late=True, change_bps=-3),
        _row(2, outcome="fetch_failed", quote_delay_s=None, change_bps=None),
        _row(3, shadow_version="unexpected_v2"),
    ]
    report = build_report(rows, until=FIRST_WEEK_END)
    week = report["weekly"][0]
    assert week["outcomes"] == {
        "fetch_failed": 1,
        "stale_book": 1,
        "version_mismatch": 1,
    }
    assert week["segments"]["processing_ms"]["n"] == 2
    assert week["segments"]["end_to_end_after_detection_ms"]["n"] == 1
    assert week["segments"]["quote_round_trip_ms"]["negative_n"] == 1
    assert week["quote_change_bps"]["late"]["negative_n"] == 1
    assert week["late_share_of_attempts"] == 0.5
    assert week["late_flag_mismatch_n"] == 1


def test_week_bounds_and_duplicate_identity_fail_closed() -> None:
    with pytest.raises(ValueError, match="Monday"):
        build_report([], until=FIRST_WEEK_END + timedelta(days=1))
    with pytest.raises(ValueError, match="duplicate"):
        build_report([_row(1), _row(1)], until=FIRST_WEEK_END)
    with pytest.raises(ValueError, match="outside"):
        build_report([_row(1, source_at=FIRST_WEEK_END)], until=FIRST_WEEK_END)


def test_quote_change_without_received_book_is_flagged_and_not_reported() -> None:
    corrupted = replace(_row(1, quote_delay_s=None), quote_change_bps=8.0)
    week = build_report([corrupted], until=FIRST_WEEK_END)["weekly"][0]
    assert week["quote_change_without_quote_n"] == 1
    assert week["quote_change_bps"]["on_time"]["n"] == 0


def test_repository_query_reads_only_operational_fields() -> None:
    sql = str(_ROWS).lower()
    assert "source_lead_shadow_attempts" in sql
    assert "trade_decisions" not in sql
    assert "outcome_bar" not in sql
    assert "pnl" not in sql
    assert "return" not in sql
    assert "ask_vwap" not in sql
