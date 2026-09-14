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
    assert parsed["qualification_version"] == "source_lead_qualified_capture_v3"
    assert len(parsed["fingerprint_sha256"]) == 64
    # Fingerprint is stable across renders ignoring generated_at.
    again = _report_dict(report, code_revision="test", working_tree_dirty=False)
    assert again["fingerprint_sha256"] == payload["fingerprint_sha256"]
