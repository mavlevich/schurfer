"""Why abnormal-flow v1 outcomes are unresolved: a post-hoc taxonomy on the burned window.

This is a diagnostic, not evidence. It never reads a price. For every primary episode and
every control row it checks the priced-proxy path the outcome reader requires (the entry
bar plus ``outcome_horizon_minutes`` further one-minute bars, see
``priced_proxy_path_times``) using only bar PRESENCE, ``price_complete`` and the collector's
own ``unbackfilled_gap_*`` markers. Each row gets exactly one ``primary_reason`` (so the
counts add up to the unresolved total) plus independent flags, the first problem minute and
the first gap length.

The result must reconcile with the ``resolved`` booleans already saved by the burned
coverage diagnostic: a row whose path looks complete here but was unresolved there is
``unexplained_by_allowed_columns`` (for example an empty entry or exit price), never folded
into another reason; a row with a path problem that was nevertheless resolved is a hard
error. Observed gaps are observations, not a proven capture failure: a missing row does not
say what happened at the source. Listing age is ``unknown``: the frozen identity export has
a snapshot time, not a verified trading-open time.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import duckdb

from .abnormal_flow_formal_runner import (
    _hash_file,
    _load_registered_contract,
    _verified_cold_bar_paths,
    dependency_bounds,
)
from .abnormal_flow_portfolio_diagnostic import _run_code_state, bundle_fingerprint
from .abnormal_flow_replay import priced_proxy_path_times
from .abnormal_flow_snapshots import SnapshotReader, decision_id

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from .abnormal_flow_replay import DecisionFeatures

TAXONOMY_VERSION: Final = "abnormal_flow_unresolved_taxonomy_v1"
# Priority order of ``primary_reason``; the first matching condition wins.
REASONS: Final = (
    "no_route_bars",
    "missing_entry_bar",
    "entry_bar_incomplete",
    "internal_gap",
    "missing_horizon_bar",
    "price_incomplete_inside",
    "unexplained_by_allowed_columns",
)
# Coarse, pre-declared buckets for the first problem minute (minute 0 is the entry bar).
FIRST_PROBLEM_BUCKETS: Final = (
    ("0", 0, 0),
    ("1-59", 1, 59),
    ("60-239", 60, 239),
    ("240-479", 240, 479),
    ("480-719", 480, 719),
    ("720", 720, 720),
)
LISTING_AGE_NOTE: Final = (
    "unknown: the frozen identity export records a snapshot time, not a verified "
    "trading-open timestamp"
)

RouteId = tuple[str, str, str, str]


@dataclass(frozen=True)
class MinuteState:
    price_complete: bool
    collector_gap: bool  # the collector marked an unbackfilled gap on this bar


@dataclass(frozen=True)
class PathDiagnosis:
    primary_reason: str | None
    missing_entry_bar: bool
    entry_bar_incomplete: bool
    internal_gap: bool
    missing_horizon_bar: bool
    price_incomplete_inside: bool
    exit_bar_incomplete: bool
    # Entry and exit bars both present and price_complete: resolvable under a rule that
    # checks only the two bars the priced proxy uses (a v2 candidate, not the v1 rule).
    entry_exit_bars_complete: bool
    collector_gap_reported: bool
    first_problem_minute: int | None
    first_gap_length: int | None
    missing_minutes: int
    incomplete_minutes: int


def _route(decision: DecisionFeatures) -> RouteId:
    return (
        decision.exchange,
        decision.market_type,
        decision.native_market_id,
        decision.capture_version,
    )


def diagnose_path(
    minutes: Mapping[datetime, MinuteState] | None,
    *,
    decision_at: datetime,
    outcome_horizon_minutes: int,
    resolved: bool,
) -> PathDiagnosis:
    """Classify one row. ``minutes`` is the route's bars over the scanned window (``None``
    when the route has none there). ``no_route_bars`` means no bar anywhere in THIS
    episode's path, whether or not the route has bars elsewhere in the window."""
    entry_at, _exit_bar_start, _exit_at = priced_proxy_path_times(
        decision_at, outcome_horizon_minutes
    )
    last = outcome_horizon_minutes
    absent: list[int] = []
    incomplete: list[int] = []
    collector_gap = False
    for offset in range(last + 1):
        state = None if minutes is None else minutes.get(entry_at + timedelta(minutes=offset))
        if state is None:
            absent.append(offset)
            continue
        collector_gap = collector_gap or state.collector_gap
        if not state.price_complete:
            incomplete.append(offset)
    missing_entry = 0 in absent
    entry_incomplete = 0 in incomplete
    internal_gap = any(0 < offset < last for offset in absent)
    missing_horizon = last in absent
    incomplete_inside = any(offset > 0 for offset in incomplete)
    exit_incomplete = last in incomplete
    entry_exit_complete = not (
        missing_entry or entry_incomplete or missing_horizon or exit_incomplete
    )
    flags = (missing_entry, entry_incomplete, internal_gap, missing_horizon, incomplete_inside)
    problems = sorted(set(absent) | set(incomplete))
    first_gap_length: int | None = None
    if absent:
        run = 1
        while absent[0] + run in absent:
            run += 1
        first_gap_length = run

    primary: str | None
    if len(absent) == last + 1:
        if resolved:
            raise ValueError("a row with no bar in its path cannot have been resolved")
        primary = "no_route_bars"
    elif any(flags):
        if resolved:
            raise ValueError("a row with a path problem was recorded as resolved")
        primary = REASONS[1:6][flags.index(True)]
    else:
        primary = None if resolved else "unexplained_by_allowed_columns"
    return PathDiagnosis(
        primary_reason=primary,
        missing_entry_bar=missing_entry,
        entry_bar_incomplete=entry_incomplete,
        internal_gap=internal_gap,
        missing_horizon_bar=missing_horizon,
        price_incomplete_inside=incomplete_inside,
        exit_bar_incomplete=exit_incomplete,
        entry_exit_bars_complete=entry_exit_complete,
        collector_gap_reported=collector_gap,
        first_problem_minute=problems[0] if problems else None,
        first_gap_length=first_gap_length,
        missing_minutes=len(absent),
        incomplete_minutes=len(incomplete),
    )


