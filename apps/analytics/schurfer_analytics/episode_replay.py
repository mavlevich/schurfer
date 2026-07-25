"""Read-only episode-replay readiness report and reproducibility manifest."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from .outcomes import HORIZONS_MINUTES, RESOLVER_VERSION
from .replay import (
    DEFAULT_REPLAY_HORIZONS,
    DIRECTIONAL_EPISODES,
    FORMAL_EPISODES,
    FOUNDATION_VERSION,
    MIN_FORMAL_CLUSTERS,
    QUERY_VERSION,
    ReplayDataset,
    ReplayFilters,
    build_replay_dataset,
)
from .reporting import horizon_label, json_ready, markdown_table, parse_utc_datetime

CONFIRMATION_COHORT_START = datetime(2026, 7, 26, tzinfo=UTC)
PROTOCOL_VERSION = "episode_replay_protocol_v1"


@dataclass(frozen=True)
class ReplayManifest:
    protocol_version: str
    replay_engine_version: str
    query_version: str
    code_revision: str
    working_tree_dirty: bool
    generated_at: datetime
    dataset_since: datetime | None
    dataset_until_exclusive: datetime
    input_fingerprint: str
    strategy_versions: tuple[str, ...]
    resolver_version: str
    required_horizons: tuple[int, ...]
    accepted_outcome_statuses: tuple[str, ...]
    fallback_allowed: bool
    observation_unit: str = "pump_event_id"
    cluster_unit: str = "canonical_asset_or_normalized_base_fallback"


@dataclass(frozen=True)
class ReplayHealth:
    decisions: int
    attributed_decisions: int
    unassigned_decisions: int
    episodes: int
    eligible_episodes: int
    excluded_episodes: int
    eligible_decisions: int
    eligible_clusters: int
    formal_sample_episodes: int
    formal_sample_clusters: int
    readiness: str


@dataclass(frozen=True)
class ExclusionRow:
    scope: str
    reason: str
    count: int


@dataclass(frozen=True)
class ClusterRow:
    cluster_key: str
    episodes: int
    share_pct: float


@dataclass(frozen=True)
class ReplayReadinessReport:
    manifest: ReplayManifest
    health: ReplayHealth
    exclusions: tuple[ExclusionRow, ...]
    cluster_concentration: tuple[ClusterRow, ...]


def _readiness(eligible_episodes: int, formal_sample_clusters: int) -> str:
    if eligible_episodes < DIRECTIONAL_EPISODES:
        return "collecting"
    if eligible_episodes < FORMAL_EPISODES:
        return "directional_only"
    if formal_sample_clusters < MIN_FORMAL_CLUSTERS:
        return "insufficient_diversity"
    return "formal_sample_ready"


def build_report(
    dataset: ReplayDataset,
    filters: ReplayFilters,
    *,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool = False,
) -> ReplayReadinessReport:
    revision = code_revision.strip()
    if not revision:
        raise ValueError("code revision must not be empty")
    eligible = dataset.eligible_episodes
    formal_sample = eligible[:FORMAL_EPISODES]
    eligible_cluster_counts = Counter(episode.cluster_key for episode in eligible)
    formal_clusters = {episode.cluster_key for episode in formal_sample}
    eligible_decisions = sum(len(episode.decisions) for episode in eligible)

    episode_exclusions = Counter(
        reason for episode in dataset.excluded_episodes for reason in episode.exclusion_reasons
    )
    unassigned_exclusions = Counter(
        reason for _, reasons in dataset.unassigned_reasons for reason in reasons
    )
    exclusions = tuple(
        [
            ExclusionRow("episode", reason, count)
            for reason, count in sorted(
                episode_exclusions.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ]
        + [
            ExclusionRow("unassigned_decision", reason, count)
            for reason, count in sorted(
                unassigned_exclusions.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ]
    )
    denominator = len(eligible)
    concentration = tuple(
        ClusterRow(
            cluster_key=cluster,
            episodes=count,
            share_pct=count / denominator * 100 if denominator else 0.0,
        )
        for cluster, count in sorted(
            eligible_cluster_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )[:10]
    )
    manifest = ReplayManifest(
        protocol_version=PROTOCOL_VERSION,
        replay_engine_version=FOUNDATION_VERSION,
        query_version=QUERY_VERSION,
        code_revision=revision,
        working_tree_dirty=working_tree_dirty,
        generated_at=generated_at,
        dataset_since=filters.since,
        dataset_until_exclusive=filters.until,
        input_fingerprint=dataset.input_fingerprint,
        strategy_versions=filters.strategy_versions,
        resolver_version=filters.resolver_version,
        required_horizons=filters.required_horizons,
        accepted_outcome_statuses=filters.accepted_outcome_statuses,
        fallback_allowed=filters.allow_fallback,
    )
    return ReplayReadinessReport(
        manifest=manifest,
        health=ReplayHealth(
            decisions=len(dataset.decisions),
            attributed_decisions=len(dataset.decisions) - len(dataset.unassigned_decisions),
            unassigned_decisions=len(dataset.unassigned_decisions),
            episodes=len(dataset.episodes),
            eligible_episodes=len(eligible),
            excluded_episodes=len(dataset.excluded_episodes),
            eligible_decisions=eligible_decisions,
            eligible_clusters=len(eligible_cluster_counts),
            formal_sample_episodes=len(formal_sample),
            formal_sample_clusters=len(formal_clusters),
            readiness=_readiness(len(eligible), len(formal_clusters)),
        ),
        exclusions=exclusions,
        cluster_concentration=concentration,
    )


def render_json(report: ReplayReadinessReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True)


def render_markdown(report: ReplayReadinessReport) -> str:
    manifest = report.manifest
    health = report.health
    lines = [
        "# Episode Replay Readiness",
        "",
        f"Generated: {manifest.generated_at.isoformat()}",
        f"Protocol: {manifest.protocol_version}",
        f"Code revision: `{manifest.code_revision}`",
        f"Working tree dirty: {'yes' if manifest.working_tree_dirty else 'no'}",
        f"Input fingerprint: `{manifest.input_fingerprint}`",
        (
            "Scope: "
            f"{manifest.dataset_since.isoformat() if manifest.dataset_since else 'all time'}"
            f" <= decision < {manifest.dataset_until_exclusive.isoformat()}"
        ),
        f"Strategies: {', '.join(manifest.strategy_versions)}",
        (
            "Outcomes: "
            f"resolver={manifest.resolver_version}, "
            f"horizons={','.join(horizon_label(item) for item in manifest.required_horizons)}, "
            f"statuses={','.join(manifest.accepted_outcome_statuses)}"
        ),
        "",
        "> Input-readiness only. This command does not simulate entries, exits, costs, or",
        "> strategy edge.",
        "",
        "## Dataset",
        "",
    ]
    lines.extend(
        markdown_table(
            ("Metric", "Value"),
            [
                ("Decisions", health.decisions),
                ("Directly attributed decisions", health.attributed_decisions),
                ("Unassigned decisions", health.unassigned_decisions),
                ("Episodes", health.episodes),
                ("Eligible episodes", health.eligible_episodes),
                ("Excluded episodes", health.excluded_episodes),
                ("Eligible decisions", health.eligible_decisions),
                ("Eligible asset clusters", health.eligible_clusters),
                ("First formal sample episodes", health.formal_sample_episodes),
                ("First formal sample clusters", health.formal_sample_clusters),
                ("Readiness", health.readiness),
            ],
        )
    )
    lines.extend(["", "## Exclusions", ""])
    lines.extend(
        markdown_table(
            ("Scope", "Reason", "Count"),
            [(row.scope, row.reason, row.count) for row in report.exclusions],
        )
    )
    lines.extend(["", "## Eligible cluster concentration", ""])
    lines.extend(
        markdown_table(
            ("Cluster", "Episodes", "Share"),
            [
                (row.cluster_key, row.episodes, f"{row.share_pct:.2f}%")
                for row in report.cluster_concentration
            ],
        )
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and group deterministic pump-episode replay inputs"
    )
    parser.add_argument(
        "--since",
        type=parse_utc_datetime,
        default=CONFIRMATION_COHORT_START,
        help="inclusive UTC cutoff; defaults to the protocol confirmation start",
    )
    parser.add_argument(
        "--until",
        type=parse_utc_datetime,
        help="exclusive UTC cutoff; defaults to the run start",
    )
    parser.add_argument(
        "--strategy-version",
        action="append",
        help="strategy cohort to include; repeat for multiple cohorts",
    )
    parser.add_argument(
        "--horizon",
        action="append",
        type=int,
        choices=HORIZONS_MINUTES,
        help="required complete outcome horizon; repeat for multiple horizons",
    )
    parser.add_argument("--resolver-version", default=RESOLVER_VERSION)
    parser.add_argument(
        "--allow-fallback",
        action="store_true",
        help="accept complete cross-venue fallback outcomes for sensitivity only",
    )
    parser.add_argument(
        "--code-revision",
        default=os.getenv("SCHURFER_GIT_SHA"),
        help="Git revision recorded in the manifest",
    )
    parser.add_argument(
        "--working-tree-dirty",
        action=argparse.BooleanOptionalAction,
        required=True,
        help="record whether the report was generated from uncommitted source changes",
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser


async def _run(args: argparse.Namespace) -> str:
    from .replay_repository import ReplayRepository

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for episode-replay")
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    generated_at = datetime.now(UTC)
    filters = ReplayFilters(
        since=args.since,
        until=args.until or generated_at,
        strategy_versions=tuple(args.strategy_version or ("pump_short_v1_market_quality",)),
        resolver_version=args.resolver_version,
        required_horizons=tuple(args.horizon or DEFAULT_REPLAY_HORIZONS),
        allow_fallback=args.allow_fallback,
    )
    repository = ReplayRepository.from_url(db_url)
    try:
        decisions = await repository.load(filters)
    finally:
        await repository.close()
    dataset = build_replay_dataset(decisions, filters)
    report = build_report(
        dataset,
        filters,
        generated_at=generated_at,
        code_revision=args.code_revision or "",
        working_tree_dirty=args.working_tree_dirty,
    )
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    args = build_parser().parse_args()
    sys.stdout.write(asyncio.run(_run(args)))
