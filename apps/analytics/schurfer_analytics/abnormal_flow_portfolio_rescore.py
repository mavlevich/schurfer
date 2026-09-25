"""Rescore a persisted abnormal-flow position bundle without rereading market data."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from .abnormal_flow_formal_runner import _hash_file
from .abnormal_flow_portfolio_diagnostic import (
    DEFAULT_K_VALUES,
    _parse_k_values,
    _run_code_state,
    _write_bundle,
    load_positions,
    portfolio_frontier,
)
from .portfolio_engine_v2 import PositionSizingPolicy

RESCORE_VERSION: Final = "abnormal_flow_portfolio_rescore_v1"


def rescore_bundle(
    source_bundle: Path,
    output_dir: Path,
    *,
    sizing_policy: PositionSizingPolicy = PositionSizingPolicy.CURRENT_EQUITY_EQUAL_WEIGHT,
    k_values: tuple[int, ...] = DEFAULT_K_VALUES,
) -> dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite diagnostic bundle: {output_dir}")
    source_report_path = source_bundle / "diagnostic_report.json"
    source_hash_path = source_bundle / "diagnostic_report.sha256"
    positions_path = source_bundle / "portfolio_positions.parquet"
    source_report_hash = _hash_file(source_report_path)
    if source_hash_path.read_text().strip() != source_report_hash:
        raise ValueError("source diagnostic report hash mismatch")
    source_report = json.loads(source_report_path.read_text())
    if not isinstance(source_report, dict):
        raise ValueError("source diagnostic report must be an object")
    expected_positions_hash = source_report.get("artifacts", {}).get("portfolio_positions.parquet")
    observed_positions_hash = _hash_file(positions_path)
    if expected_positions_hash != observed_positions_hash:
        raise ValueError("source position artifact hash mismatch")

    policy = source_report.get("policy")
    if not isinstance(policy, dict):
        raise ValueError("source diagnostic report has no policy")
    initial_capital = float(policy["initial_capital"])
    max_positions_per_asset = int(policy["max_positions_per_asset"])
    positions = load_positions(positions_path)
    resolved_positions = [position for position in positions if position.exit_at is not None]
    full_frontier = portfolio_frontier(
        positions,
        initial_capital=initial_capital,
        k_values=k_values,
        max_positions_per_asset=max_positions_per_asset,
        sizing_policy=sizing_policy,
    )
    resolved_frontier = portfolio_frontier(
        resolved_positions,
        initial_capital=initial_capital,
        k_values=k_values,
        max_positions_per_asset=max_positions_per_asset,
        sizing_policy=sizing_policy,
    )
    report: dict[str, Any] = {
        "diagnostic_version": RESCORE_VERSION,
        "classification": "post_hoc_burned_window_diagnostic_not_formal_evidence",
        "generated_at": datetime.now(UTC).isoformat(),
        "provenance": {
            "source_bundle": str(source_bundle),
            "source_report_sha256": source_report_hash,
            "source_positions_sha256": observed_positions_hash,
            "source_snapshot_fingerprint": source_report.get("provenance", {}).get(
                "snapshot_fingerprint"
            ),
            "source_outcomes_fingerprint": source_report.get("outcomes_fingerprint"),
            "run": _run_code_state(),
        },
        "policy": {
            "initial_capital": initial_capital,
            "k_values": sorted(k_values),
            "max_positions_per_asset": max_positions_per_asset,
            "sizing": sizing_policy.value,
            "unresolved_outcomes": "fail_closed_and_reserve_capital",
        },
        "coverage": source_report.get("coverage"),
        "portfolio_frontier": full_frontier,
        "resolved_only_sensitivity": {
            "classification": "conditional_on_resolved_outcomes_not_evidence",
            "positions": len(resolved_positions),
            "portfolio_frontier": resolved_frontier,
        },
    }
    _write_bundle(
        output_dir,
        write_positions=lambda path: shutil.copyfile(positions_path, path),
        report=report,
    )
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-bundle", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--k-values", type=_parse_k_values, default=DEFAULT_K_VALUES)
    parser.add_argument(
        "--sizing-policy",
        type=PositionSizingPolicy,
        choices=tuple(PositionSizingPolicy),
        default=PositionSizingPolicy.CURRENT_EQUITY_EQUAL_WEIGHT,
    )
    return parser


def main() -> None:
    args: Any = build_parser().parse_args()
    sys.stderr.write("[rescore] verifying source bundle and loading saved positions\n")
    sys.stderr.flush()
    report = rescore_bundle(
        args.source_bundle,
        args.output_dir,
        sizing_policy=args.sizing_policy,
        k_values=args.k_values,
    )
    sys.stdout.write(
        json.dumps(
            {
                "output_dir": str(args.output_dir),
                "sizing": report["policy"]["sizing"],
                "k_values": report["policy"]["k_values"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
