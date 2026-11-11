"""Registered pre-entry dependence calibration for HYP-037 A and B."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path
from statistics import NormalDist, variance
from typing import TYPE_CHECKING, Any, Final

from . import bybit_squeeze_continuation as sq
from .bybit_carry_feasibility import publish_once
from .bybit_compressed_spring import FIRST_DECISION_MS, HOUR_MS, Features, load_features
from .bybit_post_funding_screen import verified
from .bybit_squeeze_policy import (
    ABSOLUTE_TAIL,
    HYPOTHESIS_ID,
    MIN_COMPLETE,
    MIN_NET,
    MIN_VALID_DRAWS,
    _source_binding,
    paired_week_samples,
    week_key,
)
from .clustered_inference import derived_seed

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

VERSION: Final = "hyp037_power_calibration_v1"
COUNTS_SHA256: Final = "a9ef438e49c8bbf64a2c1c634b0f213f129ae3fc439bd9f3d591e562e5e16169"
EVENTS_GZIP_SHA256: Final = "0e9ba16f5131a9afb332481c7bcc29f6361d165972b505d5ffea91515207850b"
ITERATIONS: Final = 10_000
SEED: Final = derived_seed(33, VERSION)
# name, endpoint offset from decision d in hours, duration in hours
PROXIES: Final = (
    *((f"pre_{lag}h", 2 + lag, 4) for lag in range(0, 145, 24)),
    ("waiting_bar", 0, 1),
)
LIMITATION: Final = (
    "Conditional planning estimate transferring pre-entry dependence to a four-hour hold; "
    "post-entry dependence can differ and 51-week dependence estimates remain noisy."
)
Opportunity = tuple[str, int, bool]


def calibration_contract() -> dict[str, Any]:
    return {
        "hypothesis_id": HYPOTHESIS_ID,
        "stage": "preentry_dependence_only",
        "rules_version": VERSION,
        "features_sha256": sq.FEATURES_SHA256,
        "counts_sha256": COUNTS_SHA256,
        "events_gzip_sha256": EVENTS_GZIP_SHA256,
        "proxies": [
            {"name": name, "end_offset_hours": offset, "span_hours": span}
            for name, offset, span in PROXIES
        ],
        "minimum_complete": MIN_COMPLETE,
        "minimum_defined_symbol_days": sq.FLOOR_CLUSTERS,
        "minimum_weeks": sq.FLOOR_WEEKS,
        "all_original_weeks_required": True,
        "iterations": ITERATIONS,
        "seed": SEED,
        "minimum_valid_draws": MIN_VALID_DRAWS,
        "design_effect_floor": sq.DESIGN_EFFECT,
        "design_effect_aggregation": "maximum_of_all_eight_proxies_and_floor",
        "inflation": sq.INFLATION,
        "absolute_tail": ABSOLUTE_TAIL,
        "economic_minimum_net": MIN_NET,
        "planning_target_power": 0.80,
        "assumed_true_net_for_power": [0.003, 0.005],
        "mde_limit": sq.MDE_MAX,
        "above_limit": "stop_candidate_without_outcomes",
        "unestimable": "block_candidate_without_outcomes",
    }


def _proxy_effect(
    rows: Sequence[tuple[str, float, bool]],
    intended_weeks: set[str],
    intended_count: int,
    samples: tuple[float, ...],
    iterations: int,
) -> dict[str, Any]:
    values = [value for _, value, _ in rows]
    missing_weeks = sorted(intended_weeks - {week for week, _, _ in rows})
    checks = {
        "complete": intended_count > 0 and len(values) / intended_count >= MIN_COMPLETE,
        "mature": len(values) >= sq.FLOOR_CLUSTERS,
        "weeks": len(intended_weeks) >= sq.FLOOR_WEEKS and not missing_weeks,
        "draws": len(samples) / iterations >= MIN_VALID_DRAWS,
    }
    independent = variance(values) / len(values) if len(values) > 1 else None
    clustered = variance(samples) if len(samples) > 1 else None
    checks["finite_variance"] = (
        independent is not None
        and math.isfinite(independent)
        and independent > 0
        and clustered is not None
        and math.isfinite(clustered)
        and clustered >= 0
    )
    effect = None
    if all(checks.values()) and clustered is not None and independent is not None:
        effect = clustered / independent
    return {
        "status": "estimable" if effect is not None else "not_estimable",
        "defined": len(values),
        "intended": intended_count,
        "valid_draws": len(samples),
        "missing_weeks": missing_weeks,
        "checks": checks,
        "design_effect": effect,
    }


def _decision(base_mde: float, effects: Sequence[float | None]) -> dict[str, Any]:
    available = [effect for effect in effects if effect is not None]
    report: dict[str, Any] = {
        "proxy_effect_min": min(available) if available else None,
        "proxy_effect_max": max(available) if available else None,
        "limitation": LIMITATION,
    }
    if len(available) != len(PROXIES):
        return report | {
            "verdict": "not_estimable",
            "next_action": "block_candidate_without_outcomes",
        }
    effect = max(sq.DESIGN_EFFECT, *available)
    mde = base_mde * math.sqrt(effect / sq.DESIGN_EFFECT)
    normal = NormalDist()
    critical = normal.inv_cdf(1 - ABSOLUTE_TAIL)
    se = mde / (critical + normal.inv_cdf(0.80))
    economic_boundary = max(MIN_NET, critical * se)
    power = {
        f"at_{bps}_bps": {
            "detect_zero": normal.cdf((bps / 10_000) / se - critical),
            "pass_both_net_conditions": normal.cdf((bps / 10_000 - economic_boundary) / se),
        }
        for bps in (30, 50)
    }
    within = mde <= sq.MDE_MAX
    return report | {
        "calibrated_design_effect": effect,
        "mde": mde,
        "mde_bps": mde * 10_000,
        "normal_approximation_power": power,
        "verdict": "planning_gate_pass" if within else "insufficient_power",
        "next_action": "review_outcome_contract" if within else "stop_candidate_without_outcomes",
    }


def calibrate(
    features: Features,
    opportunities: Sequence[Opportunity],
    base_mde: Mapping[str, float],
    *,
    iterations: int = ITERATIONS,
    seed: int = SEED,
) -> dict[str, Any]:
    if len({(s, d) for s, d, _ in opportunities}) != len(opportunities):
        raise ValueError("duplicate frozen event")
    if iterations < 100 or seed < 0:
        raise ValueError("invalid bootstrap configuration")
    for symbol, d, member in opportunities:
        if (
            not symbol
            or not isinstance(member, bool)
            or isinstance(d, bool)
            or not FIRST_DECISION_MS <= d <= sq.LAST_DECISION_MS
            or d % HOUR_MS
        ):
            raise ValueError("invalid frozen event or decision boundary")
    for name in ("A", "B"):
        if not math.isfinite(base_mde[name]) or base_mde[name] <= 0:
            raise ValueError("base MDE must be finite and positive")
    intended = {
        name: [(s, d, member) for s, d, member in opportunities if name == "A" or member]
        for name in ("A", "B")
    }
    report: dict[str, Any] = {
        "version": VERSION,
        "stage": "preentry_dependence_only",
        "iterations": iterations,
        "seed": seed,
        "proxies": {},
    }
    for label, offset, span in PROXIES:
        rows = []
        for symbol, d, member in opportunities:
            series = features.instruments.get(symbol)
            value = sq.change(series, d - offset * HOUR_MS, span) if series else None
            if value is not None and math.isfinite(value):
                rows.append((week_key(d), value, member))
        samples = paired_week_samples(rows, iterations=iterations, seed=seed)
        report["proxies"][label] = {"span_hours": span, "end_offset_hours": offset}
        for name in ("A", "B"):
            subset = [row for row in rows if name == "A" or row[2]]
            report["proxies"][label][name] = _proxy_effect(
                subset,
                {week_key(d) for _, d, _ in intended[name]},
                len(intended[name]),
                samples[name],
                iterations,
            )
    report["candidates"] = {
        name: _decision(
            base_mde[name], [proxy[name]["design_effect"] for proxy in report["proxies"].values()]
        )
        for name in ("A", "B")
    }
    return report


def _frozen_inputs(directory: Path) -> tuple[dict[str, Any], list[Opportunity]]:
    counts = json.loads(verified(directory / "counts.json", COUNTS_SHA256).read_text())
    verified(directory / "run.json", counts["run_sha256"])
    body = gzip.decompress(verified(directory / "events.json.gz", EVENTS_GZIP_SHA256).read_bytes())
    if hashlib.sha256(body).hexdigest() != counts["events_sha256"]:
        raise ValueError("decompressed frozen events hash mismatch")
    manifest = json.loads(body)
    if counts["hypothesis_id"] != HYPOTHESIS_ID or manifest["run_sha256"] != counts["run_sha256"]:
        raise ValueError("foreign frozen input binding")
    opportunities = [(e["symbol"], e["decision_ms"], e["in_B"]) for e in manifest["events"]]
    if (
        len(opportunities) != counts["candidates"]["A"]["shape"]["impulses"]
        or sum(member for _, _, member in opportunities)
        != counts["candidates"]["B"]["shape"]["impulses"]
    ):
        raise ValueError("frozen counts and membership mismatch")
    return counts, opportunities


def run_calibration(
    *,
    features_path: Path,
    preflight_dir: Path,
    registration_path: Path,
    registration_sha256: str,
    protocol_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    registration = json.loads(verified(registration_path, registration_sha256).read_text())
    if not isinstance(registration, dict) or registration.get("contract") != calibration_contract():
        raise ValueError("calibration registration contract mismatch")
    protocol_sha = registration["protocol_sha256"]
    verified(protocol_path, protocol_sha)
    sources = _source_binding(extra_source_names=(Path(__file__).name,))
    if sources["working_tree_dirty"]:
        raise ValueError("calibration requires a clean committed revision")
    counts, opportunities = _frozen_inputs(preflight_dir)
    verified(features_path, sq.FEATURES_SHA256)
    output_dir.mkdir(parents=True, exist_ok=False)
    binding = {
        "contract": calibration_contract(),
        "registration_sha256": registration_sha256,
        "protocol_sha256": protocol_sha,
        "sources": sources,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    run_sha = publish_once(output_dir / "run.json", binding)
    base = {name: counts["candidates"][name]["power"]["mde"] for name in ("A", "B")}
    report = calibrate(load_features(features_path), opportunities, base)
    if _source_binding(extra_source_names=(Path(__file__).name,)) != sources:
        raise ValueError("source changed; calibration remains unsealed")
    verified(features_path, sq.FEATURES_SHA256)
    verified(registration_path, registration_sha256)
    verified(protocol_path, protocol_sha)
    _frozen_inputs(preflight_dir)
    report |= {
        "hypothesis_id": HYPOTHESIS_ID,
        "run_sha256": run_sha,
        "registration_sha256": registration_sha256,
        "protocol_sha256": protocol_sha,
        "sources": sources,
    }
    publish_once(output_dir / "calibration.json", report)
    return report


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("features", "preflight-dir", "registration", "protocol", "output-dir"):
        parser.add_argument(f"--{flag}", required=True, type=Path)
    parser.add_argument("--registration-sha256", required=True)
    args = parser.parse_args(argv)
    report = run_calibration(
        features_path=args.features,
        preflight_dir=args.preflight_dir,
        registration_path=args.registration,
        registration_sha256=args.registration_sha256,
        protocol_path=args.protocol,
        output_dir=args.output_dir,
    )
    for name, candidate in report["candidates"].items():
        sys.stdout.write(f"{name}: {candidate['verdict']} mde_bps={candidate.get('mde_bps')}\n")


if __name__ == "__main__":
    main()
