"""HYP-027: two groups the score cannot tell apart, and whether they differ.

Registered in docs/research/pump-age-resolution-v1.md and bound by
docs/research/entry-signal-family-rules-v1.md.

HYP-023 reported `pump_age` as a candidate and the candidate was withdrawn: the
component carries 142 distinct values across 821 episodes, 368 of them reading
exactly 0.6 minutes, and all four quintile boundaries fell inside a tied value.
The monotonicity was a property of the sort order.

What survived does not depend on that partition. Production scores pump age zero
points below one hour, and 692 of those 821 episodes were under an hour old, so
the whole measured range of ages in the mass the system actually trades is
collapsed into a single bucket. This study compares two groups that both score
**zero**, which is the entire design: a difference between them is invisible to
the composite by construction rather than by weighting.

The cutoffs are absolute and fixed by the contract. They are never recomputed per
window, because recomputing a boundary from the data in front of you is exactly
the mistake this hypothesis exists to avoid repeating.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import median
from typing import TYPE_CHECKING, Any

from .research_contract import verdict as contract_verdict
from .score_component_study import (
    component_value,
    horizon_cost_pct,
    pick_episode_rows,
    within_window,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .research_contract import ResearchContract

STUDY_VERSION = "pump_age_resolution_study_v1"

# Minutes. Group A is at most the first, group B is above it up to the second.
# Both sit inside production's zero-point bucket, which ends at 60 minutes.
GROUP_A_MAX_MINUTES = 0.6
GROUP_B_MAX_MINUTES = 60.0

GROUP_A = "pump_age_at_most_0.6_minutes"
GROUP_B = "pump_age_above_0.6_to_60_minutes"

MINUTES_PER_HOUR = 60.0


@dataclass(frozen=True)
class AgeObservation:
    """One episode, its age when the decision was made, and its forward outcome."""

    pump_event_id: int
    cluster_key: str
    decision_at: datetime
    age_minutes: float
    gross_short_return_pct: float
    mfe_pct: float | None
    mae_pct: float | None

    def net_short_return_pct(self, horizon_minutes: int) -> float:
        return self.gross_short_return_pct - horizon_cost_pct(horizon_minutes)


def assign_group(age_minutes: float) -> str | None:
    """Which group an episode belongs to, or None if it is out of scope.

    Episodes older than an hour are excluded rather than reported. They are the
    ones production does score -- 1 point above an hour, 2 above four -- and
    testing them is a different question with its own window. Measuring them
    here descriptively would spend their data before that contract exists.
    """
    if age_minutes < 0:
        return None
    if age_minutes <= GROUP_A_MAX_MINUTES:
        return GROUP_A
    if age_minutes <= GROUP_B_MAX_MINUTES:
        return GROUP_B
    return None


def select_one_per_episode(rows: Sequence[dict[str, Any]]) -> tuple[AgeObservation, ...]:
    """One observation per pump event, by the rule the whole family shares.

    Episodes with no recorded age are dropped rather than defaulted: absent and
    zero are different things, and a decision that recorded no age is not a
    decision about a brand-new pump.
    """
    observations = []
    for row in pick_episode_rows(rows):
        age_hours = component_value((row.get("components") or {}).get("pump_age"))
        if age_hours is None:
            continue
        observations.append(
            AgeObservation(
                pump_event_id=int(row["pump_event_id"]),
                cluster_key=str(row.get("cluster_key") or f"base:{row['base']}"),
                decision_at=row["ts"],
                age_minutes=age_hours * MINUTES_PER_HOUR,
                gross_short_return_pct=float(row["short_return_pct"]),
                mfe_pct=None if row.get("mfe_pct") is None else float(row["mfe_pct"]),
                mae_pct=None if row.get("mae_pct") is None else float(row["mae_pct"]),
            )
        )
    return tuple(observations)


@dataclass(frozen=True)
class GroupStat:
    """One group's outcome distribution. Only the median can move the verdict."""

    label: str
    episodes: int
    clusters: int
    median_net_pct: float
    p25_net_pct: float
    p75_net_pct: float
    median_mfe_pct: float | None
    median_mae_pct: float | None
    min_age_minutes: float
    max_age_minutes: float


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def group_stat(
    observations: Sequence[AgeObservation], label: str, horizon_minutes: int
) -> GroupStat | None:
    if not observations:
        return None
    nets = [item.net_short_return_pct(horizon_minutes) for item in observations]
    mfes = [item.mfe_pct for item in observations if item.mfe_pct is not None]
    maes = [item.mae_pct for item in observations if item.mae_pct is not None]
    ages = [item.age_minutes for item in observations]
    return GroupStat(
        label=label,
        episodes=len(observations),
        clusters=len({item.cluster_key for item in observations}),
        median_net_pct=median(nets),
        p25_net_pct=_percentile(nets, 0.25),
        p75_net_pct=_percentile(nets, 0.75),
        median_mfe_pct=median(mfes) if mfes else None,
        median_mae_pct=median(maes) if maes else None,
        min_age_minutes=min(ages),
        max_age_minutes=max(ages),
    )


