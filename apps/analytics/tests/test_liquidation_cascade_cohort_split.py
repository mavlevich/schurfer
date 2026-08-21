from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.liquidation_cascade_cohort_split import (
    CohortAcceptance,
    CohortBoundaries,
    CohortBoundaryConflictError,
    Segment,
    assign_segment,
    classify_episodes,
    resolve_cohort_boundaries,
)
from schurfer_analytics.liquidation_cascade_episodes import CascadeEpisode

_DISCOVERY_END = datetime(2026, 8, 10, tzinfo=UTC)
_VALIDATION_END = datetime(2026, 8, 15, tzinfo=UTC)
_BOUNDARIES = CohortBoundaries(discovery_end=_DISCOVERY_END, validation_end=_VALIDATION_END)


def _episode(trigger_at: datetime, *, episode_id: int = 1) -> CascadeEpisode:
    return CascadeEpisode(
        episode_id=episode_id,
        exchange="bybit",
        symbol="TESTUSDT",
        trigger_at=trigger_at,
        last_trigger_at=trigger_at,
        peak_price_drop_pct=-0.06,
        peak_oi_drop_pct=-0.2,
        trigger_minutes=1,
        data_quality_unresolved=False,
    )


def test_episode_footprint_entirely_before_boundary_is_discovery() -> None:
    episode = _episode(_DISCOVERY_END - timedelta(hours=2))
    segment = assign_segment(
        footprint_start=episode.trigger_at - timedelta(minutes=15),
        footprint_end=episode.trigger_at + timedelta(minutes=60),
        boundaries=_BOUNDARIES,
    )
    assert segment == Segment.DISCOVERY


def test_episode_footprint_straddling_discovery_boundary_is_excluded_purge() -> None:
    episode = _episode(_DISCOVERY_END - timedelta(minutes=10))
    segment = assign_segment(
        footprint_start=episode.trigger_at - timedelta(minutes=15),
        footprint_end=episode.trigger_at + timedelta(minutes=60),
        boundaries=_BOUNDARIES,
    )
    assert segment == Segment.EXCLUDED_PURGE


def test_episode_footprint_straddling_validation_boundary_is_excluded_purge() -> None:
    episode = _episode(_VALIDATION_END - timedelta(minutes=10))
    segment = assign_segment(
        footprint_start=episode.trigger_at - timedelta(minutes=15),
        footprint_end=episode.trigger_at + timedelta(minutes=60),
        boundaries=_BOUNDARIES,
    )
    assert segment == Segment.EXCLUDED_PURGE


def test_episode_footprint_after_validation_end_is_test() -> None:
    episode = _episode(_VALIDATION_END + timedelta(hours=2))
    segment = assign_segment(
        footprint_start=episode.trigger_at - timedelta(minutes=15),
        footprint_end=episode.trigger_at + timedelta(minutes=60),
        boundaries=_BOUNDARIES,
    )
    assert segment == Segment.TEST


def test_classify_episodes_never_assigns_one_episode_to_two_segments() -> None:
    episodes = (
        _episode(_DISCOVERY_END - timedelta(hours=5), episode_id=1),
        _episode(_DISCOVERY_END - timedelta(minutes=5), episode_id=2),  # purge
        _episode(_DISCOVERY_END + timedelta(hours=5), episode_id=3),
        _episode(_VALIDATION_END - timedelta(minutes=5), episode_id=4),  # purge
        _episode(_VALIDATION_END + timedelta(hours=5), episode_id=5),
    )
    by_segment = classify_episodes(
        episodes,
        boundaries=_BOUNDARIES,
        feature_lookback_minutes=15,
        outcome_horizon_minutes=60,
    )
    seen_ids: list[int] = []
    for rows in by_segment.values():
        seen_ids.extend(row.episode_id for row in rows)
    assert sorted(seen_ids) == [1, 2, 3, 4, 5]
    assert len(seen_ids) == len(set(seen_ids))
    assert {row.episode_id for row in by_segment[Segment.DISCOVERY]} == {1}
    assert {row.episode_id for row in by_segment[Segment.VALIDATION]} == {3}
    assert {row.episode_id for row in by_segment[Segment.TEST]} == {5}
    assert {row.episode_id for row in by_segment[Segment.EXCLUDED_PURGE]} == {2, 4}


def test_resolve_cohort_boundaries_freezes_on_first_use() -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    boundaries, acceptance, changed = resolve_cohort_boundaries(
        requested=_BOUNDARIES, accepted=None, accept_new_cohort=False, now=now
    )
    assert boundaries == _BOUNDARIES
    assert acceptance.boundaries == _BOUNDARIES
    assert changed is True


def test_resolve_cohort_boundaries_reuses_matching_prior_acceptance_without_change() -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    prior = CohortAcceptance(boundaries=_BOUNDARIES, accepted_at=now)
    boundaries, acceptance, changed = resolve_cohort_boundaries(
        requested=_BOUNDARIES, accepted=prior, accept_new_cohort=False, now=now
    )
    assert boundaries == _BOUNDARIES
    assert acceptance is prior
    assert changed is False


def test_resolve_cohort_boundaries_refuses_silent_drift() -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    prior = CohortAcceptance(boundaries=_BOUNDARIES, accepted_at=now)
    moved = CohortBoundaries(
        discovery_end=_DISCOVERY_END + timedelta(days=1), validation_end=_VALIDATION_END
    )
    with pytest.raises(CohortBoundaryConflictError):
        resolve_cohort_boundaries(requested=moved, accepted=prior, accept_new_cohort=False, now=now)


def test_resolve_cohort_boundaries_allows_explicit_rebaseline() -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    prior = CohortAcceptance(boundaries=_BOUNDARIES, accepted_at=now)
    moved = CohortBoundaries(
        discovery_end=_DISCOVERY_END + timedelta(days=1), validation_end=_VALIDATION_END
    )
    boundaries, acceptance, changed = resolve_cohort_boundaries(
        requested=moved, accepted=prior, accept_new_cohort=True, now=now
    )
    assert boundaries == moved
    assert acceptance.boundaries == moved
    assert changed is True


def test_boundaries_reject_validation_end_before_discovery_end() -> None:
    with pytest.raises(ValueError, match="validation_end must be strictly after"):
        CohortBoundaries(discovery_end=_VALIDATION_END, validation_end=_DISCOVERY_END)
