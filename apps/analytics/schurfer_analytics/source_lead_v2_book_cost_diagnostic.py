"""One registered, descriptive HYP-012 v2 capture/send/exit book-cost read.

No OHLCV, return, funding, trade decision or live exchange endpoint is read.
The fixed window and first-writer-wins artifact are defined in
docs/research/source-lead-v2-book-cost-diagnostic-v1.md.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import math
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import fmean
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from .outcome_repository import async_database_url
from .source_lead_exit_capture import EXIT_VERSION, exit_target_at
from .source_lead_forward_cohort import QUALIFICATION_VERSION
from .source_lead_multi_source_report import complete_digest, load_verified, write_once
from .source_lead_shadow_diagnostic import SHADOW_VERSION, _percentile

REPORT_VERSION = "source_lead_v2_book_cost_diagnostic_v1"
SEND_COST_VERSION = "source_lead_send_book_costs_v1"
WINDOW_START = datetime(2026, 9, 30, tzinfo=UTC)
WINDOW_END = datetime(2026, 10, 14, tzinfo=UTC)
READ_AFTER = datetime(2026, 10, 15, tzinfo=UTC)
CANONICAL_ARTIFACT_DIR = Path("/runtime/research/source_lead_v2_book_cost_diagnostic")
ARTIFACT_NAME = "window-2026-09-30_2026-10-14.json"
MAX_ROWS = 100_000
MIN_BOOK_AGE_MS = -1000
MAX_BOOK_AGE_MS = 2000

_ROWS = text("""
    SELECT c.id AS capture_id, c.source_first_observed_at,
           t.status AS target_status, t.observed_at AS entry_at,
           t.instrument ->> 'identity_key' AS target_identity_key,
           t.liquidity -> 'quote_timing' ->> 'contract_size_source'
               AS capture_contract_size_source,
           t.liquidity -> 'quote_timing' ->> 'book_age_ms' AS capture_book_age_ms,
           t.liquidity ->> 'spread_bps' AS capture_spread_bps,
           t.liquidity ->> 'ask_impact_bps' AS capture_ask_impact_bps,
           a.shadow_version, a.instrument_identity_key AS send_identity_key,
           a.send_cost_capture_version, a.outcome AS attempt_outcome,
           a.late AS attempt_late, a.book_age_ms AS send_book_age_ms,
           a.quantity AS send_quantity, a.send_spread_bps,
           a.send_notional_ask_impact_bps, a.send_qty_ask_impact_bps,
           e.exit_version, e.target_exchange AS exit_target_exchange,
           e.outcome AS exit_outcome, e.timeliness AS exit_timeliness,
           e.instrument_identity_key AS exit_identity_key,
           e.entry_at AS exit_entry_at, e.target_at AS exit_target_at,
           e.book_age_ms AS exit_book_age_ms,
           e.contract_size_source AS exit_contract_size_source,
           e.hypothetical_qty AS exit_quantity,
           e.bid_filled_qty AS exit_filled_quantity,
           e.spread_bps AS exit_spread_bps, e.impact_bps AS exit_impact_bps,
           e.book_snapshot AS exit_book_snapshot, e.book_sha256 AS exit_book_sha256
    FROM app.source_lead_qualifications q
    JOIN app.source_lead_captures c ON c.id = q.capture_id
    LEFT JOIN app.source_lead_target_observations t
      ON t.capture_id = q.capture_id
     AND t.target_exchange = q.selected_target_exchange
    LEFT JOIN app.source_lead_shadow_attempts a
      ON a.capture_id = q.capture_id
     AND a.qualification_version = q.qualification_version
    LEFT JOIN app.source_lead_exit_observations e
      ON e.capture_id = q.capture_id
     AND e.qualification_version = q.qualification_version
    WHERE q.qualification_version = :qualification_version
      AND q.status = 'qualified'
      AND q.selected_target_exchange = 'bybit'
      AND c.source_first_observed_at >= :window_start
      AND c.source_first_observed_at < :window_end
    ORDER BY c.source_first_observed_at, c.id
    LIMIT :limit
