"""Outcome-blind readiness view for the source-lead forward cohort (HYP-012).

It answers "is the frozen Gate-to-Binance cohort accumulating, and where are events
lost" WITHOUT reading any outcome. To avoid drifting from the frozen contract it does
NOT define its own candidate set or maturity: it consumes the SAME qualified episodes
the formal reader uses (cohort-start-filtered, `QUALIFICATION_VERSION`, a `sampled`
target observation) and reuses `episode_is_matured` on each episode's entry time.

Deliberately it does NOT emit a "ready to read" verdict. Timing maturity (enough
wall-clock time elapsed) is necessary but NOT sufficient for the formal read: that gate
is `formal_verdict` over RESOLVED episodes with the concentration limits, which needs
the exit-bar outcome this outcome-blind view never fetches. So this reports timing
progress and accumulation only, and surfaces concentration so a premature "looks ready"
is visible as still gated.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .source_lead_forward_cohort import (
    EVIDENCE_FLOOR,
    MAX_SINGLE_ASSET_EPISODE_SHARE,
    MAX_SINGLE_WEEK_EPISODE_SHARE,
    episode_is_matured,
    expected_exit_boundary_ms,
)

if TYPE_CHECKING:
    from datetime import datetime

FLOOR_EPISODES: int = EVIDENCE_FLOOR["min_resolved_episodes"]
FLOOR_CLUSTERS: int = EVIDENCE_FLOOR["min_distinct_asset_clusters"]
FLOOR_WEEKS: int = EVIDENCE_FLOOR["min_distinct_utc_weeks"]

# Capacity view (owner economics): a USD 300 pot in USD 50 positions is six
# concurrent slots. A position is held from entry to the end of its exit bar.
CAPACITY_CAPITAL_USD = 300
CAPACITY_POSITION_USD = 50


@dataclass(frozen=True)
class QualifiedEpisode:
    """One qualified cohort episode, from the formal candidate query. `entry_at` is the
    target observation's `observed_at` (the entry time the reader resolves from), NOT
    `qualified_at`."""

    entry_at: datetime
    canonical_asset_id: str


@dataclass(frozen=True)
class ReadinessInputs:
    """Everything needed to build the readiness view, all outcome-blind. `episodes` are
    the formal candidate set; `database_now` and `cohort_start` fix the exposure window;
    `excluded_by_reason`/`captured_in_cohort` are the cohort-scoped diagnostic funnel."""

    cohort_start: datetime
    database_now: datetime
    episodes: tuple[QualifiedEpisode, ...]
    captured_in_cohort: int = 0
    excluded_by_reason: dict[str, int] = field(default_factory=dict)
    # Captures with NO qualification row of this version, keyed "status:eligibility_reason"
    # (e.g. excluded at capture because Gate was not the unique first source).
    pre_qualification_by_reason: dict[str, int] = field(default_factory=dict)
    # Every qualification row of this version in the cohort (qualified + excluded).
    qualification_rows: int = 0
    # Qualified rows that are NOT a formal episode, keyed by the selected target
    # observation's status ("missing" when there is none).
    qualified_without_episode_by_status: dict[str, int] = field(default_factory=dict)
    # v2: per-venue target outcomes from qualification details, keyed "venue:reason"
    # ("executable" when the venue passed every check; tradable or not).
    target_reasons_by_venue: dict[str, int] = field(default_factory=dict)
    # v2 exit-book diagnostic coverage, keyed "outcome:timeliness". Statuses and
    # delays only: prices are outcome data and are never read here.
    exit_coverage: dict[str, int] = field(default_factory=dict)
    exit_lateness_ms: tuple[int, ...] = ()
    # Episodes whose exit window has closed (the denominator), and how many of
    # them have no exit row at all: a stopped exit service shows up here.
    exit_due: int = 0
    exit_missing: int = 0


@dataclass(frozen=True)
class CapacitySummary:
    """How many qualified episodes a fixed pot could actually have taken,
    from entry times alone (outcome-blind)."""

    slots: int
    max_concurrent: int
    taken: int
    skipped: int
    skipped_share: float | None


def _release_at(entry: datetime) -> float:
    return (expected_exit_boundary_ms(entry) + 60_000) / 1000


def capacity_summary(entries: list[datetime], slots: int) -> CapacitySummary:
    """Greedy first-come simulation: an episode is taken if a slot is free at
    its entry; the slot is released at the end of its exit bar.

    `max_concurrent` is the demand: the largest number of episodes whose
    holding windows overlap, counted over ALL signals regardless of the slot
    limit (a separate sweep, so an overloaded minute is not capped at slots+1)."""
    ordered = sorted(entries)
    demand: list[float] = []
    max_concurrent = 0
    for entry in ordered:
        while demand and demand[0] <= entry.timestamp():
            heapq.heappop(demand)
        heapq.heappush(demand, _release_at(entry))
        max_concurrent = max(max_concurrent, len(demand))

    open_until: list[float] = []
    taken = skipped = 0
    for entry in ordered:
        while open_until and open_until[0] <= entry.timestamp():
            heapq.heappop(open_until)
        if len(open_until) >= slots:
            skipped += 1
            continue
        taken += 1
        heapq.heappush(open_until, _release_at(entry))
    total = taken + skipped
    return CapacitySummary(
        slots=slots,
        max_concurrent=max_concurrent,
        taken=taken,
        skipped=skipped,
        skipped_share=skipped / total if total else None,
    )


def _percentile(values: tuple[int, ...], q: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


@dataclass(frozen=True)
class ReadinessReport:
    """Timing-only progress toward the evidence floor. `timing_floors_met` means the
    three COUNT floors are met on timing alone; it is NOT the formal read gate (which
    also needs resolved outcomes under the concentration caps). Concentration shares are
    reported so an over-concentrated set that meets the counts is still visibly gated."""

    captured_in_cohort: int
    candidates: int
    matured: int
    distinct_clusters: int
    distinct_weeks: int
    largest_asset_share: float | None
    largest_week_share: float | None
    concentration_ok: bool
    exposure_weeks: float
    qualified_per_week: float | None
    weeks_to_episode_floor: float | None
    meets_episode_floor: bool
    meets_cluster_floor: bool
    meets_week_floor: bool
    timing_floors_met: bool
    excluded_by_reason: dict[str, int]
    pre_qualification_by_reason: dict[str, int]
    # Stopped before qualification, split by what it means.
    capture_excluded: int  # expected: the capture itself was ineligible
    capture_in_flight: int  # still collecting
    capture_abandoned: int  # the capture process gave up (a known failure mode)
    # A COMPLETE eligible capture never qualified, or an unknown status: a pipeline error.
    pipeline_errors: int
    qualification_rows: int
    qualified_rows: int
    qualified_without_episode_by_status: dict[str, int]
    # qualified rows == formal candidates + qualified rows without an episode
    lower_funnel_reconciles: bool
    qualified_by_week: dict[str, int] = field(default_factory=dict)
    capacity: CapacitySummary | None = None
    target_reasons_by_venue: dict[str, int] = field(default_factory=dict)
    exit_coverage: dict[str, int] = field(default_factory=dict)
    exit_lateness_p50_ms: int | None = None
    exit_lateness_p90_ms: int | None = None
    exit_due: int = 0
    exit_missing: int = 0


def _utc_week_key(moment: datetime) -> str:
    iso = moment.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _largest_share(counts: list[int], total: int) -> float | None:
    if total <= 0 or not counts:
        return None
    return max(counts) / total


def build_readiness(
    inputs: ReadinessInputs,
    *,
    floor_episodes: int = FLOOR_EPISODES,
    floor_clusters: int = FLOOR_CLUSTERS,
    floor_weeks: int = FLOOR_WEEKS,
) -> ReadinessReport:
    """Reduce the formal candidate episodes to timing-only readiness. Maturity reuses
    `episode_is_matured` on each episode's entry time; the rate uses the FIXED exposure
    window (cohort_start to database_now), never the span between first and last event.
    Deterministic; reads no outcome."""
    episodes = inputs.episodes
    candidates = len(episodes)
    matured = sum(1 for e in episodes if episode_is_matured(e.entry_at, inputs.database_now))

    by_asset: dict[str, int] = {}
    by_week: dict[str, int] = {}
    for e in episodes:
        by_asset[e.canonical_asset_id] = by_asset.get(e.canonical_asset_id, 0) + 1
        by_week[_utc_week_key(e.entry_at)] = by_week.get(_utc_week_key(e.entry_at), 0) + 1

    largest_asset_share = _largest_share(list(by_asset.values()), candidates)
    largest_week_share = _largest_share(list(by_week.values()), candidates)
    concentration_ok = (
        largest_asset_share is not None
        and largest_week_share is not None
        and largest_asset_share <= MAX_SINGLE_ASSET_EPISODE_SHARE
        and largest_week_share <= MAX_SINGLE_WEEK_EPISODE_SHARE
    )

    exposure_days = (inputs.database_now - inputs.cohort_start).total_seconds() / 86400.0
    exposure_weeks = exposure_days / 7.0
    rate = candidates / exposure_weeks if exposure_weeks > 0 else None

    meets_episodes = matured >= floor_episodes
    meets_clusters = len(by_asset) >= floor_clusters
    meets_weeks = len(by_week) >= floor_weeks

    stage = {"excluded": 0, "collecting": 0, "abandoned": 0, "error": 0}
    for key, count in inputs.pre_qualification_by_reason.items():
        status = key.split(":", 1)[0]
        stage[status if status in ("excluded", "collecting", "abandoned") else "error"] += count
    qualified_rows = inputs.qualification_rows - sum(inputs.excluded_by_reason.values())

    weeks_to_floor: float | None = None
    if not meets_episodes and rate and rate > 0:
        weeks_to_floor = (floor_episodes - matured) / rate

    return ReadinessReport(
        captured_in_cohort=inputs.captured_in_cohort,
        candidates=candidates,
        matured=matured,
        distinct_clusters=len(by_asset),
        distinct_weeks=len(by_week),
        largest_asset_share=largest_asset_share,
        largest_week_share=largest_week_share,
        concentration_ok=concentration_ok,
        exposure_weeks=exposure_weeks,
        qualified_per_week=rate,
        weeks_to_episode_floor=weeks_to_floor,
        meets_episode_floor=meets_episodes,
        meets_cluster_floor=meets_clusters,
        meets_week_floor=meets_weeks,
        timing_floors_met=meets_episodes and meets_clusters and meets_weeks,
        excluded_by_reason=dict(inputs.excluded_by_reason),
        pre_qualification_by_reason=dict(inputs.pre_qualification_by_reason),
        capture_excluded=stage["excluded"],
        capture_in_flight=stage["collecting"],
        capture_abandoned=stage["abandoned"],
        pipeline_errors=stage["error"],
        qualification_rows=inputs.qualification_rows,
        qualified_rows=qualified_rows,
        qualified_without_episode_by_status=dict(inputs.qualified_without_episode_by_status),
        lower_funnel_reconciles=qualified_rows
        == candidates + sum(inputs.qualified_without_episode_by_status.values()),
        qualified_by_week=dict(sorted(by_week.items())),
        capacity=capacity_summary(
            [e.entry_at for e in episodes], CAPACITY_CAPITAL_USD // CAPACITY_POSITION_USD
        ),
        target_reasons_by_venue=dict(sorted(inputs.target_reasons_by_venue.items())),
        exit_coverage=dict(sorted(inputs.exit_coverage.items())),
        exit_lateness_p50_ms=_percentile(inputs.exit_lateness_ms, 0.5),
        exit_lateness_p90_ms=_percentile(inputs.exit_lateness_ms, 0.9),
        exit_due=inputs.exit_due,
        exit_missing=inputs.exit_missing,
    )


__all__ = [
    "FLOOR_CLUSTERS",
    "FLOOR_EPISODES",
    "FLOOR_WEEKS",
    "CapacitySummary",
    "QualifiedEpisode",
    "ReadinessInputs",
    "ReadinessReport",
    "build_readiness",
    "capacity_summary",
]
