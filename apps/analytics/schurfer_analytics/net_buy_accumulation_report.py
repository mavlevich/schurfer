"""Read-only report for the net-buy accumulation discovery.

Runs the DuckDB scanner over the frozen cold-bar Parquet, applies the frozen
post-fire verdict, and renders the two-primary result (magnitude and elevated-buy
breadth) with the required metrics, provenance and the SHA hashes of the cold-bar
manifests it read. Discovery only: a positive result is a candidate, never a
proven edge, and economics are `adj_return` (fees+funding, slippage unknown).

It never writes, never trades, and refuses a window past the held-out boundary.
The heavy work is SQL-side in the repository; this module orchestrates and
renders.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .net_buy_accumulation import (
    CONTRACT_VERSION,
    PRIMARIES,
    PrimaryResult,
    cost_pct_at_horizon,
    evaluate_joint,
)
from .net_buy_accumulation_repository import scan_fires

# Frozen discovery window (contract, final for this read).
DISCOVERY_START = datetime(2026, 8, 18, tzinfo=UTC)
DISCOVERY_END = datetime(2026, 9, 10, 20, 0, tzinfo=UTC)
HELD_OUT_START = datetime(2026, 9, 10, 20, 0, tzinfo=UTC)


class HeldOutWindowError(ValueError):
    """Raised when a window past the frozen held-out boundary is requested."""


@dataclass(frozen=True)
class Report:
    contract_version: str
    generated_at: str
    code_revision: str
    working_tree_dirty: bool
    formal_run: bool
    cohort_start: str
    cohort_end: str
    cost_deduction_pp: float
    manifest_hashes: dict[str, str]
    results: dict[str, PrimaryResult]


def _manifest_hashes(cold_bars_dir: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for manifest in sorted(cold_bars_dir.glob("bars-*.manifest.json")):
        try:
            data = json.loads(manifest.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        sha = data.get("sha256") or data.get("payload_hash") or ""
        hashes[manifest.name] = str(sha)
    return hashes


def generate_report(
    *,
    cold_bars_dir: str,
    cohort_start: datetime,
    cohort_end: datetime,
    code_revision: str,
    working_tree_dirty: bool,
) -> Report:
    if cohort_end > HELD_OUT_START:
        raise HeldOutWindowError(
            f"cohort_end={cohort_end.isoformat()} is past the held-out boundary "
            f"{HELD_OUT_START.isoformat()}; this discovery pass may not read it."
        )
    directory = Path(cold_bars_dir)
    glob = str(directory / "bars-*.parquet")
    episodes = scan_fires(parquet_glob=glob, cohort_start=cohort_start, cohort_end=cohort_end)
    by_primary = {p: [e for e in episodes if e.primary == p] for p in PRIMARIES}
    results = evaluate_joint(by_primary)
    return Report(
        contract_version=CONTRACT_VERSION,
        generated_at=datetime.now(UTC).isoformat(),
        code_revision=code_revision,
        working_tree_dirty=working_tree_dirty,
        formal_run=(
            not working_tree_dirty
            and cohort_start == DISCOVERY_START
            and cohort_end == DISCOVERY_END
        ),
        cohort_start=cohort_start.isoformat(),
        cohort_end=cohort_end.isoformat(),
        cost_deduction_pp=cost_pct_at_horizon(),
        manifest_hashes=_manifest_hashes(directory),
        results=results,
    )


def _fmt(value: float | None, digits: int = 3) -> str:
    return "--" if value is None else f"{value:.{digits}f}"


def render_markdown(report: Report) -> str:
    lines = [
        f"# net-buy accumulation discovery -- {report.contract_version}",
        "",
        f"Generated: {report.generated_at}",
        (
            f"Provenance: code_revision={report.code_revision} "
            f"working_tree_dirty={report.working_tree_dirty} formal_run={report.formal_run}"
        ),
        f"Window: [{report.cohort_start}, {report.cohort_end}); "
        f"cost deduction (fees+funding): {report.cost_deduction_pp:.4f} pp; slippage UNKNOWN.",
        "",
        "Discovery only. A positive result is a candidate for a fresh prospective "
        "cohort, never a proven edge; economics are adj_return (slippage unknown).",
        "",
        "## Verdict (per primary)",
        "",
        "| Primary | Verdict | Fires | Resolved | Mean adj (pp) | boot p | LB | Clusters |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for primary in PRIMARIES:
        r = report.results[primary]
        boot = r.bootstrap
        lines.append(
            f"| {primary} | {r.verdict_prebonf} | {r.fires} | {r.resolved_fires} | "
            f"{_fmt(r.mean_adj_return_pct)} | {_fmt(boot.p_one_sided if boot else None)} | "
            f"{_fmt(boot.lower_bound if boot else None)} | {r.distinct_clusters} |"
        )
    lines += ["", "## Required metrics (per primary)", ""]
    for primary in PRIMARIES:
        r = report.results[primary]
        lines += [
            f"### {primary}",
            "",
            f"- fires / resolved: {r.fires} / {r.resolved_fires}",
            f"- mean / spread(top-bottom) adj_return: {_fmt(r.mean_adj_return_pct)} / "
            f"{_fmt(r.top_minus_bottom_spread_pp)} pp (spread is diagnostic)",
            f"- monotone increasing: {r.monotone_increasing}; "
            f"Rule 6 distinct/largest-tie: {r.ties.distinct_values}/{r.ties.largest_tied_group}, "
            f"adjacent-distinct: {r.ties.adjacent_boundaries_distinct}",
            f"- diversity: min fires/quantile {r.min_fires_per_quantile}, clusters "
            f"{r.distinct_clusters}, weekly-min {r.weekly_min_fires}",
            f"- fires per fully-covered week: {_fmt(r.fires_per_fully_covered_week, 1)}; "
            f"tradable-liquidity share: {_fmt(r.tradable_share, 2)}",
            "",
            "| Quantile | Episodes | Clusters | Median adj_return (pp) |",
            "| --- | --- | --- | --- |",
        ]
        lines += [
            f"| {q.index} | {q.episodes} | {q.clusters} | {_fmt(q.median_adj_return_pct)} |"
            for q in r.quantiles
        ]
        lines.append("")
    lines += [
        "## Cold-bar manifests read (SHA)",
        "",
        *[f"- {name}: {sha}" for name, sha in report.manifest_hashes.items()],
        "",
    ]
    return "\n".join(lines)


def render_json(report: Report) -> str:
    def _q(r: PrimaryResult) -> list[dict[str, object]]:
        return [
            {
                "index": q.index,
                "episodes": q.episodes,
                "clusters": q.clusters,
                "median_adj_return_pct": q.median_adj_return_pct,
            }
            for q in r.quantiles
        ]

    payload = {
        "contract_version": report.contract_version,
        "generated_at": report.generated_at,
        "code_revision": report.code_revision,
        "working_tree_dirty": report.working_tree_dirty,
        "formal_run": report.formal_run,
        "cohort_start": report.cohort_start,
        "cohort_end": report.cohort_end,
        "cost_deduction_pp": report.cost_deduction_pp,
        "manifest_hashes": report.manifest_hashes,
        "results": {
            p: {
                "verdict": r.verdict_prebonf,
                "fires": r.fires,
                "resolved_fires": r.resolved_fires,
                "mean_adj_return_pct": r.mean_adj_return_pct,
                "top_minus_bottom_spread_pp": r.top_minus_bottom_spread_pp,
                "monotone_increasing": r.monotone_increasing,
                "distinct_clusters": r.distinct_clusters,
                "min_fires_per_quantile": r.min_fires_per_quantile,
                "weekly_min_fires": r.weekly_min_fires,
                "fires_per_fully_covered_week": r.fires_per_fully_covered_week,
                "tradable_share": r.tradable_share,
                "bootstrap": (
                    None
                    if r.bootstrap is None
                    else {
                        "mean": r.bootstrap.mean,
                        "lower_bound": r.bootstrap.lower_bound,
                        "p_one_sided": r.bootstrap.p_one_sided,
                    }
                ),
                "quantiles": _q(r),
            }
            for p, r in report.results.items()
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


def _parse(value: str) -> datetime:
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cold-bars", required=True, help="dir with bars-*.parquet + manifests")
    parser.add_argument("--cohort-start", default=DISCOVERY_START.isoformat())
    parser.add_argument("--cohort-end", default=DISCOVERY_END.isoformat())
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--code-revision", default="unknown")
    parser.add_argument("--working-tree-dirty", action="store_true")
    parser.add_argument("--no-working-tree-dirty", dest="working_tree_dirty", action="store_false")
    parser.set_defaults(working_tree_dirty=True)
    args = parser.parse_args()

    report = generate_report(
        cold_bars_dir=args.cold_bars,
        cohort_start=_parse(args.cohort_start),
        cohort_end=_parse(args.cohort_end),
        code_revision=args.code_revision,
        working_tree_dirty=args.working_tree_dirty,
    )
    out = render_json(report) if args.format == "json" else render_markdown(report)
    sys.stdout.write(out + "\n")


__all__ = ["HeldOutWindowError", "generate_report", "main", "render_json", "render_markdown"]
