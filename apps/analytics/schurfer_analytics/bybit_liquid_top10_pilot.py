"""Liquid top-10 flow pilot: the scope of two new calendar blocks (pre-protocol).

docs/research/bybit-liquid-top10-pilot-v1.md fixes the blocks, the pool, the matching
rule and the gate before any fetch. This module writes the tape scope of the two blocks
and its size from HYP-032's archive-size record (no request), and the counts on the
sealed tape: events, same-instrument controls, balance, concentration, MDE and the gate.
No trade return is computed; every feature is from data before its own t.

The scope holds, for each instrument in the monthly top 10 on any decision day of a
block, every day from 22 days before the block (15 days of rank and sigma history for
the earliest control, which can be 7 days before the block) to 7 days after it (the
latest control), so an event at a block's edge has the same chance of a control.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from . import bybit_absorbed_buying as v1
from .bybit_carry_feasibility import publish_once
from .bybit_compressed_spring import DAY_MS, HOUR_MS, RANK_HOURS, estimate, load_features
from .bybit_compressed_spring import VERSION as TAPE_VERSION
from .bybit_compressed_spring_cond4 import Tape, load_tape
from .bybit_post_funding_screen import verified
from .bybit_squeeze_continuation import _day, _iso, _week
from .reporting import normalize_code_revision

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .bybit_compressed_spring import Features

VERSION: Final = "bybit_liquid_top10_pilot_v1"
TOP: Final = 10
# Decision days, inclusive (UTC), fixed in the note before any fetch.
BLOCKS: Final = (
    (v1._ms(2024, 11, 18), v1._ms(2024, 12, 1)),
    (v1._ms(2025, 4, 14), v1._ms(2025, 4, 27)),
)
CONTROL_WINDOW_D: Final = 7
LEAD_D: Final = CONTROL_WINDOW_D + v1.SCOPE_LEAD_DAYS
CONTROL_QB: Final = (0.15, 0.85)
Z1_CALIPER: Final = 0.25
QV_CALIPER: Final = 0.10
BALANCED: Final = ("z1", "z4", "ln_sigma1", "qv")
COVERAGE_MIN: Final = 0.85
SMD_MAX: Final = 0.10
EFFECTIVE_SYMBOLS_MIN: Final = 6.0
TOP_SYMBOL_MAX: Final = 0.25
MDE_MAX: Final = 0.0045
POISSON_LOWER: Final = 0.05  # one-sided

Key = tuple[str, int]


def top_pools(features: Features) -> dict[int, list[str]]:
    return {m: v1.pool(features, m)[:TOP] for m in v1.MONTHS}


def scope_days(features: Features) -> dict[str, list[str]]:
    """Each top-10 member's days from 22 days before its block to 7 days after it."""
    pools = top_pools(features)
    days: dict[str, set[str]] = defaultdict(set)
    for first, last in BLOCKS:
        members = {s for d in range(first, last + DAY_MS, DAY_MS) for s in pools[v1.month_of(d)]}
        for symbol in members:
            series = features.instruments[symbol]
            d = first - LEAD_D * DAY_MS
            while d <= last + CONTROL_WINDOW_D * DAY_MS:
                if series.launch_ms <= d + DAY_MS and d <= series.end_ms:
                    days[symbol].add(_day(d))
                d += DAY_MS
    return {s: sorted(v) for s, v in sorted(days.items())}


def seen_overlap(days: dict[str, list[str]]) -> int:
    """Symbol-days shared with HYP-036's pilot scope (blocks' days with their lead)."""
    seen = set()
    for first, end in v1.BLOCKS:
        d = first - v1.SCOPE_LEAD_DAYS * DAY_MS
        while d < end:
            seen.add(_day(d))
            d += DAY_MS
    return sum(1 for v in days.values() for d in v if d in seen)


# ---------------------------------------------------------------- counts


def bound_tape(
    manifest_path: Path, manifest_sha256: str, scope: dict[str, Any], scope_sha256: str,
    features: Features,
) -> None:  # fmt: skip
    """v1's binding check for this study's scope: the scope's days recomputed, the run
    bound to exactly this scope with HYP-032's rules, the manifest holding its days."""
    if (scope.get("version"), scope.get("study"), scope.get("mode")) != (
        TAPE_VERSION,
        VERSION,
        "blocks",
    ):
        raise SystemExit("the scope is not this pilot's")
    if scope.get("primary") != scope_days(features):
        raise SystemExit("the scope's days are not the ones the note fixes")
    manifest = json.loads(verified(manifest_path, manifest_sha256).read_bytes())
    if manifest.get("version") != TAPE_VERSION or manifest.get("binding") != v1.tape_binding(
        scope_sha256
    ):
        raise SystemExit("the tape is bound to other inputs, rules or scope")
    held = sorted((p["symbol"], p["day"]) for p in manifest["parts"])
    wanted = sorted((s, d) for s, days in scope["primary"].items() for d in days)
    if held != wanted:
        raise SystemExit("the tape's symbol-days are not exactly the scope's")


