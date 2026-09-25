"""Diagnose burned-window abnormal-flow outcome and control coverage."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean, median
from typing import TYPE_CHECKING, Any, Final

import duckdb

from .abnormal_flow_formal_runner import (
    _hash_file,
    _load_registered_contract,
    _load_registered_evaluation_manifest,
    _verified_cold_bar_paths,
    dependency_bounds,
)
from .abnormal_flow_portfolio_diagnostic import _read_outcomes_once, _run_code_state
from .abnormal_flow_snapshots import SnapshotReader, decision_id

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from .abnormal_flow_replay import DecisionFeatures, RouteKey

COVERAGE_DIAGNOSTIC_VERSION: Final = "abnormal_flow_coverage_diagnostic_v1"


def _coverage(total: int, resolved: int) -> dict[str, int | float]:
    return {
        "total": total,
        "resolved": resolved,
        "unresolved": total - resolved,
        "resolved_fraction": resolved / total if total else 0.0,
    }


def _grouped_coverage(rows: Iterable[tuple[tuple[str, ...], bool]]) -> list[dict[str, Any]]:
    counts: dict[tuple[str, ...], list[int]] = defaultdict(lambda: [0, 0])
    for key, resolved in rows:
        counts[key][0] += 1
        counts[key][1] += int(resolved)
    return [
        {"group": list(key), **_coverage(total, resolved)}
        for key, (total, resolved) in sorted(counts.items())
    ]


def _feature_summary(
    episodes: Sequence[DecisionFeatures], resolved_keys: set[RouteKey]
) -> dict[str, Any]:
    features = ("oi_growth_pct", "buy_pressure", "containment", "pre_decision_turnover_usd")
    result: dict[str, Any] = {}
    for feature in features:
        split: dict[str, list[float]] = {"resolved": [], "unresolved": []}
        for episode in episodes:
            value = getattr(episode, feature)
            if value is not None:
                bucket = "resolved" if episode.route_key() in resolved_keys else "unresolved"
                split[bucket].append(float(value))
        result[feature] = {
            name: {
                "count": len(values),
                "mean": fmean(values) if values else None,
                "median": median(values) if values else None,
            }
            for name, values in split.items()
        }
    return result


def summarize_coverage(
    episodes: Sequence[DecisionFeatures],
    controls_by_primary: Mapping[str, Sequence[DecisionFeatures]],
    resolved_keys: set[RouteKey],
) -> dict[str, Any]:
    primary_rows = [
        (
            episode,
            episode.route_key() in resolved_keys,
        )
        for episode in episodes
    ]
    control_rows = [
        (primary_id, control, control.route_key() in resolved_keys)
        for primary_id, controls in controls_by_primary.items()
        for control in controls
    ]
    resolved_primary = sum(resolved for _, resolved in primary_rows)
    resolved_controls = sum(resolved for _, _, resolved in control_rows)
    primaries_with_resolved_control = {
        primary_id for primary_id, _control, resolved in control_rows if resolved
    }
    resolved_primary_ids = {decision_id(episode) for episode, resolved in primary_rows if resolved}
    resolved_primaries_with_resolved_control = (
        primaries_with_resolved_control & resolved_primary_ids
    )
    return {
        "primary": {
            "overall": _coverage(len(primary_rows), resolved_primary),
            "by_exchange": _grouped_coverage(
                ((episode.exchange,), resolved) for episode, resolved in primary_rows
            ),
            "by_utc_week": _grouped_coverage(
                ((episode.iso_week,), resolved) for episode, resolved in primary_rows
            ),
            "by_exchange_utc_week": _grouped_coverage(
                ((episode.exchange, episode.iso_week), resolved)
                for episode, resolved in primary_rows
            ),
            "feature_comparison": _feature_summary(episodes, resolved_keys),
        },
        "controls": {
            "overall": _coverage(len(control_rows), resolved_controls),
            "by_exchange": _grouped_coverage(
                ((control.exchange,), resolved) for _, control, resolved in control_rows
            ),
            "by_utc_week": _grouped_coverage(
                ((control.iso_week,), resolved) for _, control, resolved in control_rows
            ),
            "by_exchange_utc_week": _grouped_coverage(
                ((control.exchange, control.iso_week), resolved)
                for _, control, resolved in control_rows
            ),
            "primary_episodes_with_at_least_one_resolved_control": (
                len(primaries_with_resolved_control)
            ),
            "resolved_primary_episodes_with_at_least_one_resolved_control": (
                len(resolved_primaries_with_resolved_control)
            ),
            "primary_episode_coverage_fraction": (
                len(primaries_with_resolved_control) / len(episodes) if episodes else 0.0
            ),
            "resolved_primary_episode_coverage_fraction": (
                len(resolved_primaries_with_resolved_control) / len(resolved_primary_ids)
                if resolved_primary_ids
                else 0.0
            ),
        },
    }


def _verify_expected_totals(summary: dict[str, Any], formal_report_path: Path) -> None:
    formal = json.loads(formal_report_path.read_text())
    expected = {
        "resolved_episodes": formal["resolved_episodes"],
        "unresolved_episodes": formal["unresolved_episodes"],
        "resolved_controls": formal["resolved_controls"],
        "unresolved_controls": formal["unresolved_controls"],
        "episodes_with_matched_control": formal["episodes_with_matched_control"],
    }
    primary = summary["primary"]["overall"]
    controls = summary["controls"]
    observed = {
        "resolved_episodes": primary["resolved"],
        "unresolved_episodes": primary["unresolved"],
        "resolved_controls": controls["overall"]["resolved"],
        "unresolved_controls": controls["overall"]["unresolved"],
        "episodes_with_matched_control": controls[
            "resolved_primary_episodes_with_at_least_one_resolved_control"
        ],
    }
    if observed != expected:
        raise ValueError(f"coverage totals differ from burned report: {observed} != {expected}")


def _coverage_rows(
    episodes: Sequence[DecisionFeatures],
    controls_by_primary: Mapping[str, Sequence[DecisionFeatures]],
    resolved_keys: set[RouteKey],
) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = [
        (
            "primary",
            None,
            decision_id(episode),
            episode.exchange,
            episode.iso_week,
            episode.canonical_asset,
            episode.decision_at,
            episode.route_key() in resolved_keys,
            None
            if episode.route_key() in resolved_keys
            else "missing_or_incomplete_priced_proxy_path",
        )
        for episode in episodes
    ]
    rows.extend(
        (
            "control",
            primary_id,
            decision_id(control),
            control.exchange,
            control.iso_week,
            control.canonical_asset,
            control.decision_at,
            control.route_key() in resolved_keys,
            None
            if control.route_key() in resolved_keys
            else "missing_or_incomplete_priced_proxy_path",
        )
        for primary_id, controls in controls_by_primary.items()
        for control in controls
    )
    return rows


def _publish(
    output_dir: Path, report: dict[str, Any], coverage_rows: Sequence[tuple[Any, ...]]
) -> None:
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite diagnostic bundle: {output_dir}")
    staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=output_dir.parent))
    try:
        rows_path = staging / "coverage_rows.parquet"
        with duckdb.connect(":memory:") as db:
            db.execute(
                """
                CREATE TABLE coverage_rows (
                    role VARCHAR, primary_decision_id VARCHAR, decision_id VARCHAR,
                    exchange VARCHAR, iso_week VARCHAR, canonical_asset VARCHAR,
                    decision_at TIMESTAMPTZ, resolved BOOLEAN, unresolved_reason VARCHAR
                )
                """
            )
            db.executemany(
                "INSERT INTO coverage_rows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                coverage_rows,
            )
            db.execute(
                "COPY (SELECT * FROM coverage_rows ORDER BY role, primary_decision_id, "
                "decision_at, decision_id) TO ? (FORMAT PARQUET, COMPRESSION ZSTD)",
                [str(rows_path)],
            )
        report["artifacts"] = {"coverage_rows.parquet": _hash_file(rows_path)}
        report_path = staging / "coverage_diagnostic.json"
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        (staging / "coverage_diagnostic.sha256").write_text(_hash_file(report_path) + "\n")
        staging.rename(output_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def run_coverage_diagnostic(
    *,
    snapshot_dir: Path,
    contract_path: Path,
    evaluation_manifest_path: Path,
    scan_manifest_path: Path,
    formal_report_path: Path,
    cold_bars_dir: Path,
    output_dir: Path,
    burned_window_diagnostic: bool,
) -> dict[str, Any]:
    if not burned_window_diagnostic:
        raise ValueError("refusing returns read without --burned-window-diagnostic")
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite diagnostic bundle: {output_dir}")
    started_at = time.monotonic()

    def announce(message: str) -> None:
        sys.stderr.write(f"[coverage +{time.monotonic() - started_at:.1f}s] {message}\n")
        sys.stderr.flush()

    contract, contract_hash, contract_file_hash = _load_registered_contract(contract_path)
    evaluation_manifest, evaluation_manifest_file_hash = _load_registered_evaluation_manifest(
        evaluation_manifest_path
    )
    scan_manifest_hash = _hash_file(scan_manifest_path)
    if scan_manifest_hash != evaluation_manifest.input_audit_fingerprint:
        raise ValueError("scan manifest does not match the registered evaluation manifest")

    announce("verifying immutable snapshot")
    snapshot = SnapshotReader(snapshot_dir, snapshot_dir.name)
    if snapshot.manifest.identity.get("contract_hash") != contract_hash:
        raise ValueError("snapshot contract hash mismatch")
    episodes = snapshot.load_episodes()
    controls_by_primary = snapshot.load_controls()
    requested = {episode.route_key(): episode for episode in episodes}
    for controls in controls_by_primary.values():
        requested.update((control.route_key(), control) for control in controls)
    announce(
        f"loaded {len(episodes)} primary episodes and "
        f"{sum(len(items) for items in controls_by_primary.values())} control rows"
    )

    def cold_bar_progress(done: int, total: int, day: Any) -> None:
        announce(f"verified cold-bars {done}/{total}: {day}")

    paths = _verified_cold_bar_paths(
        cold_bars_dir,
        scan_manifest_path,
        dependency_bounds(contract),
        progress=cold_bar_progress,
    )

    def outcome_progress(scanned_streams: int, matched_routes: int) -> None:
        announce(
            f"outcome scan: {scanned_streams} instrument streams, "
            f"{matched_routes} requested routes matched"
        )

    announce(f"reading outcomes for {len(requested)} distinct requested routes")
    outcomes = _read_outcomes_once(
        paths,
        list(requested.values()),
        outcome_horizon_minutes=contract.outcome_horizon_minutes,
        progress=outcome_progress,
    )
    summary = summarize_coverage(episodes, controls_by_primary, set(outcomes))
    _verify_expected_totals(summary, formal_report_path)
    report = {
        "diagnostic_version": COVERAGE_DIAGNOSTIC_VERSION,
        "classification": "post_hoc_burned_window_diagnostic_not_formal_evidence",
        "generated_at": datetime.now(UTC).isoformat(),
        "provenance": {
            "snapshot_fingerprint": snapshot_dir.name,
            "snapshot_manifest_sha256": _hash_file(snapshot_dir / "snapshot_manifest.json"),
            "snapshot_working_tree_dirty": snapshot.manifest.working_tree_dirty,
            "contract_hash": contract_hash,
            "contract_file_hash": contract_file_hash,
            "evaluation_manifest_file_hash": evaluation_manifest_file_hash,
            "scan_manifest_hash": scan_manifest_hash,
            "burned_formal_report_hash": _hash_file(formal_report_path),
            "run": _run_code_state(),
        },
        "limitation": (
            "unresolved means missing or incomplete continuous native priced-proxy path; "
            "reason subtypes are not available from the v1 outcome reader"
        ),
        **summary,
    }
    _publish(
        output_dir,
        report,
        _coverage_rows(episodes, controls_by_primary, set(outcomes)),
    )
    announce(f"complete: {output_dir}")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--cold-bars-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--contract",
        type=Path,
        default=Path("docs/research/evidence/abnormal-flow-v1/formal/contract.json"),
    )
    parser.add_argument(
        "--evaluation-manifest",
        type=Path,
        default=Path("docs/research/evidence/abnormal-flow-v1/formal/evaluation_manifest.json"),
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
    parser.add_argument("--burned-window-diagnostic", action="store_true")
    return parser


def main() -> None:
    args: Any = build_parser().parse_args()
    report = run_coverage_diagnostic(
        snapshot_dir=args.snapshot_dir,
        contract_path=args.contract,
        evaluation_manifest_path=args.evaluation_manifest,
        scan_manifest_path=args.scan_manifest,
        formal_report_path=args.formal_report,
        cold_bars_dir=args.cold_bars_dir,
        output_dir=args.output_dir,
        burned_window_diagnostic=args.burned_window_diagnostic,
    )
    sys.stdout.write(
        json.dumps(
            {
                "output_dir": str(args.output_dir),
                "primary": report["primary"]["overall"],
                "controls": report["controls"]["overall"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
