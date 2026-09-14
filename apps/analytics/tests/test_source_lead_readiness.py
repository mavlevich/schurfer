"""Tests for the outcome-blind source-lead readiness funnel."""

from __future__ import annotations

from schurfer_analytics.source_lead_readiness import (
    ReadinessFunnel,
    summarize_readiness,
)


def _funnel(**kw: object) -> ReadinessFunnel:
    base: dict[str, object] = {
        "captured": 1000,
        "qualification_attempts": 989,
        "qualified": 9,
        "excluded": 980,
        "excluded_by_reason": {"source_identity_unapproved": 800, "no_approved_target": 180},
        "qualified_clusters": 5,
        "qualified_weeks": 1,
        "matured": 9,
        "span_days": 6.0,
    }
    base.update(kw)
    return ReadinessFunnel(**base)  # type: ignore[arg-type]


def test_rate_is_qualified_per_week() -> None:
    f = _funnel(qualified=9, span_days=6.0)
    s = summarize_readiness(f)
    assert s.qualified_per_week is not None
    assert abs(s.qualified_per_week - (9 / (6.0 / 7.0))) < 1e-9  # ~10.5/week


def test_top_exclusion_reason_is_the_largest() -> None:
    s = summarize_readiness(_funnel())
    assert s.top_exclusion_reason == "source_identity_unapproved"
    assert s.top_exclusion_count == 800


def test_not_ready_when_below_any_floor() -> None:
    s = summarize_readiness(_funnel(matured=9, qualified_clusters=5, qualified_weeks=1))
    assert not s.meets_episode_floor
    assert not s.meets_cluster_floor
    assert not s.meets_week_floor
    assert not s.ready


def test_ready_only_when_all_floors_met() -> None:
    s = summarize_readiness(_funnel(matured=120, qualified_clusters=8, qualified_weeks=5))
    assert s.meets_episode_floor and s.meets_cluster_floor and s.meets_week_floor
    assert s.ready


def test_episode_floor_uses_matured_not_qualified() -> None:
    # 120 qualified but only 40 matured -> episode floor NOT met (only matured can read).
    f = _funnel(qualified=120, matured=40, qualified_clusters=8, qualified_weeks=5)
    s = summarize_readiness(f)
    assert not s.meets_episode_floor
    assert not s.ready


def test_weeks_to_floor_projects_from_rate() -> None:
    # 10 matured, ~10.5/week -> need 90 more -> ~8.6 weeks.
    f = _funnel(qualified=9, matured=10, span_days=6.0)
    s = summarize_readiness(f)
    assert s.weeks_to_episode_floor is not None
    assert abs(s.weeks_to_episode_floor - (90 / (9 / (6.0 / 7.0)))) < 1e-6


def test_weeks_to_floor_is_none_when_rate_zero() -> None:
    s = summarize_readiness(_funnel(qualified=0, matured=0, span_days=6.0))
    assert s.weeks_to_episode_floor is None


def test_weeks_to_floor_none_when_already_met() -> None:
    s = summarize_readiness(_funnel(matured=150, qualified_clusters=8, qualified_weeks=5))
    assert s.meets_episode_floor
    assert s.weeks_to_episode_floor is None
