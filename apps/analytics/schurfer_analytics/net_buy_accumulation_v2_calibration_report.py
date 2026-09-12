"""CLI driver for the net-buy accumulation v2 CALIBRATION tool.

Runs the outcome-blind grid on a fixed calibration window, builds the fingerprinted
`CalibrationArtifact`, and applies the frozen deterministic algorithm to pick the
per-primary threshold and derive the prospective window length. It NEVER reads a
return, forms no economic verdict, and `formal_run` is impossible here (the guard
raises). All the human-frozen constants (grids, unresolved rate, sizing margin,
window ceiling) are explicit CLI inputs, not defaults chosen after the fact.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .net_buy_accumulation_v2_calibration import (
    CALIBRATION_TOOL_VERSION,
    PRIMARY_MAG,
    PRIMARY_SHAPE,
    CalibrationArtifact,
    ThresholdCount,
    assert_calibration_only,
    decide_window,
    n_target,
    select_primary,
    summarize_threshold,
)
from .net_buy_accumulation_v2_repository import scan_calibration_grid


def _manifest_hashes(cold_bars_dir: str) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for manifest in sorted(Path(cold_bars_dir).glob("bars-*.manifest.json")):
        try:
            data = json.loads(manifest.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        hashes[manifest.name] = str(data.get("sha256") or data.get("payload_hash") or "")
    return hashes


def _parse_ts(value: str) -> datetime:
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _parse_grid(value: str) -> tuple[float, ...]:
    return tuple(float(x) for x in value.split(",") if x.strip())


def run(
    *,
    cold_bars_dir: str,
    cal_start: datetime,
    cal_end: datetime,
    theta_m_grid: tuple[float, ...],
    theta_s_grid: tuple[float, ...],
    b_completeness_min_fraction: float,
    max_finalization_lag_seconds: int,
    expected_unresolved_rate: float,
    sizing_margin: float,
    max_window_days: float,
    code_revision: str,
    memory_limit: str | None,
    threads: int | None,
) -> dict[str, Any]:
    assert_calibration_only(formal_run=False)  # this tool can only ever be calibration
    glob = str(Path(cold_bars_dir) / "bars-*.parquet")
    grids = {PRIMARY_MAG: theta_m_grid, PRIMARY_SHAPE: theta_s_grid}
    raw = scan_calibration_grid(
        parquet_glob=glob,
        cal_start=cal_start,
        cal_end=cal_end,
        theta_grids=grids,
        b_completeness_min_fraction=b_completeness_min_fraction,
        max_finalization_lag_seconds=max_finalization_lag_seconds,
        memory_limit=memory_limit,
        threads=threads,
    )
    counts: list[ThresholdCount] = [
        summarize_threshold(primary, theta, rows) for (primary, theta), rows in raw.items()
    ]
    counts.sort(key=lambda c: (c.primary, c.theta))

    artifact = CalibrationArtifact(
        tool_version=CALIBRATION_TOOL_VERSION,
        generated_at=datetime.now(UTC).isoformat(),
        code_revision=code_revision,
        calibration_window_start=cal_start.isoformat(),
        calibration_window_end=cal_end.isoformat(),
        b_completeness_min_fraction=b_completeness_min_fraction,
        max_finalization_lag_seconds=max_finalization_lag_seconds,
        baseline_activity_floor_usd=100_000.0,
        theta_m_grid=theta_m_grid,
        theta_s_grid=theta_s_grid,
        cold_bar_manifests=_manifest_hashes(cold_bars_dir),
        counts=tuple(counts),
    )

    # Deterministic algorithm: pick per-primary threshold + derive the window.
    calibration_days = (cal_end - cal_start).total_seconds() / 86400.0
    target = n_target(
        expected_unresolved_rate=expected_unresolved_rate, sizing_margin=sizing_margin
    )
    selections = []
    for primary in (PRIMARY_MAG, PRIMARY_SHAPE):
        by_theta = {c.theta: c.dedup_fires for c in counts if c.primary == primary}
        selections.append(
            select_primary(
                primary,
                by_theta,
                calibration_days=calibration_days,
                n_target_fires=target,
                max_window_days=max_window_days,
            )
        )
    decision = decide_window(selections)

    return {
        "artifact": json.loads(artifact.to_json()),
        "algorithm": {
            "n_target_fires": target,
            "expected_unresolved_rate": expected_unresolved_rate,
            "sizing_margin": sizing_margin,
            "max_window_days": max_window_days,
            "calibration_days": calibration_days,
            "per_primary": [s.__dict__ for s in decision.per_primary],
            "window_days": decision.window_days,
            "too_slow": decision.too_slow,
        },
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cold-bars", required=True)
    p.add_argument("--cal-start", required=True)
    p.add_argument("--cal-end", required=True)
    p.add_argument("--theta-m-grid", default="0.10,0.15,0.20,0.25,0.30")
    p.add_argument("--theta-s-grid", default="0.10,0.15,0.20,0.25,0.35")
    p.add_argument("--b-fraction", type=float, default=0.99)
    p.add_argument("--max-lag-seconds", type=int, default=120)
    p.add_argument("--expected-unresolved-rate", type=float, default=0.05)
    p.add_argument("--sizing-margin", type=float, default=1.5)
    p.add_argument("--max-window-days", type=float, default=120.0)
    p.add_argument("--code-revision", default="unknown")
    p.add_argument("--memory-limit", default=None)
    p.add_argument("--threads", type=int, default=None)
    args = p.parse_args()

    out = run(
        cold_bars_dir=args.cold_bars,
        cal_start=_parse_ts(args.cal_start),
        cal_end=_parse_ts(args.cal_end),
        theta_m_grid=_parse_grid(args.theta_m_grid),
        theta_s_grid=_parse_grid(args.theta_s_grid),
        b_completeness_min_fraction=args.b_fraction,
        max_finalization_lag_seconds=args.max_lag_seconds,
        expected_unresolved_rate=args.expected_unresolved_rate,
        sizing_margin=args.sizing_margin,
        max_window_days=args.max_window_days,
        code_revision=args.code_revision,
        memory_limit=args.memory_limit,
        threads=args.threads,
    )
    sys.stdout.write(json.dumps(out, indent=2, sort_keys=True, default=str) + "\n")


__all__ = ["main", "run"]
