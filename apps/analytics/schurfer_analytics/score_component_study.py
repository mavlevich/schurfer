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
- no decision whose outcome window runs past the study window.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from statistics import median
from typing import TYPE_CHECKING, Any

from schurfer_performance import DEFAULT_COSTS, CostParameters

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


@dataclass(frozen=True)
class ComponentResult:
    """One component's relationship to forward outcome, and the verdict on it."""

    component: str
    coverage_episodes: int
    quintiles: tuple[QuintileStat, ...]
    verdict: str
    detail: str = ""

    @property
    def spread_pct(self) -> float | None:
        """Top minus bottom quintile median. None when either is missing."""
        if len(self.quintiles) < QUINTILES:
            return None
        return self.quintiles[-1].median_net_pct - self.quintiles[0].median_net_pct

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


def select_one_per_episode(
    rows: Sequence[dict[str, Any]],
) -> tuple[EpisodeObservation, ...]:
    """Reduce many decisions per episode to one, by the registered rule.

    First decision whose action opened something, else the earliest. This is
    `select_episode_decision`'s rule, restated over raw rows because this study
    reads decisions directly rather than through the replay dataset. Two
    research lines that disagree about which decision represents an episode
    cannot be compared, so the rule is copied rather than reinvented.
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

    observations = []
    for row in by_episode.values():
        components = {
            name: float(value)
            for name in COMPONENTS
            if (value := (row.get("components") or {}).get(name)) is not None
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


def within_window(
    observations: Sequence[EpisodeObservation], contract: ResearchContract
) -> tuple[EpisodeObservation, ...]:
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
    quintiles = _quintile_stats(observations, component, contract.outcome_horizon_minutes)
    result = ComponentResult(
        component=component,
        coverage_episodes=len(present),
        quintiles=quintiles,
        verdict="inconclusive",
    )
    if not quintiles:
        return ComponentResult(
            component=component,
            coverage_episodes=len(present),
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
            component=component,
            coverage_episodes=len(present),
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
    if abs(spread) >= candidate_spread_pct and result.monotone:
        return ComponentResult(
            component=component,
            coverage_episodes=len(present),
            quintiles=quintiles,
            verdict="candidate",
            detail="earns a read of the held-out window, nothing more",
        )
    if abs(spread) < rejection_spread_pct:
        return ComponentResult(
            component=component,
            coverage_episodes=len(present),
            quintiles=quintiles,
            verdict="no_signal",
            detail="",
        )
    return ComponentResult(
        component=component,
        coverage_episodes=len(present),
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
        f"> One decision per episode, {episodes} episodes after excluding those whose "
        f"outcome window runs past the study window. Floors: "
        f"{contract.minimum_completed_trades} episodes and {contract.minimum_clusters} "
        "clusters across the compared quintiles, checked before any verdict in "
        "either direction. This report never changes production entries.",
        "",
        "## Verdicts",
        "",
        "| Component | Coverage | Spread (top - bottom) | Monotone | Verdict | Detail |",
        "| --- | ---: | ---: | --- | --- | --- |",
    ]
    for result in results:
        spread = result.spread_pct
        lines.append(
            f"| {result.component} | {result.coverage_episodes} | "
            f"{'n/a' if spread is None else f'{spread:+.2f}'} | "
            f"{'yes' if result.monotone else 'no'} | {result.verdict} | {result.detail} |"
        )

    lines.extend(["", "## Quintile medians, net of costs", ""])
    for result in results:
        if not result.quintiles:
            continue
        lines.extend(
            [
                f"### {result.component}",
                "",
                "| Quintile | Episodes | Clusters | Median net |",
                "| ---: | ---: | ---: | ---: |",
            ]
        )
        for stat in result.quintiles:
            lines.append(
                f"| {stat.index + 1} | {stat.episodes} | {stat.clusters} | "
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
    """Decisions with their score components and forward outcome, one row each.

    Aggregated in SQL rather than pulled raw: the discovery window holds tens of
    thousands of decisions and this reads them over an SSH tunnel.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from .outcome_repository import async_database_url

    engine = create_async_engine(async_database_url(db_url), pool_pre_ping=True, pool_size=1)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text("""
                    SELECT d.pump_event_id, d.base, d.ts, d.action,
                           d.features->'signal'->'components' AS components,
                           o.short_return_pct
                    FROM app.trade_decisions d
                    JOIN app.trade_decision_outcomes o
                      ON o.decision_id = d.decision_id
                     AND o.horizon_minutes = :horizon
                    WHERE d.strategy_version = ANY(:strategies)
                      AND d.ts >= :since AND d.ts < :until
                      AND o.status = 'complete'
                      AND o.short_return_pct IS NOT NULL
                      AND d.pump_event_id IS NOT NULL
                    ORDER BY d.pump_event_id, d.ts
                """),
                {
                    "horizon": contract.outcome_horizon_minutes,
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
        )
    )


__all__ = [
    "COMPONENTS",
    "STUDY_VERSION",
    "ComponentResult",
    "EpisodeObservation",
    "QuintileStat",
    "horizon_cost_pct",
    "main",
    "render_markdown",
    "select_one_per_episode",
    "study_component",
    "within_window",
]
