"""Preflight and synthetic event evaluation for the proposed HYP-033 successor.

The preflight CLI reads only the registered historical features snapshot. Outcome
evaluation accepts in-memory inputs, with no downloader or outcome-file reader.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import NormalDist, fmean
from typing import TYPE_CHECKING, Any, Final

from . import bybit_squeeze_continuation as sq
from .bybit_carry_feasibility import publish_once
from .bybit_compressed_spring import (
    FIRST_DECISION_MS,
    HOUR_MS,
    MINUTE_MS,
    RANK_HOURS,
    RANK_MIN_DEFINED,
    SECOND_HALF_MS,
    WINDOW_END_MS,
    WINDOW_FIRST_MS,
    Features,
    load_features,
)
from .bybit_post_funding_screen import file_sha256, verified
from .clustered_inference import derived_seed, percentile

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

VERSION: Final = "hyp033_policy_preflight_v1"
COST_BPS: Final = 30.0
MIN_NET: Final = 0.003
MIN_COMPLETE: Final = 0.95
MIN_VALID_DRAWS: Final = 0.95
ABSOLUTE_TAIL: Final = 0.025  # Two candidates, Bonferroni family alpha 0.05.
DIAGNOSTIC_TAIL: Final = 0.05
SEED: Final = derived_seed(33, VERSION)
EventKey = tuple[str, int]
HYPOTHESIS_ID: Final = "HYP-037"


def preflight_contract() -> dict[str, Any]:
    """Constants checked against the frozen machine registration before parsing."""
    return {
        "hypothesis_id": HYPOTHESIS_ID,
        "stage": "preflight_only",
        "rules_version": VERSION,
        "candidates": ["A", "B"],
        "features_sha256": sq.FEATURES_SHA256,
        "window_first_ms": WINDOW_FIRST_MS,
        "window_end_exclusive_ms": WINDOW_END_MS,
        "first_decision_ms": FIRST_DECISION_MS,
        "last_decision_ms": sq.LAST_DECISION_MS,
        "hold_hours": sq.HOLD_H,
        "entry_delay_ms": MINUTE_MS,
        "min_turnover_usd": sq.MIN_TURNOVER_USD,
        "impulse_min_return": sq.IMPULSE_MIN_RETURN,
        "impulse_min_z": sq.IMPULSE_MIN_Z,
        "first_lookback_hours": sq.FIRST_LOOKBACK_H,
        "g_min_percentile": sq.G_MIN_PCT,
        "rank_hours": RANK_HOURS,
        "rank_min_defined": RANK_MIN_DEFINED,
        "floor_clusters": sq.FLOOR_CLUSTERS,
        "floor_symbols": sq.FLOOR_SYMBOLS,
        "floor_weeks": sq.FLOOR_WEEKS,
        "top5_symbol_share_max": sq.TOP_SHARE_MAX,
        "fg_determinable_min": sq.DETERMINABLE_MIN,
        "scale_substitution_percentile": sq.SUBSTITUTE_PCT,
        "planning_inflation": sq.INFLATION,
        "planning_design_effect": sq.DESIGN_EFFECT,
        "planning_absolute_tail": ABSOLUTE_TAIL,
        "planning_power": 0.80,
        "planning_mde_max": sq.MDE_MAX,
    }


def _source_binding() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[3]
    executable = shutil.which("git")
    if executable is None:
        raise ValueError("Git required to record actual source provenance")
    revision = subprocess.run(  # noqa: S603 - fixed local Git commands
        [executable, "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(  # noqa: S603 - fixed local Git commands
        [executable, "-C", str(root), "status", "--porcelain", "--untracked-files=normal"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    source_files = (
        "bybit_squeeze_policy.py",
        "bybit_squeeze_continuation.py",
        "bybit_compressed_spring.py",
        "clustered_inference.py",
        "bybit_post_funding_screen.py",
        "bybit_carry_feasibility.py",
        "reporting.py",
    )
    package = Path(__file__).resolve().parent
    return {
        "code_revision": revision,
        "working_tree_dirty": bool(status),
        "source_sha256": {name: file_sha256(package / name) for name in source_files},
    }


def run_preflight(
    *,
    features_path: Path,
    registration_path: Path,
    registration_sha256: str,
    protocol_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Validate the registered scope, then publish one bound counts-only run."""
    registration = json.loads(verified(registration_path, registration_sha256).read_text())
    if not isinstance(registration, dict) or registration.get("contract") != preflight_contract():
        raise ValueError("registration contract does not match the running preflight")
    protocol_sha256 = registration.get("protocol_sha256")
    if not isinstance(protocol_sha256, str) or len(protocol_sha256) != 64:
        raise ValueError("registration needs a protocol SHA-256")
    verified(protocol_path, protocol_sha256)
    verified(features_path, sq.FEATURES_SHA256)
    sources = _source_binding()
    output_dir.mkdir(parents=True, exist_ok=False)
    run = {
        "hypothesis_id": HYPOTHESIS_ID,
        "stage": "preflight_only",
        "contract": preflight_contract(),
        "registration_sha256": registration_sha256,
        "protocol_sha256": protocol_sha256,
        "sources": sources,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    run_sha256 = publish_once(output_dir / "run.json", run)
    report, events = build_preflight(load_features(features_path))
    if _source_binding() != sources:
        raise ValueError("code changed during preflight; result remains unsealed")
    verified(registration_path, registration_sha256)
    verified(protocol_path, protocol_sha256)
    verified(features_path, sq.FEATURES_SHA256)
    event_manifest = {
        "hypothesis_id": HYPOTHESIS_ID,
        "run_sha256": run_sha256,
        "events": [
            {"symbol": e.symbol, "decision_ms": e.d, "in_B": e.cell == "event"} for e in events
        ],
    }
    events_sha256 = publish_once(output_dir / "events.json", event_manifest)
    report |= {
        "hypothesis_id": HYPOTHESIS_ID,
        "run_sha256": run_sha256,
        "events_sha256": events_sha256,
        "registration_sha256": registration_sha256,
        "protocol_sha256": protocol_sha256,
        "sources": sources,
    }
    publish_once(output_dir / "counts.json", report)
    return report


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", required=True, type=Path)
    parser.add_argument("--registration", required=True, type=Path)
    parser.add_argument("--registration-sha256", required=True)
    parser.add_argument("--protocol", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    report = run_preflight(
        features_path=args.features,
        registration_path=args.registration,
        registration_sha256=args.registration_sha256,
        protocol_path=args.protocol,
        output_dir=args.output_dir,
    )
    for name, candidate in report["candidates"].items():
        shape, power = candidate["shape"], candidate["power"]
        sys.stdout.write(
            f"{name}: events={shape['impulses']} symbols={shape['symbols']} "
            f"weeks={shape['iso_weeks']} peak_holds={candidate['peak_unconstrained_holds']} "
            f"planning_mde_bps={power.get('mde_bps')}\n"
        )
    sys.stdout.write(f"counts_sha256={file_sha256(args.output_dir / 'counts.json')}\n")


def week_key(ms: int) -> str:
    iso = datetime.fromtimestamp(ms / 1000, UTC).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _validate_events(events: Sequence[sq.Impulse]) -> None:
    keys = [e.key for e in events]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate event identity")
    if len({e.cluster for e in events}) != len(events):
        raise ValueError("more than one first impulse per symbol-day")
    for event in events:
        if not event.symbol.strip():
            raise ValueError("empty instrument identity")
        if not FIRST_DECISION_MS <= event.d <= sq.LAST_DECISION_MS:
            raise ValueError("event outside discovery decision boundary")
        if event.d % HOUR_MS:
            raise ValueError("decision must be on the hourly grid")


def _data_checks(events: Sequence[sq.Impulse]) -> dict[str, bool]:
    shape = sq.shape(events)
    n = shape["clusters"]
    return {
        "clusters": n >= sq.FLOOR_CLUSTERS,
        "symbols": shape["symbols"] >= sq.FLOOR_SYMBOLS,
        "weeks": shape["iso_weeks"] >= sq.FLOOR_WEEKS,
        "top5_symbols": n > 0 and shape["top5_symbol_clusters"] / n <= sq.TOP_SHARE_MAX,
    }


def _peak_holds(events: Sequence[sq.Impulse]) -> int:
    boundaries: list[tuple[int, int]] = []
    for event in events:
        start = event.d + MINUTE_MS
        boundaries.extend(((start, 1), (start + sq.HOLD_H * HOUR_MS, -1)))
    active = peak = 0
    for _, delta in sorted(boundaries):  # Exits (-1) precede entries at the same instant.
        active += delta
        peak = max(peak, active)
    return peak


def _planning_power(
    instruments: Mapping[str, sq.Instrument], events: Sequence[sq.Impulse]
) -> dict[str, Any]:
    if not events:
        return {"status": "not_estimable", "reason": "no_events"}
    raw = {e.key: instruments[e.symbol].scale(e.d - HOUR_MS, relative=False) for e in events}
    if not any(value is not None for value in raw.values()):
        return {"status": "not_estimable", "reason": "no_preentry_scales"}
    scales, substituted = sq.filled(raw)
    corrected_z = NormalDist().inv_cdf(1 - ABSOLUTE_TAIL) + NormalDist().inv_cdf(0.80)
    estimate = sq.mde(sq.weights_net(events), scales, sq.INFLATION, sq.DESIGN_EFFECT)
    corrected = estimate * corrected_z / sq.Z_POWER
    return {
        "status": "planning_estimate",
        "mde": corrected,
        "mde_bps": corrected * 10_000,
        "scales_substituted": substituted,
        "tail": ABSOLUTE_TAIL,
        "inflation": sq.INFLATION,
        "design_effect": sq.DESIGN_EFFECT,
        "within_50_bps": corrected <= sq.MDE_MAX,
        "assumptions": "v1 pre-entry scales and assumed dependence, not observed event power",
    }


def build_preflight(
    features: Features,
    *,
    first_d: int = FIRST_DECISION_MS,
    last_d: int = sq.LAST_DECISION_MS,
) -> tuple[dict[str, Any], tuple[sq.Impulse, ...]]:
    """Scan the existing feature representation, returning counts without outcomes."""
    if not FIRST_DECISION_MS <= first_d <= last_d <= sq.LAST_DECISION_MS:
        raise ValueError("preflight scope outside discovery decision boundary")
    if first_d % HOUR_MS or last_d % HOUR_MS:
        raise ValueError("preflight scope must use whole hours")
    if sq.BENCHMARK not in features.instruments:
        raise ValueError("BTCUSDT missing from features")
    benchmark = features.instruments[sq.BENCHMARK]
    instruments = {
        symbol: sq.Instrument(symbol, series, benchmark)
        for symbol, series in sorted(features.instruments.items())
    }
    funnel: Counter[str] = Counter()
    events = tuple(
        event
        for instrument in instruments.values()
        for event in sq.scan(instrument, funnel, first_d, last_d)
    )
    _validate_events(events)
    filtered = tuple(e for e in events if e.cell == "event")
    determinable = sum(e.f in sq.DETERMINED and e.g in sq.DETERMINED for e in events)
    report: dict[str, Any] = {
        "version": VERSION,
        "stage": "preflight_only",
        "first_decision_ms": first_d,
        "last_decision_ms": last_d,
        "instruments": len(instruments),
        "failed_instruments": sorted(features.failed),
        "funnel": dict(sorted(funnel.items())),
        "fg_determinable": determinable,
        "fg_denominator": len(events),
        "fg_determinable_share": determinable / len(events) if events else None,
        "candidates": {},
    }
    for name, group in (("A", events), ("B", filtered)):
        checks = _data_checks(group)
        if name == "B":
            checks["fg_determinable"] = bool(events) and (
                determinable / len(events) >= sq.DETERMINABLE_MIN
            )
        report["candidates"][name] = {
            "shape": sq.shape(group),
            "data_checks": checks,
            "events_by_week": dict(sorted(Counter(week_key(e.d) for e in group).items())),
            "peak_unconstrained_holds": _peak_holds(group),
            "power": _planning_power(instruments, group),
        }
    return report, tuple(sorted(events, key=lambda e: (e.d, e.symbol)))


@dataclass(frozen=True)
class EventOutcome:
    gross_return: float | None
    funding_return: float | None
    unresolved_reason: str | None = None

    def __post_init__(self) -> None:
        unresolved = self.gross_return is None or self.funding_return is None
        if unresolved != bool(self.unresolved_reason and self.unresolved_reason.strip()):
            raise ValueError("unresolved outcome needs a reason; complete outcome forbids one")
        for value in (self.gross_return, self.funding_return):
            if value is not None and (isinstance(value, bool) or not math.isfinite(value)):
                raise ValueError("outcome must be finite")
        if self.gross_return is not None and self.gross_return < -1:
            raise ValueError("unlevered long gross return cannot be below -1")

    def net(self, cost_bps: float) -> float | None:
        if self.gross_return is None or self.funding_return is None:
            return None
        return self.gross_return + self.funding_return - cost_bps / 10_000


def paired_week_samples(
    rows: Sequence[tuple[str, float, bool]], *, iterations: int = 10_000, seed: int = SEED
) -> dict[str, tuple[float, ...]]:
    """Resample common weeks and recompute event-weighted A, B and their difference."""
    if iterations < 100 or seed < 0:
        raise ValueError("at least 100 draws and a nonnegative seed required")
    by_week: dict[str, list[tuple[float, bool]]] = defaultdict(list)
    for week, net, in_b in rows:
        if not week or not math.isfinite(net):
            raise ValueError("week and finite net required")
        by_week[week].append((net, in_b))
    totals = []
    for week in sorted(by_week):
        values = by_week[week]
        b_values = [net for net, in_b in values if in_b]
        totals.append(
            (math.fsum(net for net, _ in values), len(values), math.fsum(b_values), len(b_values))
        )
    samples: dict[str, list[float]] = {"A": [], "B": [], "B_minus_A": []}
    if not totals:
        return {name: () for name in samples}
    rng = random.Random(seed)  # noqa: S311 - reproducible research sampling
    for _ in range(iterations):
        drawn = [totals[rng.randrange(len(totals))] for _ in totals]
        a = math.fsum(t[0] for t in drawn) / sum(t[1] for t in drawn)
        samples["A"].append(a)
        b_count = sum(t[3] for t in drawn)
        if b_count:
            b = math.fsum(t[2] for t in drawn) / b_count
            samples["B"].append(b)
            samples["B_minus_A"].append(b - a)
    return {name: tuple(values) for name, values in samples.items()}


def _bound(samples: tuple[float, ...], iterations: int, tail: float) -> float | None:
    if len(samples) / iterations < MIN_VALID_DRAWS:
        return None
    return percentile(tuple(sorted(samples)), tail)


def _robustness(rows: Sequence[tuple[sq.Impulse, float]]) -> dict[str, bool]:
    if not rows:
        return {"both_halves": False, "without_top5_symbols": False, "without_top5_weeks": False}
    halves = [
        [net for e, net in rows if (e.d < SECOND_HALF_MS) == early] for early in (True, False)
    ]
    checks = {"both_halves": all(values and fmean(values) > 0 for values in halves)}
    for name in ("symbols", "weeks"):
        totals: dict[str, float] = defaultdict(float)
        for event, net in rows:
            key = event.symbol if name == "symbols" else week_key(event.d)
            totals[key] += net
        top = set(sorted(totals, key=lambda k: (-totals[k], k))[: sq.TOP])
        remaining = [
            net for e, net in rows if (e.symbol if name == "symbols" else week_key(e.d)) not in top
        ]
        checks[f"without_top5_{name}"] = bool(remaining) and fmean(remaining) > 0
    return checks


def evaluate_events(
    events: Sequence[sq.Impulse],
    outcomes: Mapping[EventKey, EventOutcome],
    *,
    cost_bps: float = COST_BPS,
    iterations: int = 10_000,
    seed: int = SEED,
) -> dict[str, Any]:
    """Evaluate the frozen intended-event denominator, including missing outcomes."""
    _validate_events(events)
    if not math.isfinite(cost_bps) or cost_bps < 0:
        raise ValueError("cost must be finite and nonnegative")
    if set(outcomes) - {e.key for e in events}:
        raise ValueError("outcome outside the frozen event set")
    complete = []
    missing: dict[EventKey, str] = {}
    for event in events:
        outcome = outcomes.get(event.key)
        net = outcome.net(cost_bps) if outcome is not None else None
        if net is None:
            reason = outcome.unresolved_reason if outcome is not None else None
            missing[event.key] = reason or "missing_outcome"
        else:
            complete.append((event, net))
    samples = paired_week_samples(
        [(week_key(e.d), net, e.cell == "event") for e, net in complete],
        iterations=iterations,
        seed=seed,
    )
    report: dict[str, Any] = {
        "version": VERSION,
        "stage": "event_evaluation_only",
        "cost_bps": cost_bps,
        "iterations": iterations,
        "seed": seed,
        "absolute_tail": ABSOLUTE_TAIL,
        "candidates": {},
    }
    determinable = sum(e.f in sq.DETERMINED and e.g in sq.DETERMINED for e in events)
    for name in ("A", "B"):
        intended = [e for e in events if name == "A" or e.cell == "event"]
        resolved = [(e, net) for e, net in complete if name == "A" or e.cell == "event"]
        checks = _data_checks(intended)
        checks["complete"] = bool(intended) and len(resolved) / len(intended) >= MIN_COMPLETE
        checks["resolved_evidence"] = all(_data_checks([e for e, _ in resolved]).values())
        if name == "B":
            checks["fg_determinable"] = bool(events) and (
                determinable / len(events) >= sq.DETERMINABLE_MIN
            )
        mean = fmean(net for _, net in resolved) if resolved else None
        lower = _bound(samples[name], iterations, ABSOLUTE_TAIL)
        guards = _robustness(resolved)
        if not all(checks.values()):
            verdict = "insufficient_data"
        elif mean is not None and mean < 0:
            verdict = "fail"
        elif (
            mean is not None
            and mean >= MIN_NET
            and lower is not None
            and lower > 0
            and all(guards.values())
        ):
            verdict = "discovery_candidate"
        else:
            verdict = "inconclusive"
        report["candidates"][name] = {
            "intended": len(intended),
            "complete": len(resolved),
            "missing_by_reason": dict(
                Counter(missing[e.key] for e in intended if e.key in missing)
            ),
            "data_checks": checks,
            "robustness": guards,
            "mean_net": mean,
            "lower_bound": lower,
            "valid_draws": len(samples[name]),
            "verdict": verdict,
        }
    a = report["candidates"]["A"]["mean_net"]
    b = report["candidates"]["B"]["mean_net"]
    report["filter_diagnostic"] = {
        "mean_B_minus_A": b - a if a is not None and b is not None else None,
        "lower_bound": _bound(samples["B_minus_A"], iterations, DIAGNOSTIC_TAIL),
        "valid_draws": len(samples["B_minus_A"]),
        "decides_candidate": False,
    }
    return report


if __name__ == "__main__":
    main()