def admits(state: v1.State) -> bool:
    if state.reason is not None:
        return False
    assert state.qb is not None and state.qv is not None and state.z1 is not None
    lo, hi = CONTROL_QB
    return lo <= state.qb <= hi and state.qv >= v1.QV_MIN and abs(state.z1) <= v1.Z1_MAX


def decide(
    features: Features, tapes: dict[str, Tape], top: dict[int, list[str]]
) -> tuple[list[v1.Member], dict[str, list[v1.Member]], Counter[str], dict[Key, int]]:
    """Events in the blocks, every instrument's control candidates within 7 days of a
    block, the funnel and each event's block index."""
    funnel: Counter[str] = Counter()
    events: list[v1.Member] = []
    candidates: dict[str, list[v1.Member]] = defaultdict(list)
    block_of: dict[Key, int] = {}
    for b, (first, last) in enumerate(BLOCKS):
        end = min(last + DAY_MS, v1.LAST_DECISION_MS + HOUR_MS)
        lo = first - CONTROL_WINDOW_D * DAY_MS
        hi = min(end + CONTROL_WINDOW_D * DAY_MS, v1.LAST_DECISION_MS + HOUR_MS)
        members = sorted({s for d in range(first, end, DAY_MS) for s in top[v1.month_of(d)]})
        for symbol in members:
            series = features.instruments[symbol]
            by_hour = v1.states(symbol, series, tapes.get(symbol), lo, hi)
            for t in range(lo, hi, HOUR_MS):
                state = by_hour[t]
                if v1._tradable(series, t) and admits(state):
                    candidates[symbol].append(v1.Member(state, 0))
            for t in range(first, end, HOUR_MS):
                if symbol not in top[v1.month_of(t)] or not v1._tradable(series, t):
                    continue
                state = by_hour[t]
                funnel["member_hours"] += 1
                status = state.qualifies
                funnel[f"qualifies_{status}"] += 1
                if status != "true":
                    continue
                first_ok = v1.first_status(by_hour, t)
                funnel[f"first_{first_ok}"] += 1
                if first_ok == "true":
                    events.append(v1.Member(state, 0))
                    block_of[(symbol, t)] = b
    return events, candidates, funnel, block_of


def match(
    events: Sequence[v1.Member], candidates: dict[str, list[v1.Member]]
) -> dict[Key, list[v1.Member]]:
    """The same instrument, more than 8 h and at most 7 days away; calipers on sigma1,
    z1, z4 and qV; the 5 nearest by the caliper-scaled distance, then sigma1, then time."""
    pairs: dict[Key, list[v1.Member]] = {}
    for e in events:
        es = e.state
        assert es.z1 is not None and es.z4 is not None and es.sigma1 is not None
        assert es.qv is not None
        ranked = []
        seen: set[int] = set()
        for c in candidates[e.symbol]:
            gap = abs(c.d - e.d)
            if c.d in seen or gap <= v1.EPISODE_H * HOUR_MS or gap > CONTROL_WINDOW_D * DAY_MS:
                continue
            seen.add(c.d)  # the two blocks' candidate lists may share no hour, but be safe
            cs = c.state
            assert cs.z1 is not None and cs.z4 is not None and cs.sigma1 is not None
            assert cs.qv is not None
            ratio = abs(math.log(cs.sigma1 / es.sigma1))
            dz1, dz4, dqv = cs.z1 - es.z1, cs.z4 - es.z4, cs.qv - es.qv
            if (
                ratio > v1.SIGMA_RATIO_MAX
                or abs(dz1) > Z1_CALIPER
                or abs(dz4) > v1.Z_CALIPER
                or abs(dqv) > QV_CALIPER
            ):
                continue
            distance = math.hypot(dz1 / Z1_CALIPER, dz4 / v1.Z_CALIPER, dqv / QV_CALIPER)
            ranked.append((distance, ratio, c.d, c))
        ranked.sort(key=lambda r: r[:3])
        pairs[e.key] = [r[3] for r in ranked[: v1.MAX_CONTROLS]]
    return pairs


def btc_z4(features: Features) -> dict[int, float]:
    """BTC's own 4 h log change over twice its sigma1 at each hour (reported only)."""
    btc = features.instruments[v1.BENCHMARK]
    out: dict[int, float] = {}
    for t in range(v1.FIRST_DECISION_MS - 30 * DAY_MS, v1.LAST_DECISION_MS + HOUR_MS, HOUR_MS):
        r4 = v1._log_change(btc, t, v1.HOLD_H)
        sigma = v1._sd(v1._log_change(btc, t - k * HOUR_MS, 1) for k in range(1, RANK_HOURS + 1))
        if r4 is not None and sigma:
            out[t] = r4 / (2 * sigma)
    return out


