"""HYP-033: short-squeeze continuation after a first impulse, steps 1 and 2.

Implements the counts (step 1) and the power estimate (step 2) of
docs/research/bybit-squeeze-continuation-v1.md on HYP-032's verified features snapshot.
No request is made and nothing after any decision time t_d is read: the report holds
counts, the frozen event-control pairs and minimum detectable effects computed from
volatility before each impulse.

- ``counts``: the funnel of decision hours, the four cells of first impulses, the
  events with their matched controls (``pairs.json``), the power estimate and the floor.
"""

from __future__ import annotations

import argparse
import bisect
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from .bybit_carry_feasibility import publish_once
from .bybit_compressed_spring import (
    FIRST_DECISION_MS,
    HOUR_MS,
    RANK_HOURS,
    RANK_MIN_DEFINED,
    SECOND_HALF_MS,
    Features,
    RollingRank,
    Series,
    last_decision_ms,
    last_rate_before,
    load_features,
)
from .bybit_post_funding_screen import verified
from .reporting import markdown_table, normalize_code_revision

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

VERSION: Final = "bybit_squeeze_continuation_v1"
FEATURES_SHA256: Final = "a3b54d6ce092a73afbd4911e8ea72862a707846b45fbb23d05ec1d91f71ebab6"
BENCHMARK: Final = "BTCUSDT"

HOLD_H: Final = 4
LAST_DECISION_MS: Final = last_decision_ms(HOLD_H)
MIN_TURNOVER_USD: Final = 2_000_000.0
TURNOVER_BARS: Final = 24
IMPULSE_MIN_RETURN: Final = 0.02
IMPULSE_MIN_Z: Final = 3.0
FIRST_LOOKBACK_H: Final = 24
G_MIN_PCT: Final = 0.80

CONTROL_WINDOW_MS: Final = 72 * HOUR_MS
Z_CALIPER: Final = 0.5
MAX_CONTROLS: Final = 5
TURNOVER_BIN_EDGES: Final = (10_000_000.0, 50_000_000.0)

Z_POWER: Final = 1.645 + 0.842
INFLATION: Final = 1.5
DESIGN_EFFECT: Final = 1.2
INFLATIONS: Final = (1.0, 1.5, 2.0)
DESIGN_EFFECTS: Final = (1.0, 1.2, 1.5)
SUBSTITUTE_PCT: Final = 0.90
MDE_MAX: Final = 0.005

FLOOR_CLUSTERS: Final = 150
FLOOR_SYMBOLS: Final = 40
FLOOR_WEEKS: Final = 26
TOP: Final = 5
TOP_SHARE_MAX: Final = 0.30
CONTROL_SHARE_MIN: Final = 0.80
DETERMINABLE_MIN: Final = 0.90

DETERMINED: Final = ("true", "false")


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def _week(ms: int) -> str:
    iso = datetime.fromtimestamp(ms / 1000, UTC).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------- prices


def close_at(series: Series, h: int) -> float | None:
    """The close at time h: the close of the bar [h - 1 h, h)."""
    bar = series.bars.get(h - HOUR_MS)
    return bar[2] if bar is not None and bar[2] > 0 else None


def change(series: Series, h: int, span_h: int = 1) -> float | None:
    """close(h) / close(h - span) - 1, or None when either close is missing."""
    now, before = close_at(series, h), close_at(series, h - span_h * HOUR_MS)
    return None if now is None or before is None else now / before - 1


def sample_sd(values: Iterable[float | None]) -> float | None:
    """Sample standard deviation under the 80% rule; None when degenerate."""
    defined = [v for v in values if v is not None]
    if len(defined) < RANK_MIN_DEFINED:
        return None
    mean = math.fsum(defined) / len(defined)
    var = math.fsum((v - mean) ** 2 for v in defined) / (len(defined) - 1)
    return math.sqrt(var) if var > 0 else None


def oi_change(series: Series, d: int) -> float | None:
    """G at decision d = t + 1 h: OI(record stamped t) / OI(record stamped t - 2 h) - 1."""
    now = series.oi.get(d - HOUR_MS)
    before = series.oi.get(d - 3 * HOUR_MS)
    if now is None or before is None or before <= 0:
        return None
    return now / before - 1


