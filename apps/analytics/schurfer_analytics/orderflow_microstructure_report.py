"""research/orderflow-microstructure-v1 (HYP-024) -- CLI, orchestration, and
Markdown/JSON rendering for the single registered taker-imbalance statistic.

Read-only. Enforces the frozen held-out cutoff (2026-08-25): a --cohort-end at
or past it is refused so this discovery pass can never read the held-out
window. `--code-revision` and `--working-tree-dirty`/`--no-working-tree-dirty`
are computed by the Makefile (the analytics container carries no `.git`) and
passed in, exactly like early-momentum-net-evidence-report. A dirty tree never
changes the verdict, but the run is not a `formal_run` and never authorizes
reading the held-out window on its own.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .orderflow_microstructure import (
    CONTEXT_LOOKBACK_MINUTES,
    COST_MODEL_VERSION,
    DISCOVERY_START,
    HELD_OUT_START,
    HORIZON_MINUTES,
    HYPOTHESIS_ID,
    PRIMARY_LOOKBACK_MINUTES,
    REPORT_VERSION,
    RESOLVER_VERSION,
    STRATEGY_VERSION,
    CoverageFunnelStep,
    ExchangeCoverage,
    QuintileAnalysis,
    Verdict,
    build_coverage,
    compared_distinct_clusters,
    compute_quintiles,
    cost_pct_at_horizon,
    dataset_fingerprint,
    evaluate_verdict,
)
from .orderflow_microstructure_repository import (
    MOMENTUM_CAPTURE_VERSION,
    HeldOutWindowError,
    OrderflowMicrostructureRepository,
)
from .reporting import (
    format_number as _num,
)
from .reporting import (
    format_percentage as _pct,
)
from .reporting import (
    json_ready as _json_ready,
)
from .reporting import (
    markdown_table as _table,
)
from .reporting import (
    normalize_code_revision,
    parse_utc_datetime,
)

if TYPE_CHECKING:
    from .orderflow_microstructure import ResolvedDecisionRow


@dataclass(frozen=True)
class OrderflowReport:
    report_version: str
    hypothesis_id: str
    generated_at: datetime
    code_revision: str
    working_tree_dirty: bool
    formal_run: bool
    db_snapshot_at: datetime
    cohort_start: datetime
    cohort_end: datetime
    held_out_start: datetime
    strategy_version: str
    resolver_version: str
    horizon_minutes: int
    capture_version: str
    cost_model_version: str
    cost_deduction_pp: float
    dataset_fingerprint: str
    total_cohort_decisions: int
    measured_episodes: int
    coverage_funnel: tuple[CoverageFunnelStep, ...]
    coverage_by_exchange: tuple[ExchangeCoverage, ...]
    primary: QuintileAnalysis
    context: tuple[QuintileAnalysis, ...]
    verdict: Verdict


async def generate_report(
    *,
    db_url: str,
    cohort_end: datetime,
    code_revision: str,
    working_tree_dirty: bool,
    cohort_start: datetime = DISCOVERY_START,
) -> OrderflowReport:
    _guard_window(cohort_start=cohort_start, cohort_end=cohort_end)
    repository = OrderflowMicrostructureRepository.from_url(db_url)
    try:
        db_now, rows = await repository.fetch(cohort_start=cohort_start, cohort_end=cohort_end)
    finally:
        await repository.close()
    return build_report(
        rows=rows,
        db_snapshot_at=db_now,
        cohort_start=cohort_start,
        cohort_end=cohort_end,
        code_revision=code_revision,
        working_tree_dirty=working_tree_dirty,
    )


def _guard_window(*, cohort_start: datetime, cohort_end: datetime) -> None:
    if cohort_end > HELD_OUT_START:
        raise HeldOutWindowError(
            f"--cohort-end={cohort_end.isoformat()} is past the held-out boundary "
            f"{HELD_OUT_START.isoformat()}; this discovery pass may not read the held-out "
            "window. Pass an earlier --cohort-end."
        )
    if cohort_start < DISCOVERY_START:
        raise ValueError(
            f"cohort_start={cohort_start.isoformat()} predates the frozen discovery start "
            f"{DISCOVERY_START.isoformat()} (the oldest surviving bar)."
        )
    if cohort_end <= cohort_start:
        raise ValueError("cohort_end must be after cohort_start")


def build_report(
    *,
    rows: tuple[ResolvedDecisionRow, ...],
    db_snapshot_at: datetime,
    cohort_start: datetime,
    cohort_end: datetime,
    code_revision: str,
    working_tree_dirty: bool,
) -> OrderflowReport:
    """Pure build from already-fetched rows, so the whole verdict pipeline can
    be exercised in tests against hand-built rows without a database."""
    coverage = build_coverage(rows)
    measured = coverage.measured
    primary = compute_quintiles(measured, lookback_minutes=PRIMARY_LOOKBACK_MINUTES)
    context = tuple(
        compute_quintiles(measured, lookback_minutes=minutes)
        for minutes in CONTEXT_LOOKBACK_MINUTES
    )
    compared_clusters = compared_distinct_clusters(measured, primary)
    verdict = evaluate_verdict(primary, compared_clusters=compared_clusters)

    return OrderflowReport(
        report_version=REPORT_VERSION,
        hypothesis_id=HYPOTHESIS_ID,
        generated_at=datetime.now(UTC),
        code_revision=normalize_code_revision(code_revision),
        working_tree_dirty=working_tree_dirty,
        formal_run=not working_tree_dirty,
        db_snapshot_at=db_snapshot_at,
        cohort_start=cohort_start,
        cohort_end=cohort_end,
        held_out_start=HELD_OUT_START,
        strategy_version=STRATEGY_VERSION,
        resolver_version=RESOLVER_VERSION,
        horizon_minutes=HORIZON_MINUTES,
        capture_version=MOMENTUM_CAPTURE_VERSION,
        cost_model_version=COST_MODEL_VERSION,
        cost_deduction_pp=cost_pct_at_horizon(),
        dataset_fingerprint=dataset_fingerprint(measured),
        total_cohort_decisions=len(rows),
        measured_episodes=len(measured),
        coverage_funnel=coverage.funnel,
        coverage_by_exchange=coverage.by_exchange,
        primary=primary,
        context=context,
        verdict=verdict,
    )


# --- Rendering ----------------------------------------------------------


def render_json(report: OrderflowReport) -> str:
    return json.dumps(_json_ready(asdict(report)), indent=2, sort_keys=True, default=str)


def _quintile_rows(analysis: QuintileAnalysis) -> list[tuple[Any, ...]]:
    return [
        (
            q.index,
            q.episodes,
            q.distinct_clusters,
            f"{_num(q.feature_min, 4)} .. {_num(q.feature_max, 4)}",
            _pct(q.median_net_return_pct),
            _pct(q.median_gross_return_pct),
            _pct(q.median_mfe_pct),
            _pct(q.median_mae_pct),
        )
        for q in analysis.quintiles
    ]


def _quintile_section(title: str, analysis: QuintileAnalysis, *, registered: bool) -> list[str]:
    lines = [f"### {title}", ""]
    if not registered:
        lines += [
            "_Context only. This lookback is reported to show whether any relationship is a "
            "knife edge; it does NOT replace the registered ten-minute measure._",
            "",
        ]
    lines += _table(
        (
            "Quintile",
            "Episodes",
            "Clusters",
            "Feature range",
            "Median net",
            "Median gross",
            "Median MFE",
            "Median MAE",
        ),
        _quintile_rows(analysis),
    )
    spread = analysis.top_minus_bottom_median_spread_pp
    lines += [""]
    lines += _table(
        ("Metric", "Value"),
        [
            ("Top-minus-bottom median net spread", _pct(spread)),
            ("Monotone increasing", analysis.monotone_increasing),
            ("Monotone decreasing", analysis.monotone_decreasing),
            ("Distinct feature values (Rule 6)", analysis.ties.distinct_value_count),
            ("Largest tied group (Rule 6)", analysis.ties.largest_tied_group),
            ("Adjacent boundaries distinct (Rule 6)", analysis.ties.adjacent_boundaries_distinct),
            (
                "Tied boundary pairs (Rule 6)",
                ", ".join(str(pair) for pair in analysis.ties.tied_boundary_pairs) or "—",
            ),
        ],
    )
    return lines


def render_markdown(report: OrderflowReport) -> str:
    v = report.verdict
    lines = [
        f"# {report.hypothesis_id} order-flow microstructure -- {v.verdict}",
        "",
        f"Generated: {report.generated_at.isoformat()}",
        (
            f"Provenance: code_revision={report.code_revision} "
            f"working_tree_dirty={report.working_tree_dirty} formal_run={report.formal_run}"
        ),
        "",
    ]
    if not report.formal_run:
        lines += [
            "> **NOT A FORMAL RUN** -- working tree was dirty. The verdict below is "
            "provisional/local-only. A `candidate` here never authorizes reading the "
            "held-out window on its own; re-run against a clean, committed revision first.",
            "",
        ]

    lines += ["## Contract and reproducibility fingerprint", ""]
    lines += _table(
        ("Field", "Value"),
        [
            ("report_version", report.report_version),
            ("strategy_version", report.strategy_version),
            (
                "resolver_version / horizon",
                f"{report.resolver_version} / {report.horizon_minutes}m",
            ),
            ("capture_version", report.capture_version),
            ("cost_model_version", report.cost_model_version),
            ("cost deduction (net = gross - this)", _pct(report.cost_deduction_pp)),
            ("cohort_start (frozen)", report.cohort_start.isoformat()),
            ("cohort_end", report.cohort_end.isoformat()),
            ("held_out_start (never read here)", report.held_out_start.isoformat()),
            ("db_snapshot_at", report.db_snapshot_at.isoformat()),
            ("dataset_fingerprint", report.dataset_fingerprint),
        ],
    )

    lines += ["", "## Verdict", ""]
    lines += _table(
        ("Field", "Value"),
        [
            ("verdict", v.verdict),
            ("registered measure", f"{PRIMARY_LOOKBACK_MINUTES}-minute taker imbalance"),
            ("top-minus-bottom median net spread", _pct(v.spread_pp)),
            (
                "compared quintile episodes (top / bottom)",
                f"{v.top_quintile_episodes} / {v.bottom_quintile_episodes}",
            ),
            ("compared asset clusters (union)", v.compared_distinct_clusters),
            ("meets episode floor (>=150 each)", v.meets_episode_floor),
            ("meets cluster floor (>=30 union)", v.meets_cluster_floor),
            ("reasons", "; ".join(v.reasons) or "—"),
        ],
    )

    lines += ["", "## Coverage funnel (decisions -> measured episodes)", ""]
    lines += _table(
        ("Step", "Label", "Remaining", "Excluded", "Reason"),
        [
            (s.step, s.label, s.remaining, s.excluded, s.exclusion_reason or "—")
            for s in report.coverage_funnel
        ],
    )

    lines += [
        "",
        "### Coverage per exchange (venues without bars are coverage loss, not negatives)",
        "",
    ]
    lines += _table(
        (
            "Exchange",
            "Cohort decisions",
            "Identity resolved",
            "Measured",
            "Unresolved identity",
            "Ambiguous identity",
            "Missing/incomplete bars",
        ),
        [
            (
                c.exchange,
                c.resolved_cohort_decisions,
                c.identity_resolved,
                c.measured_episodes,
                c.unresolved_identity,
                c.ambiguous_identity,
                c.missing_or_incomplete_bars,
            )
            for c in report.coverage_by_exchange
        ],
    )

    lines += [
        "",
        "## Primary metric -- registered ten-minute taker imbalance",
        "",
        f"Total cohort decisions with a resolved 60m outcome: {report.total_cohort_decisions}. "
        f"Measured episodes (identity resolved + complete ten-bar pre-window): "
        f"{report.measured_episodes}.",
        "",
    ]
    lines += _quintile_section(
        f"{PRIMARY_LOOKBACK_MINUTES}-minute quintiles (REGISTERED)",
        report.primary,
        registered=True,
    )

    lines += ["", "## Secondary context (never replaces the registered measure)", ""]
    for analysis in report.context:
        lines += _quintile_section(
            f"{analysis.lookback_minutes}-minute quintiles",
            analysis,
            registered=False,
        )
        lines += [""]

    return "\n".join(lines) + "\n"


# --- CLI ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only HYP-024 order-flow microstructure report. cohort_start is frozen "
            "(2026-08-10); --cohort-end defaults to and may not exceed the held-out "
            "boundary 2026-08-25."
        )
    )
    parser.add_argument(
        "--cohort-end",
        type=parse_utc_datetime,
        default=HELD_OUT_START,
        help="exclusive UTC ISO-8601 cohort end; refused if past 2026-08-25 (held out)",
    )
    parser.add_argument("--code-revision", type=str, required=True)
    dirty = parser.add_mutually_exclusive_group(required=True)
    dirty.add_argument("--no-working-tree-dirty", action="store_true")
    dirty.add_argument("--working-tree-dirty", action="store_true")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser


async def _run(args: argparse.Namespace) -> str:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for hyp-024-orderflow-report")
    report = await generate_report(
        db_url=db_url,
        cohort_end=args.cohort_end,
        code_revision=args.code_revision,
        working_tree_dirty=bool(args.working_tree_dirty),
    )
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    args = build_parser().parse_args()
    sys.stdout.write(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
