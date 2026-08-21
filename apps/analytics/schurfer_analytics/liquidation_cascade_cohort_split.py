"""Frozen discovery/validation/test cohort boundary for analysis/liquidation-
cascade-validation-v2.

The recovered `feature/alpha-research` grid search (`583213f`, never merged)
ran its whole sweep against one undivided window and picked a leaderboard
row after looking at all of it -- no split at all. This module freezes a
TWO-boundary chronological split (`discovery_end`, `validation_end`) once,
the same "accept once, refuse silent drift" guarantee
`momentum_flow_cohort_acceptance.py` already provides for its own single
boundary, generalized to two fields here because moving either boundary
after looking at results is exactly the p-hacking failure mode this PR
exists to close.

`assign_segment` keeps every episode's own feature-lookback and outcome-
observation footprint (not just its trigger instant) out of the neighboring
segment: an episode whose footprint straddles a boundary is `excluded_purge`,
never silently rounded into whichever side is closer. This is the purge/
embargo the plan calls for, expressed directly as footprint containment
rather than a separate timedelta parameter every caller would otherwise have
to apply consistently by hand.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .liquidation_cascade_episodes import CascadeEpisode

COHORT_STATE_ENV_VAR = "LIQUIDATION_CASCADE_VALIDATION_COHORT_STATE_PATH"
DEFAULT_COHORT_STATE_PATH = "runtime/liquidation-cascade-validation-cohort.json"


class Segment(StrEnum):
    DISCOVERY = "discovery"
    VALIDATION = "validation"
    TEST = "test"
    EXCLUDED_PURGE = "excluded_purge"


@dataclass(frozen=True)
class CohortBoundaries:
    """Both boundaries are exclusive upper bounds: `[.., discovery_end)` is
    the discovery side, `[discovery_end, validation_end)` (minus purge) is
    validation, `[validation_end, ..)` is test."""

    discovery_end: datetime
    validation_end: datetime

    def __post_init__(self) -> None:
        fields = (("discovery_end", self.discovery_end), ("validation_end", self.validation_end))
        for name, value in fields:
            if value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.validation_end <= self.discovery_end:
            raise ValueError("validation_end must be strictly after discovery_end")


@dataclass(frozen=True)
class CohortAcceptance:
    boundaries: CohortBoundaries
    accepted_at: datetime


class CohortBoundaryConflictError(ValueError):
    """Raised when a run's requested discovery/validation boundary differs
    from the already-accepted one and the operator did not explicitly opt
    into re-baselining via `--accept-new-cohort-boundary`."""


def resolve_cohort_state_path(explicit: str | None, *, env: Mapping[str, str]) -> Path:
    return Path(explicit or env.get(COHORT_STATE_ENV_VAR) or DEFAULT_COHORT_STATE_PATH)


def parse_accepted_cohort(payload: str) -> CohortAcceptance:
    data = json.loads(payload)
    return CohortAcceptance(
        boundaries=CohortBoundaries(
            discovery_end=datetime.fromisoformat(data["discovery_end"]),
            validation_end=datetime.fromisoformat(data["validation_end"]),
        ),
        accepted_at=datetime.fromisoformat(data["accepted_at"]),
    )


def serialize_accepted_cohort(acceptance: CohortAcceptance) -> str:
    return (
        json.dumps(
            {
                "discovery_end": acceptance.boundaries.discovery_end.isoformat(),
                "validation_end": acceptance.boundaries.validation_end.isoformat(),
                "accepted_at": acceptance.accepted_at.isoformat(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def resolve_cohort_boundaries(
    *,
    requested: CohortBoundaries,
    accepted: CohortAcceptance | None,
    accept_new_cohort: bool,
    now: datetime,
) -> tuple[CohortBoundaries, CohortAcceptance, bool]:
    """Pure decision logic, mirroring
    `momentum_flow_cohort_acceptance.resolve_capture_cohort_started_at` for
    a two-field boundary. Returns `(boundaries_to_use, acceptance_record,
    changed)`; `changed` is True only when the caller must persist a new
    record."""
    if accepted is None:
        fresh = CohortAcceptance(boundaries=requested, accepted_at=now)
        return requested, fresh, True
    if accepted.boundaries == requested:
        return accepted.boundaries, accepted, False
    if not accept_new_cohort:
        raise CohortBoundaryConflictError(
            "discovery/validation cohort boundary already frozen at "
            f"discovery_end={accepted.boundaries.discovery_end.isoformat()} "
            f"validation_end={accepted.boundaries.validation_end.isoformat()} "
            f"(accepted {accepted.accepted_at.isoformat()}); got a different "
            f"boundary discovery_end={requested.discovery_end.isoformat()} "
            f"validation_end={requested.validation_end.isoformat()} -- pass "
            "--accept-new-cohort-boundary to deliberately re-baseline this "
            "research line's split (this drops comparability with any "
            "earlier report run against the old boundary)"
        )
    return requested, CohortAcceptance(boundaries=requested, accepted_at=now), True


def load_accepted_cohort(path: Path) -> CohortAcceptance | None:
    if not path.exists():
        return None
    return parse_accepted_cohort(path.read_text())


def save_accepted_cohort(path: Path, acceptance: CohortAcceptance) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize_accepted_cohort(acceptance))


def assign_segment(
    *,
    footprint_start: datetime,
    footprint_end: datetime,
    boundaries: CohortBoundaries,
) -> Segment:
    """`footprint_start`/`footprint_end` are the episode's own feature-
    lookback-to-outcome-horizon span, not its bare trigger instant -- see
    `classify_episode_segment`."""
    if footprint_end <= boundaries.discovery_end:
        return Segment.DISCOVERY
    if footprint_start >= boundaries.discovery_end and footprint_end <= boundaries.validation_end:
        return Segment.VALIDATION
    if footprint_start >= boundaries.validation_end:
        return Segment.TEST
    return Segment.EXCLUDED_PURGE


def classify_episode_segment(
    episode: CascadeEpisode,
    *,
    boundaries: CohortBoundaries,
    feature_lookback_minutes: int,
    outcome_horizon_minutes: int,
) -> Segment:
    footprint_start = episode.trigger_at - timedelta(minutes=feature_lookback_minutes)
    footprint_end = episode.trigger_at + timedelta(minutes=outcome_horizon_minutes)
    return assign_segment(
        footprint_start=footprint_start, footprint_end=footprint_end, boundaries=boundaries
    )


def classify_episodes(
    episodes: tuple[CascadeEpisode, ...],
    *,
    boundaries: CohortBoundaries,
    feature_lookback_minutes: int,
    outcome_horizon_minutes: int,
) -> dict[Segment, tuple[CascadeEpisode, ...]]:
    buckets: dict[Segment, list[CascadeEpisode]] = {segment: [] for segment in Segment}
    for episode in episodes:
        segment = classify_episode_segment(
            episode,
            boundaries=boundaries,
            feature_lookback_minutes=feature_lookback_minutes,
            outcome_horizon_minutes=outcome_horizon_minutes,
        )
        buckets[segment].append(episode)
    return {segment: tuple(rows) for segment, rows in buckets.items()}