class Instrument:
    """One instrument's impulse logic, memoized per hour."""

    def __init__(self, symbol: str, series: Series, benchmark: Series) -> None:
        self.symbol = symbol
        self.series = series
        self.benchmark = benchmark
        self._sd: dict[int, float | None] = {}
        self._trigger: dict[int, str] = {}
        starts = sorted(series.bars)
        self._base = starts[0] if starts else 0
        size = (starts[-1] - self._base) // HOUR_MS + 1 if starts else 0
        self._sum = [0.0] * (size + 1)
        self._count = [0] * (size + 1)
        for i in range(size):
            bar = series.bars.get(self._base + i * HOUR_MS)
            self._sum[i + 1] = self._sum[i] + (bar[3] if bar is not None else 0.0)
            self._count[i + 1] = self._count[i] + (bar is not None)

    def turnover_before(self, t: int) -> float | None:
        """Turnover of the 24 bars ending t - 1 h (before the impulse bar [t - 1 h, t))."""
        lo = (t - (TURNOVER_BARS + 1) * HOUR_MS - self._base) // HOUR_MS
        hi = (t - 2 * HOUR_MS - self._base) // HOUR_MS + 1
        if lo < 0 or hi > len(self._sum) - 1:
            return None
        if self._count[hi] - self._count[lo] < TURNOVER_BARS:
            return None
        return self._sum[hi] - self._sum[lo]

    def hourly_sd(self, t: int) -> float | None:
        """SD of the 336 hourly returns of the bars before the bar ending t."""
        if t not in self._sd:
            self._sd[t] = sample_sd(
                change(self.series, t - k * HOUR_MS) for k in range(1, RANK_HOURS + 1)
            )
        return self._sd[t]

    def trigger(self, t: int) -> str:
        """Whether the bar ending t is an impulse: true, false or a reason."""
        if t not in self._trigger:
            r1 = change(self.series, t)
            if r1 is None:
                status = "kline_gap"
            elif r1 < IMPULSE_MIN_RETURN:
                status = "false"
            elif (sd := self.hourly_sd(t)) is None:
                status = "rank_history_short"
            else:
                status = "true" if r1 / sd >= IMPULSE_MIN_Z else "false"
            self._trigger[t] = status
        return self._trigger[t]

    def first(self, t: int) -> str:
        """No trigger in the 24 bars before the impulse bar ending t."""
        statuses = [self.trigger(t - k * HOUR_MS) for k in range(1, FIRST_LOOKBACK_H + 1)]
        if "true" in statuses:
            return "false"
        if "kline_gap" in statuses:
            return "kline_gap"
        if "rank_history_short" in statuses:
            return "rank_history_short"
        return "true"

    def scale(self, t: int, *, relative: bool) -> float | None:
        """SD of the 4-hour returns at each hourly close of the 14 days before the
        impulse bar ending t; BTC-relative when ``relative``."""
        values: list[float | None] = []
        for k in range(1, RANK_HOURS + 1):
            h = t - k * HOUR_MS
            r = change(self.series, h, HOLD_H)
            if r is not None and relative:
                b = change(self.benchmark, h, HOLD_H)
                r = None if b is None else r - b
            values.append(r)
        return sample_sd(values)


# ---------------------------------------------------------------- decisions


@dataclass(frozen=True)
class Impulse:
    """A liquid first impulse with its waiting bar; d is the decision time t + 1 h."""

    symbol: str
    d: int
    z1: float
    zw: float
    turnover: float
    f: str
    g: str

    @property
    def key(self) -> tuple[str, int]:
        return self.symbol, self.d

    @property
    def cluster(self) -> tuple[str, str]:
        return self.symbol, _day(self.d)

    @property
    def cell(self) -> str:
        """event (F and G), f_only, g_only, neither, pool_partial or undefined."""
        if self.f == "true" and self.g == "true":
            return "event"
        if self.f in DETERMINED and self.g in DETERMINED:
            return {("true", "false"): "f_only", ("false", "true"): "g_only"}.get(
                (self.f, self.g), "neither"
            )
        return "pool_partial" if "false" in (self.f, self.g) else "undefined"

    @property
    def is_control_candidate(self) -> bool:
        return "false" in (self.f, self.g)


def funding_status(times: Sequence[int], rates: Sequence[float], d: int) -> str:
    rate = last_rate_before(times, rates, d)
    if rate is None:
        return "no_settlement"
    return "true" if rate < 0 else "false"


def oi_status(g: float | None, rank: float | None) -> str:
    if g is None:
        return "oi_gap"
    if rank is None:
        return "rank_history_short"
    return "true" if g > 0 and rank >= G_MIN_PCT else "false"


