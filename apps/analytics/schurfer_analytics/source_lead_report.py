"""Discovery-only paired screen for early source-led longs."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from statistics import median

from .clustered_inference import (
    CLUSTER_BOOTSTRAP_VERSION,
    DEFAULT_BOOTSTRAP_ITERATIONS,
    DEFAULT_BOOTSTRAP_SEED,
)
from .reporting import (
    ReportWindowNotStartedError,
    format_number,
    format_percentage,
    json_ready,
    markdown_table,
    normalize_code_revision,
    parse_utc_datetime,
    resolve_report_until,
)
from .source_lead import (
    DEFAULT_FUNDING_COST_BPS_PER_8H,
    DEFAULT_TAKER_FEE_BPS_PER_SIDE,
    ENTRY_DELAYS_MINUTES,
    EXECUTION_EXCHANGES,
    LONG_HORIZONS_MINUTES,
    MAX_CONFIRMATION_LAG_MINUTES,
    MAX_ROUND_TRIP_IMPACT_BPS,
    PRIMARY_DELAY_MINUTES,
    PRIMARY_HORIZON_MINUTES,
    SOURCE_EXCHANGES,
    SOURCE_LEAD_COHORT_START,
    SOURCE_LEAD_VERSION,
    CandidateBuildResult,
    LaneMetrics,
    PrimaryRouteInference,
    SourceLeadCandidate,
    SourceLeadEvent,
    SourceLeadOutcome,
    SourceLeadPath,
    build_lane_metrics,
    build_primary_inference,
    build_source_lead_candidates,
    evaluate_source_lead_candidate,
    source_lead_input_fingerprint,
    source_lead_path_fingerprint,
)

REPORT_VERSION = "source_lead_long_screen_discovery_v1"
PATH_VERSION = "ccxt_exact_symbol_1m_source_to_confirmation_plus_240m_v1"


@dataclass(frozen=True)
class CountRow:
    name: str
    count: int


@dataclass(frozen=True)
class RouteTiming:
    source_exchange: str
    execution_exchange: str
    candidates: int
    clusters: int
    median_confirmation_lag_seconds: float | None
    p95_confirmation_lag_seconds: float | None


@dataclass(frozen=True)
class SourceLeadManifest:
    report_version: str
    study_version: str
    path_version: str
    bootstrap_version: str
    code_revision: str
    working_tree_dirty: bool
    generated_at: datetime
    dataset_since: datetime
    dataset_until_exclusive: datetime
    input_fingerprint: str
    path_fingerprint: str
    source_exchanges: tuple[str, ...]
    execution_exchanges: tuple[str, ...]
    entry_delays_minutes: tuple[int, ...]
    horizons_minutes: tuple[int, ...]
    primary_delay_minutes: int
    primary_horizon_minutes: int
    maximum_confirmation_lag_minutes: int
    assumed_round_trip_impact_bps: float
    taker_fee_bps_per_side: float
    funding_cost_bps_per_8h: float
    bootstrap_iterations: int
    bootstrap_seed: int
    report_scope: str = "post_hoc_discovery_only_no_strategy_change"
    target_policy: str = "later_locally_validated_target_confirmation_required"
    comparison_policy: str = "same_event_same_execution_venue_same_exit_endpoint"


@dataclass(frozen=True)
class SourceLeadReport:
    manifest: SourceLeadManifest
    events: int
    candidates: int
    candidate_inputs: tuple[SourceLeadCandidate, ...]
    event_statuses: tuple[CountRow, ...]
    route_statuses: tuple[tuple[str, str, str, int], ...]
    path_statuses: tuple[CountRow, ...]
    route_timing: tuple[RouteTiming, ...]
    primary_inference: tuple[PrimaryRouteInference, ...]
    lane_metrics: tuple[LaneMetrics, ...]
    outcomes: tuple[SourceLeadOutcome, ...]
    paths: tuple[SourceLeadPath, ...]


def _percentile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _route_timing(candidates: tuple[SourceLeadCandidate, ...]) -> tuple[RouteTiming, ...]:
    routes = sorted({(row.source_exchange, row.execution_exchange) for row in candidates})
    rows: list[RouteTiming] = []
    for source, execution in routes:
        group = tuple(
            row
            for row in candidates
            if row.source_exchange == source and row.execution_exchange == execution
        )
        lags = [row.confirmation_lag_seconds for row in group]
        rows.append(
            RouteTiming(
                source_exchange=source,
                execution_exchange=execution,
                candidates=len(group),
                clusters=len({row.cluster_key for row in group}),
                median_confirmation_lag_seconds=median(lags) if lags else None,
                p95_confirmation_lag_seconds=_percentile(lags, 0.95),
            )
        )
    return tuple(rows)


def _count_rows(counter: Counter[str]) -> tuple[CountRow, ...]:
    return tuple(
        CountRow(name, count)
        for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    )


def _validate_contract(
    since: datetime,
    until: datetime,
    *,
    taker_fee_bps_per_side: float,
    funding_cost_bps_per_8h: float,
    bootstrap_iterations: int,
) -> None:
    if since < SOURCE_LEAD_COHORT_START:
        raise ValueError("source-lead screen cannot include the left-censored source window")
    if since >= until:
        raise ValueError("source-lead screen requires since before until")
    if not math.isfinite(taker_fee_bps_per_side) or taker_fee_bps_per_side < 0:
        raise ValueError("taker fee must be finite and non-negative")
    if not math.isfinite(funding_cost_bps_per_8h):
        raise ValueError("funding cost must be finite")
    if bootstrap_iterations < 100:
        raise ValueError("bootstrap iterations must be at least 100")


def build_source_lead_report(
    events: tuple[SourceLeadEvent, ...],
    candidate_build: CandidateBuildResult,
    paths: tuple[SourceLeadPath, ...],
    *,
    since: datetime,
    until: datetime,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
    taker_fee_bps_per_side: float = DEFAULT_TAKER_FEE_BPS_PER_SIDE,
    funding_cost_bps_per_8h: float = DEFAULT_FUNDING_COST_BPS_PER_8H,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    bootstrap_seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> SourceLeadReport:
    _validate_contract(
        since,
        until,
        taker_fee_bps_per_side=taker_fee_bps_per_side,
        funding_cost_bps_per_8h=funding_cost_bps_per_8h,
        bootstrap_iterations=bootstrap_iterations,
    )
    revision = normalize_code_revision(code_revision)
    for event in events:
        if not since <= event.first_seen_at < until:
            raise ValueError("source-lead event falls outside the exclusive report window")
        if any(row.first_seen_at >= until for row in event.observations):
            raise ValueError("source observation falls at or after the exclusive report cutoff")
    expected_candidates = build_source_lead_candidates(events, until=until)
    if candidate_build != expected_candidates:
        raise ValueError("candidate build does not reconcile with report inputs")
    candidate_ids = [row.candidate_id for row in candidate_build.candidates]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("source-lead report requires unique candidate ids")
    path_counts = Counter(path.candidate_id for path in paths)
    duplicate_paths = sorted(key for key, count in path_counts.items() if count > 1)
    if duplicate_paths:
        raise ValueError(f"duplicate source-lead paths: {duplicate_paths}")
    unexpected_paths = sorted(set(path_counts) - set(candidate_ids))
    if unexpected_paths:
        raise ValueError(f"paths do not belong to source-lead candidates: {unexpected_paths}")

    path_by_candidate = {path.candidate_id: path for path in paths}
    outcomes = tuple(
        outcome
        for candidate in candidate_build.candidates
        for outcome in evaluate_source_lead_candidate(
            candidate,
            path_by_candidate.get(candidate.candidate_id),
            taker_fee_bps_per_side=taker_fee_bps_per_side,
            funding_cost_bps_per_8h=funding_cost_bps_per_8h,
        )
    )
    return SourceLeadReport(
        manifest=SourceLeadManifest(
            report_version=REPORT_VERSION,
            study_version=SOURCE_LEAD_VERSION,
            path_version=PATH_VERSION,
            bootstrap_version=CLUSTER_BOOTSTRAP_VERSION,
            code_revision=revision,
            working_tree_dirty=working_tree_dirty,
            generated_at=generated_at,
            dataset_since=since,
            dataset_until_exclusive=until,
            input_fingerprint=source_lead_input_fingerprint(events),
            path_fingerprint=source_lead_path_fingerprint(paths),
            source_exchanges=SOURCE_EXCHANGES,
            execution_exchanges=EXECUTION_EXCHANGES,
            entry_delays_minutes=ENTRY_DELAYS_MINUTES,
            horizons_minutes=LONG_HORIZONS_MINUTES,
            primary_delay_minutes=PRIMARY_DELAY_MINUTES,
            primary_horizon_minutes=PRIMARY_HORIZON_MINUTES,
            maximum_confirmation_lag_minutes=MAX_CONFIRMATION_LAG_MINUTES,
            assumed_round_trip_impact_bps=MAX_ROUND_TRIP_IMPACT_BPS,
            taker_fee_bps_per_side=taker_fee_bps_per_side,
            funding_cost_bps_per_8h=funding_cost_bps_per_8h,
            bootstrap_iterations=bootstrap_iterations,
            bootstrap_seed=bootstrap_seed,
        ),
        events=len(events),
        candidates=len(candidate_build.candidates),
        candidate_inputs=candidate_build.candidates,
        event_statuses=tuple(
            CountRow(name, count) for name, count in candidate_build.event_statuses
        ),
        route_statuses=candidate_build.route_statuses,
        path_statuses=_count_rows(
            Counter(path.status for path in paths)
            + Counter(
                {"missing_path": len(set(candidate_ids) - set(path_counts))}
                if set(candidate_ids) - set(path_counts)
                else {}
            )
        ),
        route_timing=_route_timing(candidate_build.candidates),
        primary_inference=build_primary_inference(
            outcomes,
            bootstrap_iterations=bootstrap_iterations,
            bootstrap_seed=bootstrap_seed,
        ),
        lane_metrics=build_lane_metrics(outcomes),
        outcomes=outcomes,
        paths=paths,
    )


def render_json(report: SourceLeadReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True, allow_nan=False)


def render_markdown(report: SourceLeadReport) -> str:
    manifest = report.manifest
    lines = [
        "# Source Lead Long Screen",
        "",
        f"Generated: {manifest.generated_at.isoformat()}",
        f"Code revision: `{manifest.code_revision}`",
        f"Working tree dirty: {'yes' if manifest.working_tree_dirty else 'no'}",
        (
            f"Scope: {manifest.dataset_since.isoformat()} <= event "
            f"< {manifest.dataset_until_exclusive.isoformat()}"
        ),
        f"Input fingerprint: `{manifest.input_fingerprint}`",
        f"Path fingerprint: `{manifest.path_fingerprint}`",
        "",
        (
            "> Discovery-only upper screen. Each venue instrument has valid local contract "
            "metadata, but Schurfer has no canonical cross-venue token address mapping. A "
            "shared base ticker and later Binance/Bybit confirmation do not prove asset "
            "identity or historical liquidity at the earlier MEXC/Gate timestamp."
        ),
        (
            "> The primary comparison uses the same event, execution venue, and exit endpoint: "
            "early long after the source observation versus long after execution-venue "
            "confirmation. No production setting changes from this report."
        ),
        "",
        "## Input coverage",
        "",
    ]
    lines.extend(
        markdown_table(
            ("Metric", "Value"),
            [
                ("Events", report.events),
                ("Mature identity-safe route candidates", report.candidates),
                ("Assumed round-trip impact", f"{manifest.assumed_round_trip_impact_bps:.1f} bps"),
                ("Taker fee per side", f"{manifest.taker_fee_bps_per_side:.1f} bps"),
            ],
        )
    )
    lines.extend(["", "### Event statuses", ""])
    lines.extend(
        markdown_table(
            ("Status", "Events"),
            [(row.name, row.count) for row in report.event_statuses],
        )
    )
    lines.extend(["", "### Route statuses", ""])
    lines.extend(
        markdown_table(
            ("Source", "Execution", "Status", "Events"),
            [tuple(row) for row in report.route_statuses],
        )
    )
    lines.extend(["", "### Market paths", ""])
    lines.extend(
        markdown_table(
            ("Status", "Paths"),
            [(row.name, row.count) for row in report.path_statuses],
        )
    )
    lines.extend(["", "## Route timing", ""])
    lines.extend(
        markdown_table(
            ("Source", "Execution", "N", "Clusters", "Lag p50", "Lag p95"),
            [
                (
                    row.source_exchange,
                    row.execution_exchange,
                    row.candidates,
                    row.clusters,
                    format_number(row.median_confirmation_lag_seconds, suffix="s", missing="n/a"),
                    format_number(row.p95_confirmation_lag_seconds, suffix="s", missing="n/a"),
                )
                for row in report.route_timing
            ],
        )
    )
    lines.extend(["", "## Primary paired result: source entry vs confirmation entry, +30m", ""])
    lines.extend(
        markdown_table(
            (
                "Route",
                "Resolved/N",
                "Clusters",
                "Early net",
                "Confirm net",
                "Lead capture",
                "Delta",
                "Delta 95% CI",
                "Weakest LOO",
                "Without busiest week",
                "Holm p",
            ),
            [
                (
                    f"{row.source_exchange}->{row.execution_exchange}",
                    f"{row.resolved}/{row.episodes}",
                    row.clusters,
                    format_percentage(row.early_mean_net_pct, missing="n/a"),
                    format_percentage(row.control_mean_net_pct, missing="n/a"),
                    format_percentage(row.mean_lead_capture_gross_pct, missing="n/a"),
                    format_percentage(row.paired_mean_delta_pct, missing="n/a"),
                    (
                        f"[{format_percentage(row.paired_lower_95_pct)}, "
                        f"{format_percentage(row.paired_upper_95_pct)}]"
                        if row.paired_lower_95_pct is not None
                        else "n/a"
                    ),
                    format_percentage(row.weakest_leave_one_cluster_out_pct, missing="n/a"),
                    format_percentage(row.excluding_busiest_week_pct, missing="n/a"),
                    format_number(row.holm_adjusted_p_value, 4, missing="n/a"),
                )
                for row in report.primary_inference
            ],
        )
    )
    lines.extend(
        [
            "",
            "_A positive paired delta only says that the earlier timestamp improved the same "
            "event outcome under an assumed cost cap. Both early absolute net and paired delta "
            "must be positive before prospective quote capture is justified._",
            "",
            "## Early-long horizon and delay surface",
            "",
        ]
    )
    early = tuple(row for row in report.lane_metrics if row.lane == "early_long")
    lines.extend(
        markdown_table(
            (
                "Route",
                "Delay",
                "Horizon",
                "N",
                "Traded",
                "Cash",
                "Early net",
                "Confirm net",
                "Lead capture",
                "Delta",
                "Win rate",
            ),
            [
                (
                    f"{row.source_exchange}->{row.execution_exchange}",
                    f"{row.delay_minutes}m",
                    f"{row.horizon_minutes}m",
                    row.candidates,
                    row.traded,
                    row.cash,
                    format_percentage(row.mean_net_pct, missing="n/a"),
                    format_percentage(row.control_mean_net_pct, missing="n/a"),
                    format_percentage(row.mean_lead_capture_gross_pct, missing="n/a"),
                    format_percentage(row.paired_mean_delta_pct, missing="n/a"),
                    format_percentage(row.win_rate_pct, missing="n/a"),
                )
                for row in early
            ],
        )
    )
    lines.extend(["", "## Confirmation-time short screen", ""])
    short = tuple(row for row in report.lane_metrics if row.lane == "confirmation_short")
    lines.extend(
        markdown_table(
            ("Route", "Horizon", "N", "Mean net", "Median net", "Win rate", "PF"),
            [
                (
                    f"{row.source_exchange}->{row.execution_exchange}",
                    f"{row.horizon_minutes}m",
                    row.candidates,
                    format_percentage(row.mean_net_pct, missing="n/a"),
                    format_percentage(row.median_net_pct, missing="n/a"),
                    format_percentage(row.win_rate_pct, missing="n/a"),
                    format_number(row.profit_factor, missing="n/a"),
                )
                for row in short
            ],
        )
    )
    lines.extend(
        [
            "",
            "_The short table is a separate descriptive book entered at execution-venue "
            "confirmation. It is not a reversal trigger and cannot be combined with the long "
            "headline._",
        ]
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Screen MEXC/Gate first-source lead against later Binance/Bybit confirmation"
    )
    parser.add_argument("--since", type=parse_utc_datetime, default=SOURCE_LEAD_COHORT_START)
    parser.add_argument("--until", type=parse_utc_datetime)
    parser.add_argument(
        "--taker-fee-bps-per-side",
        type=float,
        default=DEFAULT_TAKER_FEE_BPS_PER_SIDE,
    )
    parser.add_argument(
        "--funding-cost-bps-per-8h",
        type=float,
        default=DEFAULT_FUNDING_COST_BPS_PER_8H,
    )
    parser.add_argument("--bootstrap-iterations", type=int, default=DEFAULT_BOOTSTRAP_ITERATIONS)
    parser.add_argument("--bootstrap-seed", type=int, default=DEFAULT_BOOTSTRAP_SEED)
    parser.add_argument("--code-revision", default=os.getenv("SCHURFER_GIT_SHA"))
    parser.add_argument(
        "--working-tree-dirty",
        action=argparse.BooleanOptionalAction,
        required=True,
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser


async def _run(args: argparse.Namespace) -> str:
    from .exchange_registry import EXCHANGE_FACTORIES
    from .source_lead_market import fetch_source_lead_paths
    from .source_lead_repository import SourceLeadRepository

    generated_at = datetime.now(UTC)
    until = resolve_report_until(
        args.until,
        generated_at,
        cohort_start=SOURCE_LEAD_COHORT_START,
        report_label="source-lead discovery",
    )
    _validate_contract(
        args.since,
        until,
        taker_fee_bps_per_side=args.taker_fee_bps_per_side,
        funding_cost_bps_per_8h=args.funding_cost_bps_per_8h,
        bootstrap_iterations=args.bootstrap_iterations,
    )
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    normalize_code_revision(args.code_revision)
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for source-lead-report")

    sys.stderr.write("source-lead: loading point-in-time source events\n")
    repository = SourceLeadRepository.from_url(db_url)
    try:
        events = await repository.load(args.since, until)
    finally:
        await repository.close()
    candidate_build = build_source_lead_candidates(events, until=until)

    def progress(exchange: str, index: int, total: int) -> None:
        sys.stderr.write(f"source-lead: fetching {exchange} ({index}/{total})\n")

    paths = await fetch_source_lead_paths(
        candidate_build.candidates,
        EXCHANGE_FACTORIES,
        on_exchange=progress,
    )
    sys.stderr.write("source-lead: building report\n")
    report = build_source_lead_report(
        events,
        candidate_build,
        paths,
        since=args.since,
        until=until,
        generated_at=generated_at,
        code_revision=args.code_revision,
        working_tree_dirty=args.working_tree_dirty,
        taker_fee_bps_per_side=args.taker_fee_bps_per_side,
        funding_cost_bps_per_8h=args.funding_cost_bps_per_8h,
        bootstrap_iterations=args.bootstrap_iterations,
        bootstrap_seed=args.bootstrap_seed,
    )
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        output = asyncio.run(_run(args))
    except ReportWindowNotStartedError as exc:
        parser.error(str(exc))
    sys.stdout.write(output)