@dataclass(frozen=True)
class StudyResult:
    """The graded comparison, and everything needed to check it."""

    groups: dict[str, GroupStat]
    difference_pct: float | None
    verdict: str
    detail: str
    excluded_over_an_hour: int
    episode_ids: tuple[int, ...]


def run_study(observations: Sequence[AgeObservation], contract: ResearchContract) -> StudyResult:
    """Split by the contract's groups and apply the registered rule to the result.

    The floors are applied to the SMALLER of the two groups rather than to the
    pooled sample, because a comparison is only as well evidenced as its thinner
    side. `verdict` checks them before it looks at the margin at all.
    """
    grouped: dict[str, list[AgeObservation]] = {GROUP_A: [], GROUP_B: []}
    excluded = 0
    for observation in observations:
        group = assign_group(observation.age_minutes)
        if group is None:
            excluded += 1
            continue
        grouped[group].append(observation)

    stats = {
        label: stat
        for label, members in grouped.items()
        if (stat := group_stat(members, label, contract.outcome_horizon_minutes)) is not None
    }
    episode_ids = tuple(
        sorted(item.pump_event_id for members in grouped.values() for item in members)
    )
    challenger, baseline = stats.get(GROUP_A), stats.get(GROUP_B)
    if challenger is None or baseline is None:
        return StudyResult(
            groups=stats,
            difference_pct=None,
            verdict="inconclusive",
            detail="one of the two groups is empty, so there is nothing to compare",
            excluded_over_an_hour=excluded,
            episode_ids=episode_ids,
        )

    # Signed, and the direction was declared in advance: the younger group above
    # the older one. A large difference the other way is a refutation.
    difference = challenger.median_net_pct - baseline.median_net_pct
    smaller_episodes = min(challenger.episodes, baseline.episodes)
    smaller_clusters = min(challenger.clusters, baseline.clusters)
    graded = contract_verdict(
        contract,
        value=difference,
        completed_trades=smaller_episodes,
        clusters=smaller_clusters,
    )
    detail = ""
    if smaller_episodes < contract.minimum_completed_trades:
        detail = (
            f"below the episode floor: {smaller_episodes} in the thinner group, "
            f"need {contract.minimum_completed_trades}"
        )
    elif smaller_clusters < contract.minimum_clusters:
        detail = (
            f"below the cluster floor: {smaller_clusters} in the thinner group, "
            f"need {contract.minimum_clusters}"
        )
    elif graded == "candidate":
        detail = "confirmed in the declared direction; it authorizes a registered gate design"
    elif graded == "rejected":
        detail = "refuted: the older group inside the hour did better"
    return StudyResult(
        groups=stats,
        difference_pct=difference,
        verdict=graded,
        detail=detail,
        excluded_over_an_hour=excluded,
        episode_ids=episode_ids,
    )


