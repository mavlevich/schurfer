"""Outcome-blind source-lead readiness report (HYP-012 forward cohort).

Prints the cohort-scoped funnel (captured -> qualified candidates -> timing-matured),
exclusion reasons, concentration, accumulation rate over the FIXED exposure window and
the projected weeks to the count floors, reading no outcome. It deliberately does NOT
print a "ready to read" verdict: timing maturity is necessary but not sufficient, and
the formal read is gated by `formal_verdict` over RESOLVED episodes with concentration
caps. Run against a database via DATABASE_URL:

    uv run --package schurfer-analytics source-lead-readiness-report --format markdown
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from .source_lead_forward_cohort import (
    EVIDENCE_FLOOR,
    MAX_SINGLE_ASSET_EPISODE_SHARE,
    MAX_SINGLE_WEEK_EPISODE_SHARE,
    OUTCOME_HORIZON_MINUTES,
    QUALIFICATION_VERSION,
    SOURCE_LEAD_FORWARD_COHORT_START,
)
from .source_lead_readiness import ReadinessReport, build_readiness
from .source_lead_readiness_repository import load_readiness_inputs


def _report_dict(
    report: ReadinessReport, *, code_revision: str, working_tree_dirty: bool
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "report": "source_lead_readiness_v2",
        "outcome_blind": True,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "code_revision": code_revision,
        "working_tree_dirty": working_tree_dirty,
        "qualification_version": QUALIFICATION_VERSION,
        "cohort_start_utc": SOURCE_LEAD_FORWARD_COHORT_START.isoformat(),
        "outcome_horizon_minutes": OUTCOME_HORIZON_MINUTES,
        "evidence_floor": EVIDENCE_FLOOR,
        "concentration_caps": {
            "max_single_asset_episode_share": MAX_SINGLE_ASSET_EPISODE_SHARE,
            "max_single_week_episode_share": MAX_SINGLE_WEEK_EPISODE_SHARE,
        },
        "readiness": asdict(report),
        "formal_read_note": (
            "Timing maturity is necessary but NOT sufficient. The formal read is "
            "formal_verdict over RESOLVED episodes (exit-bar outcomes) with the "
            "concentration caps, which this outcome-blind report never computes."
        ),
    }
    payload["fingerprint_sha256"] = hashlib.sha256(
        json.dumps(
            {k: v for k, v in payload.items() if k != "generated_at_utc"}, sort_keys=True
        ).encode()
    ).hexdigest()
    return payload


def render_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def _pct(value: float | None) -> str:
    return f"{value * 100:.1f}%" if value is not None else "n/a"


def render_markdown(report: dict[str, Any]) -> str:
    r: dict[str, Any] = report["readiness"]
    floor: dict[str, Any] = report["evidence_floor"]
    rate = r["qualified_per_week"]
    weeks_to = r["weeks_to_episode_floor"]
    lines = [
        "# source-lead readiness (outcome-blind, timing only)",
        "",
        f"Cohort `{report['qualification_version']}` from {report['cohort_start_utc']}. "
        "No outcome is read; this is NOT the formal read gate.",
        "",
        "## Funnel (cohort-scoped)",
        "",
        "| stage | count |",
        "| ----- | ----- |",
        f"| captured since cohort start | {r['captured_in_cohort']} |",
        f"| excluded at capture (expected) | {r['capture_excluded']} |",
        f"| still collecting | {r['capture_in_flight']} |",
        f"| abandoned by the capture process | {r['capture_abandoned']} |",
        f"| pipeline errors (complete or unknown, never qualified) | {r['pipeline_errors']} |",
        f"| reached qualification | {r['qualification_rows']} |",
        f"| excluded at qualification | {sum(r['excluded_by_reason'].values())} |",
        f"| qualified | {r['qualified_rows']} |",
        f"| qualified without a formal episode | "
        f"{sum(r['qualified_without_episode_by_status'].values())} |",
        f"| formal candidates | {r['candidates']} |",
        f"| timing-matured | {r['matured']} |",
        "",
        *(
            [f"> WARNING: {r['pipeline_errors']} captures never reached qualification.", ""]
            if r["pipeline_errors"]
            else []
        ),
        *(
            ["> WARNING: qualified rows do not reconcile with formal candidates.", ""]
            if not r["lower_funnel_reconciles"]
            else []
        ),
        "## Stopped before qualification (capture status:reason)",
        "",
        "| status:reason | count |",
        "| ------------- | ----- |",
        *[f"| {reason} | {count} |" for reason, count in r["pre_qualification_by_reason"].items()],
        "",
        "## Qualified without a formal episode (selected target status)",
        "",
        "| status | count |",
        "| ------ | ----- |",
        *[
            f"| {status} | {count} |"
            for status, count in r["qualified_without_episode_by_status"].items()
        ],
        "",
        "## Excluded at qualification",
        "",
        "| reason | count |",
        "| ------ | ----- |",
        *[f"| {reason} | {count} |" for reason, count in r["excluded_by_reason"].items()],
        "",
        "## Progress vs count floor (timing only)",
        "",
        f"- timing-matured: {r['matured']} / {floor['min_resolved_episodes']}"
        f" ({'met' if r['meets_episode_floor'] else 'below'})",
        f"- clusters: {r['distinct_clusters']} / {floor['min_distinct_asset_clusters']}"
        f" ({'met' if r['meets_cluster_floor'] else 'below'})",
        f"- weeks: {r['distinct_weeks']} / {floor['min_distinct_utc_weeks']}"
        f" ({'met' if r['meets_week_floor'] else 'below'})",
        f"- timing count-floors met: {'yes' if r['timing_floors_met'] else 'no'}"
        " (still NOT the formal read gate)",
        "",
        "## Concentration and accrual",
        "",
        f"- largest single-asset share: {_pct(r['largest_asset_share'])}"
        f" (cap {_pct(report['concentration_caps']['max_single_asset_episode_share'])})",
        f"- largest single-week share: {_pct(r['largest_week_share'])}"
        f" (cap {_pct(report['concentration_caps']['max_single_week_episode_share'])})",
        f"- concentration within caps: {'yes' if r['concentration_ok'] else 'no'}",
        f"- exposure window: {r['exposure_weeks']:.1f} weeks",
        f"- accrual: {rate:.1f} candidates/week" if rate is not None else "- accrual: n/a",
        (
            f"- projected weeks to episode count floor: {weeks_to:.1f}"
            if weeks_to is not None
            else "- projected weeks to episode count floor: n/a (rate 0 or floor met)"
        ),
        "",
        "## Qualified by UTC week",
        "",
        "| week | qualified |",
        "| ---- | --------- |",
        *[f"| {week} | {count} |" for week, count in r["qualified_by_week"].items()],
        "",
        *_capacity_lines(r["capacity"]),
        "## Target checks by venue (qualification details)",
        "",
        "| venue:reason | count |",
        "| ------------ | ----- |",
        *[f"| {key} | {count} |" for key, count in r["target_reasons_by_venue"].items()],
        "",
        "## Exit-book diagnostic coverage (statuses and delays only)",
        "",
        f"- exit window closed: {r['exit_due']} episodes; no exit row at all: {r['exit_missing']}",
        "",
        *(
            [f"> WARNING: {r['exit_missing']} due episodes have no exit row.", ""]
            if r["exit_missing"]
            else []
        ),
        "| outcome:timeliness | count |",
        "| ------------------ | ----- |",
        *[f"| {key} | {count} |" for key, count in r["exit_coverage"].items()],
        "",
        f"- lateness vs target: p50 {r['exit_lateness_p50_ms']} ms,"
        f" p90 {r['exit_lateness_p90_ms']} ms",
        "",
        f"> {report['formal_read_note']}",
    ]
    return "\n".join(lines) + "\n"


def _capacity_lines(capacity: dict[str, Any] | None) -> list[str]:
    if not capacity:
        return []
    return [
        f"## Capacity at {capacity['slots']} slots (USD 300 / USD 50, entry times only)",
        "",
        f"- peak demand (overlapping holds, all signals): {capacity['max_concurrent']}",
        f"- taken: {capacity['taken']}, skipped for lack of a free slot: {capacity['skipped']}"
        f" ({_pct(capacity['skipped_share'])})",
        "",
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--code-revision", default="unknown")
    parser.add_argument("--working-tree-dirty", dest="dirty", action="store_true")
    parser.add_argument("--no-working-tree-dirty", dest="dirty", action="store_false")
    parser.set_defaults(dirty=True)
    return parser


async def _run(args: argparse.Namespace) -> str:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for source-lead-readiness-report")
    inputs = await load_readiness_inputs(db_url)
    report = build_readiness(inputs)
    payload = _report_dict(report, code_revision=args.code_revision, working_tree_dirty=args.dirty)
    return render_json(payload) if args.format == "json" else render_markdown(payload)


def main() -> None:
    args = build_parser().parse_args()
    sys.stdout.write(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