def poisson_lower(n: int, alpha: float = POISSON_LOWER) -> float:
    """One-sided lower confidence bound of a Poisson mean after observing n."""
    if n == 0:
        return 0.0

    def upper_tail(mu: float) -> float:  # P(X >= n | mu)
        term, below = math.exp(-mu), 0.0
        for k in range(n):
            below += term
            term *= mu / (k + 1)
        return 1.0 - below

    lo, hi = 0.0, float(n)
    for _ in range(200):
        mid = (lo + hi) / 2
        if upper_tail(mid) < alpha:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def concentration(events: Sequence[v1.Member]) -> dict[str, Any]:
    weights = v1.weights_net(events)
    by_symbol: dict[str, float] = defaultdict(float)
    by_week: dict[str, float] = defaultdict(float)
    for (symbol, t), w in weights.items():
        by_symbol[symbol] += w
        by_week[_week(t)] += w
    return {
        "symbols": len(by_symbol),
        "effective_symbols": round(1 / math.fsum(w * w for w in by_symbol.values()), 3)
        if by_symbol
        else 0.0,
        "largest_symbol_share": round(max(by_symbol.values(), default=0.0), 4),
        "iso_weeks": len(by_week),
        "largest_week_share": round(max(by_week.values(), default=0.0), 4),
    }


