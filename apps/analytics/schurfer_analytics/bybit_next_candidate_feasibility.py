"""Next-candidate feasibility after HYP-036: family 5 against family 2 on majors.

Planning only (docs/research/next-candidate-feasibility-v1.md): independent
observations, the comparison that can be built and the MDE, before any return. Both
use data already held and pre-t features only; no request, no trade return.

- ``family5``: pumps (24 h close-to-close return at least P) with an open-interest turn
  (OI at least D below its peak since 24 h before the pump) within 24 h while the pump
  is at least half kept; controls are other pumps not turned by the same hour since the
  pump (a risk set), matched on the kept share, pump size and sigma1. Features snapshot.
- ``majors``: HYP-036's event on the monthly top 10 of its pool (BTC excluded), on the
  pilot tape's two blocks; controls at the same hour among the top 10, or the same
  instrument within 7 days; plus the tape size of a full year.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from . import bybit_absorbed_buying as v1
from . import bybit_absorbed_buying_match_check as mc
from .bybit_carry_feasibility import publish_once
from .bybit_compressed_spring import (
    DAY_MS,
    HOUR_MS,
    RANK_HOURS,
    WINDOW_END_MS,
    estimate,
    load_features,
)
from .bybit_compressed_spring_cond4 import load_tape
from .bybit_post_funding_screen import verified
from .bybit_squeeze_continuation import _day, _iso
from .reporting import normalize_code_revision

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from .bybit_compressed_spring import Features, Series

VERSION: Final = "bybit_next_candidate_feasibility_v1"
PUMPS: Final = (0.15, 0.20, 0.30)
OI_DROPS: Final = (0.03, 0.05, 0.10)
PRIMARY: Final = (0.20, 0.05)
EPISODE_GAP_H: Final = 72
PUMP_H: Final = 24
MAX_K: Final = 24
KEPT_MIN: Final = 0.5
CALIPER_KEPT: Final = 0.10
CALIPER_SIZE: Final = 0.25
TOP: Final = 10
OWN_WINDOW_D: Final = 7
Z1_CALIPER: Final = 0.25

Key = tuple[str, int]


# ---------------------------------------------------------------- family 5


@dataclass(frozen=True)
class Hour:
    """One hour k after a pump's first hour t0."""

    symbol: str
    t0: int
    k: int
    t: int
    kept: float  # share of the pump's rise still held at t
    size: float  # peak over the 24 h base, minus 1
    turned: bool


def _close(series: Series, t: int) -> float | None:
    bar = series.bars.get(t - HOUR_MS)
    return bar[2] if bar and bar[2] > 0 else None


def pumps(features: Features, pools: dict[int, list[str]], pump: float) -> list[Key]:
    """First hours whose 24 h close-to-close log return is at least log(1 + pump), the
    instrument in its month's pool and tradable; one per instrument per 72 h."""
    found: list[Key] = []
    for t in range(v1.FIRST_DECISION_MS, v1.LAST_DECISION_MS + HOUR_MS, HOUR_MS):
        for symbol in pools[v1.month_of(t)]:
            series = features.instruments[symbol]
            r = v1._log_change(series, t, PUMP_H)
            if r is not None and r >= math.log1p(pump) and v1._tradable(series, t):
                found.append((symbol, t))
    found.sort()
    kept: list[Key] = []
    last: dict[str, int] = {}
    for symbol, t in found:
        if t - last.get(symbol, -(10**15)) >= EPISODE_GAP_H * HOUR_MS:
            kept.append((symbol, t))
            last[symbol] = t
    return kept


