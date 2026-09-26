"""Tests for the outcome-blind source-lead readiness view (pure logic + render)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from schurfer_analytics.source_lead_readiness import (
    QualifiedEpisode,
    ReadinessInputs,
    build_readiness,
)
from schurfer_analytics.source_lead_readiness_report import (
    _report_dict,
    render_json,
    render_markdown,
)

COHORT_START = datetime(2026, 9, 3, tzinfo=UTC)
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)  # ~2 weeks of exposure


def _ep(entry_at: datetime, asset: str) -> QualifiedEpisode:
    return QualifiedEpisode(entry_at=entry_at, canonical_asset_id=asset)


def _inputs(episodes: list[QualifiedEpisode], **kw: object) -> ReadinessInputs:
    base: dict[str, object] = {
        "cohort_start": COHORT_START,
        "database_now": NOW,
        "episodes": tuple(episodes),
        "captured_in_cohort": 400,
        "excluded_by_reason": {"source_identity_unapproved": 380, "no_approved_target": 11},
    }
    base.update(kw)
    return ReadinessInputs(**base)  # type: ignore[arg-type]


def test_maturity_uses_entry_time_not_row_count() -> None:
    # One episode 2h old (matured), one 5 min old (exit bar not closed -> not matured).
    eps = [_ep(NOW - timedelta(hours=2), "AAA"), _ep(NOW - timedelta(minutes=5), "BBB")]
    r = build_readiness(_inputs(eps))
    assert r.candidates == 2
    assert r.matured == 1


def test_matured_never_exceeds_candidates() -> None:
    eps = [_ep(NOW - timedelta(hours=1), f"A{i}") for i in range(9)]
    r = build_readiness(_inputs(eps))
    assert r.matured <= r.candidates == 9


def test_clusters_and_weeks_counted_by_entry_time() -> None:
    eps = [
        _ep(datetime(2026, 9, 7, tzinfo=UTC), "AAA"),  # week 37
        _ep(datetime(2026, 9, 8, tzinfo=UTC), "AAA"),  # week 37, same asset
        _ep(datetime(2026, 9, 15, tzinfo=UTC), "BBB"),  # week 38
    ]
    r = build_readiness(_inputs(eps))
    assert r.distinct_clusters == 2
    assert r.distinct_weeks == 2


def test_rate_uses_fixed_exposure_window_not_event_span() -> None:
    # 10 episodes all on a single day; exposure is cohort_start..NOW (~14.5 days).
    day = datetime(2026, 9, 10, tzinfo=UTC)
    eps = [_ep(day + timedelta(minutes=i), f"A{i}") for i in range(10)]
    r = build_readiness(_inputs(eps))
    expected = 10 / (((NOW - COHORT_START).total_seconds() / 86400.0) / 7.0)
    assert r.qualified_per_week is not None
    assert abs(r.qualified_per_week - expected) < 1e-9
    # A span-based rate (10 events within ~9 minutes) would be astronomically higher.
    assert r.qualified_per_week < 20


def test_concentration_flags_single_asset_domination() -> None:
    eps = [_ep(NOW - timedelta(hours=1), "AAA") for _ in range(10)]
    r = build_readiness(_inputs(eps))
    assert r.largest_asset_share == 1.0
    assert not r.concentration_ok


def test_below_floors_and_no_ready_field() -> None:
    eps = [_ep(NOW - timedelta(hours=1), f"A{i % 5}") for i in range(9)]
    r = build_readiness(_inputs(eps))
    assert not r.meets_episode_floor
    assert not r.meets_cluster_floor  # 5 assets < 7
    assert not r.timing_floors_met
    # There is no "ready to read" boolean: timing floors are explicitly not the gate.
    assert not hasattr(r, "ready")


def test_weeks_to_floor_projects_from_exposure_rate() -> None:
    eps = [_ep(NOW - timedelta(hours=1), f"A{i}") for i in range(10)]
    r = build_readiness(_inputs(eps))
    assert r.qualified_per_week is not None
    assert r.weeks_to_episode_floor is not None
    assert abs(r.weeks_to_episode_floor - (100 - r.matured) / r.qualified_per_week) < 1e-6


def test_empty_cohort_is_safe() -> None:
    r = build_readiness(_inputs([]))
    assert r.candidates == 0
    assert r.matured == 0
    assert r.largest_asset_share is None
    assert not r.concentration_ok


def test_render_markdown_and_json_and_fingerprint() -> None:
    eps = [_ep(NOW - timedelta(hours=1), f"A{i % 3}") for i in range(9)]
    report = build_readiness(_inputs(eps))
    payload = _report_dict(report, code_revision="test", working_tree_dirty=False)
    md = render_markdown(payload)
    assert "outcome-blind" in md
    assert "NOT the formal read gate" in md
    assert "ready to read: YES" not in md
    parsed = json.loads(render_json(payload))
    assert parsed["outcome_blind"] is True
    assert parsed["qualification_version"] == "source_lead_qualified_capture_v4"
    assert len(parsed["fingerprint_sha256"]) == 64
    # Fingerprint is stable across renders ignoring generated_at.
    again = _report_dict(report, code_revision="test", working_tree_dirty=False)
    assert again["fingerprint_sha256"] == payload["fingerprint_sha256"]


def test_funnel_separates_expected_exclusions_from_pipeline_errors() -> None:
    """Review P1: a capture that completed eligible but never qualified is an error, not
    'stopped before qualification'."""
    report = build_readiness(
        _inputs(
            [_ep(NOW - timedelta(hours=1), "A")],
            captured_in_cohort=100,
            pre_qualification_by_reason={
                "excluded:gate_not_unique_first_source": 40,
                "collecting:eligible": 2,
                "abandoned:eligible": 1,
                "complete:eligible": 3,
                "mystery:none": 1,
            },
            qualification_rows=53,
            excluded_by_reason={"source_identity_unapproved": 50},
            qualified_without_episode_by_status={"fetch_failed": 2},
        )
    )
    assert report.capture_excluded == 40
    assert report.capture_in_flight == 2
    assert report.capture_abandoned == 1
    assert report.pipeline_errors == 4
    assert report.qualified_rows == 3
    assert report.lower_funnel_reconciles  # 3 qualified == 1 candidate + 2 without episode
    md = render_markdown(_report_dict(report, code_revision="t", working_tree_dirty=False))
    assert "WARNING: 4 captures never reached qualification" in md
    assert "| fetch_failed | 2 |" in md


def test_lower_funnel_mismatch_is_flagged() -> None:
    """Review P2: qualified rows must equal formal candidates plus explained non-episodes."""
    report = build_readiness(
        _inputs(
            [],
            qualification_rows=5,
            excluded_by_reason={"source_identity_unapproved": 3},
            qualified_without_episode_by_status={},
        )
    )
    assert report.qualified_rows == 2
    assert not report.lower_funnel_reconciles
    md = render_markdown(_report_dict(report, code_revision="t", working_tree_dirty=False))
    assert "do not reconcile" in md


# --- v2 additions -----------------------------------------------------------------


def test_capacity_skips_episodes_only_when_every_slot_is_busy() -> None:
    from schurfer_analytics.source_lead_readiness import capacity_summary

    t0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    # Seven entries within one minute: six slots take six, the seventh waits.
    burst = [t0 + timedelta(seconds=5 * i) for i in range(7)]
    # One more after every slot's exit bar has ended (about 32 minutes later).
    later = [t0 + timedelta(minutes=40)]
    summary = capacity_summary(burst + later, slots=6)
    assert summary.max_concurrent == 7
    assert summary.taken == 7
    assert summary.skipped == 1
    assert summary.skipped_share == 1 / 8
    assert capacity_summary([], slots=6).skipped_share is None


def test_v2_sections_render_weeks_capacity_venues_and_exit_coverage() -> None:
    eps = [_ep(NOW - timedelta(days=d), f"A{d % 3}") for d in range(10)]
    report = build_readiness(
        _inputs(
            eps,
            target_reasons_by_venue={
                "bybit:executable": 7,
                "bybit:target_book_stale": 2,
                "binance:executable": 5,
            },
            exit_coverage={"sampled:on_time": 6, "missed:missed": 1},
            exit_lateness_ms=(100, 200, 300, 40_000),
        )
    )
    assert sum(report.qualified_by_week.values()) == 10
    assert report.capacity is not None and report.capacity.taken == 10
    assert report.exit_lateness_p50_ms == 300
    assert report.exit_lateness_p90_ms == 40_000
    md = render_markdown(_report_dict(report, code_revision="t", working_tree_dirty=False))
    for heading in (
        "## Qualified by UTC week",
        "## Capacity at 6 slots",
        "| bybit:target_book_stale | 2 |",
        "| sampled:on_time | 6 |",
        "p50 300 ms",
    ):
        assert heading in md
    # Never a price: coverage is statuses and delays only.
    assert "bid_vwap" not in md