def first_problem_bucket(minute: int | None) -> str | None:
    if minute is None:
        return None
    for label, low, high in FIRST_PROBLEM_BUCKETS:
        if low <= minute <= high:
            return label
    raise ValueError(f"first problem minute {minute} outside the path")


@dataclass(frozen=True)
class TaxonomyRow:
    role: str
    primary_decision_id: str | None
    decision_id: str
    exchange: str
    iso_week: str
    canonical_asset: str
    decision_at: datetime
    resolved: bool
    diagnosis: PathDiagnosis


def _grouped(rows: Iterable[TaxonomyRow], key: Any) -> list[dict[str, Any]]:
    groups: dict[tuple[str, ...], Counter[str]] = defaultdict(Counter)
    for row in rows:
        groups[key(row)][row.diagnosis.primary_reason or "resolved"] += 1
    return [
        {"group": list(group), "total": sum(counts.values()), **dict(sorted(counts.items()))}
        for group, counts in sorted(groups.items())
    ]


def summarize(rows: Sequence[TaxonomyRow]) -> dict[str, Any]:
    """Reason totals, flags, first-problem buckets and venue/week splits per role, plus the
    control comparison both by venue/week stratum and paired to each control's primary."""
    summary: dict[str, Any] = {}
    for role in ("primary", "control"):
        members = [row for row in rows if row.role == role]
        unresolved = [row for row in members if not row.resolved]
        reasons = Counter(row.diagnosis.primary_reason for row in unresolved)
        summary[role] = {
            "rows": len(members),
            "unresolved": len(unresolved),
            "primary_reason": {reason: reasons.get(reason, 0) for reason in REASONS},
            "flags": {
                name: sum(getattr(row.diagnosis, name) for row in unresolved)
                for name in (
                    "missing_entry_bar",
                    "entry_bar_incomplete",
                    "internal_gap",
                    "missing_horizon_bar",
                    "price_incomplete_inside",
                    "exit_bar_incomplete",
                    "entry_exit_bars_complete",
                    "collector_gap_reported",
                )
            },
            "first_problem_bucket": dict(
                sorted(
                    Counter(
                        first_problem_bucket(row.diagnosis.first_problem_minute) or "none"
                        for row in unresolved
                    ).items()
                )
            ),
            "by_exchange": _grouped(members, lambda row: (row.exchange,)),
            "by_asset": _grouped(members, lambda row: (row.canonical_asset,)),
            "by_utc_week": _grouped(members, lambda row: (row.iso_week,)),
            "by_exchange_utc_week": _grouped(members, lambda row: (row.exchange, row.iso_week)),
        }
    if sum(summary[role]["primary_reason"].values()) != summary[role]["unresolved"]:
        raise ValueError("primary reasons do not add up to the unresolved total")

    strata: dict[tuple[str, str], dict[str, list[int]]] = defaultdict(
        lambda: {"primary": [0, 0], "control": [0, 0]}
    )
    for row in rows:
        cell = strata[(row.exchange, row.iso_week)][row.role]
        cell[0] += 1
        cell[1] += int(not row.resolved)
    primary_reason_by_id = {
        row.decision_id: row.diagnosis.primary_reason or "resolved"
        for row in rows
        if row.role == "primary"
    }
    paired: Counter[str] = Counter()
    paired_reasons: Counter[tuple[str, str]] = Counter()
    for row in rows:
        if row.role != "control" or row.primary_decision_id is None:
            continue
        primary_reason = primary_reason_by_id[row.primary_decision_id]
        control_reason = row.diagnosis.primary_reason or "resolved"
        primary_state = "primary_resolved" if primary_reason == "resolved" else "primary_unresolved"
        control_state = "control_resolved" if row.resolved else "control_unresolved"
        paired[f"{primary_state}|{control_state}"] += 1
        paired_reasons[(primary_reason, control_reason)] += 1
    summary["control_comparison"] = {
        "by_exchange_utc_week": [
            {
                "group": list(group),
                "primary_rows": cells["primary"][0],
                "primary_unresolved": cells["primary"][1],
                "control_rows": cells["control"][0],
                "control_unresolved": cells["control"][1],
            }
            for group, cells in sorted(strata.items())
        ],
        "paired_to_primary": dict(sorted(paired.items())),
        "paired_reasons": [
            {"primary_reason": primary, "control_reason": control, "control_rows": count}
            for (primary, control), count in sorted(paired_reasons.items())
        ],
    }
    summary["listing_age"] = LISTING_AGE_NOTE
    return summary


