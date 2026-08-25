"""CLI/report wrapper for the fixed early_momentum flow-feature discovery."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from .early_momentum_net_evidence import COHORT_MATURITY_BUFFER_SECONDS
from .early_momentum_unused_flow_features import (
    DISCOVERY_END,
    DISCOVERY_START,
    FlowFeatureAnalysis,
    analyze,
)
from .early_momentum_unused_flow_features_repository import (
    EarlyMomentumUnusedFlowFeaturesRepository,
)
from .reporting import json_ready, markdown_table, normalize_code_revision


class DiscoveryWindowNotMatureError(ValueError):
    pass


@dataclass(frozen=True)
class FlowFeatureReport:
    generated_at: datetime
    code_revision: str
    working_tree_dirty: bool
    formal_run: bool
    analysis: FlowFeatureAnalysis


async def generate_report(
    *, db_url: str, code_revision: str, working_tree_dirty: bool
) -> FlowFeatureReport:
    repository = EarlyMomentumUnusedFlowFeaturesRepository.from_url(db_url)
    try:
        db_now, rows = await repository.fetch(
            cohort_start=DISCOVERY_START,
            cohort_end=DISCOVERY_END,
        )
    finally:
        await repository.close()
    maturity_deadline = DISCOVERY_END + timedelta(seconds=COHORT_MATURITY_BUFFER_SECONDS)
    if db_now < maturity_deadline:
        raise DiscoveryWindowNotMatureError(
            f"discovery window is not mature: database clock {db_now.isoformat()} is before "
            f"{maturity_deadline.isoformat()}"
        )
    return FlowFeatureReport(
        generated_at=db_now,
        code_revision=normalize_code_revision(code_revision),
        working_tree_dirty=working_tree_dirty,
        formal_run=not working_tree_dirty,
        analysis=analyze(rows),
    )


def render_json(report: FlowFeatureReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True)


def _value(value: float | None, digits: int = 4) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def render_markdown(report: FlowFeatureReport) -> str:
    analysis = report.analysis
    candidate = analysis.candidate
    lines = [
        f"# early_momentum unused flow features — {analysis.verdict}",
        "",
        f"Generated: {report.generated_at.isoformat()}",
        f"Provenance: code_revision={report.code_revision} "
        f"working_tree_dirty={report.working_tree_dirty} formal_run={report.formal_run}",
        "",
        "> Discovery only. This report cannot authorize PAPER contract changes or any live "
        "order. The frozen candidate requires a newly registered prospective cohort.",
        "",
        "## Dataset contract and coverage",
        "",
    ]
    lines.extend(
        markdown_table(
            ("Field", "Value"),
            [
                ("report_version", analysis.report_version),
                ("dataset_version", analysis.dataset_version),
                ("discovery_start", analysis.discovery_start.isoformat()),
                ("discovery_end", analysis.discovery_end.isoformat()),
                ("raw comparable trades", str(analysis.raw_rows)),
                ("feature-complete trades", str(analysis.comparable_feature_rows)),
                ("dataset_fingerprint", analysis.dataset_fingerprint),
                ("verdict reasons", ", ".join(analysis.verdict_reasons)),
            ],
        )
    )
    lines += ["", "### Exclusions", ""]
    if analysis.exclusions:
        lines.extend(
            markdown_table(
                ("Reason", "Rows"),
                [(reason, str(count)) for reason, count in analysis.exclusions.items()],
            )
        )
    else:
        lines.append("_No feature rows excluded._")
    lines += [
        "",
        "## Continuous diagnostics",
        "",
    ]
    lines.extend(
        markdown_table(
            ("Feature", "Correlation with net return"),
            [(feature, _value(value)) for feature, value in analysis.correlations.items()],
        )
    )
    lines += [
        "",
        "Correlation is descriptive, not a monotonicity assumption. Quartiles below expose "
        "the observed non-linear shape instead of hiding it behind one coefficient.",
        "",
        "## Feature quartiles",
        "",
    ]
    lines.extend(
        markdown_table(
            (
                "Feature",
                "Q",
                "N",
                "Range",
                "Mean net",
                "Median net",
                "Total net USD",
                "PF",
            ),
            [
                (
                    row.feature,
                    str(row.quartile),
                    str(row.trades),
                    f"[{row.minimum:.4f}, {row.maximum:.4f}]",
                    f"{row.mean_net_return_pct:.3f}%",
                    f"{row.median_net_return_pct:.3f}%",
                    f"{row.total_net_pnl_usd:.2f}",
                    _value(row.profit_factor, 3),
                )
                for row in analysis.quartiles
            ],
        )
    )
    lines += ["", "## Frozen discovery candidate", ""]
    lines.extend(
        markdown_table(
            ("Field", "Value"),
            [
                ("candidate_version", candidate.candidate_version),
                (
                    "selection",
                    f"{candidate.imbalance_min_inclusive:.2f} <= imbalance_15m < "
                    f"{candidate.imbalance_max_exclusive:.2f}",
                ),
                (
                    "baseline / selected",
                    f"{candidate.baseline_trades} / {candidate.selected_trades}",
                ),
                ("rejected to cash", str(candidate.rejected_to_cash)),
                ("selected mean net", f"{_value(candidate.selected_mean_net_return_pct, 3)}%"),
                ("selected median net", f"{_value(candidate.selected_median_net_return_pct, 3)}%"),
                ("selected total net USD", f"{candidate.selected_total_net_pnl_usd:.2f}"),
                ("selected PF", _value(candidate.selected_profit_factor, 3)),
                ("selected clusters", str(candidate.selected_clusters)),
                ("selected UTC weeks", str(candidate.selected_utc_weeks)),
            ],
        )
    )
    lines += [
        "",
        "The candidate is a filter-to-cash challenger over baseline v4 entries. It does not "
        "invent a different entry price or reuse post-outcome liquidity. Validation must "
        "start from a new database-clock registration created after this candidate is merged.",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discovery-only unused flow-feature read for early_momentum_v4"
    )
    parser.add_argument("--code-revision", required=True)
    dirty = parser.add_mutually_exclusive_group(required=True)
    dirty.add_argument("--no-working-tree-dirty", action="store_true")
    dirty.add_argument("--working-tree-dirty", action="store_true")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser


async def _run(args: argparse.Namespace) -> str:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required")
    report = await generate_report(
        db_url=db_url,
        code_revision=args.code_revision,
        working_tree_dirty=bool(args.working_tree_dirty),
    )
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    args = build_parser().parse_args()
    sys.stdout.write(asyncio.run(_run(args)))


__all__ = [
    "DiscoveryWindowNotMatureError",
    "FlowFeatureReport",
    "generate_report",
    "render_json",
    "render_markdown",
]