def render_markdown(
    result: StudyResult,
    contract: ResearchContract,
    *,
    generated_at: datetime,
    code_revision: str,
    latest_decision_at: datetime | None,
) -> str:
    difference = result.difference_pct
    lines = [
        "# Pump age inside production's zero-point bucket",
        "",
        f"Generated: {generated_at.isoformat()}",
        f"Code revision: `{code_revision}`",
        f"Version: `{STUDY_VERSION}`, contract `{contract.hypothesis_id}` "
        f"`{contract.compute_checksum()[:16]}`",
        f"Window: {contract.window_since.date()} to {contract.window_until.date()}, "
        f"horizon {contract.outcome_horizon_minutes} minutes",
        f"Latest decision included: "
        f"{'none' if latest_decision_at is None else latest_decision_at.isoformat()}",
        "",
        "> Both groups score **zero** on pump age in production, so a difference "
        "between them is invisible to the composite by construction. Cutoffs are "
        "absolute and fixed by the contract. Episodes older than 60 minutes are "
        "excluded, not reported: they are the ones production does score. This "
        "report never changes production entries.",
        "",
        f"## Verdict: `{result.verdict}`",
        "",
        f"Difference (challenger minus baseline): "
        f"**{'n/a' if difference is None else f'{difference:+.2f}'}** points of median net "
        f"return, against a confirmation margin of {contract.candidate_margin:+.1f} and a "
        f"refutation margin of {contract.rejection_margin:+.1f}.",
        "",
    ]
    if result.detail:
        lines.extend([result.detail, ""])
    lines.extend(
        [
            "## Groups",
            "",
            "| Group | Age range (min) | Episodes | Clusters | Median net | p25 | p75 "
            "| Median MFE | Median MAE |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for label in (GROUP_A, GROUP_B):
        stat = result.groups.get(label)
        if stat is None:
            lines.append(f"| {label} | - | 0 | 0 | - | - | - | - | - |")
            continue
        lines.append(
            f"| {stat.label} | {stat.min_age_minutes:g} to {stat.max_age_minutes:g} | "
            f"{stat.episodes} | {stat.clusters} | {stat.median_net_pct:+.2f}% | "
            f"{stat.p25_net_pct:+.2f}% | {stat.p75_net_pct:+.2f}% | "
            f"{'n/a' if stat.median_mfe_pct is None else f'{stat.median_mfe_pct:+.2f}%'} | "
            f"{'n/a' if stat.median_mae_pct is None else f'{stat.median_mae_pct:+.2f}%'} |"
        )
    lines.extend(
        [
            "",
            f"Excluded as older than {GROUP_B_MAX_MINUTES:g} minutes: "
            f"{result.excluded_over_an_hour} episodes.",
            "",
            "Everything except the median difference is context and cannot change the",
            "verdict. A confirmation authorizes a registered design for an age gate and",
            "nothing else: a relationship between a decision-time value and a forward",
            "price move is not a strategy, and age is not a property anyone selected --",
            "it is how long after the pump the scanner arrived.",
            "",
        ]
    )
    return "\n".join(lines)


async def load_observations(db_url: str, contract: ResearchContract) -> tuple[dict[str, Any], ...]:
    """Decisions with their recorded pump age and forward outcome, one row each."""
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
                           o.short_return_pct, o.mfe_pct, o.mae_pct
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

    from .research_contract import freeze_or_verify_sample, load_contract

    parser = argparse.ArgumentParser(description="HYP-027: pump age inside the zero-point bucket")
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument(
        "--sample-manifest",
        type=Path,
        help="Where the measured sample is frozen. One read means one sample.",
    )
    parser.add_argument("--code-revision", default=os.getenv("SCHURFER_GIT_SHA"))
    parser.add_argument(
        "--working-tree-dirty", action=argparse.BooleanOptionalAction, required=True
    )
    args = parser.parse_args()

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for pump-age-resolution-study")
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")

    contract = load_contract(args.contract)
    rows = asyncio.run(load_observations(db_url, contract))
    observations = within_window(select_one_per_episode(rows), contract)
    result = run_study(observations, contract)
    if args.sample_manifest is not None:
        freeze_or_verify_sample(contract, result.episode_ids, args.sample_manifest)
    sys.stdout.write(
        render_markdown(
            result,
            contract,
            generated_at=datetime.now(UTC),
            code_revision=args.code_revision,
            latest_decision_at=max((item.decision_at for item in observations), default=None),
        )
    )
