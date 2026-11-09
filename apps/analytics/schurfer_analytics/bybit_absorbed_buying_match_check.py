"""HYP-036 matching check: one changed control rule on the sealed pilot tape.

docs/research/bybit-absorbed-buying-v2-matching-check.md fixes the rule before the run:
the whole top 150 at the same hour, a control qB band of 0.15 to 0.85 and a z1 caliper of
0.25, everything else as in v1. The v1 rule is recomputed by the same code and must give
v1's counts before any verdict. No request is made and no trade return is computed.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

from . import bybit_absorbed_buying as v1
from .bybit_carry_feasibility import publish_once
from .bybit_compressed_spring import load_features
from .bybit_compressed_spring_cond4 import load_tape
from .bybit_post_funding_screen import verified
from .bybit_squeeze_continuation import _iso, control_shape
from .reporting import normalize_code_revision

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .bybit_compressed_spring import Features

VERSION: Final = "bybit_absorbed_buying_match_check_v1"
COVERAGE_MIN: Final = 0.85
SMD_MAX: Final = 0.10
MDE_MAX: Final = 0.0045
BALANCED: Final = ("z1", "z4", "ln_sigma1", "ln_turnover")
# v1's pilot counts (counts.json db24fde0) that the v1 rule must reproduce.
V1_EVENTS: Final = 784
V1_WITH_CONTROL: Final = 442
V1_Z1_SMD: Final = 0.175


@dataclass(frozen=True)
class Rule:
    qb: tuple[float, float]
    z1_caliper: float
    by_tercile: bool

    def admits(self, state: v1.State) -> bool:
        if state.reason is not None:
            return False
        assert state.qb is not None and state.qv is not None and state.z1 is not None
        lo, hi = self.qb
        return lo <= state.qb <= hi and state.qv >= v1.QV_MIN and abs(state.z1) <= v1.Z1_MAX


RULES: Final = {
    "v1": Rule(v1.CONTROL_QB, v1.Z_CALIPER, by_tercile=True),
    "check": Rule((0.15, 0.85), 0.25, by_tercile=False),
}


def wide(state: v1.State) -> bool:
    """Admitted by either rule (the qB bands nest), so one pass collects both pools."""
    return any(rule.admits(state) for rule in RULES.values())


def turnovers(features: Features, pools: dict[int, list[str]]) -> dict[tuple[int, str], float]:
    """Each pool member's 1-hour kline turnover over the 30 days before its month: the
    pool's own ranking measure."""
    out = {}
    for month, members in pools.items():
        for symbol in members:
            bars = features.instruments[symbol].bars
            out[(month, symbol)] = math.fsum(
                bar[3]
                for start, bar in bars.items()
                if month - v1.POOL_LOOKBACK_MS <= start < month
            )
    return out


smd = v1.smd_of  # v1's weighted SMD, None for different means with no spread


def balance(
    weights: dict[tuple[str, int], float],
    members: dict[tuple[str, int], v1.Member],
    turnover: dict[tuple[int, str], float],
) -> dict[str, Any] | None:
    if not weights:
        return None
    out = {k: v["smd_unrounded"] for k, v in v1.balance(weights, members).items()}
    out["ln_turnover"] = smd(weights, lambda key: math.log(turnover[(v1.month_of(key[1]), key[0])]))
    return {k: round(v, 4) if v is not None else None for k, v in out.items()} | {
        "passes": all(out[n] is not None and abs(out[n]) <= SMD_MAX for n in BALANCED)
    }


def reuse(weights: dict[tuple[str, int], float], slots: int) -> dict[str, Any]:
    controls = {k: -w for k, w in weights.items() if w < 0}
    total = math.fsum(controls.values())
    return {
        "distinct_controls": len(controls),
        "pair_slots": slots,
        "slots_per_distinct_control": round(slots / len(controls), 3) if controls else None,
        "largest_control_weight_share": round(max(controls.values()) / total, 4)
        if controls
        else None,
    }


