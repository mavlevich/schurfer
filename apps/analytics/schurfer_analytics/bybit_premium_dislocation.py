"""HYP-034: perpetual discount to the index, long, steps 1 and 2.

Implements docs/research/bybit-premium-dislocation-v1.md.

- ``fetch-premium``: 60-minute premium index klines for every instrument of HYP-032's
  verified features snapshot over the discovery window, stored once in a gzip JSON-lines
  snapshot.
- ``counts``: reads only the two verified snapshots (the premium one pinned in code
  after its fetch) and publishes the funnel, the events with their matched controls
  (``pairs.json``), the balance of pre-decision returns, the overlap components and the
  power estimate with the floor. Nothing after any decision time t is read.

Amendment 1 (2026-11-08, before any return): controls also need z1 within 0.5, the
nearest are ranked by the Euclidean distance of (z4, z1) differences, the balance SMD
weighs its spreads like its means, and |SMD of r1| at most 0.10 is a floor item.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import gzip
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from .bybit_carry_feasibility import publish_once
from .bybit_compressed_spring import (
    FIRST_DECISION_MS,
    HOUR_MS,
    RANK_HOURS,
    WINDOW_END_MS,
    WINDOW_FIRST_MS,
    Features,
    RollingRank,
    load_features,
)
from .bybit_post_funding_screen import SnapshotWriter, _pages, file_sha256, recovered, verified
from .bybit_squeeze_continuation import (
    BENCHMARK,
    DESIGN_EFFECT,
    DESIGN_EFFECTS,
    DETERMINED,
    FEATURES_SHA256,
    HOLD_H,
    INFLATION,
    INFLATIONS,
    LAST_DECISION_MS,
    MAX_CONTROLS,
    MIN_TURNOVER_USD,
    Z_POWER,
    Instrument,
    _day,
    _iso,
    _week,
    change,
    control_shape,
    filled,
    floor,
    shape,
    turnover_bin,
    weights_excess,
    weights_net,
)
from .exchange_registry import EXCHANGE_FACTORIES
from .reporting import markdown_table, normalize_code_revision

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

VERSION: Final = "bybit_premium_dislocation_v1"
AMENDMENT: Final = 1
# Pinned by a commit after the fetch and before the first count; None refuses counts.
PREMIUM_SHA256: Final[str | None] = (
    "ec05a2c39f6e9eaa5fba52396ff286b331253ad42a30678e24204c720c28e43a"
)

P_DEPTH: Final = 0.005  # |P(t)| at least 0.50%
P_RANK_LOW: Final = 0.05
P_RANK_HIGH: Final = 0.95
ORDINARY_ABS: Final = 0.001  # ordinary hour: |P(t)| below 0.10%
FIRST_LOOKBACK_H: Final = 24
HOLD_MS: Final = HOLD_H * HOUR_MS

CONTROL_WINDOW_MS: Final = 72 * HOUR_MS
Z4_CALIPER: Final = 0.5
Z1_CALIPER: Final = 0.5  # Amendment 1
SMD_R1_MAX: Final = 0.10  # Amendment 1: the balance gate

KLINE_PAGE_LIMIT: Final = 1000
CONCURRENCY: Final = 4

SIDES: Final = ("neg", "pos")


# ---------------------------------------------------------------- fetching


async def fetch_premium(client: Any, features: Features, path: Path) -> dict[str, Any]:
    recovered.clear()
    writer = SnapshotWriter(path)
    writer.record("version", None, {}, {"version": VERSION})
    errors: dict[str, str] = {}
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def one(symbol: str, end: int) -> None:
        if end >= WINDOW_END_MS:
            raise SystemExit(f"refusing a request ending at or after {WINDOW_END_MS} ms")
        async with semaphore:
            try:
                await _pages(
                    client.public_get_v5_market_premium_index_price_kline,
                    {"category": "linear", "symbol": symbol, "interval": "60",
                     "start": WINDOW_FIRST_MS, "end": end, "limit": KLINE_PAGE_LIMIT},
                    what=f"premium 60 {symbol}",
                    limit=KLINE_PAGE_LIMIT,
                    oldest=lambda item: int(item[0]),
                    record=lambda p, r: writer.record("premium_60", symbol, p, r),
                )  # fmt: skip
            except Exception as exc:  # one symbol's failure is counted, not a stop
                errors[symbol] = str(exc)[:300]
                writer.record("fetch_error", symbol, {}, {"error": errors[symbol]})

    items = sorted(features.instruments.items())
    try:
        await asyncio.gather(*(one(s, series.end_ms) for s, series in items))
    finally:
        writer.close()
    return {
        "version": VERSION,
        "symbols": len(items),
        "errors": errors,
        "responses": writer.written,
        "recovered_requests": dict(recovered),
    }


@dataclass(frozen=True)
class Premium:
    closes: dict[str, dict[int, float]]  # symbol -> bar start -> close
    failed: tuple[str, ...]


def load_premium(path: Path) -> Premium:
    version = ""
    closes: dict[str, dict[int, float]] = defaultdict(dict)
    failed: set[str] = set()
    with gzip.open(path, "rb") as stream:
        for line_bytes in stream:
            line = json.loads(line_bytes)
            kind, symbol = line["kind"], line["symbol"]
            if kind == "version":
                version = line["response"]["version"]
            elif kind == "fetch_error":
                failed.add(symbol)
            elif kind == "premium_60":
                result = line["response"]["result"]
                if result.get("symbol") not in (None, symbol):
                    failed.add(symbol)
                    continue
                for item in result["list"]:
                    start = int(item[0])
                    if WINDOW_FIRST_MS <= start < WINDOW_END_MS:
                        closes[symbol][start] = float(item[4])
    if version != VERSION:
        raise SystemExit(f"premium snapshot is not {VERSION}: {version!r}")
    return Premium({s: c for s, c in closes.items() if s not in failed}, tuple(sorted(failed)))


# ---------------------------------------------------------------- decisions


@dataclass(frozen=True, slots=True)
class Hour:
    """A liquid decision hour t with its pre-t path; role is event, v1_event or ordinary.

    An ordinary hour carries for each side whether it is in that side's control pool
    (no dislocation of that side in the 24 hours before t)."""

    symbol: str
    d: int  # the decision time t
    role: str
    r1: float
    r4: float
    z4: float
    z1: float
    turnover: float
    pool_neg: bool = False
    pool_pos: bool = False

    @property
    def key(self) -> tuple[str, int]:
        return self.symbol, self.d

    @property
    def cluster(self) -> tuple[str, str]:
        return self.symbol, _day(self.d)

    @property
    def down(self) -> bool:
        return self.r1 < 0


def dislocation(p: float | None, rank: float | None, side: str) -> str:
    """Whether P(t) is a dislocation of that side: true, false or a reason."""
    if p is None:
        return "premium_gap"
    if (p > -P_DEPTH) if side == "neg" else (p < P_DEPTH):
        return "false"
    if rank is None:
        return "rank_history_short"
    return "true" if (rank <= P_RANK_LOW if side == "neg" else rank >= P_RANK_HIGH) else "false"


def first(statuses: Mapping[int, str], t: int) -> str:
    """No dislocation in the 24 hours before t; undefined by the first reason found."""
    prior = [statuses.get(t - k * HOUR_MS, "premium_gap") for k in range(1, FIRST_LOOKBACK_H + 1)]
    if "true" in prior:
        return "false"
    for reason in ("premium_gap", "rank_history_short"):
        if reason in prior:
            return reason
    return "true"


def scan(
    inst: Instrument,
    premium: Mapping[int, float],
    funnel: Counter[str],
    first_t: int = FIRST_DECISION_MS,
    last_t: int = LAST_DECISION_MS,
) -> list[Hour]:
    """Every decision hour t of one instrument, from its own data before t."""
    series = inst.series
    rank_of = RollingRank()
    statuses: dict[str, dict[int, str]] = {side: {} for side in SIDES}
    out: list[Hour] = []
    last = min(last_t, series.end_ms - series.end_ms % HOUR_MS)
    for t in range(first_t - RANK_HOURS * HOUR_MS, last + 1, HOUR_MS):
        p = premium.get(t - HOUR_MS)
        rank = rank_of.rank(p) if p is not None else None
        for side in SIDES:
            statuses[side][t] = dislocation(p, rank, side)
        if t >= first_t and t >= series.launch_ms:
            hour = _decide(inst, funnel, t, p, statuses)
            if hour is not None:
                out.append(hour)
        rank_of.push(p)
    return out


def _decide(
    inst: Instrument,
    funnel: Counter[str],
    t: int,
    p: float | None,
    statuses: Mapping[str, Mapping[int, str]],
) -> Hour | None:
    funnel["grid"] += 1
    turnover = inst.turnover_before(t + HOUR_MS)  # the 24 bars ending t
    if turnover is None:
        funnel["liquidity_kline_gap"] += 1
        return None
    if turnover < MIN_TURNOVER_USD:
        funnel["liquidity_illiquid"] += 1
        return None
    funnel["liquidity_liquid"] += 1
    status = {side: statuses[side][t] for side in SIDES}
    funnel[f"neg_{status['neg']}"] += 1
    funnel[f"pos_{status['pos']}"] += 1
    if status["neg"] in DETERMINED:
        funnel["premium_determinable"] += 1
    firsts = {side: first(statuses[side], t) for side in SIDES}
    role = None
    if status["neg"] == "true":
        funnel[f"neg_first_{firsts['neg']}"] += 1
        role = "event" if firsts["neg"] == "true" else None
    elif status["pos"] == "true":
        funnel[f"pos_first_{firsts['pos']}"] += 1
        role = "v1_event" if firsts["pos"] == "true" else None
    elif p is not None and abs(p) < ORDINARY_ABS:
        role = "ordinary"
    if role is None:
        return None
    r1, r4 = change(inst.series, t), change(inst.series, t, HOLD_H)
    sd = inst.hourly_sd(t + HOUR_MS)  # the 336 hourly returns ending t
    if r1 is None or r4 is None or sd is None:
        funnel[f"{role}_path_undefined"] += 1
        return None
    funnel[f"{role}_kept"] += 1
    pools = {"pool_neg": firsts["neg"] == "true", "pool_pos": firsts["pos"] == "true"}
    if role == "ordinary":
        funnel["ordinary_pool_neg"] += pools["pool_neg"]
        funnel["ordinary_pool_pos"] += pools["pool_pos"]
    else:
        pools = {"pool_neg": False, "pool_pos": False}
    return Hour(inst.symbol, t, role, r1, r4, r4 / (2 * sd), r1 / sd, turnover, **pools)


# ---------------------------------------------------------------- matching


def match(
    events: Sequence[Hour], pool: Sequence[Hour], z1_caliper: float | None = Z1_CALIPER
) -> dict[tuple[str, int], list[Hour]]:
    """For each event, at most 5 nearest controls inside the calipers, the same ISO week,
    turnover bin and last-hour direction, within 72 hours, on another instrument.

    Amended: z1 within the caliper too, nearest by the Euclidean distance of the (z4, z1)
    differences. ``z1_caliper=None`` is the original rule (z4 only, nearest by |z4
    difference|), kept to report which events the amendment leaves without a control."""
    buckets: dict[tuple[str, int, bool], list[Hour]] = defaultdict(list)
    for h in pool:
        buckets[(_week(h.d), turnover_bin(h.turnover), h.down)].append(h)
    for bucket in buckets.values():
        bucket.sort(key=lambda h: h.z4)
    keys = {k: [h.z4 for h in b] for k, b in buckets.items()}
    pairs: dict[tuple[str, int], list[Hour]] = {}
    for e in events:
        k = (_week(e.d), turnover_bin(e.turnover), e.down)
        bucket, z = buckets.get(k, []), keys.get(k, [])
        lo = bisect.bisect_left(z, e.z4 - Z4_CALIPER)
        hi = bisect.bisect_right(z, e.z4 + Z4_CALIPER)
        ranked = []
        for h in bucket[lo:hi]:
            if h.symbol == e.symbol or abs(h.d - e.d) > CONTROL_WINDOW_MS:
                continue
            dz4, dz1 = h.z4 - e.z4, h.z1 - e.z1
            if z1_caliper is not None and abs(dz1) > z1_caliper:
                continue
            distance = abs(dz4) if z1_caliper is None else math.hypot(dz4, dz1)
            tie = abs(math.log(h.turnover / e.turnover))
            ranked.append((distance, tie, h.symbol, h.d, h))
        ranked.sort(key=lambda r: r[:4])
        pairs[e.key] = [r[4] for r in ranked[:MAX_CONTROLS]]
    return pairs


# ---------------------------------------------------------------- power


def components(units: Sequence[tuple[str, int]]) -> list[list[tuple[str, int]]]:
    """Overlap components: holds on one instrument starting less than H apart share
    returns, and are chained transitively."""
    by_symbol: dict[str, list[int]] = defaultdict(list)
    for symbol, d in units:
        by_symbol[symbol].append(d)
    out: list[list[tuple[str, int]]] = []
    for symbol in sorted(by_symbol):
        times = sorted(set(by_symbol[symbol]))
        group = [times[0]]
        for d in times[1:]:
            if d - group[-1] < HOLD_MS:
                group.append(d)
            else:
                out.append([(symbol, x) for x in group])
                group = [d]
        out.append([(symbol, x) for x in group])
    return out


def mde_overlap(
    weights: Mapping[tuple[str, int], float],
    scales: Mapping[tuple[str, int], float],
    inflation: float,
    design_effect: float,
) -> float:
    """MDE with each overlap component's contribution bounded for any correlation inside
    it: (sum of |w| x inflation x sigma)^2."""
    variance = math.fsum(
        math.fsum(abs(weights[u]) * inflation * scales[u] for u in group) ** 2
        for group in components(list(weights))
    )
    return Z_POWER * math.sqrt(variance * design_effect)


def overlap_shape(weights: Mapping[tuple[str, int], float]) -> dict[str, int]:
    groups = components(list(weights))
    return {
        "units": len(weights),
        "components": len(groups),
        "largest_component": max((len(g) for g in groups), default=0),
        "units_in_shared_components": sum(len(g) for g in groups if len(g) > 1),
    }


def power(
    instruments: Mapping[str, Instrument],
    events: Sequence[Hour],
    pairs: Mapping[tuple[str, int], Sequence[Hour]],
) -> dict[str, Any]:
    legs = {
        "a_net": (weights_net(events), False),
        "b_excess": (weights_excess(events, pairs), True),
    }
    out: dict[str, Any] = {}
    for name, (weights, relative) in legs.items():
        if not weights:
            out[name] = None
            continue
        raw = {(s, d): instruments[s].scale(d + HOUR_MS, relative=relative) for s, d in weights}
        scales, missing = filled(raw)
        grid = {
            f"inflation_{i}_deff_{k}": round(mde_overlap(weights, scales, i, k) * 10_000, 2)
            for i in INFLATIONS
            for k in DESIGN_EFFECTS
        }
        primary = mde_overlap(weights, scales, INFLATION, DESIGN_EFFECT)
        out[name] = {
            **overlap_shape(weights),
            "scales_substituted": missing,
            "mde_bps": round(primary * 10_000, 2),
            "mde_unrounded": primary,
            "sensitivity_bps": grid,
        }
    return out


def balance(
    events: Sequence[Hour], pairs: Mapping[tuple[str, int], Sequence[Hour]]
) -> dict[str, Any] | None:
    """Weighted means (as in (b)) and standardized mean differences of the raw r1 and
    r4 of the paired events and their controls; returns before t only. The spreads use
    the same weights as the means (Amendment 1)."""
    weights = weights_excess(events, pairs)
    if not weights:
        return None
    hours = {e.key: e for e in events} | {c.key: c for cs in pairs.values() for c in cs}
    out: dict[str, Any] = {}
    for name in ("r1", "r4"):
        sides: dict[bool, list[tuple[float, float]]] = {True: [], False: []}
        for key, w in weights.items():
            sides[w > 0].append((abs(w), getattr(hours[key], name)))
        stats = {side: _weighted(vals) for side, vals in sides.items()}
        pooled = math.sqrt((stats[True][1] + stats[False][1]) / 2)
        diff = stats[True][0] - stats[False][0]
        smd = diff / pooled if pooled > 0 else None
        out[name] = {
            "event_mean_bps": round(stats[True][0] * 10_000, 1),
            "control_mean_bps": round(stats[False][0] * 10_000, 1),
            "smd": round(smd, 3) if smd is not None else None,
            "smd_unrounded": smd,
        }
    return out


def _weighted(values: Sequence[tuple[float, float]]) -> tuple[float, float]:
    """Weighted mean and variance sum(a (v - m)^2) / sum(a) of (a, v) pairs."""
    total = math.fsum(a for a, _ in values)
    mean = math.fsum(a * v for a, v in values) / total
    return mean, math.fsum(a * (v - mean) ** 2 for a, v in values) / total


def lost_control(
    events: Sequence[Hour],
    pairs: Mapping[tuple[str, int], Sequence[Hour]],
    original: Mapping[tuple[str, int], Sequence[Hour]],
) -> dict[str, Any]:
    """Events with a control under the original rule and none under the amended one,
    against those that keep one; pre-t values only."""
    groups = {
        "lost": [e for e in events if original.get(e.key) and not pairs.get(e.key)],
        "kept": [e for e in events if pairs.get(e.key)],
    }
    out: dict[str, Any] = {name: _describe(group) for name, group in groups.items()}
    out["mean_r1_bps_all_events"] = _mean_bps([e.r1 for e in events])
    return out


def _describe(group: Sequence[Hour]) -> dict[str, Any]:
    clusters = Counter(e.symbol for e in group)
    return {
        "events": len(group),
        "down_last_hour_share": round(sum(e.down for e in group) / len(group), 3)
        if group
        else None,
        "median_z1": round(_median([e.z1 for e in group]), 3) if group else None,
        "median_z4": round(_median([e.z4 for e in group]), 3) if group else None,
        "mean_r1_bps": _mean_bps([e.r1 for e in group]),
        "turnover_bins": dict(sorted(Counter(turnover_bin(e.turnover) for e in group).items())),
        "top5_symbol_share": (
            round(sum(n for _, n in clusters.most_common(5)) / len(group), 3) if group else None
        ),
    }


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _mean_bps(values: Sequence[float]) -> float | None:
    return round(math.fsum(values) / len(values) * 10_000, 1) if values else None


def gate(verdict: dict[str, Any], bal: dict[str, Any] | None) -> dict[str, Any]:
    """Amendment 1: |SMD of r1| at most 0.10 on the amended pairs; checked after the data
    items and before power."""
    smd = bal["r1"]["smd_unrounded"] if bal else None
    balanced = smd is not None and abs(smd) <= SMD_R1_MAX
    out = {**verdict, "balance": {"r1_smd": balanced}}
    if verdict["verdict"] != "insufficient_data" and not balanced:
        out["verdict"] = "insufficient_balance"
    return out


# ---------------------------------------------------------------- report


def side_report(
    instruments: Mapping[str, Instrument], events: Sequence[Hour], pool: Sequence[Hour]
) -> tuple[dict[str, Any], dict[tuple[str, int], list[Hour]]]:
    pairs = match(events, pool)
    original = match(events, pool, z1_caliper=None)
    report = {
        "events": shape(events),
        "controls": control_shape(events, pairs),
        "controls_original_rule": control_shape(events, original),
        "lost_control": lost_control(events, pairs, original),
        "events_by_iso_week": dict(sorted(Counter(_week(e.d) for e in events).items())),
        "events_down_last_hour": sum(1 for e in events if e.down),
        "balance": balance(events, pairs),
        "power": power(instruments, events, pairs) if events else {},
    }
    return report, pairs


def frozen_pairs(
    events: Sequence[Hour], pairs: Mapping[tuple[str, int], Sequence[Hour]]
) -> list[dict[str, Any]]:
    return [
        {
            "symbol": e.symbol,
            "t": _iso(e.d),
            "controls": [{"symbol": c.symbol, "t": _iso(c.d)} for c in pairs[e.key]],
        }
        for e in sorted(events, key=lambda x: (x.d, x.symbol))
    ]


def build(features: Features, premium: Premium) -> tuple[dict[str, Any], dict[str, Any]]:
    if BENCHMARK not in features.instruments:
        raise SystemExit(f"{BENCHMARK} is not in the snapshot")
    benchmark = features.instruments[BENCHMARK]
    symbols = sorted(s for s in features.instruments if s in premium.closes)
    instruments = {s: Instrument(s, features.instruments[s], benchmark) for s in symbols}
    funnel: Counter[str] = Counter()
    hours: list[Hour] = []
    for symbol in symbols:
        hours += scan(instruments[symbol], premium.closes[symbol], funnel)
    events = [h for h in hours if h.role == "event"]
    v1_events = [h for h in hours if h.role == "v1_event"]
    primary, pairs = side_report(instruments, events, [h for h in hours if h.pool_neg])
    v1, v1_pairs = side_report(instruments, v1_events, [h for h in hours if h.pool_pos])
    determinable = (funnel["premium_determinable"], funnel["liquidity_liquid"])
    report = {
        "version": VERSION,
        "amendment": AMENDMENT,
        "step": "steps 1 and 2: counts and power, no return after any decision",
        "features_sha256": FEATURES_SHA256,
        "premium_sha256": PREMIUM_SHA256,
        "instruments": len(instruments),
        "without_premium": sorted(set(features.instruments) - set(premium.closes)),
        "failed_premium": list(premium.failed),
        "first_decision": _iso(FIRST_DECISION_MS),
        "last_decision": _iso(LAST_DECISION_MS),
        "funnel": dict(sorted(funnel.items())),
        "premium_determinable_share": (
            round(determinable[0] / determinable[1], 4) if determinable[1] else None
        ),
        "primary": primary,
        "v1": v1,
        "floor": gate(
            floor(primary["events"], primary["controls"], determinable, primary["power"]),
            primary["balance"],
        ),
    }
    frozen = {
        "version": VERSION,
        "amendment": AMENDMENT,
        "primary": frozen_pairs(events, pairs),
        "v1": frozen_pairs(v1_events, v1_pairs),
    }
    return report, frozen


def render_markdown(report: dict[str, Any]) -> str:
    funnel = [(k, f"{v:,}") for k, v in report["funnel"].items()]
    lines = [f"# {VERSION}, amendment {AMENDMENT}: counts and power", ""]
    lines += [*markdown_table(("Funnel item", "Decision hours"), funnel), ""]
    for side in ("primary", "v1"):
        part = report[side]
        lines += [f"## {side}", ""]
        rows = [(k, str(v)) for k, v in {**part["events"], **part["controls"]}.items()]
        lines += [*markdown_table(("Item", "Value"), rows), ""]
        original = part["controls_original_rule"]["events_with_control"]
        lines += [f"Events with a control under the original rule: {original}", ""]
        lost = part["lost_control"]
        lost_rows = [(k, str(lost["lost"][k]), str(lost["kept"][k])) for k in lost["kept"]]
        lines += [*markdown_table(("Pre-t item", "Lost a control", "Keep one"), lost_rows), ""]
        lines += [f"Mean r1 of all events, bps: {lost['mean_r1_bps_all_events']}", ""]
        if part["balance"]:
            bal = [
                (k, str(b["event_mean_bps"]), str(b["control_mean_bps"]), str(b["smd"]))
                for k, b in part["balance"].items()
            ]
            head = ("Pre-t return", "Event mean, bps", "Control mean, bps", "SMD")
            lines += [*markdown_table(head, bal), ""]
        power_rows = [
            (leg, str(v["units"]), str(v["components"]), str(v["largest_component"]),
             str(v["scales_substituted"]), f"{v['mde_bps']:.2f}")
            if v
            else (leg, "0", "0", "0", "0", "not estimable")
            for leg, v in part["power"].items()
        ]  # fmt: skip
        power_head = ("Leg", "Units", "Components", "Largest", "Scales substituted", "MDE, bps")
        lines += [*markdown_table(power_head, power_rows), ""]
    verdict = report["floor"]
    items = {**verdict["data"], **verdict.get("balance", {}), **verdict["power"]}
    floor_rows = [(k, str(v)) for k, v in items.items()]
    lines += [*markdown_table(("Floor item (primary)", "Holds"), floor_rows), ""]
    lines += [f"Premium determinable share: {report['premium_determinable_share']}", ""]
    lines += [f"Verdict: **{verdict['verdict']}**"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("fetch-premium", help="60-minute premium index klines")
    fetch.add_argument("--features", type=Path, required=True)
    fetch.add_argument("--features-sha256", required=True)
    fetch.add_argument("--snapshot", type=Path, required=True)
    counts = sub.add_parser("counts", help="funnel, pairs, balance, power and floor")
    counts.add_argument("--features", type=Path, required=True)
    counts.add_argument("--features-sha256", required=True)
    counts.add_argument("--premium", type=Path, required=True)
    counts.add_argument("--premium-sha256", required=True)
    counts.add_argument("--output-dir", type=Path, required=True)
    counts.add_argument("--code-revision", required=True)
    dirty = counts.add_mutually_exclusive_group(required=True)
    dirty.add_argument("--working-tree-dirty", dest="working_tree_dirty", action="store_true")
    dirty.add_argument("--no-working-tree-dirty", dest="working_tree_dirty", action="store_false")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.features_sha256 != FEATURES_SHA256:
        raise SystemExit(f"the protocol pins the features snapshot {FEATURES_SHA256}")
    if args.command == "fetch-premium":
        if args.snapshot.exists():
            raise SystemExit(f"{args.snapshot} exists; a snapshot is written once")
        features = load_features(verified(args.features, args.features_sha256))

        async def collect() -> dict[str, Any]:
            client = EXCHANGE_FACTORIES["bybit"]()
            try:
                return await fetch_premium(client, features, args.snapshot)
            finally:
                await client.close()

        coverage = asyncio.run(collect())
        sys.stdout.write(json.dumps(coverage | {"sha256": file_sha256(args.snapshot)}) + "\n")
        return
    if PREMIUM_SHA256 is None or args.premium_sha256 != PREMIUM_SHA256:
        raise SystemExit(f"the premium snapshot must be pinned in code first ({PREMIUM_SHA256})")
    features = load_features(verified(args.features, args.features_sha256))
    premium = load_premium(verified(args.premium, args.premium_sha256))
    report, frozen = build(features, premium)
    render_markdown(report)  # fail before anything is published, not after
    report |= {
        "pairs_sha256": publish_once(args.output_dir / "pairs.json", frozen),
        "code_revision": normalize_code_revision(args.code_revision),
        "working_tree_dirty": args.working_tree_dirty,
    }
    digest = publish_once(args.output_dir / "counts.json", report)
    text = render_markdown(report)
    (args.output_dir / "counts.md").write_text(text, encoding="utf-8")
    sys.stdout.write(text + f"\ncounts sha256 {digest}\n")
