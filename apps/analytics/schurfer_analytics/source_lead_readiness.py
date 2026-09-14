"""Outcome-blind readiness funnel for the source-lead forward cohort (HYP-012).

It answers "is the Gate to Binance forward cohort accumulating, and where are events
lost" WITHOUT reading any return: captured -> qualification-attempted -> qualified
(identity + executable target) vs excluded-by-reason -> matured (enough wall-clock time
elapsed for the outcome horizon, a timing fact, never the outcome itself). It also
projects when the cohort could reach the registered evidence floor at the observed
accumulation rate.

This exists so we can see progress and diagnose loss (identity coverage, no executable
target, latency) without peeking at outcomes, which would break the frozen forward
contract's freeze-before-read discipline. It promotes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The registered forward-cohort floor and outcome horizon, mirrored from
# source_lead_forward_cohort so readiness is measured against the same bar.
FLOOR_EPISODES = 100
FLOOR_CLUSTERS = 7
FLOOR_WEEKS = 4


@dataclass(frozen=True)
class ReadinessFunnel:
    """Raw outcome-blind counts through the source-lead pipeline over a window.

    `qualified` are captures that passed identity AND got an executable target selected;
    `excluded_by_reason` sums the excluded captures per exclusion reason.
    `matured` counts qualified episodes whose outcome horizon has already elapsed in
    wall-clock time (a timing fact, NOT the outcome). `qualified_clusters` is the number
    of distinct canonical assets among qualified; `qualified_weeks` the number of
    distinct UTC weeks with a qualified capture."""

    captured: int
    qualification_attempts: int
    qualified: int
    excluded: int
    excluded_by_reason: dict[str, int] = field(default_factory=dict)
    qualified_clusters: int = 0
    qualified_weeks: int = 0
    matured: int = 0
    span_days: float = 0.0


@dataclass(frozen=True)
class ReadinessSummary:
    """Derived readiness: accumulation rate, floor gaps, and a projection. All
    outcome-blind. `weeks_to_episode_floor` is None when the rate is zero (never
    reaches the floor) or the floor is already met."""

    qualified_per_week: float | None
    top_exclusion_reason: str | None
    top_exclusion_count: int
    meets_episode_floor: bool
    meets_cluster_floor: bool
    meets_week_floor: bool
    ready: bool
    weeks_to_episode_floor: float | None


def summarize_readiness(
    funnel: ReadinessFunnel,
    *,
    floor_episodes: int = FLOOR_EPISODES,
    floor_clusters: int = FLOOR_CLUSTERS,
    floor_weeks: int = FLOOR_WEEKS,
) -> ReadinessSummary:
    """Reduce a funnel to readiness against the registered floor. Uses `matured` (not
    raw qualified) for the episode floor, since only matured episodes can enter a read.
    Deterministic; reads no outcome."""
    weeks_span = funnel.span_days / 7.0
    rate = funnel.qualified / weeks_span if weeks_span > 0 else None

    top_reason: str | None = None
    top_count = 0
    for reason, count in funnel.excluded_by_reason.items():
        if count > top_count:
            top_reason, top_count = reason, count

    meets_episodes = funnel.matured >= floor_episodes
    meets_clusters = funnel.qualified_clusters >= floor_clusters
    meets_weeks = funnel.qualified_weeks >= floor_weeks
    ready = meets_episodes and meets_clusters and meets_weeks

    weeks_to_floor: float | None = None
    if not meets_episodes and rate and rate > 0:
        weeks_to_floor = (floor_episodes - funnel.matured) / rate

    return ReadinessSummary(
        qualified_per_week=rate,
        top_exclusion_reason=top_reason,
        top_exclusion_count=top_count,
        meets_episode_floor=meets_episodes,
        meets_cluster_floor=meets_clusters,
        meets_week_floor=meets_weeks,
        ready=ready,
        weeks_to_episode_floor=weeks_to_floor,
    )


__all__ = [
    "FLOOR_CLUSTERS",
    "FLOOR_EPISODES",
    "FLOOR_WEEKS",
    "ReadinessFunnel",
    "ReadinessSummary",
    "summarize_readiness",
]