def build(features: Features, tapes: dict[str, Tape]) -> tuple[dict[str, Any], dict[str, Any]]:
    top = top_pools(features)
    events, candidates, funnel, block_of = decide(features, tapes, top)
    pairs = match(events, candidates)
    members = {m.key: m for m in (*events, *(c for v in candidates.values() for c in v))}
    btc = btc_z4(features)
    rhos = {
        leg: {m: v1.month_rho(features, m, top[m], relative=leg == "b") for m in v1.MONTHS}
        for leg in ("a", "b")
    }
    days_scale = sum((b - a) // DAY_MS + 1 for a, b in BLOCKS) / v1.decision_days("full")

    def side(evs: Sequence[v1.Member]) -> dict[str, Any]:
        with_control = sum(1 for e in evs if pairs.get(e.key))
        weights = v1.weights_excess(evs, pairs)
        bal = v1.balance(weights, members) if weights else None
        out: dict[str, Any] = {
            "events": len(evs),
            "with_control": with_control,
            "coverage": round(with_control / len(evs), 4) if evs else None,
            "balance": {k: v["smd"] for k, v in bal.items()} if bal else None,
        }
        if weights and bal is not None:
            smd = v1.smd_of(weights, lambda k: btc.get(k[1], 0.0))
            out["balance"]["btc_z4"] = round(smd, 4) if smd is not None else None
            out["balance_passes"] = all(
                bal[n]["smd_unrounded"] is not None and abs(bal[n]["smd_unrounded"]) <= SMD_MAX
                for n in BALANCED
            )
        return out

    overall = side(events)
    blocks = {
        _iso(a): side([e for e in events if block_of[e.key] == b])
        for b, (a, _) in enumerate(BLOCKS)
    }
    shape = concentration(events)
    mdes = v1.power(features, events, pairs, rhos, days_scale) if events else {}
    clusters = len({e.cluster for e in events})
    lower = poisson_lower(clusters)
    inflate = math.sqrt(clusters / lower) if lower > 0 else math.inf
    power = {
        leg: {
            "units": v["units"],
            "mde_bps": v["mde_bps"],
            "mde_conservative_bps": round(v["mde_unrounded"] * inflate * 10_000, 2)
            if math.isfinite(inflate)
            else None,
            "point_ok": v["mde_unrounded"] <= MDE_MAX,
            "conservative_ok": math.isfinite(inflate) and v["mde_unrounded"] * inflate <= MDE_MAX,
        }
        if v
        else None
        for leg, v in mdes.items()
    }
    coverage_ok = (overall["coverage"] or 0) >= COVERAGE_MIN and all(
        (v["coverage"] or 0) >= COVERAGE_MIN for v in blocks.values()
    )
    checks = {
        "coverage": coverage_ok,
        "balance": bool(overall.get("balance_passes")),
        "instruments": shape["effective_symbols"] >= EFFECTIVE_SYMBOLS_MIN
        and shape["largest_symbol_share"] <= TOP_SYMBOL_MAX,
        "mde_point": bool(power) and all(v and v["point_ok"] for v in power.values()),
        "mde_conservative": bool(power) and all(v and v["conservative_ok"] for v in power.values()),
    }
    if (
        not (checks["coverage"] and checks["balance"] and checks["instruments"])
        or not checks["mde_point"]
    ):
        verdict = "fail"
    elif not checks["mde_conservative"]:
        verdict = "indeterminate"
    else:
        verdict = "pass"
    report = {
        "version": VERSION,
        "step": "pilot counts: pre-t features only, no trade return",
        "features_sha256": v1.FEATURES_SHA256,
        "blocks": [[_iso(a), _iso(b)] for a, b in BLOCKS],
        "funnel": dict(sorted(funnel.items())),
        "determinable_share": round(v1.determined_hours(funnel) / funnel["member_hours"], 4)
        if funnel["member_hours"]
        else None,
        "clusters": clusters,
        "clusters_lower_95": round(lower, 2),
        "events_by_symbol": dict(Counter(e.symbol for e in events).most_common()),
        "overall": overall,
        "blocks_detail": blocks,
        "concentration": shape,
        "days_scale": round(days_scale, 4),
        "power": power,
        "checks": checks,
        "verdict": verdict,
    }
    frozen = {
        "version": VERSION,
        "events": [
            {"symbol": e.symbol, "t": _iso(e.d), "controls": [_iso(c.d) for c in pairs[e.key]]}
            for e in sorted(events, key=lambda x: (x.d, x.symbol))
        ],
    }
    return report, frozen


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    scope = sub.add_parser("scope", help="the two blocks' tape scope and its size")
    scope.add_argument("--archive-sizes", type=Path, required=True)
    scope.add_argument("--archive-sizes-sha256", required=True)
    counts = sub.add_parser("counts", help="the pilot counts and gate on the sealed tape")
    for name in ("manifest", "scope"):
        counts.add_argument(f"--{name}", type=Path, required=True)
        counts.add_argument(f"--{name}-sha256", required=True)
    for cmd in (scope, counts):
        cmd.add_argument("--features", type=Path, required=True)
        cmd.add_argument("--features-sha256", required=True)
        cmd.add_argument("--output-dir", type=Path, required=True)
        cmd.add_argument("--code-revision", required=True)
        dirty = cmd.add_mutually_exclusive_group(required=True)
        dirty.add_argument("--working-tree-dirty", dest="working_tree_dirty", action="store_true")
        dirty.add_argument(
            "--no-working-tree-dirty", dest="working_tree_dirty", action="store_false"
        )
    args = parser.parse_args(argv)
    if args.features_sha256 != v1.FEATURES_SHA256:
        raise SystemExit(f"the note pins the features snapshot {v1.FEATURES_SHA256}")
    provenance = {
        "code_revision": normalize_code_revision(args.code_revision),
        "working_tree_dirty": args.working_tree_dirty,
    }
    if args.command == "scope" and args.archive_sizes_sha256 != v1.ARCHIVE_SIZES_SHA256:
        raise SystemExit(f"the size estimate pins HYP-032's record {v1.ARCHIVE_SIZES_SHA256}")
    features = load_features(verified(args.features, args.features_sha256))
    if args.command == "counts":
        scope_body = json.loads(verified(args.scope, args.scope_sha256).read_bytes())
        bound_tape(args.manifest, args.manifest_sha256, scope_body, args.scope_sha256, features)
        tapes, meta = load_tape(args.manifest, args.manifest_sha256)
        report, frozen = build(features, tapes)
        report["pairs_sha256"] = publish_once(args.output_dir / "pairs.json", frozen)
        report |= {"manifest": meta, "scope_sha256": args.scope_sha256} | provenance
        digest = publish_once(args.output_dir / "counts.json", report)
        sys.stdout.write(f"counts.json sha256 {digest}\nverdict: {report['verdict']}\n")
        return
    days = scope_days(features)
    listed, sizes = v1.load_archive_sizes(verified(args.archive_sizes, args.archive_sizes_sha256))
    # HYP-032's fetcher reads the version and the "primary" days of a scope.
    body = {"version": TAPE_VERSION, "study": VERSION, "mode": "blocks", "primary": days}
    digest = publish_once(args.output_dir / "scope.json", body)
    size: dict[str, Any] = (
        estimate(days, listed, sizes)
        | {
            "scope_sha256": digest,
            "blocks": [[_iso(a), _iso(b)] for a, b in BLOCKS],
            "symbols": len(days),
            "symbol_days_shared_with_hyp036_pilot": seen_overlap(days),
        }
        | provenance
    )
    publish_once(args.output_dir / "scope-size.json", size)
    sys.stdout.write(json.dumps(size, indent=2) + "\n")


if __name__ == "__main__":
    main()