def episode(
    features: Features, pools: dict[int, list[str]], symbol: str, t0: int, oi_drop: float
) -> list[Hour] | None:
    """Hours k = 1..24 after t0 while the pump is at least half kept, ending at the first
    turn; None when the 24 h base is missing."""
    series = features.instruments[symbol]
    base = _close(series, t0 - PUMP_H * HOUR_MS)
    if base is None:
        return None
    before = [c for k in range(PUMP_H + 1) if (c := _close(series, t0 - k * HOUR_MS))]
    peak = max(before, default=base)
    oi_peak = max(
        (v for s, v in series.oi.items() if t0 - PUMP_H * HOUR_MS <= s <= t0), default=None
    )
    hours: list[Hour] = []
    for k in range(1, MAX_K + 1):
        t = t0 + k * HOUR_MS
        if t > v1.LAST_DECISION_MS or symbol not in pools[v1.month_of(t)]:
            break
        if not v1._tradable(series, t):
            break
        c = _close(series, t)
        oi = series.oi.get(t)
        if c is None or oi is None or not oi_peak:
            continue
        peak = max(peak, c)
        kept = (c - base) / (peak - base) if peak > base else 0.0
        if kept < KEPT_MIN:
            break
        turned = oi <= (1 - oi_drop) * oi_peak
        oi_peak = max(oi_peak, oi)
        hours.append(Hour(symbol, t0, k, t, kept, peak / base - 1, turned))
        if turned:
            break
    return hours


def _member(symbol: str, t: int) -> v1.Member:
    return v1.Member(v1.State(symbol, t, None, None, None, None, None, None, None, None), 0)