""")


@dataclass(frozen=True)
class BookCostRow:
    capture_id: int
    source_first_observed_at: datetime
    target_status: str | None = None
    entry_at: datetime | None = None
    target_identity_key: str | None = None
    capture_contract_size_source: str | None = None
    capture_book_age_ms: Any = None
    capture_spread_bps: Any = None
    capture_ask_impact_bps: Any = None
    shadow_version: str | None = None
    send_identity_key: str | None = None
    send_cost_capture_version: str | None = None
    attempt_outcome: str | None = None
    attempt_late: bool | None = None
    send_book_age_ms: int | None = None
    send_quantity: Decimal | None = None
    send_spread_bps: Decimal | None = None
    send_notional_ask_impact_bps: Decimal | None = None
    send_qty_ask_impact_bps: Decimal | None = None
    exit_version: str | None = None
    exit_target_exchange: str | None = None
    exit_outcome: str | None = None
    exit_timeliness: str | None = None
    exit_identity_key: str | None = None
    exit_entry_at: datetime | None = None
    exit_target_at: datetime | None = None
    exit_book_age_ms: int | None = None
    exit_contract_size_source: str | None = None
    exit_quantity: Decimal | None = None
    exit_filled_quantity: Decimal | None = None
    exit_spread_bps: Decimal | None = None
    exit_impact_bps: Decimal | None = None
    exit_book_snapshot: dict[str, Any] | None = None
    exit_book_sha256: str | None = None


def _nonnegative(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _positive(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (TypeError, ValueError):
        return None
    return number if number.is_finite() and number > 0 else None


def _fresh(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        age = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(age) and MIN_BOOK_AGE_MS <= age <= MAX_BOOK_AGE_MS


def _distribution(values: list[float]) -> dict[str, int | float | None]:
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "mean": fmean(ordered) if ordered else None,
        "p50": _percentile(ordered, 0.5),
        "p90": _percentile(ordered, 0.9),
        "p99": _percentile(ordered, 0.99),
        "max": ordered[-1] if ordered else None,
    }


def _exit_snapshot_integrity_failure(row: BookCostRow) -> str | None:
    if row.exit_outcome != "sampled":
        return None
    if not isinstance(row.exit_book_snapshot, dict) or not row.exit_book_sha256:
        return "sampled_exit_snapshot_missing"
    try:
        digest = hashlib.sha256(
            json.dumps(row.exit_book_snapshot, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    except (TypeError, ValueError):
        return "sampled_exit_snapshot_invalid"
    if digest != row.exit_book_sha256:
        return "sampled_exit_snapshot_hash_mismatch"
    return None


def _integrity_failures(rows: list[BookCostRow]) -> Counter[str]:
    failures: Counter[str] = Counter()
    seen: set[int] = set()
    for row in rows:
        if row.capture_id in seen:
            failures["duplicate_qualified_capture"] += 1
        seen.add(row.capture_id)
        if row.shadow_version not in (None, SHADOW_VERSION):
            failures["unexpected_shadow_version"] += 1
        if row.attempt_outcome is not None and row.shadow_version is None:
            failures["shadow_attempt_missing_version"] += 1
        if row.send_cost_capture_version not in (None, SEND_COST_VERSION):
            failures["unexpected_send_cost_version"] += 1
        if row.exit_version not in (None, EXIT_VERSION):
            failures["unexpected_exit_version"] += 1
        if row.exit_outcome is not None and row.exit_version is None:
            failures["exit_observation_missing_version"] += 1
        if failure := _exit_snapshot_integrity_failure(row):
            failures[failure] += 1
    return failures


def build_report(rows: list[BookCostRow]) -> dict[str, Any]:
    identity = {
        "report_version": REPORT_VERSION,
        "qualification_version": QUALIFICATION_VERSION,
        "shadow_version": SHADOW_VERSION,
        "send_cost_capture_version": SEND_COST_VERSION,
        "exit_version": EXIT_VERSION,
        "window_start_utc": WINDOW_START.isoformat(),
        "window_end_utc": WINDOW_END.isoformat(),
        "read_eligible_at_utc": READ_AFTER.isoformat(),
    }
    failures = _integrity_failures(rows)
    if failures:
        return {
            **identity,
            "status": "integrity_failed",
            "rows_read": len(rows),
            "unique_capture_ids": len({row.capture_id for row in rows}),
            "integrity_failures": dict(sorted(failures.items())),
            "interpretation": "no_book_cost_estimates_due_to_integrity_failure",
        }

    target: Counter[str] = Counter()
    attempt: Counter[str] = Counter()
    cost_version: Counter[str] = Counter()
    exit_status: Counter[str] = Counter()
    missing: Counter[str] = Counter()
    samples: dict[str, list[float]] = {
        "capture_spread_bps": [],
        "capture_ask_impact_bps": [],
        "send_on_time_spread_bps": [],
        "send_on_time_notional_ask_impact_bps": [],
        "send_on_time_qty_ask_impact_bps": [],
        "send_late_spread_bps": [],
        "send_late_notional_ask_impact_bps": [],
        "send_late_qty_ask_impact_bps": [],
        "exit_on_time_spread_bps": [],
        "exit_on_time_bid_impact_bps": [],
    }
    paired = 0
    paired_same_quantity = 0
    for row in rows:
        target[row.target_status or "no_target"] += 1
        capture_ready = (
            row.target_status == "sampled"
            and row.capture_contract_size_source == "instrument"
            and _fresh(row.capture_book_age_ms)
            and isinstance(row.target_identity_key, str)
            and row.target_identity_key.startswith("bybit:swap:")
        )
        if not capture_ready:
            missing["capture_unusable"] += 1
        else:
            capture_spread = _nonnegative(row.capture_spread_bps)
            capture_impact = _nonnegative(row.capture_ask_impact_bps)
            if capture_spread is None or capture_impact is None:
                missing["capture_cost_missing_or_invalid"] += 1
            else:
                samples["capture_spread_bps"].append(capture_spread)
                samples["capture_ask_impact_bps"].append(capture_impact)

        attempt[row.attempt_outcome or "no_attempt"] += 1
        cost_version[
            "versioned"
            if row.send_cost_capture_version == SEND_COST_VERSION
            else "pre_version_or_none"
        ] += 1
        send_ready = False
        if row.attempt_outcome is None:
            missing["no_attempt"] += 1
        elif row.send_cost_capture_version is None:
            missing["send_pre_version"] += 1
        elif not capture_ready or row.send_identity_key != row.target_identity_key:
            missing["send_identity_or_capture_mismatch"] += 1
        elif row.attempt_outcome != "shadow_recorded":
            missing[f"send_outcome:{row.attempt_outcome}"] += 1
        elif not _fresh(row.send_book_age_ms):
            missing["send_unfresh_book"] += 1
        elif row.attempt_late is None:
            missing["send_late_flag_missing"] += 1
        else:
            send_values = (
                _nonnegative(row.send_spread_bps),
                _nonnegative(row.send_notional_ask_impact_bps),
                _nonnegative(row.send_qty_ask_impact_bps),
            )
            if any(value is None for value in send_values):
                missing["send_cost_missing_or_invalid"] += 1
            elif _positive(row.send_quantity) is None:
                missing["send_quantity_missing_or_invalid"] += 1
            else:
                send_ready = not row.attempt_late
                label = "send_late" if row.attempt_late else "send_on_time"
                for suffix, value in zip(
                    ("spread_bps", "notional_ask_impact_bps", "qty_ask_impact_bps"),
                    send_values,
                    strict=True,
                ):
                    assert value is not None
                    samples[f"{label}_{suffix}"].append(value)

        exit_status[f"{row.exit_outcome or 'no_exit'}:{row.exit_timeliness or 'none'}"] += 1
        exit_ready = False
        if row.exit_outcome is None:
            missing["no_exit"] += 1
        elif row.exit_outcome != "sampled":
            missing[f"exit_outcome:{row.exit_outcome}"] += 1
        elif row.exit_timeliness != "on_time":
            missing[f"exit_timeliness:{row.exit_timeliness or 'none'}"] += 1
        elif not capture_ready or row.entry_at is None:
            missing["exit_capture_unusable"] += 1
        elif (
            row.exit_target_exchange != "bybit"
            or row.exit_identity_key != row.target_identity_key
            or row.exit_entry_at != row.entry_at
        ):
            missing["exit_identity_or_entry_mismatch"] += 1
        elif row.exit_target_at != exit_target_at(row.entry_at):
            missing["exit_target_time_mismatch"] += 1
        elif not _fresh(row.exit_book_age_ms):
            missing["exit_unfresh_book"] += 1
        elif row.exit_contract_size_source != "instrument":
            missing["exit_contract_size_unknown"] += 1
        else:
            quantity = _positive(row.exit_quantity)
            filled = _positive(row.exit_filled_quantity)
            if quantity is None or filled is None or filled < quantity:
                missing["exit_depth_or_quantity_missing"] += 1
            else:
                exit_spread = _nonnegative(row.exit_spread_bps)
                exit_impact = _nonnegative(row.exit_impact_bps)
                if exit_spread is None or exit_impact is None:
                    missing["exit_cost_missing_or_invalid"] += 1
                else:
                    exit_ready = True
                    samples["exit_on_time_spread_bps"].append(exit_spread)
                    samples["exit_on_time_bid_impact_bps"].append(exit_impact)

        if send_ready and exit_ready:
            paired += 1
            if row.send_quantity == row.exit_quantity:
                paired_same_quantity += 1

    return {
        **identity,
        "status": "complete",
        "integrity_failures": {},
        "eligible": len(rows),
        "target_status": dict(sorted(target.items())),
        "attempt_outcome": dict(sorted(attempt.items())),
        "send_cost_version": dict(sorted(cost_version.items())),
        "exit_outcome_timeliness": dict(sorted(exit_status.items())),
        "missingness": dict(sorted(missing.items())),
        "paired_on_time": paired,
        "paired_same_quantity": paired_same_quantity,
        "book_cost_bps": {key: _distribution(values) for key, values in samples.items()},
        "interpretation": "descriptive_observed_books_only_no_return_or_cost_model_change",
    }


async def load_rows(
    db_url: str,
    *,
    window_start: datetime = WINDOW_START,
    window_end: datetime = WINDOW_END,
    read_after: datetime = READ_AFTER,
    qualification_version: str = QUALIFICATION_VERSION,
) -> tuple[datetime, list[BookCostRow]]:
    engine = create_async_engine(async_database_url(db_url), pool_pre_ping=True)
    try:
        async with engine.connect() as raw:
            conn = await raw.execution_options(
                isolation_level="REPEATABLE READ", postgresql_readonly=True
            )
            async with conn.begin():
                database_now = (await conn.execute(text("SELECT now()"))).scalar_one()
                if database_now < read_after:
                    raise ValueError("book-cost read is before the registered read time")
                result = await conn.execute(
                    _ROWS,
                    {
                        "qualification_version": qualification_version,
                        "window_start": window_start,
                        "window_end": window_end,
                        "limit": MAX_ROWS + 1,
                    },
                )
                mappings = result.mappings().all()
        if len(mappings) > MAX_ROWS:
            raise ValueError(f"more than {MAX_ROWS} qualified episodes; refusing truncation")
        return database_now, [BookCostRow(**dict(row)) for row in mappings]
    finally:
        await engine.dispose()


def _load_artifact(path: Path) -> dict[str, Any]:
    complete_digest(path)
    report, _ = load_verified(path)
    expected_versions = {
        "report_version": REPORT_VERSION,
        "qualification_version": QUALIFICATION_VERSION,
        "shadow_version": SHADOW_VERSION,
        "send_cost_capture_version": SEND_COST_VERSION,
        "exit_version": EXIT_VERSION,
    }
    for field, expected in expected_versions.items():
        if report.get(field) != expected:
            raise ValueError(f"saved book-cost artifact has an unexpected {field}")
    if report.get("window_start_utc") != WINDOW_START.isoformat():
        raise ValueError("saved book-cost artifact has an unexpected window start")
    if report.get("window_end_utc") != WINDOW_END.isoformat():
        raise ValueError("saved book-cost artifact has an unexpected window end")
    return report


async def report_once(
    db_url: str,
    *,
    artifact_dir: Path = CANONICAL_ARTIFACT_DIR,
    code_revision: str,
    dirty: bool,
) -> dict[str, Any]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    with (artifact_dir / ".read.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        path = artifact_dir / ARTIFACT_NAME
        if path.exists():
            return _load_artifact(path)
        database_now, rows = await load_rows(db_url)
        report = build_report(rows)
        report["database_now_utc"] = database_now.isoformat()
        report["generated_at_utc"] = datetime.now(UTC).isoformat()
        report["code_revision"] = code_revision
        report["working_tree_dirty"] = dirty
        report["row_snapshot_sha256"] = hashlib.sha256(
            json.dumps(
                [asdict(row) for row in rows],
                default=str,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        write_once(path, report)
        return _load_artifact(path)


def render_markdown(report: dict[str, Any]) -> str:
    if report["status"] == "integrity_failed":
        lines = [
            "# HYP-012 v2 book-cost diagnostic",
            "",
            f"Window: {report['window_start_utc']} to {report['window_end_utc']} (exclusive).",
            "Status: integrity_failed. No book-cost estimates were published.",
            f"Rows read: {report['rows_read']}; unique capture IDs: "
            f"{report['unique_capture_ids']}.",
            "",
            "| integrity failure | n |",
            "| ----------------- | - |",
        ]
        lines.extend(f"| {key} | {n} |" for key, n in report["integrity_failures"].items())
        lines.extend(
            [
                "",
                f"Database read: {report['database_now_utc']}; code: {report['code_revision']}; "
                f"dirty: {report['working_tree_dirty']}.",
                f"Rows SHA-256: {report['row_snapshot_sha256']}.",
                "",
            ]
        )
        return "\n".join(lines)
    lines = [
        "# HYP-012 v2 book-cost diagnostic",
        "",
        f"Window: {report['window_start_utc']} to {report['window_end_utc']} (exclusive).",
        "Status: complete.",
        f"Eligible: {report['eligible']}; on-time paired: {report['paired_on_time']}; "
        f"same quantity: {report['paired_same_quantity']}.",
        "Descriptive book costs only; no return, fill or cost-model update.",
        "",
        "| book metric | n | mean bps | p50 | p90 | p99 | max |",
        "| ----------- | - | -------- | --- | --- | --- | --- |",
    ]
    for name, stats in report["book_cost_bps"].items():
        lines.append(
            f"| {name} | {stats['n']} | {stats['mean']} | {stats['p50']} | "
            f"{stats['p90']} | {stats['p99']} | {stats['max']} |"
        )
    for label in (
        "target_status",
        "attempt_outcome",
        "send_cost_version",
        "exit_outcome_timeliness",
        "missingness",
    ):
        lines.extend(["", f"## {label}", "", "| reason | n |", "| ------ | - |"])
        lines.extend(f"| {key} | {n} |" for key, n in report[label].items())
    lines.extend(
        [
            "",
            f"Database read: {report['database_now_utc']}; code: {report['code_revision']}; "
            f"dirty: {report['working_tree_dirty']}.",
            f"Rows SHA-256: {report['row_snapshot_sha256']}.",
            "",
        ]
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--code-revision", default="unknown")
    parser.add_argument("--working-tree-dirty", dest="dirty", action="store_true")
    parser.add_argument("--no-working-tree-dirty", dest="dirty", action="store_false")
    parser.set_defaults(dirty=True)
    return parser


async def _run(args: argparse.Namespace) -> tuple[str, bool]:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required")
    report = await report_once(db_url, code_revision=args.code_revision, dirty=args.dirty)
    output = (
        json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.format == "json"
        else render_markdown(report)
    )
    return output, report["status"] == "integrity_failed"


def main() -> None:
    output, failed = asyncio.run(_run(build_parser().parse_args()))
    sys.stdout.write(output)
    if failed:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