def build_rows(
    episodes: Sequence[DecisionFeatures],
    controls_by_primary: Mapping[str, Sequence[DecisionFeatures]],
    resolved_by_key: Mapping[tuple[str, str | None, str], bool],
    bars_by_route: Mapping[RouteId, Mapping[datetime, MinuteState]],
    *,
    outcome_horizon_minutes: int,
) -> list[TaxonomyRow]:
    """One row per primary episode and per control row, each reconciled against the saved
    ``resolved`` boolean keyed by ``(role, primary_decision_id, decision_id)``."""
    members: list[tuple[str, str | None, DecisionFeatures]] = [
        ("primary", None, episode) for episode in episodes
    ]
    members.extend(
        ("control", primary_id, control)
        for primary_id, controls in controls_by_primary.items()
        for control in controls
    )
    if len(members) != len(resolved_by_key):
        raise ValueError(
            f"row count {len(members)} does not match the saved coverage rows "
            f"({len(resolved_by_key)})"
        )
    rows: list[TaxonomyRow] = []
    for role, primary_id, decision in members:
        key = (role, primary_id, decision_id(decision))
        if key not in resolved_by_key:
            raise ValueError(f"no saved coverage row for {key}")
        resolved = resolved_by_key[key]
        rows.append(
            TaxonomyRow(
                role=role,
                primary_decision_id=primary_id,
                decision_id=decision_id(decision),
                exchange=decision.exchange,
                iso_week=decision.iso_week,
                canonical_asset=decision.canonical_asset,
                decision_at=decision.decision_at,
                resolved=resolved,
                diagnosis=diagnose_path(
                    bars_by_route.get(_route(decision)),
                    decision_at=decision.decision_at,
                    outcome_horizon_minutes=outcome_horizon_minutes,
                    resolved=resolved,
                ),
            )
        )
    return rows


