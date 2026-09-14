"""Outcome-blind source-lead readiness report (HYP-012 forward cohort).

Prints the captured -> qualified -> matured funnel, exclusion reasons, accumulation
rate and the projected weeks to the registered evidence floor, reading no outcome. Run
against a database via DATABASE_URL:

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

from .source_lead_forward_cohort import EVIDENCE_FLOOR, OUTCOME_HORIZON_MINUTES
from .source_lead_readiness import ReadinessFunnel, ReadinessSummary, summarize_readiness
from .source_lead_readiness_repository import (
    DEFAULT_QUALIFICATION_VERSION,
    SourceLeadReadinessRepository,
)


def _report_dict(
    funnel: ReadinessFunnel,
    summary: ReadinessSummary,
    *,
    qualification_version: str,
    code_revision: str,
    working_tree_dirty: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "report": "source_lead_readiness_v1",
        "outcome_blind": True,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "code_revision": code_revision,
        "working_tree_dirty": working_tree_dirty,
        "qualification_version": qualification_version,
        "outcome_horizon_minutes": OUTCOME_HORIZON_MINUTES,
        "evidence_floor": EVIDENCE_FLOOR,
        "funnel": asdict(funnel),
        "summary": asdict(summary),
    }
    payload["fingerprint_sha256"] = hashlib.sha256(
        json.dumps(
            {k: v for k, v in payload.items() if k != "generated_at_utc"}, sort_keys=True
        ).encode()
    ).hexdigest()
    return payload


def render_json(report: dict[str, object]) -> str:
    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def _pct(n: int, d: int) -> str:
    return f"{(100.0 * n / d):.1f}%" if d else "n/a"


def render_markdown(report: dict[str, object]) -> str:
    f: dict[str, Any] = report["funnel"]  # type: ignore[assignment]
    s: dict[str, Any] = report["summary"]  # type: ignore[assignment]
    floor: dict[str, Any] = report["evidence_floor"]  # type: ignore[assignment]
    rate = s["qualified_per_week"]
    weeks_to = s["weeks_to_episode_floor"]
    lines = [
        "# source-lead readiness (outcome-blind)",
        "",
        f"Qualification version `{report['qualification_version']}`. No outcome is read.",
        "",
        "## Funnel",
        "",
        "| stage | count |",
        "| ----- | ----- |",
        f"| captured (all) | {f['captured']} |",
        f"| qualification attempts | {f['qualification_attempts']} |",
        f"| qualified | {f['qualified']} ({_pct(f['qualified'], f['qualification_attempts'])}) |",
        f"| excluded | {f['excluded']} |",
        f"| matured (horizon elapsed) | {f['matured']} |",
        "",
        "## Exclusion reasons",
        "",
        "| reason | count |",
        "| ------ | ----- |",
        *[f"| {reason} | {count} |" for reason, count in f["excluded_by_reason"].items()],
        "",
        "## Readiness vs evidence floor",
        "",
        f"- qualified/week: {rate:.1f}" if rate is not None else "- qualified/week: n/a",
        f"- clusters: {f['qualified_clusters']} / {floor['min_distinct_asset_clusters']}"
        f" ({'met' if s['meets_cluster_floor'] else 'below'})",
        f"- weeks: {f['qualified_weeks']} / {floor['min_distinct_utc_weeks']}"
        f" ({'met' if s['meets_week_floor'] else 'below'})",
        f"- matured episodes: {f['matured']} / {floor['min_resolved_episodes']}"
        f" ({'met' if s['meets_episode_floor'] else 'below'})",
        f"- ready to read: {'YES' if s['ready'] else 'NO'}",
        (
            f"- projected weeks to episode floor: {weeks_to:.1f}"
            if weeks_to is not None
            else "- projected weeks to episode floor: n/a (rate 0 or floor already met)"
        ),
    ]
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qualification-version", default=DEFAULT_QUALIFICATION_VERSION)
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
    repository = SourceLeadReadinessRepository.from_url(db_url)
    try:
        funnel = await repository.load(args.qualification_version)
    finally:
        await repository.close()
    summary = summarize_readiness(funnel)
    report = _report_dict(
        funnel,
        summary,
        qualification_version=args.qualification_version,
        code_revision=args.code_revision,
        working_tree_dirty=args.dirty,
    )
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    args = build_parser().parse_args()
    sys.stdout.write(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
