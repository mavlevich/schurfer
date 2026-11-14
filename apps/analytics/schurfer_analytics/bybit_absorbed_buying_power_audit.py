"""Bounded HYP-036 planning audit; no network, tape scan or strategy outcome.

Run with ``python -m schurfer_analytics.bybit_absorbed_buying_power_audit``.
The fixed contract is docs/research/hyp036-power-audit-v1.md. Reuses the original
scale/covariance model, while keeping observed size separate from projections.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from . import bybit_absorbed_buying as ab
from .bybit_carry_feasibility import publish_once
from .bybit_compressed_spring import DAY_MS, HOUR_MS, load_features
from .bybit_post_funding_screen import file_sha256, verified
from .bybit_squeeze_continuation import _day, _week
from .bybit_squeeze_policy import _source_binding

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .bybit_compressed_spring import Features

VERSION: Final = "hyp036_power_audit_v1"
COUNTS_SHA256: Final = "db24fde0daa8c148cf64a2e7597bca5e7cce5c414e58e4a6b9632c83734ba86b"
EVENTS_SHA256: Final = "338b6e3811bbeb95312e4bb17dd0ff5b1f6b8acfbd3dff8cd7a090fe07a5cba4"
EXTRA_VARIANCE: Final = (1.0, 1.5, 2.0, 4.0)
PLANNING_LIMIT_BPS: Final = 30.0


def read_events(payload: dict[str, Any]) -> list[ab.Member]:
    """Reuse frozen identities, never rerank events or select by control availability."""
    if payload.get("mode") != "blocks" or payload.get("version") != ab.VERSION:
        raise ValueError("expected HYP-036 v1 pilot event metadata")
    events = []
    seen = set()
    for row in payload["events"]:
        stamp = datetime.fromisoformat(row["t"])
        if stamp.utcoffset() != timedelta(0):
            raise ValueError("event timestamp must be aware UTC")
        t = int(stamp.timestamp() * 1000)
        symbol = row["symbol"]
        if not isinstance(symbol, str) or not symbol or t % HOUR_MS:
            raise ValueError("invalid event symbol or off-hour timestamp")
        if not any(a <= t < b for a, b in ab.BLOCKS):
            raise ValueError("event outside frozen pilot decision blocks")
        if (symbol, t) in seen:
            raise ValueError("duplicate event")
        seen.add((symbol, t))
        # Only key/cluster properties are used. No artificial feature is fabricated.
        state = ab.State(symbol, t, None, None, None, None, None, None, None, "metadata_only")
        events.append(ab.Member(state, 0))
    if not events:
        raise ValueError("empty pilot")
    return sorted(events, key=lambda e: (e.d, e.symbol))


def sample_shape(events: Sequence[ab.Member], days: int) -> dict[str, Any]:
    clusters = {e.cluster for e in events}
    weeks = Counter(_week(e.d) for e in events)
    hours = Counter(e.d for e in events)
    return {
        "events": len(events),
        "symbol_day_clusters": len(clusters),
        "symbols": len({e.symbol for e in events}),
        "iso_weeks": len(weeks),
        "decision_days": days,
        "events_by_iso_week": dict(sorted(weeks.items())),
        "max_events_same_hour": max(hours.values()),
        "hour_concentration": sum(n * n for n in hours.values()) / len(events),
        "week_concentration": sum(n * n for n in weeks.values()) / len(events),
    }


def model(
    weights: dict[tuple[str, int], float],
    scales: dict[tuple[str, int], float],
    rhos: dict[int, float],
    days: int,
) -> dict[str, Any]:
    """Same old model; extra variance factors are scenarios, not estimated deff."""
    if days <= 0:
        raise ValueError("positive observed decision-day count required")
    full_days = ab.decision_days("full")
    variance = ab.block_variance(weights, scales, rhos)
    own = ab.mde(variance, ab.INFLATION, ab.DESIGN_EFFECT) * 10_000
    projected = own * math.sqrt(days / full_days)
    return {
        "observed_sample_mde_bps": own,
        "discovery_projection_mde_bps": projected,
        "projection_variance_factor": days / full_days,
        "projection_decision_days": full_days,
        "conditional_below_30_bps": projected <= PLANNING_LIMIT_BPS,
        "extra_variance_to_reach_30_bps": (PLANNING_LIMIT_BPS / projected) ** 2
        if projected > 0
        else None,
        "extra_variance_scenarios_bps": {str(k): projected * math.sqrt(k) for k in EXTRA_VARIANCE},
        "rho_sensitivity_bps": {
            str(k): ab.mde(
                ab.block_variance(weights, scales, rhos, k),
                ab.INFLATION,
                ab.DESIGN_EFFECT,
                days / full_days,
            )
            * 10_000
            for k in ab.RHO_FACTORS
        },
    }


def build(
    features: Features, events: Sequence[ab.Member], historical: dict[str, Any]
) -> dict[str, Any]:
    """Only scales ending at/before each t and month-rho from before that month."""
    days = ab.decision_days("blocks")
    if historical["decision_days"] != days or len(events) != 784:
        raise ValueError("pilot days or frozen event count changed")
    if len({e.cluster for e in events}) != 750:
        raise ValueError("frozen pilot clusters changed")
    months = sorted({ab.month_of(e.d) for e in events})
    pools = {m: ab.pool(features, m) for m in months}
    benchmark = features.instruments[ab.BENCHMARK]
    legs: dict[str, Any] = {}
    for name, relative in (("raw_short_price_proxy", False), ("btc_adjusted_short_proxy", True)):
        scales = {}
        for e in events:
            value = ab.scale(features.instruments[e.symbol], benchmark, e.d, relative=relative)
            if value is None or not math.isfinite(value) or value <= 0:
                raise ValueError("missing/non-positive pre-t scale; no substitution in this audit")
            scales[e.key] = value
        rhos = {m: ab.month_rho(features, m, pools[m], relative=relative) for m in months}
        whole = model(ab.weights_net(events), scales, rhos, days)
        blocks = {}
        for a, b in ab.BLOCKS:
            inside = [e for e in events if a <= e.d < b]
            if not inside:
                raise ValueError("empty fixed calendar block")
            blocks[_day(a)] = {
                "sample": sample_shape(inside, (b - a) // DAY_MS),
                "model": model(ab.weights_net(inside), scales, rhos, (b - a) // DAY_MS),
            }
        legs[name] = {
            "combined": whole,
            "blocks": blocks,
            "pre_month_rho": {_day(m): rhos[m] for m in months},
            "scales_missing": 0,
        }
    actual = legs["raw_short_price_proxy"]["combined"]["discovery_projection_mde_bps"]
    expected = historical["power"]["a_net"]["mde_unrounded"] * 10_000
    if not math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-9):
        raise ValueError("original a_net planning MDE was not reproduced")
    return {
        "version": VERSION,
        "status": "planning_only_requires_design_decision",
        "sample": sample_shape(events, days),
        "historical_a_net_reproduced": True,
        "historical_a_net_mde_bps": expected,
        "legs": legs,
        "exam_power": None,
        "exam_assigned": False,
        "strategy_outcomes_computed": False,
        "network_requests": 0,
        "limitations": [
            "Four observed ISO weeks do not calibrate year-long weekly dependence.",
            "Additional variance factors are stress scenarios, not estimated correlations.",
            "Projection assumes unchanged event rate, scales and dependence per decision day.",
            "Price log-return proxies exclude funding variance and native execution costs.",
            "BTC adjustment is descriptive; it is not an executed hedge or a matched excess.",
            "No full-fetch, outcome-read, confirmation or trading permission follows.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    for name in ("features", "counts", "events", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args(argv)
    sources = _source_binding(
        extra_source_names=(
            "bybit_absorbed_buying.py",
            "bybit_absorbed_buying_power_audit.py",
        )
    )
    if sources["working_tree_dirty"]:
        raise SystemExit("commit contract and code before running this audit")
    paths = {"features": args.features, "counts": args.counts, "events": args.events}
    expected = {"features": ab.FEATURES_SHA256, "counts": COUNTS_SHA256, "events": EVENTS_SHA256}
    for name, path in paths.items():
        verified(path, expected[name])
    historical = json.loads(args.counts.read_bytes())
    if historical["features_sha256"] != ab.FEATURES_SHA256:
        raise ValueError("counts are bound to different features")
    if historical["pairs_sha256"] != EVENTS_SHA256 or historical["mode"] != "blocks":
        raise ValueError("counts are bound to another event list or mode")
    events = read_events(json.loads(args.events.read_bytes()))
    root = Path(__file__).resolve().parents[3]
    contract = root / "docs/research/hyp036-power-audit-v1.md"
    started = datetime.now(UTC).isoformat()
    report = build(load_features(args.features), events, historical)
    report["provenance"] = {
        **sources,
        "started_at_utc": started,
        "completed_at_utc": datetime.now(UTC).isoformat(),
        "contract_sha256": file_sha256(contract),
        "inputs": {k: {"path": str(p.resolve()), "sha256": expected[k]} for k, p in paths.items()},
    }
    publish_once(args.output, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