def load_bar_states(
    paths: Sequence[str],
    routes: Iterable[RouteId],
    *,
    window_start: datetime,
    window_end: datetime,
) -> dict[RouteId, dict[datetime, MinuteState]]:
    """Presence, ``price_complete`` and collector gap markers only; no price column. A
    duplicated minute is complete only when every copy is."""
    with duckdb.connect(":memory:") as db:
        db.execute(
            "CREATE TABLE routes (exchange VARCHAR, market_type VARCHAR, symbol VARCHAR, "
            "capture_version VARCHAR)"
        )
        db.executemany("INSERT INTO routes VALUES (?, ?, ?, ?)", sorted(set(routes)))
        result = db.execute(
            """
            SELECT b.exchange, b.market_type, b.symbol, b.capture_version, b.bucket_start,
                   bool_and(coalesce(b.price_complete, false)) AS price_complete,
                   bool_or(coalesce(b.unbackfilled_gap_minutes, 0) > 0) AS collector_gap
            FROM read_parquet(?) b
            JOIN routes r USING (exchange, market_type, symbol, capture_version)
            WHERE b.bucket_start >= ? AND b.bucket_start < ?
            GROUP BY ALL
            """,
            [list(paths), window_start, window_end],
        ).fetchall()
    states: dict[RouteId, dict[datetime, MinuteState]] = defaultdict(dict)
    for exchange, market_type, symbol, capture, bucket, complete, gap in result:
        states[(exchange, market_type, symbol, capture)][bucket.astimezone(UTC)] = MinuteState(
            bool(complete), bool(gap)
        )
    return dict(states)


def _load_saved_coverage(
    bundle: Path, *, expected_provenance: Mapping[str, str]
) -> tuple[dict[tuple[str, str | None, str], bool], str, dict[str, Any]]:
    """The saved ``resolved`` labels, only from a bundle proven to belong to THIS burned
    run: intact hashes AND provenance matching the current snapshot, contract, scan
    manifest and burned formal report."""
    report_path = bundle / "coverage_diagnostic.json"
    report_hash = _hash_file(report_path)
    if (bundle / "coverage_diagnostic.sha256").read_text().strip() != report_hash:
        raise ValueError("coverage diagnostic report hash mismatch")
    report = json.loads(report_path.read_text())
    provenance = report.get("provenance", {})
    mismatched = {
        name: (provenance.get(name), value)
        for name, value in expected_provenance.items()
        if provenance.get(name) != value
    }
    if mismatched:
        raise ValueError(f"coverage bundle is not from this burned run: {mismatched}")
    rows_path = bundle / "coverage_rows.parquet"
    expected = report["artifacts"]["coverage_rows.parquet"]
    if _hash_file(rows_path) != expected:
        raise ValueError("coverage rows artifact hash mismatch")
    with duckdb.connect(":memory:") as db:
        rows = db.execute(
            "SELECT role, primary_decision_id, decision_id, resolved FROM read_parquet(?)",
            [str(rows_path)],
        ).fetchall()
    resolved: dict[tuple[str, str | None, str], bool] = {}
    for role, primary_id, row_decision_id, flag in rows:
        key = (str(role), None if primary_id is None else str(primary_id), str(row_decision_id))
        if key in resolved:
            raise ValueError(f"duplicate saved coverage row {key}")
        resolved[key] = bool(flag)
    return resolved, report_hash, report


def verify_totals_against_burned_report(
    rows: Sequence[TaxonomyRow], formal_report: Mapping[str, Any]
) -> None:
    """The unresolved rows explained here must be exactly the burned run's counts."""
    observed = {
        "resolved_episodes": sum(r.resolved for r in rows if r.role == "primary"),
        "unresolved_episodes": sum(not r.resolved for r in rows if r.role == "primary"),
        "resolved_controls": sum(r.resolved for r in rows if r.role == "control"),
        "unresolved_controls": sum(not r.resolved for r in rows if r.role == "control"),
    }
    expected = {name: formal_report[name] for name in observed}
    if observed != expected:
        raise ValueError(f"totals differ from the burned report: {observed} != {expected}")