def scan(
    inst: Instrument,
    funnel: Counter[str],
    first_d: int = FIRST_DECISION_MS,
    last_d: int = LAST_DECISION_MS,
) -> list[Impulse]:
    """Every decision hour d of one instrument, from its own data before d."""
    series = inst.series
    times = sorted(series.funding)
    rates = [series.funding[s] for s in times]
    g_rank = RollingRank()
    out: list[Impulse] = []
    last = min(last_d, series.end_ms - series.end_ms % HOUR_MS)
    for d in range(first_d - RANK_HOURS * HOUR_MS, last + 1, HOUR_MS):
        g = oi_change(series, d)
        if d >= first_d and d >= series.launch_ms:
            rank = g_rank.rank(g) if g is not None else None
            impulse = _decide(inst, funnel, d, g, rank, times, rates)
            if impulse is not None:
                out.append(impulse)
        g_rank.push(g)
    return out


def _decide(
    inst: Instrument,
    funnel: Counter[str],
    d: int,
    g: float | None,
    rank: float | None,
    times: Sequence[int],
    rates: Sequence[float],
) -> Impulse | None:
    t = d - HOUR_MS
    funnel["grid"] += 1
    turnover = inst.turnover_before(t)
    if turnover is None:
        funnel["liquidity_kline_gap"] += 1
        return None
    if turnover < MIN_TURNOVER_USD:
        funnel["liquidity_illiquid"] += 1
        return None
    funnel["liquidity_liquid"] += 1
    trigger = inst.trigger(t)
    funnel[f"trigger_{trigger}"] += 1
    if trigger != "true":
        return None
    first = inst.first(t)
    funnel[f"first_{first}"] += 1
    if first != "true":
        return None
    f, g_status = funding_status(times, rates, d), oi_status(g, rank)
    funnel[f"f_{f}"] += 1
    funnel[f"g_{g_status}"] += 1
    if f in DETERMINED and g_status in DETERMINED:
        funnel["fg_determinable"] += 1
    sd = inst.hourly_sd(t)
    c_t, c_d, r1 = close_at(inst.series, t), close_at(inst.series, d), change(inst.series, t)
    if sd is None or c_t is None or c_d is None or r1 is None:
        funnel["wait_kline_gap"] += 1
        return None
    impulse = Impulse(inst.symbol, d, r1 / sd, (c_d / c_t - 1) / sd, turnover, f, g_status)
    funnel[f"cell_{impulse.cell}"] += 1
    return impulse


# ---------------------------------------------------------------- matching


def turnover_bin(turnover: float) -> int:
    return bisect.bisect_right(TURNOVER_BIN_EDGES, turnover)


def match(
    events: Sequence[Impulse], pool: Sequence[Impulse]
) -> dict[tuple[str, int], list[Impulse]]:
    """For each event, at most 5 nearest controls by (z1, zw) inside the calipers, the
    same ISO week and turnover bin, within 72 hours, on another instrument."""
    by_key: dict[tuple[str, int], list[Impulse]] = defaultdict(list)
    for p in pool:
        by_key[(_week(p.d), turnover_bin(p.turnover))].append(p)
    pairs: dict[tuple[str, int], list[Impulse]] = {}
    for e in events:
        ranked = []
        for p in by_key[(_week(e.d), turnover_bin(e.turnover))]:
            dz1, dzw = p.z1 - e.z1, p.zw - e.zw
            if p.symbol == e.symbol or abs(p.d - e.d) > CONTROL_WINDOW_MS:
                continue
            if abs(dz1) > Z_CALIPER or abs(dzw) > Z_CALIPER:
                continue
            tie = abs(math.log(p.turnover / e.turnover))
            ranked.append((math.hypot(dz1, dzw), tie, p.symbol, p.d, p))
        ranked.sort(key=lambda r: r[:4])
        pairs[e.key] = [r[4] for r in ranked[:MAX_CONTROLS]]
    return pairs


# ---------------------------------------------------------------- power


def weights_net(events: Sequence[Impulse]) -> dict[tuple[str, int], float]:
    """Unit weights of the cluster mean over the events."""
    clusters = Counter(e.cluster for e in events)
    return {e.key: 1 / (len(clusters) * clusters[e.cluster]) for e in events}


def weights_excess(
    events: Sequence[Impulse], pairs: dict[tuple[str, int], list[Impulse]]
) -> dict[tuple[str, int], float]:
    """Unit weights of the cluster mean excess; a control's weight sums over its pairs."""
    paired = [e for e in events if pairs.get(e.key)]
    clusters = Counter(e.cluster for e in paired)
    out: dict[tuple[str, int], float] = defaultdict(float)
    for e in paired:
        w = 1 / (len(clusters) * clusters[e.cluster])
        out[e.key] += w
        controls = pairs[e.key]
        for c in controls:
            out[c.key] -= w / len(controls)
    return dict(out)


