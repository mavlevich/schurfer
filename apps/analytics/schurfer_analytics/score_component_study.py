"""Does any single score component carry the signal the composite does not.

HYP-023, registered in docs/research/score-components-v1.md and bound by
docs/research/entry-signal-family-rules-v1.md.

HYP-019 found the composite score ranks candidates backwards. Nobody asked about
the ingredients. The two possibilities that separates are worth very different
things: if no component is informative the ingredients are wrong and the
weighting is beside the point, while if one is informative and the composite is
not, the weighting is destroying signal already being collected.

The family rules are what make the numbers mean anything here, and each exists
because the first draft would have counted evidence it does not have:

- one decision per episode, because the scanner evaluates a live pump about once
  a minute and hundreds of overlapping 60-minute outcomes are not hundreds of
  observations;
- floors in episodes and asset clusters rather than rows;
- the same floors before a negative verdict as before a positive one;
- no decision whose outcome window runs past the study window;
- no candidate from a partition the component does not impose: a quintile
  boundary that falls inside a tied value splits equal measurements by sort
  order, and an ordering produced that way says nothing about the component.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from statistics import median
from typing import TYPE_CHECKING, Any, Protocol

from schurfer_performance import DEFAULT_COSTS, CostParameters

from .episode_selection import episode_decision_query
from .outcomes import RESOLVER_VERSION
from .research_contract import crosses_window_boundary

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .research_contract import ResearchContract

STUDY_VERSION = "score_component_study_v1"

# The score's own ingredients, as recorded on every decision under
# features.signal.components.
COMPONENTS = (
    "pump_age",
    "funding_rate",
    "price_extent",
    "oi_trend",
    "retrace_from_peak",
    "mad_score",
)

QUINTILES = 5


def horizon_cost_pct(horizon_minutes: int, costs: CostParameters = DEFAULT_COSTS) -> float:
    """Round-trip cost for a position held this long, in percent of notional.

    The shared model, used as-is rather than re-derived: two taker fees plus
    funding prorated over the horizon. A study that invents its own costs is not
    comparable with the replay, which is the point of having one model.
    """
    fees_bps = costs.taker_fee_bps_per_side * 2
    funding_bps = costs.funding_cost_bps_per_8h * (horizon_minutes / 480)
    return (fees_bps + funding_bps) / 100


def component_value(recorded: Any) -> float | None:
    """The raw measurement a component recorded, or None if it recorded nothing.

    Five of the six are objects carrying both a `value` -- the measurement, in
    the component's own units -- and `points`, the 0-2 contribution the score
    actually sums. `mad_score` is a bare number.

    This study reads `value`, and the contract says so. The ingredient is the
    measurement; `points` is the composite's own discretisation, and HYP-019
    already found the composite ranks backwards. Measuring `value` is what makes
    "the ingredients carry signal the weighting discards" answerable at all --
    reading `points` would be asking the same question HYP-019 answered.

    Which part of the machinery loses it, the bucketing or the weights, is a
    separate hypothesis. It is not folded in here, because six components read
    two ways is twelve searches wearing the costume of six.
    """
    if recorded is None:
        return None
    if isinstance(recorded, dict):
        raw = recorded.get("value")
        return None if raw is None else float(raw)
    if isinstance(recorded, int | float):
        return float(recorded)
    return None


@dataclass(frozen=True)
class EpisodeObservation:
    """One episode, reduced to a single decision and its forward outcome."""

    pump_event_id: int
    cluster_key: str
    decision_at: datetime
    components: dict[str, float]
    gross_short_return_pct: float

    def net_short_return_pct(self, horizon_minutes: int) -> float:
        return self.gross_short_return_pct - horizon_cost_pct(horizon_minutes)


@dataclass(frozen=True)
class QuintileStat:
    index: int
    episodes: int
    clusters: int
    median_net_pct: float
    lower_value: float
    upper_value: float


@dataclass(frozen=True)
class ComponentResult:
    """One component's relationship to forward outcome, and the verdict on it."""

    component: str
    coverage_episodes: int
    quintiles: tuple[QuintileStat, ...]
    verdict: str
    detail: str = ""
    distinct_values: int = 0
    largest_tied_group: int = 0

    @property
    def spread_pct(self) -> float | None:
        """Top minus bottom quintile median. None when either is missing."""
        if len(self.quintiles) < QUINTILES:
            return None
        return self.quintiles[-1].median_net_pct - self.quintiles[0].median_net_pct

    @property
    def tied_boundaries(self) -> int:
        """How many of the four quintile boundaries fall inside one tied value.

        A component recorded at coarse resolution has large groups of episodes
        sharing a value. Splitting by rank position then cuts through such a
        group, and which side of the boundary an episode lands on is decided by
        the sort's tie order rather than by the component. `pump_age` is recorded
        to a tenth of a minute and 368 of 821 discovery episodes read exactly
        0.6: quintiles two and three sat wholly inside that single value, so the
        difference between their medians was noise wearing the shape of a trend.
        """
        if len(self.quintiles) < QUINTILES:
            return 0
        return sum(
            1
            for earlier, later in pairwise(self.quintiles)
            if earlier.upper_value == later.lower_value
        )

    @property
    def separated(self) -> bool:
        """Whether every adjacent pair of quintiles differs in the component.

        Without this the ordering the monotonicity check reads is imposed by the
        sort and not by the measurement, so it can be neither believed nor
        disbelieved.
        """
        return len(self.quintiles) >= QUINTILES and self.tied_boundaries == 0

    @property
    def monotone(self) -> bool:
        """Whether the medians move in one direction across all five.

        A relationship strong only between the extremes and noisy in between is
        reported as such rather than treated as a signal: two tails can differ
        for reasons that have nothing to do with the ordering the component
        claims to impose.
        """
        if len(self.quintiles) < QUINTILES:
            return False
        values = [quintile.median_net_pct for quintile in self.quintiles]
        steps = list(pairwise(values))
        rising = all(later >= earlier for earlier, later in steps)
        falling = all(later <= earlier for earlier, later in steps)
        return rising or falling