def _row_tuple(row: TaxonomyRow) -> tuple[Any, ...]:
    diagnosis = row.diagnosis
    return (
        row.role,
        row.primary_decision_id,
        row.decision_id,
        row.exchange,
        row.iso_week,
        row.canonical_asset,
        row.decision_at,
        row.resolved,
        diagnosis.primary_reason,
        diagnosis.missing_entry_bar,
        diagnosis.entry_bar_incomplete,
        diagnosis.internal_gap,
        diagnosis.missing_horizon_bar,
        diagnosis.price_incomplete_inside,
        diagnosis.exit_bar_incomplete,
        diagnosis.entry_exit_bars_complete,
        diagnosis.collector_gap_reported,
        diagnosis.first_problem_minute,
        diagnosis.first_gap_length,
        diagnosis.missing_minutes,
        diagnosis.incomplete_minutes,
    )


def _publish(output_dir: Path, rows: Sequence[TaxonomyRow], report: dict[str, Any]) -> None:
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite taxonomy bundle: {output_dir}")
    staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=output_dir.parent))
    try:
        rows_path = staging / "taxonomy_rows.parquet"
        with duckdb.connect(":memory:") as db:
            db.execute(
                """
                CREATE TABLE taxonomy_rows (
                    role VARCHAR, primary_decision_id VARCHAR, decision_id VARCHAR,
                    exchange VARCHAR, iso_week VARCHAR, canonical_asset VARCHAR,
                    decision_at TIMESTAMPTZ, resolved BOOLEAN, primary_reason VARCHAR,
                    missing_entry_bar BOOLEAN, entry_bar_incomplete BOOLEAN,
                    internal_gap BOOLEAN, missing_horizon_bar BOOLEAN,
                    price_incomplete_inside BOOLEAN, exit_bar_incomplete BOOLEAN,
                    entry_exit_bars_complete BOOLEAN, collector_gap_reported BOOLEAN,
                    first_problem_minute INTEGER, first_gap_length INTEGER,
                    missing_minutes INTEGER, incomplete_minutes INTEGER
                )
                """
            )
            db.executemany(
                f"INSERT INTO taxonomy_rows VALUES ({', '.join('?' * 21)})",  # noqa: S608
                [_row_tuple(row) for row in rows],
            )
            db.execute(
                "COPY (SELECT * FROM taxonomy_rows ORDER BY role, primary_decision_id, "
                "decision_at, decision_id) TO ? (FORMAT PARQUET, COMPRESSION ZSTD)",
                [str(rows_path)],
            )
        report["artifacts"] = {"taxonomy_rows.parquet": _hash_file(rows_path)}
        report["bundle_fingerprint"] = bundle_fingerprint(
            {key: value for key, value in report.items() if key != "artifacts"}
        )
        report_path = staging / "taxonomy_report.json"
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")
        (staging / "taxonomy_report.sha256").write_text(_hash_file(report_path) + "\n")
        staging.rename(output_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def run_taxonomy(
    *,
    snapshot_dir: Path,
    contract_path: Path,
    scan_manifest_path: Path,
    coverage_bundle: Path,
    formal_report_path: Path,
    cold_bars_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite taxonomy bundle: {output_dir}")
    started_at = time.monotonic()

    def announce(message: str) -> None:
        sys.stderr.write(f"[taxonomy +{time.monotonic() - started_at:.1f}s] {message}\n")
        sys.stderr.flush()

    burned_report_hash_before = _hash_file(formal_report_path)
    contract, contract_hash, contract_file_hash = _load_registered_contract(contract_path)
    snapshot = SnapshotReader(snapshot_dir, snapshot_dir.name)
    if snapshot.manifest.identity.get("contract_hash") != contract_hash:
        raise ValueError("snapshot contract hash mismatch")
    episodes = snapshot.load_episodes()
    controls = snapshot.load_controls()
    resolved_by_key, coverage_report_hash, _coverage_report = _load_saved_coverage(
        coverage_bundle,
        expected_provenance={
            "snapshot_fingerprint": snapshot_dir.name,
            "snapshot_manifest_sha256": _hash_file(snapshot_dir / "snapshot_manifest.json"),
            "contract_hash": contract_hash,
            "scan_manifest_hash": _hash_file(scan_manifest_path),
            "burned_formal_report_hash": burned_report_hash_before,
        },
    )
    announce(f"loaded {len(episodes)} episodes and {len(resolved_by_key)} saved coverage rows")

    paths = _verified_cold_bar_paths(cold_bars_dir, scan_manifest_path, dependency_bounds(contract))
    decisions = list(episodes) + [c for items in controls.values() for c in items]
    window_start = min(d.decision_at for d in decisions)
    window_end = max(d.decision_at for d in decisions) + timedelta(
        minutes=contract.outcome_horizon_minutes + 2
    )
    announce(f"reading bar presence for {len({_route(d) for d in decisions})} routes")
    bars = load_bar_states(
        paths,
        (_route(d) for d in decisions),
        window_start=window_start,
        window_end=window_end,
    )
    rows = build_rows(
        episodes,
        controls,
        resolved_by_key,
        bars,
        outcome_horizon_minutes=contract.outcome_horizon_minutes,
    )
    verify_totals_against_burned_report(rows, json.loads(formal_report_path.read_text()))
    summary = summarize(rows)
    if _hash_file(formal_report_path) != burned_report_hash_before:
        raise RuntimeError("the burned formal report changed during the run")
    report: dict[str, Any] = {
        "diagnostic_version": TAXONOMY_VERSION,
        "classification": "post_hoc_burned_window_diagnostic_not_formal_evidence",
        "generated_at": datetime.now(UTC).isoformat(),
        "reads": "bar presence, price_complete, unbackfilled_gap_minutes; no price column",
        "provenance": {
            "snapshot_fingerprint": snapshot_dir.name,
            "snapshot_manifest_sha256": _hash_file(snapshot_dir / "snapshot_manifest.json"),
            "contract_hash": contract_hash,
            "contract_file_hash": contract_file_hash,
            "scan_manifest_hash": _hash_file(scan_manifest_path),
            "coverage_report_sha256": coverage_report_hash,
            "burned_formal_report_sha256": burned_report_hash_before,
            "cold_bar_files": len(paths),
            "scanned_window": [window_start.isoformat(), window_end.isoformat()],
            "run": _run_code_state(),
        },
        "reason_order": list(REASONS),
        "first_problem_buckets": [label for label, _low, _high in FIRST_PROBLEM_BUCKETS],
        **summary,
    }
    _publish(output_dir, rows, report)
    announce(f"complete: {output_dir}")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--cold-bars-dir", type=Path, required=True)
    parser.add_argument("--coverage-bundle", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--contract",
        type=Path,
        default=Path("docs/research/evidence/abnormal-flow-v1/formal/contract.json"),
    )
    parser.add_argument(
        "--scan-manifest",
        type=Path,
        default=Path(
            "docs/research/evidence/abnormal-flow-v1/20260921T062152Z-96acbc87/manifest.json"
        ),
    )
    parser.add_argument(
        "--formal-report",
        type=Path,
        default=Path(
            "docs/research/evidence/abnormal-flow-v1/diagnostic-v1-burned/formal_run_report.json"
        ),
    )
    return parser


def main() -> None:
    args: Any = build_parser().parse_args()
    report = run_taxonomy(
        snapshot_dir=args.snapshot_dir,
        contract_path=args.contract,
        scan_manifest_path=args.scan_manifest,
        coverage_bundle=args.coverage_bundle,
        formal_report_path=args.formal_report,
        cold_bars_dir=args.cold_bars_dir,
        output_dir=args.output_dir,
    )
    sys.stdout.write(
        json.dumps(
            {
                "output_dir": str(args.output_dir),
                "primary": report["primary"]["primary_reason"],
                "control": report["control"]["primary_reason"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
