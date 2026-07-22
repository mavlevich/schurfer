"""Read-only measurement report for decision and forward-outcome quality."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from .outcomes import HORIZONS_MINUTES, RESOLVER_VERSION


@dataclass(frozen=True)
class ReportFilters:
    since: datetime | None = None
    until: datetime | None = None
    strategy_versions: tuple[str, ...] = ()
    resolver_version: str = RESOLVER_VERSION
    exchange_horizon: int = 60

    def __post_init__(self) -> None:
        if self.since is not None and self.until is not None and self.since >= self.until:
            raise ValueError("--since must be earlier than --until")
        if self.exchange_horizon not in HORIZONS_MINUTES:
            raise ValueError(f"exchange horizon must be one of {HORIZONS_MINUTES}")
        if not self.resolver_version.strip():
            raise ValueError("resolver version must not be empty")


@dataclass(frozen=True)
class DatasetHealth:
    total_decisions: int
    first_decision_at: datetime | None
    last_decision_at: datetime | None
    observation_hours: float
    decisions_per_hour: float | None
    unique_episodes: int
    direct_episode_ids_present_pct: float
    decision_ids_present_pct: float
    prices_present_pct: float
    features_present_pct: float
    signal_present_pct: float
    liquidity_present_pct: float
    liquidity_sampled_pct: float
    sampled_contract_size_present_pct: float
    liquidity_fetch_failed_pct: float
    liquidity_no_exchange_pct: float
    quality_present_pct: float
    signal_lag_samples: int
    signal_lag_avg_seconds: float | None
    signal_lag_p50_seconds: float | None
    signal_lag_p95_seconds: float | None


@dataclass(frozen=True)
class CohortRow:
    strategy_version: str
    decisions: int
    episodes: int
    taken: int
    skipped: int
    first_decision_at: datetime
    last_decision_at: datetime


@dataclass(frozen=True)
class QualityReasonRow:
    strategy_version: str
    reason: str
    decisions: int


@dataclass(frozen=True)
class CoverageRow:
    strategy_version: str
    horizon_minutes: int
    status: str
    decisions: int


@dataclass(frozen=True)
class PerformanceRow:
    strategy_version: str
    horizon_minutes: int
    segment: str
    exchange: str | None
    decisions: int
    episodes: int
    exact_venue: int
    fallback_venue: int
    avg_short_return_pct: float | None
    median_short_return_pct: float | None
    win_rate_pct: float | None
    avg_mfe_pct: float | None
    avg_mae_pct: float | None


@dataclass(frozen=True)
class MeasurementReport:
    generated_at: datetime
    filters: ReportFilters
    health: DatasetHealth
    cohorts: tuple[CohortRow, ...]
    quality_reasons: tuple[QualityReasonRow, ...]
    coverage: tuple[CoverageRow, ...]
    performance: tuple[PerformanceRow, ...]
    exchange_performance: tuple[PerformanceRow, ...]


def _json_ready(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_ready(item) for item in value]
    return value


def render_json(report: MeasurementReport) -> str:
    return json.dumps(_json_ready(asdict(report)), indent=2, sort_keys=True)


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}%"


def _number(value: float | None, decimals: int = 2) -> str:
    return "—" if value is None else f"{value:.{decimals}f}"


def _horizon(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 1440:
        return f"{minutes // 60}h"
    return f"{minutes // 1440}d"


def _table(headers: tuple[str, ...], rows: list[tuple[Any, ...]]) -> list[str]:
    if not rows:
        return ["_No rows._"]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend(
        "| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |" for row in rows
    )
    return lines


def render_markdown(report: MeasurementReport) -> str:
    filters = report.filters
    health = report.health
    scope = [f"resolver={filters.resolver_version}"]
    if filters.since:
        scope.append(f"since={filters.since.isoformat()}")
    if filters.until:
        scope.append(f"until={filters.until.isoformat()}")
    if filters.strategy_versions:
        scope.append(f"strategies={','.join(filters.strategy_versions)}")

    lines = [
        "# Decision Measurement Report",
        "",
        f"Generated: {report.generated_at.isoformat()}",
        f"Scope: {', '.join(scope)}",
        "",
        "> Descriptive decision-level measurements only. Decisions inside one pump episode are",
        "> correlated; use the versioned episode replay before treating differences as",
        "> strategy edge.",
        "",
        "## Dataset health",
        "",
    ]
    lines.extend(
        _table(
            ("Metric", "Value"),
            [
                ("Decisions", health.total_decisions),
                ("Unique pump episodes", health.unique_episodes),
                (
                    "Decision window",
                    (
                        f"{health.first_decision_at.isoformat()} — "
                        f"{health.last_decision_at.isoformat()}"
                        if health.first_decision_at and health.last_decision_at
                        else "—"
                    ),
                ),
                ("Observation hours", _number(health.observation_hours)),
                ("Decisions/hour", _number(health.decisions_per_hour)),
                ("Direct episode IDs present", _pct(health.direct_episode_ids_present_pct)),
                ("Decision IDs present", _pct(health.decision_ids_present_pct)),
                ("Decision prices present", _pct(health.prices_present_pct)),
                ("Feature envelopes present", _pct(health.features_present_pct)),
                ("Signal payloads present", _pct(health.signal_present_pct)),
                ("Liquidity envelopes present", _pct(health.liquidity_present_pct)),
                ("Liquidity sampled", _pct(health.liquidity_sampled_pct)),
                (
                    "Sampled snapshots with contract_size",
                    _pct(health.sampled_contract_size_present_pct),
                ),
                ("Liquidity fetch failed", _pct(health.liquidity_fetch_failed_pct)),
                ("No configured exchange", _pct(health.liquidity_no_exchange_pct)),
                ("Market-quality verdict present", _pct(health.quality_present_pct)),
                ("Signal-lag samples", health.signal_lag_samples),
                (
                    "Signal lag avg / p50 / p95 seconds",
                    (
                        f"{_number(health.signal_lag_avg_seconds)} / "
                        f"{_number(health.signal_lag_p50_seconds)} / "
                        f"{_number(health.signal_lag_p95_seconds)}"
                    ),
                ),
            ],
        )
    )

    lines.extend(["", "## Strategy cohorts", ""])
    lines.extend(
        _table(
            ("Strategy", "Decisions", "Episodes", "Taken", "Skipped", "First", "Last"),
            [
                (
                    row.strategy_version,
                    row.decisions,
                    row.episodes,
                    row.taken,
                    row.skipped,
                    row.first_decision_at.isoformat(),
                    row.last_decision_at.isoformat(),
                )
                for row in report.cohorts
            ],
        )
    )

    lines.extend(["", "## Market-quality reasons", ""])
    lines.extend(
        _table(
            ("Strategy", "Reason/status", "Decisions"),
            [(row.strategy_version, row.reason, row.decisions) for row in report.quality_reasons],
        )
    )

    lines.extend(["", "## Outcome coverage", ""])
    lines.extend(
        _table(
            ("Strategy", "Horizon", "Status", "Decisions"),
            [
                (row.strategy_version, _horizon(row.horizon_minutes), row.status, row.decisions)
                for row in report.coverage
            ],
        )
    )

    performance_headers = (
        "Strategy",
        "Horizon",
        "Segment",
        "N",
        "Episodes",
        "Exact/fallback",
        "Avg return",
        "Median",
        "Win rate",
        "Avg MFE",
        "Avg MAE",
    )
    lines.extend(["", "## Raw forward outcomes", ""])
    lines.extend(
        _table(
            performance_headers,
            [
                (
                    row.strategy_version,
                    _horizon(row.horizon_minutes),
                    row.segment,
                    row.decisions,
                    row.episodes,
                    f"{row.exact_venue}/{row.fallback_venue}",
                    _pct(row.avg_short_return_pct),
                    _pct(row.median_short_return_pct),
                    _pct(row.win_rate_pct),
                    _pct(row.avg_mfe_pct),
                    _pct(row.avg_mae_pct),
                )
                for row in report.performance
            ],
        )
    )

    lines.extend(
        [
            "",
            f"## Exchange view at {_horizon(filters.exchange_horizon)}",
            "",
        ]
    )
    lines.extend(
        _table(
            ("Strategy", "Exchange", *performance_headers[2:]),
            [
                (
                    row.strategy_version,
                    row.exchange or "unknown",
                    row.segment,
                    row.decisions,
                    row.episodes,
                    f"{row.exact_venue}/{row.fallback_venue}",
                    _pct(row.avg_short_return_pct),
                    _pct(row.median_short_return_pct),
                    _pct(row.win_rate_pct),
                    _pct(row.avg_mfe_pct),
                    _pct(row.avg_mae_pct),
                )
                for row in report.exchange_performance
            ],
        )
    )
    return "\n".join(lines) + "\n"


def parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO-8601 datetime: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Report decision dataset and raw outcomes")
    parser.add_argument("--since", type=parse_datetime, help="inclusive UTC ISO-8601 cutoff")
    parser.add_argument("--until", type=parse_datetime, help="exclusive UTC ISO-8601 cutoff")
    parser.add_argument(
        "--strategy-version",
        action="append",
        default=[],
        help="strategy cohort to include; repeat for multiple cohorts",
    )
    parser.add_argument("--resolver-version", default=RESOLVER_VERSION)
    parser.add_argument(
        "--exchange-horizon",
        type=int,
        choices=HORIZONS_MINUTES,
        default=60,
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser


async def _run(args: argparse.Namespace) -> str:
    from .measurement_repository import MeasurementRepository

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for measurement-report")
    filters = ReportFilters(
        since=args.since,
        until=args.until,
        strategy_versions=tuple(args.strategy_version),
        resolver_version=args.resolver_version,
        exchange_horizon=args.exchange_horizon,
    )
    repository = MeasurementRepository.from_url(db_url)
    try:
        report = await repository.generate(filters)
    finally:
        await repository.close()
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    args = build_parser().parse_args()
    sys.stdout.write(asyncio.run(_run(args)))