def family5_run(
    features: Features,
    pools: dict[int, list[str]],
    pump: float,
    oi_drop: float,
    sigma: Callable[[str, int], float | None],
) -> tuple[dict[str, Any], list[Hour], dict[Key, list[Hour]]]:
    episodes = pumps(features, pools, pump)
    events: list[Hour] = []
    risk: dict[int, list[Hour]] = defaultdict(list)
    funnel: Counter[str] = Counter(episodes=len(episodes))
    for symbol, t0 in episodes:
        hours = episode(features, pools, symbol, t0, oi_drop)
        if hours is None:
            funnel["no_base"] += 1
            continue
        if hours and hours[-1].turned:
            events.append(hours[-1])
            funnel["turned"] += 1
        else:
            funnel["not_turned_while_kept"] += 1
        for h in hours:
            if not h.turned:
                risk[h.k].append(h)
    pairs: dict[Key, list[Hour]] = {}
    for e in events:
        es = sigma(e.symbol, e.t)
        ranked = []
        for c in risk[e.k]:
            if c.symbol == e.symbol and abs(c.t0 - e.t0) < EPISODE_GAP_H * HOUR_MS:
                continue
            dkept = (c.kept - e.kept) / CALIPER_KEPT
            dsize = math.log((1 + c.size) / (1 + e.size)) / CALIPER_SIZE
            if abs(dkept) > 1 or abs(dsize) > 1:
                continue
            cs = sigma(c.symbol, c.t)
            if es is None or cs is None or abs(math.log(cs / es)) > v1.SIGMA_RATIO_MAX:
                continue
            ranked.append((math.hypot(dkept, dsize), c.symbol, c.t, c))
        ranked.sort(key=lambda r: r[:3])
        pairs[(e.symbol, e.t)] = [r[3] for r in ranked[: v1.MAX_CONTROLS]]
    symbols = Counter(e.symbol for e in events)
    ks = sorted(e.k for e in events)
    with_control = sum(1 for p in pairs.values() if p)
    summary = {
        "pump": pump,
        "oi_drop": oi_drop,
        "funnel": dict(sorted(funnel.items())),
        "events": len(events),
        "clusters": len({(e.symbol, _day(e.t)) for e in events}),
        "symbols": len(symbols),
        "top5_symbol_share": round(sum(n for _, n in symbols.most_common(5)) / len(events), 4)
        if events
        else None,
        "k_median": ks[len(ks) // 2] if ks else None,
        "k_share_at_most_2": round(sum(1 for k in ks if k <= 2) / len(ks), 4) if ks else None,
        "with_control": with_control,
        "coverage": round(with_control / len(events), 4) if events else None,
    }
    return summary, events, pairs


def family5_power(
    features: Features,
    pools: dict[int, list[str]],
    events: Sequence[Hour],
    pairs: dict[Key, list[Hour]],
    rhos: dict[str, dict[int, float]],
    sigma: Callable[[str, int], float | None],
) -> dict[str, Any]:
    members = [_member(e.symbol, e.t) for e in events]
    mpairs = {k: [_member(c.symbol, c.t) for c in v] for k, v in pairs.items()}
    mdes = v1.power(features, members, mpairs, rhos, 1.0)
    hour_of = {(e.symbol, e.t): e for e in events}
    for v in pairs.values():
        hour_of |= {(c.symbol, c.t): c for c in v}
    weights = v1.weights_excess(members, mpairs)
    turnover = mc.turnovers(features, pools)

    def ln_sigma(k: Key) -> float:
        value = sigma(*k)
        assert value is not None
        return math.log(value)

    values: dict[str, Callable[[Key], float]] = {
        "kept": lambda k: hour_of[k].kept,
        "ln_size": lambda k: math.log1p(hour_of[k].size),
        "ln_sigma1": ln_sigma,
        "ln_turnover": lambda k: math.log(turnover[(v1.month_of(k[1]), k[0])]),
    }
    uses = Counter((c.symbol, c.t) for v in pairs.values() for c in v)
    return {
        "mde_bps": {k: v["mde_bps"] if v else None for k, v in mdes.items()},
        "balance_smd": {n: round(mc.smd(weights, f), 4) for n, f in values.items()}
        if weights
        else None,
        "max_events_per_control": max(uses.values(), default=0),
    }


def family5(features: Features) -> dict[str, Any]:
    pools = v1.pools_of(features)
    cache: dict[Key, float | None] = {}

    def sigma(symbol: str, t: int) -> float | None:
        if (symbol, t) not in cache:
            series = features.instruments[symbol]
            cache[(symbol, t)] = v1._sd(
                v1._log_change(series, t - k * HOUR_MS, 1) for k in range(1, RANK_HOURS + 1)
            )
        return cache[(symbol, t)]

    rhos = {
        leg: {m: v1.month_rho(features, m, pools[m], relative=leg == "b") for m in v1.MONTHS}
        for leg in ("a", "b")
    }
    grid = []
    for pump in PUMPS:
        for oi_drop in OI_DROPS:
            summary, events, pairs = family5_run(features, pools, pump, oi_drop, sigma)
            if (pump, oi_drop) == PRIMARY or (pump, oi_drop) == (PUMPS[0], OI_DROPS[0]):
                summary |= family5_power(features, pools, events, pairs, rhos, sigma)
            grid.append(summary)
    return {"grid": grid}


# ---------------------------------------------------------------- majors


def own_match(
    events: Sequence[v1.Member], candidates: Sequence[v1.Member]
) -> dict[Key, list[v1.Member]]:
    """The same instrument, decision hours within 7 days and outside the event's 8-hour
    episode; the check rule's calipers; the 5 nearest by (z1, z4), then by time."""
    by_symbol: dict[str, list[v1.Member]] = defaultdict(list)
    for c in candidates:
        by_symbol[c.symbol].append(c)
    pairs: dict[Key, list[v1.Member]] = {}
    for e in events:
        es = e.state
        assert es.z1 is not None and es.z4 is not None and es.sigma1 is not None
        ranked = []
        for c in by_symbol[e.symbol]:
            gap = abs(c.d - e.d)
            if gap > OWN_WINDOW_D * DAY_MS or gap <= v1.EPISODE_H * HOUR_MS:
                continue
            cs = c.state
            assert cs.z1 is not None and cs.z4 is not None and cs.sigma1 is not None
            ratio = abs(math.log(cs.sigma1 / es.sigma1))
            dz1, dz4 = cs.z1 - es.z1, cs.z4 - es.z4
            if ratio > v1.SIGMA_RATIO_MAX or abs(dz1) > Z1_CALIPER or abs(dz4) > v1.Z_CALIPER:
                continue
            ranked.append((math.hypot(dz1, dz4), ratio, c.d, c))
        ranked.sort(key=lambda r: r[:3])
        pairs[e.key] = [r[3] for r in ranked[: v1.MAX_CONTROLS]]
    return pairs


def majors(features: Features, tapes: dict[str, Any], sizes_path: Path) -> dict[str, Any]:
    pools = v1.pools_of(features)
    top = {m: p[:TOP] for m, p in pools.items()}
    events, cands, funnel = v1.decide(features, tapes, top, "blocks", mc.wide)
    check = [c for c in cands if mc.RULES["check"].admits(c.state)]
    members = {m.key: m for m in (*events, *check)}
    turnover = mc.turnovers(features, top)
    rhos = {
        leg: {m: v1.month_rho(features, m, top[m], relative=leg == "b") for m in v1.MONTHS}
        for leg in ("a", "b")
    }
    days_scale = v1.decision_days("blocks") / v1.decision_days("full")
    planned = v1.planned_member_hours(features, top, "blocks")
    full_planned = v1.planned_member_hours(features, top, "full")

    def compare(pairs: dict[Key, list[v1.Member]]) -> dict[str, Any]:
        with_control = sum(1 for e in events if pairs.get(e.key))
        mdes = v1.power(features, events, pairs, rhos, days_scale)
        return {
            "with_control": with_control,
            "coverage": round(with_control / len(events), 4) if events else None,
            "balance": mc.balance(v1.weights_excess(events, pairs), members, turnover),
            "mde_bps": {k: v["mde_bps"] if v else None for k, v in mdes.items()},
        }

    days: dict[str, set[str]] = defaultdict(set)
    for month, m_end in zip(v1.MONTHS, (*v1.MONTHS[1:], WINDOW_END_MS), strict=True):
        lo, hi = max(month, v1.FIRST_DECISION_MS), min(m_end, v1.LAST_DECISION_MS + HOUR_MS)
        if lo >= hi:
            continue
        for symbol in top[month]:
            d = lo - lo % DAY_MS - v1.SCOPE_LEAD_DAYS * DAY_MS
            while d < hi:
                days[symbol].add(_day(d))
                d += DAY_MS
    listed, sizes = v1.load_archive_sizes(verified(sizes_path, v1.ARCHIVE_SIZES_SHA256))
    symbols = Counter(e.symbol for e in events)
    return {
        "top": TOP,
        "funnel": dict(sorted(funnel.items())),
        "planned_member_hours": planned,
        "events": len(events),
        "clusters": len({e.cluster for e in events}),
        "symbols": len(symbols),
        "events_by_symbol": dict(symbols.most_common()),
        "projected_events": round(len(events) * full_planned / planned, 1) if planned else 0.0,
        "candidates": len(check),
        "rho_a": {_iso(m): round(r, 4) for m, r in rhos["a"].items()},
        "same_hour": compare(v1.match(events, check, z1_caliper=Z1_CALIPER, by_tercile=False)),
        "same_instrument_7d": compare(own_match(events, check)),
        "full_year_symbols": len(days),
        "full_year_tape": estimate({s: sorted(v) for s, v in days.items()}, listed, sizes),
    }


# ---------------------------------------------------------------- command line


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    for name in ("features", "manifest", "scope", "archive-sizes"):
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
    if args.archive_sizes_sha256 != v1.ARCHIVE_SIZES_SHA256:
        raise SystemExit(f"the size estimate pins HYP-032's record {v1.ARCHIVE_SIZES_SHA256}")
    features = load_features(verified(args.features, args.features_sha256))
    scope = json.loads(verified(args.scope, args.scope_sha256).read_bytes())
    v1.bound_tape(args.manifest, args.manifest_sha256, scope, args.scope_sha256, features,
                  "blocks")  # fmt: skip
    tapes, meta = load_tape(args.manifest, args.manifest_sha256)
    report = {
        "version": VERSION,
        "step": "planning: pre-t features only, no trade return",
        "features_sha256": v1.FEATURES_SHA256,
        "pilot_manifest": meta,
        "scope_sha256": args.scope_sha256,
        "family5": family5(features),
        "majors": majors(features, tapes, args.archive_sizes),
        "code_revision": normalize_code_revision(args.code_revision),
        "working_tree_dirty": args.working_tree_dirty,
    }
    digest = publish_once(args.output_dir / "result.json", report)
    sys.stdout.write(f"result.json sha256 {digest}\n")


if __name__ == "__main__":
    main()
