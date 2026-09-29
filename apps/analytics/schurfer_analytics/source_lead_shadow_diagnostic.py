"""Read-only weekly HYP-012 v2 shadow latency diagnostic.

Only qualification identity, target-observation timestamps, attempt status,
timestamps and quote_change_bps are selected. No price, return or PnL column is
queried. The registered v2 verdict never reads this report.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from .outcome_repository import async_database_url
from .source_lead_forward_cohort import (
    QUALIFICATION_VERSION,
    SOURCE_LEAD_FORWARD_COHORT_START,
)

SHADOW_VERSION = "source_lead_shadow_v1"
REPORT_VERSION = "source_lead_shadow_diagnostic_v1"
MAX_ROWS = 100_000

_ROWS = text("""
    SELECT c.id AS capture_id, c.source_first_observed_at,
           t.observed_at, q.qualified_at,
           a.shadow_version, a.outcome, a.first_seen_at,
           a.quote_requested_at, a.quote_received_at, a.book_ts_ms,
           a.late, a.quote_change_bps
    FROM app.source_lead_qualifications q
    JOIN app.source_lead_captures c ON c.id = q.capture_id
    LEFT JOIN app.source_lead_target_observations t
      ON t.capture_id = q.capture_id
     AND t.target_exchange = q.selected_target_exchange
    LEFT JOIN app.source_lead_shadow_attempts a
      ON a.capture_id = q.capture_id
     AND a.qualification_version = q.qualification_version
    WHERE q.qualification_version = :qv
      AND q.status = 'qualified'
      AND q.selected_target_exchange = 'bybit'
      AND c.source_first_observed_at >= :cohort_start
      AND c.source_first_observed_at < :until
    ORDER BY c.source_first_observed_at, c.id
    LIMIT :limit
