"""Render durable per-exchange pump discovery coverage."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from itertools import combinations
from typing import Any

from .reporting import json_ready as _json_ready
from .reporting import markdown_table as _table
from .reporting import parse_utc_datetime

parse_datetime = parse_utc_datetime


@dataclass(frozen=True)
class CoverageFilters:
    since: datetime | None = None
    until: datetime | None = None

    def __post_init__(self) -> None:
        if self.since is not None and self.until is not None and self.since >= self.until:
            raise ValueError("--since must be earlier than --until")


@dataclass(frozen=True)
class SourceObservation:
    event_id: int
    exchange: str
    first_seen_at: datetime


@dataclass(frozen=True)
class SourceCoverageRow:
    exchange: str
    episodes: int
    sole_source_episodes: int
    first_source_episodes: int
    confirmed_episodes: int
    lead_p50_seconds: float
    lead_p95_seconds: float


@dataclass(frozen=True)
class PairOverlapRow:
    first_exchange: str
    second_exchange: str
    episodes: int


@dataclass(frozen=True)
class ExchangeCoverageReport:
    generated_at: datetime
    filters: CoverageFilters
    total_episodes: int
    attributed_episodes: int
    sources: tuple[SourceCoverageRow, ...]
    overlaps: tuple[PairOverlapRow, ...]


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def build_report(
    filters: CoverageFilters,
    total_episodes: int,
    observations: list[SourceObservation],
) -> ExchangeCoverageReport:
    by_event: dict[int, list[SourceObservation]] = {}
    for observation in observations:
        by_event.setdefault(observation.event_id, []).append(observation)

    source_stats: dict[str, dict[str, Any]] = {}
    overlap_counts: dict[tuple[str, str], int] = {}
    for event_sources in by_event.values():
        earliest = min(source.first_seen_at for source in event_sources)
        exchanges = sorted({source.exchange for source in event_sources})
        for first, second in combinations(exchanges, 2):
            overlap_counts[(first, second)] = overlap_counts.get((first, second), 0) + 1

        for source in event_sources:
            stats = source_stats.setdefault(
                source.exchange,
                {"episodes": set(), "sole": 0, "first": 0, "confirmed": 0, "leads": []},
            )
            stats["episodes"].add(source.event_id)
            if len(exchanges) == 1:
                stats["sole"] += 1
            else:
                stats["confirmed"] += 1
            lead = max(0.0, (source.first_seen_at - earliest).total_seconds())
            stats["leads"].append(lead)
            if lead == 0:
                stats["first"] += 1

    sources = tuple(
        sorted(
            (
                SourceCoverageRow(
                    exchange=exchange,
                    episodes=len(stats["episodes"]),
                    sole_source_episodes=stats["sole"],
                    first_source_episodes=stats["first"],
                    confirmed_episodes=stats["confirmed"],
                    lead_p50_seconds=_percentile(stats["leads"], 0.5),
                    lead_p95_seconds=_percentile(stats["leads"], 0.95),
                )
                for exchange, stats in source_stats.items()
            ),
            key=lambda row: (-row.episodes, row.exchange),
        )
    )
    overlaps = tuple(
        PairOverlapRow(first, second, episodes)
        for (first, second), episodes in sorted(
            overlap_counts.items(), key=lambda item: (-item[1], item[0])
        )
    )
    return ExchangeCoverageReport(
        generated_at=datetime.now(UTC),
        filters=filters,
        total_episodes=total_episodes,
        attributed_episodes=len(by_event),
        sources=sources,
        overlaps=overlaps,
    )


def render_json(report: ExchangeCoverageReport) -> str:
    return json.dumps(_json_ready(asdict(report)), indent=2, sort_keys=True)


def render_markdown(report: ExchangeCoverageReport) -> str:
    scope: list[str] = []
    if report.filters.since:
        scope.append(f"since={report.filters.since.isoformat()}")
    if report.filters.until:
        scope.append(f"until={report.filters.until.isoformat()}")
    attribution_pct = (
        report.attributed_episodes / report.total_episodes * 100 if report.total_episodes else 0.0
    )
    lines = [
        "# Exchange Coverage Report",
        "",
        f"Generated: {report.generated_at.isoformat()}",
        f"Scope: {', '.join(scope) if scope else 'all time'}",
        "",
        f"Episodes: {report.total_episodes}",
        f"Attributed episodes: {report.attributed_episodes} ({attribution_pct:.2f}%)",
        "",
        "> Attribution starts when migration 0013 is deployed. Use --since with the deployment",
        "> timestamp for an unbiased cohort; older and already-open episodes are left-censored.",
        "",
        "## Source contribution",
        "",
    ]
    lines.extend(
        _table(
            (
                "Exchange",
                "Episodes",
                "Sole source",
                "First source",
                "Confirmed",
                "Lead p50",
                "Lead p95",
            ),
            [
                (
                    row.exchange,
                    row.episodes,
                    row.sole_source_episodes,
                    row.first_source_episodes,
                    row.confirmed_episodes,
                    f"{row.lead_p50_seconds:.1f}s",
                    f"{row.lead_p95_seconds:.1f}s",
                )
                for row in report.sources
            ],
        )
    )
    lines.extend(["", "## Pairwise overlap", ""])
    lines.extend(
        _table(
            ("First exchange", "Second exchange", "Episodes"),
            [(row.first_exchange, row.second_exchange, row.episodes) for row in report.overlaps],
        )
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Report per-exchange pump discovery coverage")
    parser.add_argument("--since", type=parse_datetime, help="inclusive UTC ISO-8601 cutoff")
    parser.add_argument("--until", type=parse_datetime, help="exclusive UTC ISO-8601 cutoff")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser


async def _run(args: argparse.Namespace) -> str:
    from .exchange_coverage_repository import ExchangeCoverageRepository

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for exchange-coverage-report")
    filters = CoverageFilters(since=args.since, until=args.until)
    repository = ExchangeCoverageRepository.from_url(db_url)
    try:
        report = await repository.generate(filters)
    finally:
        await repository.close()
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    args = build_parser().parse_args()
    sys.stdout.write(asyncio.run(_run(args)))
