"""Calibrate decision-time exit impact against the quote observed at paper close.

`execution_cost_unreliable` (added 2026-08-24, PR fix/wide-spread-exit-
cost-v1) is a diagnostic safety flag, not a trading rule. It marks episodes
whose observed close-time spread was wide enough that the decision-time
model's own known failure mode -- reusing an ENTRY-time liquidity snapshot
(`decision_impact_bps` in virtual_strategy.py) as the exit-cost estimate,
even though a book that has genuinely thinned out by exit time (most
plausible exactly when `initial_sl` fires on a sharp adverse move) is not
visible to that snapshot -- is most likely to have mattered. Do not cite a
specific historical count/percentage/mean-delta for this pattern from this
docstring: any such number belongs in a versioned, fingerprinted research
artifact (cohort, code revision, exact run command), not asserted inline in
code, and no such artifact has been archived for this finding yet -- re-run
this report to see the current numbers, and register one before treating a
specific figure as evidence rather than as a lead.

`WIDE_SPREAD_UNRELIABLE_THRESHOLD_BPS` is that same discovery-derived
threshold, deliberately not yet promoted to a production filter: it was
read off where this report's own pre-existing `close_spread` bucket
boundaries already sat, on a single backward-looking sample, not frozen
before this finding the way every other registered contract in this
codebase freezes its thresholds before looking. Using it to reject trades
in production right now would repeat the exact p-hacking risk this
project's own research discipline exists to avoid. What is safe today is
narrower: mark, on each `ComparableExit` row and in this report's own
aggregate/manifest output, which of *this report's own* modeled-vs-observed
comparisons rest on a wide-close-spread exit, so a reader of this report can
weight or exclude them explicitly instead of treating every comparison as
equally reliable. This report does not itself compute or expose `net_return`
anywhere -- that figure, when it exists, lives in a different consumer of
`calculate_performance()`'s output (see packages/performance), and this flag
is not wired to it. Extending the flag to mark net_return figures in that
other consumer is explicitly out of scope here and would need its own
change. Promoting this into an actual pre-trade filter needs its own
forward-registered contract, exactly like every other actionable threshold
in this codebase.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from statistics import mean, median
from typing import TYPE_CHECKING, Any

from .clustered_inference import percentile
from .reporting import (
    format_number,
    format_percentage,
    json_ready,
    markdown_table,
    normalize_code_revision,
    parse_utc_datetime,
)

if TYPE_CHECKING:
    from collections.abc import Callable

EXIT_LIQUIDITY_COHORT_START = datetime(2026, 7, 29, 15, 45, 34, tzinfo=UTC)
DIRECTIONAL_SAMPLE_SIZE = 30
DECISION_SAMPLE_SIZE = 100
MAX_EXIT_QUOTE_SKEW_SECONDS = 120
REPORT_CONTRACT = "exit_liquidity_calibration_v1"
# Discovery-derived (2026-08-24), diagnostic only -- see this module's own
# docstring. Never used to reject or defer a live/paper trade.
WIDE_SPREAD_UNRELIABLE_THRESHOLD_BPS = 50.0


@dataclass(frozen=True)
class ExitLiquidityFilters:
    since: datetime
    until: datetime

    def __post_init__(self) -> None:
        if self.since.tzinfo is None or self.until.tzinfo is None:
            raise ValueError("since and until must be timezone-aware")
        if self.since < EXIT_LIQUIDITY_COHORT_START:
            raise ValueError("exit-liquidity calibration cannot include pre-capture trades")
        if self.since >= self.until:
            raise ValueError("since must be earlier than until")


@dataclass(frozen=True)
class ExitLiquidityRow:
    trade_id: int
    symbol: str
    exchange: str
    size_usd: float
    entry_at: datetime
    exit_at: datetime
    exit_reason: str | None
    modeled_exit_bps: float | None
    observation_id: int | None
    observed_at: datetime | None
    observation_exchange: str | None
    observation_symbol: str | None
    observation_status: str | None
    requested_notional_usd: float | None
    filled_notional_usd: float | None
    observed_spread_bps: float | None
    observed_exit_bps: float | None
    latency_ms: int | None
    error: str | None


@dataclass(frozen=True)
class ComparableExit:
    trade_id: int
    base: str
    exchange: str
    exit_reason: str
    duration_minutes: float
    requested_depth_usd: float
    modeled_exit_bps: float
    observed_exit_bps: float
    delta_bps: float
    observed_spread_bps: float
    latency_ms: int
    # Diagnostic only -- see this module's own docstring and
    # WIDE_SPREAD_UNRELIABLE_THRESHOLD_BPS. Never a trading decision.
    execution_cost_unreliable: bool


@dataclass(frozen=True)
class CalibrationMetrics:
    observations: int
    asset_clusters: int
    largest_cluster_share_pct: float
    mean_modeled_exit_bps: float
    median_modeled_exit_bps: float
    mean_observed_exit_bps: float
    median_observed_exit_bps: float
    mean_delta_bps: float
    median_delta_bps: float
    p25_delta_bps: float
    p75_delta_bps: float
    p90_delta_bps: float
    observed_worse_pct: float
    delta_above_10bps_pct: float
    delta_above_25bps_pct: float
    mean_latency_ms: float
    p95_latency_ms: float
    # Diagnostic only -- see this module's own docstring and
    # WIDE_SPREAD_UNRELIABLE_THRESHOLD_BPS. Never a trading decision.
    execution_cost_unreliable_count: int
    execution_cost_unreliable_pct: float


@dataclass(frozen=True)
class SegmentMetrics:
    dimension: str
    bucket: str
    observations: int
    asset_clusters: int
    mean_modeled_exit_bps: float
    mean_observed_exit_bps: float
    mean_delta_bps: float
    median_delta_bps: float
    observed_worse_pct: float


@dataclass(frozen=True)
class ExitLiquidityCalibrationReport:
    manifest: dict[str, Any]
    readiness: dict[str, Any]
    observation_statuses: dict[str, int]
    exclusion_reasons: dict[str, int]
    metrics: CalibrationMetrics | None
    segments: tuple[SegmentMetrics, ...]
    comparable_exits: tuple[ComparableExit, ...]


def _finite_nonnegative(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value >= 0


def _base(symbol: str) -> str:
    return symbol.split("/", 1)[0].split(":", 1)[0].upper()


def _duration_bucket(minutes: float) -> str:
    if minutes < 60:
        return "<60m"
    if minutes < 180:
        return "60-180m"
    if minutes < 360:
        return "180-360m"
    return ">=360m"


def _spread_bucket(spread_bps: float) -> str:
    if spread_bps < 10:
        return "<10bps"
    if spread_bps < 25:
        return "10-25bps"
    if spread_bps < 50:
        return "25-50bps"
    return ">=50bps"


def _impact_bucket(impact_bps: float) -> str:
    if impact_bps < 10:
        return "<10bps"
    if impact_bps < 20:
        return "10-20bps"
    if impact_bps < 50:
        return "20-50bps"
    return ">=50bps"


def _depth_bucket(requested_usd: float) -> str:
    if requested_usd <= 50:
        return "<=50usd"
    if requested_usd <= 100:
        return "50-100usd"
    if requested_usd <= 250:
        return "100-250usd"
    return ">250usd"


def _exclusion_reason(row: ExitLiquidityRow) -> str | None:
    if row.observation_id is None:
        return "missing_observation"
    if row.observation_status != "sampled":
        return f"status:{row.observation_status or 'missing'}"
    if row.observation_exchange != row.exchange or row.observation_symbol != row.symbol:
        return "identity_mismatch"
    if not _finite_nonnegative(row.modeled_exit_bps):
        return "missing_or_invalid_modeled_impact"
    if not _finite_nonnegative(row.observed_exit_bps):
        return "missing_or_invalid_observed_impact"
    if not _finite_nonnegative(row.observed_spread_bps):
        return "missing_or_invalid_spread"
    if row.latency_ms is None or row.latency_ms < 0:
        return "missing_or_invalid_latency"
    if row.observed_at is None:
        return "missing_observed_at"
    if abs((row.exit_at - row.observed_at).total_seconds()) > MAX_EXIT_QUOTE_SKEW_SECONDS:
        return "quote_exit_time_mismatch"
    if not _finite_nonnegative(row.requested_notional_usd):
        return "missing_or_invalid_requested_notional"
    requested = row.requested_notional_usd
    assert requested is not None
    if abs(requested - row.size_usd) > 0.01:
        return "requested_notional_mismatch"
    if not _finite_nonnegative(row.filled_notional_usd):
        return "missing_or_invalid_filled_notional"
    filled = row.filled_notional_usd
    assert filled is not None
    if filled + 0.01 < requested:
        return "insufficient_visible_depth"
    return None


def _comparable(row: ExitLiquidityRow) -> ComparableExit:
    modeled = row.modeled_exit_bps
    observed = row.observed_exit_bps
    spread = row.observed_spread_bps
    latency = row.latency_ms
    requested = row.requested_notional_usd
    assert modeled is not None
    assert observed is not None
    assert spread is not None
    assert latency is not None
    assert requested is not None
    duration = (row.exit_at - row.entry_at).total_seconds() / 60
    if duration < 0:
        raise ValueError(f"trade {row.trade_id} exits before entry")
    return ComparableExit(
        trade_id=row.trade_id,
        base=_base(row.symbol),
        exchange=row.exchange,
        exit_reason=(row.exit_reason or "unknown").strip() or "unknown",
        duration_minutes=duration,
        requested_depth_usd=requested,
        modeled_exit_bps=modeled,
        observed_exit_bps=observed,
        delta_bps=observed - modeled,
        observed_spread_bps=spread,
        latency_ms=latency,
        execution_cost_unreliable=spread >= WIDE_SPREAD_UNRELIABLE_THRESHOLD_BPS,
    )


def _metrics(rows: tuple[ComparableExit, ...]) -> CalibrationMetrics | None:
    if not rows:
        return None
    modeled = [row.modeled_exit_bps for row in rows]
    observed = [row.observed_exit_bps for row in rows]
    deltas = sorted(row.delta_bps for row in rows)
    latencies = sorted(float(row.latency_ms) for row in rows)
    cluster_counts = Counter(row.base for row in rows)
    return CalibrationMetrics(
        observations=len(rows),
        asset_clusters=len(cluster_counts),
        largest_cluster_share_pct=max(cluster_counts.values()) / len(rows) * 100,
        mean_modeled_exit_bps=mean(modeled),
        median_modeled_exit_bps=median(modeled),
        mean_observed_exit_bps=mean(observed),
        median_observed_exit_bps=median(observed),
        mean_delta_bps=mean(deltas),
        median_delta_bps=median(deltas),
        p25_delta_bps=percentile(tuple(deltas), 0.25),
        p75_delta_bps=percentile(tuple(deltas), 0.75),
        p90_delta_bps=percentile(tuple(deltas), 0.90),
        observed_worse_pct=sum(value > 0 for value in deltas) / len(deltas) * 100,
        delta_above_10bps_pct=sum(value > 10 for value in deltas) / len(deltas) * 100,
        delta_above_25bps_pct=sum(value > 25 for value in deltas) / len(deltas) * 100,
        mean_latency_ms=mean(latencies),
        p95_latency_ms=percentile(tuple(latencies), 0.95),
        execution_cost_unreliable_count=sum(row.execution_cost_unreliable for row in rows),
        execution_cost_unreliable_pct=sum(row.execution_cost_unreliable for row in rows)
        / len(rows)
        * 100,
    )


def _segment(rows: tuple[ComparableExit, ...], dimension: str, bucket: str) -> SegmentMetrics:
    deltas = [row.delta_bps for row in rows]
    return SegmentMetrics(
        dimension=dimension,
        bucket=bucket,
        observations=len(rows),
        asset_clusters=len({row.base for row in rows}),
        mean_modeled_exit_bps=mean(row.modeled_exit_bps for row in rows),
        mean_observed_exit_bps=mean(row.observed_exit_bps for row in rows),
        mean_delta_bps=mean(deltas),
        median_delta_bps=median(deltas),
        observed_worse_pct=sum(value > 0 for value in deltas) / len(deltas) * 100,
    )


def _segments(rows: tuple[ComparableExit, ...]) -> tuple[SegmentMetrics, ...]:
    dimensions: dict[str, Callable[[ComparableExit], str]] = {
        "exchange": lambda row: row.exchange,
        "exit_reason": lambda row: row.exit_reason,
        "duration": lambda row: _duration_bucket(row.duration_minutes),
        "close_spread": lambda row: _spread_bucket(row.observed_spread_bps),
        "requested_depth": lambda row: _depth_bucket(row.requested_depth_usd),
        "modeled_impact": lambda row: _impact_bucket(row.modeled_exit_bps),
    }
    result: list[SegmentMetrics] = []
    for dimension, selector in dimensions.items():
        buckets: dict[str, list[ComparableExit]] = defaultdict(list)
        for row in rows:
            buckets[selector(row)].append(row)
        result.extend(
            _segment(tuple(selected), dimension, bucket)
            for bucket, selected in sorted(buckets.items())
        )
    return tuple(result)


def _fingerprint(rows: tuple[ExitLiquidityRow, ...]) -> str:
    payload = json.dumps(
        json_ready([asdict(row) for row in rows]),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def build_exit_liquidity_calibration_report(
    rows: tuple[ExitLiquidityRow, ...],
    filters: ExitLiquidityFilters,
    *,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
) -> ExitLiquidityCalibrationReport:
    if len({row.trade_id for row in rows}) != len(rows):
        raise ValueError("duplicate trade rows in exit-liquidity calibration input")
    statuses = Counter(row.observation_status or "missing_observation" for row in rows)
    exclusions: Counter[str] = Counter()
    comparable: list[ComparableExit] = []
    for row in rows:
        reason = _exclusion_reason(row)
        if reason:
            exclusions[reason] += 1
        else:
            comparable.append(_comparable(row))
    comparable_rows = tuple(comparable)
    count = len(comparable_rows)
    if count < DIRECTIONAL_SAMPLE_SIZE:
        state = "collecting"
        interpretation = "insufficient_for_directional_calibration"
    elif count < DECISION_SAMPLE_SIZE:
        state = "directional"
        interpretation = "directional_only_no_strategy_change"
    else:
        state = "decision_ready"
        interpretation = "decision_grade_quote_calibration_not_actual_fills"
    return ExitLiquidityCalibrationReport(
        manifest={
            "contract": REPORT_CONTRACT,
            "generated_at": generated_at,
            "code_revision": normalize_code_revision(code_revision),
            "working_tree_dirty": working_tree_dirty,
            "since": filters.since,
            "until": filters.until,
            "input_fingerprint": _fingerprint(rows),
            "paper_quote_is_actual_fill": False,
            "delta_definition": "observed_close_quote_bps_minus_decision_time_modeled_bps",
            "interpretation": interpretation,
            "execution_cost_unreliable_threshold_bps": WIDE_SPREAD_UNRELIABLE_THRESHOLD_BPS,
            "execution_cost_unreliable_status": "discovery_diagnostic_not_forward_validated",
            "execution_cost_unreliable_provenance": (
                "threshold read from this report's own pre-existing close_spread bucket "
                "boundaries, not frozen before being read off a finding -- diagnostic flag "
                "only, never a production trading filter; see this module's own docstring"
            ),
        },
        readiness={
            "state": state,
            "closed_paper_shorts": len(rows),
            "observations": sum(row.observation_id is not None for row in rows),
            "comparable_observations": count,
            "capture_rate_pct": (sum(statuses.values()) - statuses["missing_observation"])
            / len(rows)
            * 100
            if rows
            else 0,
            "directional_minimum": DIRECTIONAL_SAMPLE_SIZE,
            "decision_minimum": DECISION_SAMPLE_SIZE,
            "remaining_to_directional": max(0, DIRECTIONAL_SAMPLE_SIZE - count),
            "remaining_to_decision": max(0, DECISION_SAMPLE_SIZE - count),
        },
        observation_statuses=dict(sorted(statuses.items())),
        exclusion_reasons=dict(sorted(exclusions.items())),
        metrics=_metrics(comparable_rows),
        segments=_segments(comparable_rows),
        comparable_exits=comparable_rows,
    )


def render_json(report: ExitLiquidityCalibrationReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True) + "\n"


def render_markdown(report: ExitLiquidityCalibrationReport) -> str:
    ready = report.readiness
    manifest = report.manifest
    lines = [
        "# Paper Exit Liquidity Calibration",
        "",
        f"Generated: {manifest['generated_at'].isoformat()}",
        f"Code revision: `{manifest['code_revision']}`",
        f"Working tree dirty: {'yes' if manifest['working_tree_dirty'] else 'no'}",
        f"Contract: `{manifest['contract']}`",
        f"Dataset: {manifest['since'].isoformat()} <= exit < {manifest['until'].isoformat()}",
        f"Input fingerprint: `{manifest['input_fingerprint']}`",
        "",
        "> The observed value is an executable paper quote at close time, not an actual fill.",
        "> Positive delta means the decision-time model underestimated close quote impact.",
        "",
        "## Readiness",
        "",
    ]
    lines.extend(
        markdown_table(
            ("State", "Closed shorts", "Captured", "Comparable", "Capture", "To 30", "To 100"),
            [
                (
                    ready["state"],
                    ready["closed_paper_shorts"],
                    ready["observations"],
                    ready["comparable_observations"],
                    format_percentage(ready["capture_rate_pct"]),
                    ready["remaining_to_directional"],
                    ready["remaining_to_decision"],
                )
            ],
        )
    )
    lines.extend(["", "## Observation coverage", ""])
    lines.extend(
        markdown_table(
            ("Status", "Rows"),
            [(status, count) for status, count in report.observation_statuses.items()],
        )
    )
    lines.extend(["", "## Exclusions from paired calibration", ""])
    lines.extend(
        markdown_table(
            ("Reason", "Rows"),
            [(reason, count) for reason, count in report.exclusion_reasons.items()],
        )
    )
    lines.extend(["", "## Paired calibration", ""])
    if report.metrics is None:
        lines.append("_No comparable observations yet._")
    else:
        row = report.metrics
        lines.extend(
            markdown_table(
                (
                    "N",
                    "Clusters",
                    "Largest cluster",
                    "Modeled mean",
                    "Observed mean",
                    "Delta mean",
                    "Delta median",
                    "Delta p90",
                    "Observed worse",
                ),
                [
                    (
                        row.observations,
                        row.asset_clusters,
                        format_percentage(row.largest_cluster_share_pct),
                        format_number(row.mean_modeled_exit_bps, suffix=" bps"),
                        format_number(row.mean_observed_exit_bps, suffix=" bps"),
                        format_number(row.mean_delta_bps, suffix=" bps"),
                        format_number(row.median_delta_bps, suffix=" bps"),
                        format_number(row.p90_delta_bps, suffix=" bps"),
                        format_percentage(row.observed_worse_pct),
                    )
                ],
            )
        )
        lines.extend(
            [
                "",
                f"Execution cost unreliable (observed close spread "
                f">= {WIDE_SPREAD_UNRELIABLE_THRESHOLD_BPS:g} bps -- diagnostic flag, "
                "never a trading rule; see this report's own module docstring): "
                f"{row.execution_cost_unreliable_count} of {row.observations} "
                f"({format_percentage(row.execution_cost_unreliable_pct)}).",
            ]
        )
    lines.extend(["", "## Segments", ""])
    lines.extend(
        markdown_table(
            (
                "Dimension",
                "Bucket",
                "N",
                "Clusters",
                "Modeled",
                "Observed",
                "Mean delta",
                "Median delta",
                "Observed worse",
            ),
            [
                (
                    row.dimension,
                    row.bucket,
                    row.observations,
                    row.asset_clusters,
                    format_number(row.mean_modeled_exit_bps, suffix=" bps"),
                    format_number(row.mean_observed_exit_bps, suffix=" bps"),
                    format_number(row.mean_delta_bps, suffix=" bps"),
                    format_number(row.median_delta_bps, suffix=" bps"),
                    format_percentage(row.observed_worse_pct),
                )
                for row in report.segments
            ],
        )
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Calibrate modeled paper exit impact against close-time executable quotes"
    )
    parser.add_argument("--since", type=parse_utc_datetime, default=EXIT_LIQUIDITY_COHORT_START)
    parser.add_argument("--until", type=parse_utc_datetime)
    parser.add_argument("--code-revision", default=os.getenv("SCHURFER_GIT_SHA"))
    parser.add_argument(
        "--working-tree-dirty",
        action=argparse.BooleanOptionalAction,
        required=True,
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser


async def _run(args: argparse.Namespace) -> str:
    from .exit_liquidity_calibration_repository import ExitLiquidityCalibrationRepository

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for exit-liquidity-calibration-report")
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    generated_at = datetime.now(UTC)
    filters = ExitLiquidityFilters(
        since=args.since,
        until=args.until or generated_at,
    )
    repository = ExitLiquidityCalibrationRepository.from_url(db_url)
    try:
        rows = await repository.load(filters)
    finally:
        await repository.close()
    report = build_exit_liquidity_calibration_report(
        rows,
        filters,
        generated_at=generated_at,
        code_revision=args.code_revision,
        working_tree_dirty=args.working_tree_dirty,
    )
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    args = build_parser().parse_args()
    sys.stdout.write(asyncio.run(_run(args)))