""")


@dataclass(frozen=True)
class ShadowRow:
    capture_id: int
    source_first_observed_at: datetime
    observed_at: datetime | None
    qualified_at: datetime
    shadow_version: str | None = None
    outcome: str | None = None
    first_seen_at: datetime | None = None
    quote_requested_at: datetime | None = None
    quote_received_at: datetime | None = None
    book_ts_ms: int | None = None
    late: bool | None = None
    quote_change_bps: float | None = None


def _week_start(value: datetime) -> datetime:
    day = value.astimezone(UTC).date()
    return datetime.combine(day - timedelta(days=day.weekday()), time.min, tzinfo=UTC)


def _ms(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return (a - b).total_seconds() * 1000


def _percentile(sorted_values: list[float], fraction: float) -> float | None:
    if not sorted_values:
        return None
    position = (len(sorted_values) - 1) * fraction
    lower = int(position)
    weight = position - lower
    upper = min(lower + 1, len(sorted_values) - 1)
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def _distribution(values: list[float]) -> dict[str, int | float | None]:
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "negative_n": sum(value < 0 for value in ordered),
        "p50": _percentile(ordered, 0.5),
        "p90": _percentile(ordered, 0.9),
        "p99": _percentile(ordered, 0.99),
        "max": ordered[-1] if ordered else None,
    }


def _valid_attempt(row: ShadowRow) -> bool:
    return row.shadow_version == SHADOW_VERSION


def _summary(rows: list[ShadowRow]) -> dict[str, Any]:
    outcomes: Counter[str] = Counter()
    segments: dict[str, list[float]] = {
        "capture_ms": [],
        "qualification_ms": [],
        "pickup_ms": [],
        "detection_to_shadow_ms": [],
        "processing_ms": [],
        "quote_round_trip_ms": [],
        "book_age_ms": [],
        "end_to_end_after_detection_ms": [],
    }
    quote_change: dict[str, list[float]] = {"late": [], "on_time": [], "unknown": []}
    late_count = 0
    with_quote = 0
    late_flag_mismatch = 0
    quote_change_without_quote = 0
    for row in rows:
        capture_ms = _ms(row.observed_at, row.source_first_observed_at)
        qualification_ms = _ms(row.qualified_at, row.observed_at)
        if capture_ms is not None:
            segments["capture_ms"].append(capture_ms)
        if qualification_ms is not None:
            segments["qualification_ms"].append(qualification_ms)
        if row.shadow_version is None:
            outcomes["no_attempt"] += 1
        elif not _valid_attempt(row):
            outcomes["version_mismatch"] += 1
        else:
            outcomes[row.outcome or "missing_outcome"] += 1
            if row.late:
                late_count += 1
            if row.first_seen_at is not None and row.observed_at is not None:
                computed_late = row.first_seen_at - row.observed_at > timedelta(seconds=30)
                if row.late is None or row.late != computed_late:
                    late_flag_mismatch += 1
            if row.quote_received_at is not None:
                with_quote += 1
            pairs = {
                "pickup_ms": (row.first_seen_at, row.qualified_at),
                "detection_to_shadow_ms": (row.first_seen_at, row.observed_at),
                "processing_ms": (row.quote_requested_at, row.first_seen_at),
                "quote_round_trip_ms": (row.quote_received_at, row.quote_requested_at),
                "end_to_end_after_detection_ms": (
                    row.quote_received_at,
                    row.source_first_observed_at,
                ),
            }
            for name, (end, start) in pairs.items():
                value = _ms(end, start)
                if value is not None:
                    segments[name].append(value)
            if row.quote_received_at is not None and row.book_ts_ms is not None:
                segments["book_age_ms"].append(
                    row.quote_received_at.timestamp() * 1000 - row.book_ts_ms
                )
            if row.quote_change_bps is not None:
                if row.quote_received_at is None:
                    quote_change_without_quote += 1
                else:
                    label = "unknown" if row.late is None else "late" if row.late else "on_time"
                    quote_change[label].append(row.quote_change_bps)
    attempts = sum(
        count for key, count in outcomes.items() if key not in {"no_attempt", "version_mismatch"}
    )
    return {
        "qualified": len(rows),
        "outcomes": dict(sorted(outcomes.items())),
        "with_quote": with_quote,
        "quote_coverage": with_quote / len(rows) if rows else None,
        "late_n": late_count,
        "late_share_of_attempts": late_count / attempts if attempts else None,
        "late_flag_mismatch_n": late_flag_mismatch,
        "quote_change_without_quote_n": quote_change_without_quote,
        "segments": {name: _distribution(values) for name, values in segments.items()},
        "quote_change_bps": {
            label: _distribution(values) for label, values in quote_change.items()
        },
    }


def build_report(rows: list[ShadowRow], *, until: datetime) -> dict[str, Any]:
    """Summarize only completed UTC weeks; apply the engineering rule once.

    The first complete-week prefix with >=30 shadow_recorded attempts fixes the
    engineering branch. Later calls recompute that same first prefix rather than
    choosing a more favorable week.
    """
    if until.tzinfo is None or until.utcoffset() != timedelta(0):
        raise ValueError("until must be UTC")
    if until.weekday() != 0 or until.time() != time.min:
        raise ValueError("until must be Monday 00:00 UTC")
    first_week = _week_start(SOURCE_LEAD_FORWARD_COHORT_START)
    if until <= SOURCE_LEAD_FORWARD_COHORT_START:
        raise ValueError("until must close at least one cohort week")
    if len({row.capture_id for row in rows}) != len(rows):
        raise ValueError("duplicate qualified capture ids")
    if any(
        row.source_first_observed_at < SOURCE_LEAD_FORWARD_COHORT_START
        or row.source_first_observed_at >= until
        for row in rows
    ):
        raise ValueError("row outside requested cohort prefix")
    weekly = []
    decision: dict[str, Any] = {"status": "pending", "reason": "fewer than 30 recorded"}
    start = first_week
    while start < until:
        end = start + timedelta(days=7)
        this_week = [row for row in rows if start <= row.source_first_observed_at < end]
        weekly.append({"week_start_utc": start.isoformat(), **_summary(this_week)})
        prefix = [row for row in rows if row.source_first_observed_at < end]
        prefix_summary = _summary(prefix)
        recorded = prefix_summary["outcomes"].get("shadow_recorded", 0)
        if decision["status"] == "pending" and recorded >= 30:
            latency = prefix_summary["segments"]["end_to_end_after_detection_ms"]
            failures = []
            if (prefix_summary["quote_coverage"] or 0) < 0.9:
                failures.append("quote_coverage_below_90pct")
            if latency["p50"] is None or latency["p50"] > 10_000:
                failures.append("median_above_10s_or_missing")
            if latency["p90"] is None or latency["p90"] > 30_000:
                failures.append("p90_above_30s_or_missing")
            decision = {
                "status": "pipeline_first" if failures else "detection_next",
                "first_eligible_week_end_utc": end.isoformat(),
                "shadow_recorded": recorded,
                "quote_coverage": prefix_summary["quote_coverage"],
                "end_to_end_ms": latency,
                "failed_conditions": failures,
            }
        start = end
    return {
        "report_version": REPORT_VERSION,
        "outcome_blind": True,
        "cohort_start_utc": SOURCE_LEAD_FORWARD_COHORT_START.isoformat(),
        "through_week_end_utc": until.isoformat(),
        "qualification_version": QUALIFICATION_VERSION,
        "shadow_version": SHADOW_VERSION,
        "rows": len(rows),
        "cumulative": _summary(rows),
        "weekly": weekly,
        "engineering_rule": decision,
        "heartbeat_gap_history": "unavailable: Redis stores current health, not past intervals",
        "formal_v2_verdict_changed": False,
    }


async def load_rows(
    db_url: str,
    *,
    until: datetime,
    qualification_version: str = QUALIFICATION_VERSION,
) -> tuple[datetime, list[ShadowRow]]:
    engine = create_async_engine(async_database_url(db_url), pool_pre_ping=True)
    try:
        async with engine.connect() as raw:
            conn = await raw.execution_options(
                isolation_level="REPEATABLE READ", postgresql_readonly=True
            )
            async with conn.begin():
                database_now = (await conn.execute(text("SELECT now()"))).scalar_one()
                if until > database_now:
                    raise ValueError("requested UTC week has not ended in the database clock")
                result = await conn.execute(
                    _ROWS,
                    {
                        "qv": qualification_version,
                        "cohort_start": SOURCE_LEAD_FORWARD_COHORT_START,
                        "until": until,
                        "limit": MAX_ROWS + 1,
                    },
                )
                mappings = result.mappings().all()
        if len(mappings) > MAX_ROWS:
            raise ValueError(f"more than {MAX_ROWS} qualified episodes; refusing truncation")
        return database_now, [
            ShadowRow(
                capture_id=int(row["capture_id"]),
                source_first_observed_at=row["source_first_observed_at"],
                observed_at=row["observed_at"],
                qualified_at=row["qualified_at"],
                shadow_version=row["shadow_version"],
                outcome=row["outcome"],
                first_seen_at=row["first_seen_at"],
                quote_requested_at=row["quote_requested_at"],
                quote_received_at=row["quote_received_at"],
                book_ts_ms=row["book_ts_ms"],
                late=row["late"],
                quote_change_bps=(
                    float(row["quote_change_bps"]) if row["quote_change_bps"] is not None else None
                ),
            )
            for row in mappings
        ]
    finally:
        await engine.dispose()


def render_markdown(report: dict[str, Any]) -> str:
    decision = report["engineering_rule"]
    lines = [
        "# HYP-012 v2 shadow latency diagnostic",
        "",
        f"Cohort from {report['cohort_start_utc']} through {report['through_week_end_utc']}.",
        "Read-only and outcome-blind. No order, return or formal v2 verdict is evaluated.",
        "",
        f"- qualified: {report['rows']}",
        f"- engineering rule: {decision['status']}",
        f"- heartbeat gap history: {report['heartbeat_gap_history']}",
        f"- code revision: {report.get('code_revision', 'unknown')}; "
        f"working tree dirty: {report.get('working_tree_dirty', 'unknown')}",
        f"- row snapshot SHA-256: {report.get('snapshot_sha256', 'not supplied')}",
        "",
    ]
    if decision["status"] != "pending":
        lines.extend(
            [
                f"First eligible week ended {decision['first_eligible_week_end_utc']}: "
                f"{decision['shadow_recorded']} shadow decisions; "
                f"quote coverage {decision['quote_coverage']:.1%}; "
                f"V-S p50 {decision['end_to_end_ms']['p50']} ms, "
                f"p90 {decision['end_to_end_ms']['p90']} ms.",
                f"Failed conditions: {', '.join(decision['failed_conditions']) or 'none'}.",
                "",
            ]
        )
    for week in report["weekly"]:
        lines.extend(
            [
                f"## UTC week {week['week_start_utc']}",
                "",
                f"Qualified: {week['qualified']}; quote coverage: "
                f"{week['quote_coverage'] if week['quote_coverage'] is not None else 'n/a'}",
                f"Data inconsistencies: late flag {week['late_flag_mismatch_n']}; "
                f"quote change without received book {week['quote_change_without_quote_n']}.",
                "",
                "| outcome | count |",
                "| ------- | ----- |",
                *[f"| {key} | {value} |" for key, value in week["outcomes"].items()],
                "",
                "| segment | n | negative | p50 ms | p90 ms | p99 ms | max ms |",
                "| ------- | - | -------- | ------ | ------ | ------ | ------ |",
            ]
        )
        for name, stats in week["segments"].items():
            lines.append(
                f"| {name} | {stats['n']} | {stats['negative_n']} | {stats['p50']} | "
                f"{stats['p90']} | {stats['p99']} | {stats['max']} |"
            )
        lines.extend(
            [
                "",
                f"Late attempts: {week['late_n']} ({week['late_share_of_attempts']}).",
                "",
                "| quote change bps | n | p50 | p90 |",
                "| ---------------- | - | --- | --- |",
            ]
        )
        for label, stats in week["quote_change_bps"].items():
            lines.append(f"| {label} | {stats['n']} | {stats['p50']} | {stats['p90']} |")
        lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--week-end", required=True, help="Monday UTC date, YYYY-MM-DD")
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--code-revision", default="unknown")
    parser.add_argument("--working-tree-dirty", dest="dirty", action="store_true")
    parser.add_argument("--no-working-tree-dirty", dest="dirty", action="store_false")
    parser.set_defaults(dirty=True)
    return parser


async def _run(args: argparse.Namespace) -> str:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required")
    until = datetime.combine(date.fromisoformat(args.week_end), time.min, tzinfo=UTC)
    # Validate before connecting; the DB clock check follows inside load_rows.
    build_report([], until=until)
    database_now, rows = await load_rows(db_url, until=until)
    report = build_report(rows, until=until)
    report["generated_at_utc"] = datetime.now(UTC).isoformat()
    report["database_now_utc"] = database_now.isoformat()
    report["code_revision"] = args.code_revision
    report["working_tree_dirty"] = args.dirty
    report["snapshot_sha256"] = hashlib.sha256(
        json.dumps([asdict(row) for row in rows], default=str, sort_keys=True).encode()
    ).hexdigest()
    return (
        json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.format == "json"
        else render_markdown(report)
    )


def main() -> None:
    sys.stdout.write(asyncio.run(_run(build_parser().parse_args())))


if __name__ == "__main__":
    main()