def filled(scales: dict[tuple[str, int], float | None]) -> tuple[dict[tuple[str, int], float], int]:
    """Undefined scales take the 90th percentile (nearest rank) of the defined ones."""
    defined = sorted(v for v in scales.values() if v is not None)
    if not defined:
        raise SystemExit("no defined scale")
    fill = defined[min(len(defined) - 1, math.ceil(SUBSTITUTE_PCT * len(defined)) - 1)]
    missing = sum(1 for v in scales.values() if v is None)
    return {k: (fill if v is None else v) for k, v in scales.items()}, missing


def mde(
    weights: dict[tuple[str, int], float],
    scales: dict[tuple[str, int], float],
    inflation: float,
    design_effect: float,
) -> float:
    variance = math.fsum(w * w * (inflation * scales[u]) ** 2 for u, w in weights.items())
    return Z_POWER * math.sqrt(variance * design_effect)


def power(
    instruments: dict[str, Instrument],
    events: Sequence[Impulse],
    pairs: dict[tuple[str, int], list[Impulse]],
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
        raw = {(s, d): instruments[s].scale(d - HOUR_MS, relative=relative) for s, d in weights}
        scales, missing = filled(raw)
        grid = {
            f"inflation_{i}_deff_{k}": round(mde(weights, scales, i, k) * 10_000, 2)
            for i in INFLATIONS
            for k in DESIGN_EFFECTS
        }
        primary = mde(weights, scales, INFLATION, DESIGN_EFFECT)
        out[name] = {
            "units": len(weights),
            "scales_substituted": missing,
            "mde_bps": round(primary * 10_000, 2),
            "mde_unrounded": primary,
            "sensitivity_bps": grid,
        }
    return out


# ---------------------------------------------------------------- report


def shape(impulses: Sequence[Impulse]) -> dict[str, Any]:
    clusters = Counter(i.cluster for i in impulses)
    per_symbol = Counter(s for s, _ in clusters)
    per_week = Counter(_week(i.d) for i in {i.cluster: i for i in impulses}.values())
    top_symbols = sum(n for _, n in per_symbol.most_common(TOP))
    top_weeks = sum(n for _, n in per_week.most_common(TOP))
    n = len(clusters)
    return {
        "impulses": len(impulses),
        "clusters": n,
        "symbols": len(per_symbol),
        "iso_weeks": len(per_week),
        "top5_symbol_share": round(top_symbols / n, 4) if n else None,
        "top5_week_share": round(top_weeks / n, 4) if n else None,
        "first_half_clusters": sum(1 for _, day in clusters if day < _day(SECOND_HALF_MS)),
    }


def control_shape(
    events: Sequence[Impulse], pairs: dict[tuple[str, int], list[Impulse]]
) -> dict[str, Any]:
    counts = sorted(len(pairs.get(e.key, [])) for e in events)
    uses = Counter(c.key for e in events for c in pairs.get(e.key, []))
    with_control = sum(1 for n in counts if n)

    def pct(q: float) -> int | None:
        return counts[min(len(counts) - 1, math.ceil(q * len(counts)) - 1)] if counts else None

    return {
        "events_with_control": with_control,
        "share_with_control": round(with_control / len(counts), 4) if counts else None,
        "controls_per_event_median": pct(0.5),
        "controls_per_event_p10": pct(0.1),
        "distinct_controls": len(uses),
        "max_events_per_control": max(uses.values(), default=0),
    }


def floor(
    events_shape: dict[str, Any],
    controls: dict[str, Any],
    determinable_share: float | None,
    mdes: dict[str, Any],
) -> dict[str, Any]:
    data = {
        "clusters": events_shape["clusters"] >= FLOOR_CLUSTERS,
        "symbols": events_shape["symbols"] >= FLOOR_SYMBOLS,
        "iso_weeks": events_shape["iso_weeks"] >= FLOOR_WEEKS,
        "top5_symbols": (events_shape["top5_symbol_share"] or 1.0) <= TOP_SHARE_MAX,
        "control_share": (controls["share_with_control"] or 0.0) >= CONTROL_SHARE_MIN,
        "determinable": (determinable_share or 0.0) >= DETERMINABLE_MIN,
    }
    powered = {
        leg: mdes[leg]["mde_unrounded"] <= MDE_MAX if mdes.get(leg) else False
        for leg in ("a_net", "b_excess")
    }
    if not all(data.values()):
        verdict = "insufficient_data"
    elif not all(powered.values()):
        verdict = "insufficient_power"
    else:
        verdict = "pass_to_reading"
    return {"data": data, "power": powered, "verdict": verdict}


def build(features: Features) -> tuple[dict[str, Any], dict[str, Any]]:
    if BENCHMARK not in features.instruments:
        raise SystemExit(f"{BENCHMARK} is not in the snapshot")
    benchmark = features.instruments[BENCHMARK]
    instruments = {
        s: Instrument(s, series, benchmark) for s, series in features.instruments.items()
    }
    funnel: Counter[str] = Counter()
    impulses: list[Impulse] = []
    for symbol in sorted(instruments):
        impulses += scan(instruments[symbol], funnel)
    events = [i for i in impulses if i.cell == "event"]
    pool = [i for i in impulses if i.is_control_candidate]
    pairs = match(events, pool)
    events_shape = shape(events)
    controls = control_shape(events, pairs)
    first_impulses = funnel["first_true"]
    determinable = round(funnel["fg_determinable"] / first_impulses, 4) if first_impulses else None
    mdes = power(instruments, events, pairs) if events else {}
    cells = {
        name: shape([i for i in impulses if i.cell == name])
        for name in ("event", "f_only", "g_only", "neither", "pool_partial", "undefined")
    }
    report = {
        "version": VERSION,
        "step": "steps 1 and 2: counts and power, no return after any decision",
        "features_sha256": FEATURES_SHA256,
        "instruments": len(instruments),
        "failed_instruments": list(features.failed),
        "first_decision": _iso(FIRST_DECISION_MS),
        "last_decision": _iso(LAST_DECISION_MS),
        "funnel": dict(sorted(funnel.items())),
        "fg_determinable_share": determinable,
        "cells": cells,
        "events": events_shape,
        "controls": controls,
        "events_by_iso_week": dict(sorted(Counter(_week(e.d) for e in events).items())),
        "power": mdes,
        "floor": floor(events_shape, controls, determinable, mdes),
    }
    frozen = {
        "version": VERSION,
        "events": [
            {
                "symbol": e.symbol,
                "t_d": _iso(e.d),
                "controls": [{"symbol": c.symbol, "t_d": _iso(c.d)} for c in pairs[e.key]],
            }
            for e in sorted(events, key=lambda x: (x.d, x.symbol))
        ],
    }
    return report, frozen


def render_markdown(report: dict[str, Any]) -> str:
    funnel = [(k, f"{v:,}") for k, v in report["funnel"].items()]
    cells = report["cells"]
    keys = ("impulses", "clusters", "symbols", "iso_weeks", "top5_symbol_share",
            "top5_week_share", "first_half_clusters")  # fmt: skip
    cell_rows = [(k, *(str(cells[c][k]) for c in cells)) for k in keys]
    controls = [(k, str(v)) for k, v in report["controls"].items()]
    power_rows = [
        (leg, str(v["units"]), str(v["scales_substituted"]), f"{v['mde_bps']:.2f}")
        for leg, v in report["power"].items()
    ]
    verdict = report["floor"]
    floor_rows = [(k, str(v)) for k, v in {**verdict["data"], **verdict["power"]}.items()]
    lines = [f"# {VERSION}: counts and power", ""]
    lines += [*markdown_table(("Funnel item", "Decision hours"), funnel), ""]
    lines += [*markdown_table(("Cell", *cells), cell_rows), ""]
    lines += [*markdown_table(("Controls", "Value"), controls), ""]
    lines += [*markdown_table(("Leg", "Units", "Scales substituted", "MDE, bps"), power_rows), ""]
    lines += [*markdown_table(("Floor item", "Holds"), floor_rows), ""]
    lines += [f"Determinable share: {report['fg_determinable_share']}", ""]
    lines += [f"Verdict: **{verdict['verdict']}**"]
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    counts = sub.add_parser("counts", help="funnel, cells, pairs, power and floor")
    counts.add_argument("--features", type=Path, required=True)
    counts.add_argument("--features-sha256", required=True)
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
    features = load_features(verified(args.features, args.features_sha256))
    report, frozen = build(features)
    pairs_digest = publish_once(args.output_dir / "pairs.json", frozen)
    report |= {
        "pairs_sha256": pairs_digest,
        "code_revision": normalize_code_revision(args.code_revision),
        "working_tree_dirty": args.working_tree_dirty,
    }
    digest = publish_once(args.output_dir / "counts.json", report)
    text = render_markdown(report)
    (args.output_dir / "counts.md").write_text(text, encoding="utf-8")
    sys.stdout.write(text + f"\ncounts sha256 {digest}\n")