def pick_episode_rows(rows: Sequence[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """One decision row per episode, by the registered rule.

    First decision whose action opened something, else the earliest. This is
    `select_episode_decision`'s rule, restated over raw rows because these
    studies read decisions directly rather than through the replay dataset. Two
    research lines that disagree about which decision represents an episode
    cannot be compared, so it lives here once and is imported rather than
    copied again.
    """
    by_episode: dict[int, dict[str, Any]] = {}
    for row in sorted(rows, key=lambda item: (item["pump_event_id"], item["ts"])):
        episode = int(row["pump_event_id"])
        current = by_episode.get(episode)
        opened = str(row.get("action", "")).startswith("opened")
        if current is None:
            by_episode[episode] = row
            continue
        if opened and not str(current.get("action", "")).startswith("opened"):
            by_episode[episode] = row
    return tuple(by_episode.values())


def select_one_per_episode(
    rows: Sequence[dict[str, Any]],
) -> tuple[EpisodeObservation, ...]:
    """One episode per pump event, reduced to its components and its outcome."""
    observations = []
    for row in pick_episode_rows(rows):
        # No completed outcome for the decision that represents this episode.
        # Dropped and counted, never replaced by a decision that has one.
        if row.get("short_return_pct") is None:
            continue
        components = {
            name: extracted
            for name in COMPONENTS
            if (extracted := component_value((row.get("components") or {}).get(name))) is not None
        }
        observations.append(
            EpisodeObservation(
                pump_event_id=int(row["pump_event_id"]),
                cluster_key=str(row.get("cluster_key") or f"base:{row['base']}"),
                decision_at=row["ts"],
                components=components,
                gross_short_return_pct=float(row["short_return_pct"]),
            )
        )
    return tuple(observations)


def incomplete_outcome_episodes(rows: Sequence[dict[str, Any]]) -> int:
    """Episodes whose representative decision has no completed outcome.

    Coverage, not a result. Reported so a shrinking denominator stays visible
    rather than being mistaken for a population that simply is that size.
    """
    return sum(1 for row in pick_episode_rows(rows) if row.get("short_return_pct") is None)


class HasDecisionTime(Protocol):
    """Anything `within_window` can filter: it needs only the decision's time."""

    @property
    def decision_at(self) -> datetime: ...


def within_window[ObservationT: HasDecisionTime](
    observations: Sequence[ObservationT], contract: ResearchContract
) -> tuple[ObservationT, ...]:
    """Drop episodes whose outcome window runs past the study window.

    A decision forty minutes before the window closes has a 60-minute outcome
    measured partly outside it. In a discovery and holdout split those minutes
    belong to the other side, so the windows overlap at the seam.
    """
    return tuple(
        observation
        for observation in observations
        if observation.decision_at >= contract.window_since
        and not crosses_window_boundary(contract, observation.decision_at)
    )


def _quintile_stats(
    observations: Sequence[EpisodeObservation], component: str, horizon_minutes: int
) -> tuple[QuintileStat, ...]:
    present = [item for item in observations if component in item.components]
    if len(present) < QUINTILES:
        return ()
    ordered = sorted(present, key=lambda item: item.components[component])
    size = len(ordered) / QUINTILES
    stats = []
    for index in range(QUINTILES):
        chunk = ordered[int(index * size) : int((index + 1) * size)]
        if not chunk:
            return ()
        stats.append(
            QuintileStat(
                index=index,
                episodes=len(chunk),
                clusters=len({item.cluster_key for item in chunk}),
                median_net_pct=median(item.net_short_return_pct(horizon_minutes) for item in chunk),
                lower_value=chunk[0].components[component],
                upper_value=chunk[-1].components[component],
            )
        )
    return tuple(stats)


def study_component(
    observations: Sequence[EpisodeObservation],
    component: str,
    contract: ResearchContract,
) -> ComponentResult:
    """Quintile a component and apply the registered rule to what comes out.

    The thresholds come from the contract and are not arguments. This study
    reads them as ABSOLUTE spreads rather than signed values, because a
    component that predicts the outcome downwards is as informative as one that
    predicts it upwards -- the composite already ranks backwards, which is the
    reason this hypothesis exists. That reading is stated in the contract's own
    notes so it cannot be inferred differently later.
    """
    candidate_spread_pct = abs(contract.candidate_margin)
    rejection_spread_pct = abs(contract.rejection_margin)
    present = [item for item in observations if component in item.components]
    values = [item.components[component] for item in present]
    counts = Counter(values)
    quintiles = _quintile_stats(observations, component, contract.outcome_horizon_minutes)
    facts: dict[str, Any] = {
        "component": component,
        "coverage_episodes": len(present),
        "distinct_values": len(counts),
        "largest_tied_group": max(counts.values(), default=0),
    }
    result = ComponentResult(**facts, quintiles=quintiles, verdict="inconclusive")
    if not quintiles:
        return ComponentResult(
            **facts,
            quintiles=(),
            verdict="inconclusive",
            detail="too few episodes carry this component to form quintiles",
        )

    # Sufficiency first, in both directions. The first draft required data to
    # call something a candidate and nothing to call it dead, which turns poor
    # coverage into a negative finding. mad_score is recorded on 4,067 of 62,168
    # decisions and would have been the first casualty.
    compared = (quintiles[0], quintiles[-1])
    episodes_ok = all(stat.episodes >= contract.minimum_completed_trades for stat in compared)
    clusters_ok = sum(stat.clusters for stat in compared) >= contract.minimum_clusters
    if not episodes_ok or not clusters_ok:
        return ComponentResult(
            **facts,
            quintiles=quintiles,
            verdict="inconclusive",
            detail=(
                f"below the floor: {compared[0].episodes} and {compared[-1].episodes} episodes, "
                f"{sum(stat.clusters for stat in compared)} clusters, "
                f"need {contract.minimum_completed_trades} and {contract.minimum_clusters}"
            ),
        )

    spread = result.spread_pct
    if spread is None:
        return result

    # A partition the component does not actually impose cannot support a
    # candidate. This is checked before the spread, alongside sufficiency and for
    # the same reason: the first draft would have promoted an ordering produced
    # by the sort. The check can only withdraw a candidate, never create one.
    if not result.separated:
        return ComponentResult(
            **facts,
            quintiles=quintiles,
            verdict="inconclusive",
            detail=(
                f"{result.tied_boundaries} of {QUINTILES - 1} quintile boundaries fall inside a "
                f"single tied value ({facts['largest_tied_group']} of {len(present)} episodes "
                f"share one value, {facts['distinct_values']} distinct in all), so the ordering "
                "between adjacent quintiles is imposed by the sort, not by the component"
            ),
        )
    if abs(spread) >= candidate_spread_pct and result.monotone:
        return ComponentResult(
            **facts,
            quintiles=quintiles,
            verdict="candidate",
            detail="earns a read of the held-out window, nothing more",
        )
    if abs(spread) < rejection_spread_pct:
        return ComponentResult(**facts, quintiles=quintiles, verdict="no_signal", detail="")
    return ComponentResult(
        **facts,
        quintiles=quintiles,
        verdict="inconclusive",
        detail="" if result.monotone else "spread is not monotone across all five quintiles",
    )


def render_markdown(
    results: Sequence[ComponentResult],
    contract: ResearchContract,
    *,
    generated_at: datetime,
    code_revision: str,
    episodes: int,
    incomplete_episodes: int,
) -> str:
    lines = [
        "# Score components against forward outcome",
        "",
        f"Generated: {generated_at.isoformat()}",
        f"Code revision: `{code_revision}`",
        f"Version: `{STUDY_VERSION}`, contract `{contract.hypothesis_id}` "
        f"`{contract.compute_checksum()[:16]}`",
        f"Window: {contract.window_since.date()} to {contract.window_until.date()}, "
        f"horizon {contract.outcome_horizon_minutes} minutes",
        "",
        f"> One decision per episode, chosen before any outcome was joined, "
        f"{episodes} episodes after excluding those whose outcome window runs past "
        f"the study window. {incomplete_episodes} further episodes are excluded as "
        f"coverage because the decision that represents them has no completed "
        f"outcome; they are never replaced by a decision that does. Floors: "
        f"{contract.minimum_completed_trades} episodes and {contract.minimum_clusters} "
        "clusters across the compared quintiles, checked before any verdict in "
        "either direction. This report never changes production entries.",
        "",
        "## Verdicts",
        "",
        "| Component | Coverage | Distinct | Largest tie | Spread (top - bottom) "
        "| Monotone | Separated | Verdict | Detail |",
        "| --- | ---: | ---: | ---: | ---: | --- | --- | --- | --- |",
    ]
    for result in results:
        spread = result.spread_pct
        lines.append(
            f"| {result.component} | {result.coverage_episodes} | "
            f"{result.distinct_values} | {result.largest_tied_group} | "
            f"{'n/a' if spread is None else f'{spread:+.2f}'} | "
            f"{'yes' if result.monotone else 'no'} | "
            f"{'yes' if result.separated else f'no ({result.tied_boundaries}/4)'} | "
            f"{result.verdict} | {result.detail} |"
        )

    lines.extend(["", "## Quintile medians, net of costs", ""])
    for result in results:
        if not result.quintiles:
            continue
        lines.extend(
            [
                f"### {result.component}",
                "",
                "| Quintile | Episodes | Clusters | Value range | Median net |",
                "| ---: | ---: | ---: | --- | ---: |",
            ]
        )
        for stat in result.quintiles:
            lines.append(
                f"| {stat.index + 1} | {stat.episodes} | {stat.clusters} | "
                f"{stat.lower_value:g} to {stat.upper_value:g} | "
                f"{stat.median_net_pct:+.2f}% |"
            )
        lines.append("")
    lines.extend(
        [
            "Reported and not decisive: a component may not be promoted by picking a",
            "different horizon, a different number of buckets, or the best of six",
            "after the fact. Any of those is a new hypothesis on an untouched window.",
            "",
        ]
    )
    return "\n".join(lines)


async def load_observations(db_url: str, contract: ResearchContract) -> tuple[dict[str, Any], ...]:
    """The episode's own decision, with that decision's forward outcome.

    Aggregated in SQL rather than pulled raw: the discovery window holds tens of
    thousands of decisions and this reads them over an SSH tunnel.

    The episode's decision is chosen before any outcome is joined. See
    `episode_selection.py` for why, and for the 24 episodes on this very window
    where the previous order picked a different one.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from .outcome_repository import async_database_url

    engine = create_async_engine(async_database_url(db_url), pool_pre_ping=True, pool_size=1)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text(episode_decision_query("short_return_pct")),
                {
                    "horizon": contract.outcome_horizon_minutes,
                    "resolver_version": RESOLVER_VERSION,
                    "strategies": list(contract.strategy_versions),
                    "since": contract.window_since,
                    "until": contract.window_until,
                },
            )
            return tuple(dict(row) for row in rows.mappings())
    finally:
        await engine.dispose()


def main() -> None:
    import argparse
    import asyncio
    import sys
    from pathlib import Path

    from .research_contract import load_contract

    parser = argparse.ArgumentParser(description="HYP-023: score components against outcome")
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--code-revision", default=os.getenv("SCHURFER_GIT_SHA"))
    parser.add_argument(
        "--working-tree-dirty", action=argparse.BooleanOptionalAction, required=True
    )
    args = parser.parse_args()

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for score-component-study")
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")

    contract = load_contract(args.contract)
    rows = asyncio.run(load_observations(db_url, contract))
    observations = within_window(select_one_per_episode(rows), contract)
    results = [study_component(observations, name, contract) for name in COMPONENTS]
    sys.stdout.write(
        render_markdown(
            results,
            contract,
            generated_at=datetime.now(UTC),
            code_revision=args.code_revision,
            episodes=len(observations),
            incomplete_episodes=incomplete_outcome_episodes(rows),
        )
    )


__all__ = [
    "COMPONENTS",
    "STUDY_VERSION",
    "ComponentResult",
    "EpisodeObservation",
    "QuintileStat",
    "component_value",
    "horizon_cost_pct",
    "main",
    "render_markdown",
    "select_one_per_episode",
    "study_component",
    "within_window",
]