def evaluate(
    rule: Rule,
    features: Features,
    events: Sequence[v1.Member],
    pool_members: Sequence[v1.Member],
    rhos: dict[str, dict[int, float]],
    turnover: dict[tuple[int, str], float],
    days_scale: float,
) -> tuple[dict[str, Any], dict[tuple[str, int], list[v1.Member]]]:
    candidates = [m for m in pool_members if rule.admits(m.state)]
    pairs = v1.match(events, candidates, z1_caliper=rule.z1_caliper, by_tercile=rule.by_tercile)
    members = {m.key: m for m in (*events, *candidates)}
    weights = v1.weights_excess(events, pairs)
    controls = control_shape(cast("Sequence[Any]", events), cast("dict[Any, Any]", pairs))
    blocks: dict[str, dict[str, Any]] = {}
    for a, b in v1.BLOCKS:
        inside = [e for e in events if a <= e.d < b]
        shape = control_shape(cast("Sequence[Any]", inside), cast("dict[Any, Any]", pairs))
        blocks[_iso(a)] = {
            "controls": shape,
            "balance": balance(v1.weights_excess(inside, pairs), members, turnover),
        }
    mdes = v1.power(features, events, pairs, rhos, days_scale) if events else {}
    bal = balance(weights, members, turnover)
    share = controls["events_with_control"] / controls["events"] if controls["events"] else 0.0
    block_shares = {
        k: v["controls"]["events_with_control"] / v["controls"]["events"]
        if v["controls"]["events"]
        else 0.0
        for k, v in blocks.items()
    }
    checks = {
        "coverage_overall": share >= COVERAGE_MIN,
        "coverage_each_block": all(s >= COVERAGE_MIN for s in block_shares.values()),
        "balance_overall": bool(bal and bal["passes"]),
        "a_net_mde": bool(mdes.get("a_net")) and mdes["a_net"]["mde_unrounded"] <= MDE_MAX,
        "b_excess_mde": bool(mdes.get("b_excess")) and mdes["b_excess"]["mde_unrounded"] <= MDE_MAX,
    }
    report = {
        "rule": {"qb": list(rule.qb), "z1_caliper": rule.z1_caliper, "by_tercile": rule.by_tercile},
        "candidates": len(candidates),
        "controls": controls,
        "balance": bal,
        "reuse": reuse(weights, sum(len(p) for p in pairs.values())),
        "blocks": blocks,
        "power": {
            leg: {k: v[k] for k in ("units", "scales_substituted", "mde_bps")} if v else None
            for leg, v in mdes.items()
        },
        "checks": checks,
        "passes": all(checks.values()),
    }
    return report, pairs


def build(features: Features, tapes: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    pools = v1.pools_of(features)
    events, pool_members, _ = v1.decide(features, tapes, pools, "blocks", wide)
    rhos = {
        leg: {m: v1.month_rho(features, m, pools[m], relative=leg == "b") for m in v1.MONTHS}
        for leg in ("a", "b")
    }
    turnover = turnovers(features, pools)
    days_scale = v1.decision_days("blocks") / v1.decision_days("full")
    out: dict[str, Any] = {}
    frozen: dict[str, Any] = {}
    for name, rule in RULES.items():
        out[name], pairs = evaluate(rule, features, events, pool_members, rhos, turnover,
                                    days_scale)  # fmt: skip
        if name == "check":
            frozen = {
                "version": VERSION,
                "rule": out[name]["rule"],
                "events": [
                    {
                        "symbol": e.symbol,
                        "t": _iso(e.d),
                        "controls": [c.symbol for c in pairs[e.key]],
                    }
                    for e in sorted(events, key=lambda x: (x.d, x.symbol))
                ],
            }
    reproduced = (
        out["v1"]["controls"]["events"] == V1_EVENTS
        and out["v1"]["controls"]["events_with_control"] == V1_WITH_CONTROL
        and out["v1"]["balance"] is not None
        and round(out["v1"]["balance"]["z1"], 3) == V1_Z1_SMD
    )
    if not reproduced:
        verdict = "v1_not_reproduced"
    else:
        verdict = "pass" if out["check"]["passes"] else "fail"
    report = {
        "version": VERSION,
        "step": "matching check on the pilot tape: pre-t features only, no trade return",
        "features_sha256": v1.FEATURES_SHA256,
        "events": len(events),
        "v1_reproduced": reproduced,
        "rules": out,
        "verdict": verdict,
    }
    return report, frozen


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    for name in ("features", "manifest", "scope"):
        parser.add_argument(f"--{name}", type=Path, required=True)
        parser.add_argument(f"--{name}-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--code-revision", required=True)
    dirty = parser.add_mutually_exclusive_group(required=True)
    dirty.add_argument("--working-tree-dirty", dest="working_tree_dirty", action="store_true")
    dirty.add_argument("--no-working-tree-dirty", dest="working_tree_dirty", action="store_false")
    args = parser.parse_args(argv)
    if args.features_sha256 != v1.FEATURES_SHA256:
        raise SystemExit(f"the protocol pins the features snapshot {v1.FEATURES_SHA256}")
    features = load_features(verified(args.features, args.features_sha256))
    scope = json.loads(verified(args.scope, args.scope_sha256).read_bytes())
    v1.bound_tape(args.manifest, args.manifest_sha256, scope, args.scope_sha256, features,
                  "blocks")  # fmt: skip
    tapes, meta = load_tape(args.manifest, args.manifest_sha256)
    report, frozen = build(features, tapes)
    report["pairs_sha256"] = publish_once(args.output_dir / "pairs-check.json", frozen)
    report |= {
        "manifest": meta,
        "scope_sha256": args.scope_sha256,
        "code_revision": normalize_code_revision(args.code_revision),
        "working_tree_dirty": args.working_tree_dirty,
    }
    digest = publish_once(args.output_dir / "match-check.json", report)
    sys.stdout.write(f"match-check.json sha256 {digest}\nverdict: {report['verdict']}\n")


if __name__ == "__main__":
    main()
