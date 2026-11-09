"""HYP-035: relative strength, a dollar-neutral daily book, steps 1 and 2.

Implements docs/research/bybit-relative-strength-v1.md.

- ``counts``: reads only HYP-032's verified features snapshot and publishes the funnel,
  the daily top-decile units with their same-day matched controls (``pairs.json``), the
  pre-t balance, and the power estimate of the daily portfolios with the floor. No
  request is made and nothing after any decision time t is read: the power step uses
  the week before each formation window.
"""

from __future__ import annotations

import argparse
import bisect
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from .bybit_carry_feasibility import publish_once
from .bybit_compressed_spring import (
    DAY_MS,
    FIRST_DECISION_MS,
    HOUR_MS,
    RANK_HOURS,
    RANK_MIN_DEFINED,
    SECOND_HALF_MS,
    Features,
    last_decision_ms,
    load_features,
)
from .bybit_post_funding_screen import verified
from .bybit_squeeze_continuation import (
    BENCHMARK,
    DESIGN_EFFECT,
    DESIGN_EFFECTS,
    FEATURES_SHA256,
    INFLATION,
    INFLATIONS,
    MAX_CONTROLS,
    MDE_MAX,
    MIN_TURNOVER_USD,
    SUBSTITUTE_PCT,
    TOP,
    Z_POWER,
    Instrument,
    _day,
    _iso,
    _week,
    change,
    close_at,
    turnover_bin,
)
from .reporting import markdown_table, normalize_code_revision

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

VERSION: Final = "bybit_relative_strength_v1"

HOLD_H: Final = 24
FORMATION_H: Final = 168
LAST_DECISION_MS: Final = last_decision_ms(HOLD_H) // DAY_MS * DAY_MS  # 00:00 UTC
TOP_RANK: Final = 0.90
MID_LOW: Final = 0.30
MID_HIGH: Final = 0.70
MIN_CROSS_SECTION: Final = 100
SD_CALIPER: Final = 0.25
SMD_MAX: Final = 0.10

SEGMENTS: Final = 7
SEGMENT_MIN: Final = 5
DEFINED_SHARE: Final = 0.80
F_FIXED: Final = (1.0, 2.0)

FLOOR_DAYS: Final = 150
FLOOR_SYMBOLS: Final = 40
FLOOR_WEEKS: Final = 26
TOP_SHARE_MAX: Final = 0.30
CONTROL_SHARE_MIN: Final = 0.80
DETERMINABLE_MIN: Final = 0.90

Key = tuple[str, int]


# ---------------------------------------------------------------- decisions


@dataclass(frozen=True, slots=True)
class Member:
    """A liquid instrument at a decision time t with its formation and scale inputs."""

    symbol: str
    d: int  # the decision time t, 00:00 UTC
    m: float  # 7-day log return relative to BTC
    sd: float  # hourly sd over the 336 hours before t
    turnover: float
    rank: float = 0.0

    @property
    def key(self) -> Key:
        return self.symbol, self.d


def decision_days(first: int = FIRST_DECISION_MS, last: int = LAST_DECISION_MS) -> list[int]:
    return list(range(first, last + 1, DAY_MS))


def log_change(series: Any, h: int, span_h: int) -> float | None:
    now, before = close_at(series, h), close_at(series, h - span_h * HOUR_MS)
    return None if now is None or before is None else math.log(now / before)


def member(inst: Instrument, benchmark: Instrument, d: int, funnel: Counter[str]) -> Member | None:
    """The instrument at t = d from its own bars ending at t, or None with a reason."""
    funnel["grid"] += 1
    turnover = inst.turnover_before(d + HOUR_MS)  # the 24 bars ending t
    if turnover is None:
        funnel["liquidity_kline_gap"] += 1
        return None
    if turnover < MIN_TURNOVER_USD:
        funnel["liquidity_illiquid"] += 1
        return None
    funnel["liquid"] += 1
    own, btc = log_change(inst.series, d, FORMATION_H), log_change(benchmark.series, d, FORMATION_H)
    if own is None or btc is None:
        funnel["formation_kline_gap"] += 1
        return None
    sd = inst.hourly_sd(d + HOUR_MS)  # the 336 hourly returns ending t
    if sd is None:
        funnel["scale_history_short"] += 1
        return None
    funnel["defined"] += 1
    return Member(inst.symbol, d, own - btc, sd, turnover)


def ranked(members: Sequence[Member]) -> list[Member]:
    """Cross-sectional mid-ranks of m: (below + equal / 2) / n, the instrument included."""
    values = sorted(x.m for x in members)
    n = len(values)
    out = []
    for x in members:
        below = bisect.bisect_left(values, x.m)
        equal = bisect.bisect_right(values, x.m) - below
        out.append(Member(x.symbol, x.d, x.m, x.sd, x.turnover, (below + equal / 2) / n))
    return out


def cross_section(
    instruments: Mapping[str, Instrument], benchmark: Instrument, d: int, funnel: Counter[str]
) -> list[Member] | None:
    members = [
        x
        for s in sorted(instruments)
        if s != BENCHMARK and (x := member(instruments[s], benchmark, d, funnel)) is not None
    ]
    if len(members) < MIN_CROSS_SECTION:
        funnel["thin_cross_section_days"] += 1
        funnel["thin_cross_section_instruments"] += len(members)
        return None
    funnel["days"] += 1
    return ranked(members)


# ---------------------------------------------------------------- matching


def match(events: Sequence[Member], pool: Sequence[Member]) -> dict[Key, list[Member]]:
    """For each event, at most 5 nearest same-t controls by |ln sd ratio| inside the
    caliper, in the same turnover bin, on another instrument."""
    by_key: dict[tuple[int, int], list[Member]] = defaultdict(list)
    for p in pool:
        by_key[(p.d, turnover_bin(p.turnover))].append(p)
    pairs: dict[Key, list[Member]] = {}
    for e in events:
        candidates = []
        for p in by_key.get((e.d, turnover_bin(e.turnover)), []):
            ratio = abs(math.log(p.sd / e.sd))
            if p.symbol == e.symbol or ratio > SD_CALIPER:
                continue
            candidates.append((ratio, abs(math.log(p.turnover / e.turnover)), p.symbol, p))
        candidates.sort(key=lambda r: r[:3])
        pairs[e.key] = [r[3] for r in candidates[:MAX_CONTROLS]]
    return pairs


# ---------------------------------------------------------------- weights


def weights_a(events: Sequence[Member]) -> dict[Key, float]:
    """Days weigh the same, units the same inside a day."""
    per_day = Counter(e.d for e in events)
    return {e.key: 1 / (len(per_day) * per_day[e.d]) for e in events}


def weights_b(events: Sequence[Member], pairs: Mapping[Key, Sequence[Member]]) -> dict[Key, float]:
    """(b) weights: paired events 1 / (D k_d), a control's weight summed over its pairs."""
    paired = [e for e in events if pairs.get(e.key)]
    per_day = Counter(e.d for e in paired)
    out: dict[Key, float] = defaultdict(float)
    for e in paired:
        w = 1 / (len(per_day) * per_day[e.d])
        out[e.key] += w
        controls = pairs[e.key]
        for c in controls:
            out[c.key] -= w / len(controls)
    return dict(out)


# ---------------------------------------------------------------- balance


def beta(inst: Instrument, benchmark: Instrument, d: int) -> float | None:
    """Least-squares slope of the hourly returns on BTCUSDT's over the 336 hours ending t."""
    xs, ys = [], []
    for k in range(RANK_HOURS):
        h = d - k * HOUR_MS
        y, x = change(inst.series, h), change(benchmark.series, h)
        if y is not None and x is not None:
            xs.append(x)
            ys.append(y)
    if len(xs) < RANK_MIN_DEFINED:
        return None
    mx, my = math.fsum(xs) / len(xs), math.fsum(ys) / len(ys)
    sxx = math.fsum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    return math.fsum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx


def pre_t_r24(inst: Instrument, benchmark: Instrument, d: int) -> float | None:
    own, btc = change(inst.series, d, HOLD_H), change(benchmark.series, d, HOLD_H)
    return None if own is None or btc is None else own - btc


def _weighted(values: Sequence[tuple[float, float]]) -> tuple[float, float]:
    """Weighted mean and variance sum(a (v - m)^2) / sum(a) of (a, v) pairs."""
    total = math.fsum(a for a, _ in values)
    mean = math.fsum(a * v for a, v in values) / total
    return mean, math.fsum(a * (v - mean) ** 2 for a, v in values) / total


def balance(
    weights: Mapping[Key, float], covariates: Mapping[str, Mapping[Key, float | None]]
) -> dict[str, Any] | None:
    """Weighted means and SMDs (spreads weighted like the means) of pre-t covariates;
    an undefined value drops that unit for that covariate only."""
    if not weights:
        return None
    out: dict[str, Any] = {}
    for name, values in covariates.items():
        sides: dict[bool, list[tuple[float, float]]] = {True: [], False: []}
        for key, w in weights.items():
            v = values.get(key)
            if v is not None:
                sides[w > 0].append((abs(w), v))
        if not sides[True] or not sides[False]:
            out[name] = {"event_mean": None, "control_mean": None, "smd": None,
                         "smd_unrounded": None}  # fmt: skip
            continue
        stats = {side: _weighted(vals) for side, vals in sides.items()}
        pooled = math.sqrt((stats[True][1] + stats[False][1]) / 2)
        smd = (stats[True][0] - stats[False][0]) / pooled if pooled > 0 else None
        out[name] = {
            "event_mean": round(stats[True][0], 6),
            "control_mean": round(stats[False][0], 6),
            "smd": round(smd, 3) if smd is not None else None,
            "smd_unrounded": smd,
        }
    return out


# ---------------------------------------------------------------- power


def segment_ends(d: int) -> list[int]:
    """Ends of the seven 24-hour segments of the week before the formation window,
    oldest first."""
    return [d - FORMATION_H * HOUR_MS - j * DAY_MS for j in range(SEGMENTS - 1, -1, -1)]


def _mean_if_defined(values: Sequence[float | None], n: int) -> float | None:
    defined = [v for v in values if v is not None]
    if n == 0 or len(defined) < DEFINED_SHARE * n:
        return None
    return math.fsum(defined) / len(defined)


def portfolio_segments(
    instruments: Mapping[str, Instrument],
    benchmark: Instrument,
    events: Sequence[Member],
    pairs: Mapping[Key, Sequence[Member]] | None,
) -> dict[int, list[float | None]]:
    """Per decision day, the day's portfolio over its seven pre-formation segments:
    (a) when ``pairs`` is None, else (b) over the events with a control."""
    by_day: dict[int, list[Member]] = defaultdict(list)
    for e in events:
        if pairs is None or pairs.get(e.key):
            by_day[e.d].append(e)
    out: dict[int, list[float | None]] = {}
    for d, units in sorted(by_day.items()):
        series: list[float | None] = []
        for end in segment_ends(d):

            def r(x: Member, end: int = end) -> float | None:
                return pre_t_r24(instruments[x.symbol], benchmark, end)

            if pairs is None:
                series.append(_mean_if_defined([r(e) for e in units], len(units)))
                continue
            spreads: list[float | None] = []
            for e in units:
                own = r(e)
                controls = _mean_if_defined([r(c) for c in pairs[e.key]], len(pairs[e.key]))
                spreads.append(None if own is None or controls is None else own - controls)
            series.append(_mean_if_defined(spreads, len(units)))
        out[d] = series
    return out


def day_sigmas(segments: Mapping[int, Sequence[float | None]]) -> tuple[dict[int, float], int]:
    """Sample sd of each day's defined segments (at least 5); others take the 90th
    percentile (nearest rank) of the defined ones."""
    raw: dict[int, float | None] = {}
    for d, values in segments.items():
        defined = [v for v in values if v is not None]
        if len(defined) < SEGMENT_MIN:
            raw[d] = None
            continue
        mean = math.fsum(defined) / len(defined)
        raw[d] = math.sqrt(math.fsum((v - mean) ** 2 for v in defined) / (len(defined) - 1))
    known = sorted(v for v in raw.values() if v is not None)
    if not known:
        raise SystemExit("no defined portfolio spread")
    fill = known[min(len(known) - 1, math.ceil(SUBSTITUTE_PCT * len(known)) - 1)]
    missing = sum(1 for v in raw.values() if v is None)
    return {d: fill if v is None else v for d, v in raw.items()}, missing


def weekly_factor(segments: Mapping[int, Sequence[float | None]]) -> tuple[float, list[float]]:
    """F = max(1, 1 + 2 sum((1 - k/7) rho_k)) from the pooled lag-k autocorrelations of
    the segment series around the grand mean."""
    defined = [v for values in segments.values() for v in values if v is not None]
    if len(defined) < 2:
        return 1.0, []
    grand = math.fsum(defined) / len(defined)
    var = math.fsum((v - grand) ** 2 for v in defined) / len(defined)
    rhos: list[float] = []
    for k in range(1, SEGMENTS):
        products = [
            (a - grand) * (b - grand)
            for values in segments.values()
            for a, b in zip(values[:-k], values[k:], strict=True)
            if a is not None and b is not None
        ]
        rhos.append(math.fsum(products) / len(products) / var if products and var > 0 else 0.0)
    factor = 1 + 2 * math.fsum((1 - k / SEGMENTS) * rho for k, rho in enumerate(rhos, start=1))
    return max(1.0, factor), rhos


def mde(sigmas: Mapping[int, float], inflation: float, design_effect: float, f: float) -> float:
    days = len(sigmas)
    variance = math.fsum((inflation * s) ** 2 for s in sigmas.values()) / days**2
    return Z_POWER * math.sqrt(variance * design_effect * f)


def power_leg(segments: Mapping[int, Sequence[float | None]]) -> dict[str, Any] | None:
    if not segments:
        return None
    sigmas, missing = day_sigmas(segments)
    f, rhos = weekly_factor(segments)
    primary = mde(sigmas, INFLATION, DESIGN_EFFECT, f)
    grid = {
        f"inflation_{i}_deff_{k}_f_{label}": round(mde(sigmas, i, k, fv) * 10_000, 2)
        for i in INFLATIONS
        for k in DESIGN_EFFECTS
        for label, fv in (("1", F_FIXED[0]), ("est", f), ("2", F_FIXED[1]))
    }
    ordered = sorted(sigmas.values())
    return {
        "days": len(sigmas),
        "sigmas_substituted": missing,
        "day_sigma_median_bps": round(ordered[len(ordered) // 2] * 10_000, 1),
        "rho_k": [round(r, 4) for r in rhos],
        "weekly_factor": round(f, 4),
        "mde_bps": round(primary * 10_000, 2),
        "mde_unrounded": primary,
        "sensitivity_bps": grid,
    }


# ---------------------------------------------------------------- report


def events_shape(events: Sequence[Member], previous: set[Key]) -> dict[str, Any]:
    weights = weights_a(events)
    per_symbol: dict[str, float] = defaultdict(float)
    for (symbol, _), w in weights.items():
        per_symbol[symbol] += w
    per_day = Counter(e.d for e in events)
    per_week = Counter(_week(e.d) for e in events)
    top_weight = math.fsum(sorted(per_symbol.values(), reverse=True)[:TOP])
    counts = sorted(per_day.values())
    return {
        "units": len(events),
        "days": len(per_day),
        "symbols": len(per_symbol),
        "iso_weeks": len(per_week),
        "units_per_day_median": counts[len(counts) // 2] if counts else None,
        "units_per_day_min": counts[0] if counts else None,
        "top5_symbol_weight": top_weight,
        "top5_symbol_share": round(top_weight, 4),
        "top5_week_share": (
            round(sum(n for _, n in per_week.most_common(TOP)) / len(events), 4) if events else None
        ),
        "share_in_previous_day_decile": (
            round(sum(1 for e in events if e.key in previous) / len(events), 4) if events else None
        ),
        "first_half_days": sum(1 for d in per_day if d < SECOND_HALF_MS),
    }


def controls_shape(
    events: Sequence[Member], pairs: Mapping[Key, Sequence[Member]]
) -> dict[str, Any]:
    counts = sorted(len(pairs.get(e.key, [])) for e in events)
    uses = Counter(c.key for e in events for c in pairs.get(e.key, []))
    with_control = sum(1 for n in counts if n)

    def pct(q: float) -> int | None:
        return counts[min(len(counts) - 1, math.ceil(q * len(counts)) - 1)] if counts else None

    return {
        "events": len(counts),
        "events_with_control": with_control,
        "share_with_control": round(with_control / len(counts), 4) if counts else None,
        "controls_per_event_median": pct(0.5),
        "controls_per_event_p10": pct(0.1),
        "distinct_controls": len(uses),
        "max_events_per_control": max(uses.values(), default=0),
    }


def floor(
    shape: Mapping[str, Any],
    controls: Mapping[str, Any],
    determinable: tuple[int, int],
    bal: Mapping[str, Any] | None,
    power: Mapping[str, Any],
) -> dict[str, Any]:
    """Unrounded fractions of integer counts; data, then balance, then power."""
    control = controls["events_with_control"] / controls["events"] if controls["events"] else 0.0
    defined = determinable[0] / determinable[1] if determinable[1] else 0.0
    data = {
        "days": shape["days"] >= FLOOR_DAYS,
        "symbols": shape["symbols"] >= FLOOR_SYMBOLS,
        "iso_weeks": shape["iso_weeks"] >= FLOOR_WEEKS,
        "top5_symbols": shape["top5_symbol_weight"] <= TOP_SHARE_MAX,
        "control_share": control >= CONTROL_SHARE_MIN,
        "determinable": defined >= DETERMINABLE_MIN,
    }
    smd = bal["ln_sd"]["smd_unrounded"] if bal else None
    balanced = {"ln_sd_smd": smd is not None and abs(smd) <= SMD_MAX}
    powered = {
        leg: power[leg]["mde_unrounded"] <= MDE_MAX if power.get(leg) else False
        for leg in ("a_net", "b_excess")
    }
    if not all(data.values()):
        verdict = "insufficient_data"
    elif not all(balanced.values()):
        verdict = "insufficient_balance"
    elif not all(powered.values()):
        verdict = "insufficient_power"
    else:
        verdict = "pass_to_reading"
    return {"data": data, "balance": balanced, "power": powered, "verdict": verdict}


def build(features: Features) -> tuple[dict[str, Any], dict[str, Any]]:
    if BENCHMARK not in features.instruments:
        raise SystemExit(f"{BENCHMARK} is not in the snapshot")
    bench_series = features.instruments[BENCHMARK]
    benchmark = Instrument(BENCHMARK, bench_series, bench_series)
    instruments = {
        s: Instrument(s, series, bench_series) for s, series in features.instruments.items()
    }
    funnel: Counter[str] = Counter()
    events: list[Member] = []
    pool: list[Member] = []
    sizes: list[int] = []
    for d in decision_days():
        members = cross_section(instruments, benchmark, d, funnel)
        if members is None:
            continue
        sizes.append(len(members))
        events += [x for x in members if x.rank >= TOP_RANK]
        pool += [x for x in members if MID_LOW <= x.rank <= MID_HIGH]
    pairs = match(events, pool)
    previous = {(e.symbol, e.d + DAY_MS) for e in events}
    wb = weights_b(events, pairs)
    hours = {e.key: e for e in events} | {c.key: c for cs in pairs.values() for c in cs}

    def covariate(fn: Any) -> dict[Key, float | None]:
        return {k: fn(hours[k]) for k in wb}

    covariates = {
        "ln_sd": covariate(lambda x: math.log(x.sd)),
        "beta_to_btc": covariate(lambda x: beta(instruments[x.symbol], benchmark, x.d)),
        "r24_before_t": covariate(lambda x: pre_t_r24(instruments[x.symbol], benchmark, x.d)),
        "ln_turnover": covariate(lambda x: math.log(x.turnover)),
    }
    bal = balance(wb, covariates)
    power = {
        "a_net": power_leg(portfolio_segments(instruments, benchmark, events, None)),
        "b_excess": power_leg(portfolio_segments(instruments, benchmark, events, pairs)),
    }
    shape = events_shape(events, previous)
    controls = controls_shape(events, pairs)
    determinable = (funnel["defined"], funnel["liquid"])
    ordered = sorted(sizes)
    report = {
        "version": VERSION,
        "step": "steps 1 and 2: counts and power, no return after any decision",
        "features_sha256": FEATURES_SHA256,
        "first_decision": _iso(FIRST_DECISION_MS),
        "last_decision": _iso(LAST_DECISION_MS),
        "funnel": dict(sorted(funnel.items())),
        "cross_section_median": ordered[len(ordered) // 2] if ordered else None,
        "cross_section_min": ordered[0] if ordered else None,
        "determinable_share": round(determinable[0] / determinable[1], 4)
        if determinable[1]
        else None,
        "events": shape,
        "controls": controls,
        "balance": bal,
        "power": power,
        "floor": floor(shape, controls, determinable, bal, power),
    }
    frozen = {
        "version": VERSION,
        "pairs": [
            {
                "symbol": e.symbol,
                "t": _iso(e.d),
                "rank": round(e.rank, 6),
                "controls": [c.symbol for c in pairs[e.key]],
            }
            for e in sorted(events, key=lambda x: (x.d, x.symbol))
        ],
    }
    return report, frozen


def render_markdown(report: Mapping[str, Any]) -> str:
    lines = [f"# {VERSION}: counts and power", ""]
    funnel = [(k, f"{v:,}") for k, v in report["funnel"].items()]
    lines += [*markdown_table(("Funnel item", "Instrument-days"), funnel), ""]
    lines += [
        f"Cross-section per day: median {report['cross_section_median']}, "
        f"minimum {report['cross_section_min']}; determinable share "
        f"{report['determinable_share']}",
        "",
    ]
    shape = {k: v for k, v in report["events"].items() if k != "top5_symbol_weight"}
    rows = [(k, str(v)) for k, v in {**shape, **report["controls"]}.items()]
    lines += [*markdown_table(("Item", "Value"), rows), ""]
    if report["balance"]:
        bal = [
            (k, str(b["event_mean"]), str(b["control_mean"]), str(b["smd"]))
            for k, b in report["balance"].items()
        ]
        head = ("Pre-t covariate", "Event mean", "Control mean", "SMD")
        lines += [*markdown_table(head, bal), ""]
    power_rows = [
        (leg, str(v["days"]), str(v["sigmas_substituted"]), str(v["day_sigma_median_bps"]),
         str(v["weekly_factor"]), f"{v['mde_bps']:.2f}")
        if v
        else (leg, "0", "0", "-", "-", "not estimable")
        for leg, v in report["power"].items()
    ]  # fmt: skip
    power_head = ("Leg", "Days", "Substituted", "Day sigma median, bps", "F", "MDE, bps")
    lines += [*markdown_table(power_head, power_rows), ""]
    verdict = report["floor"]
    items = {**verdict["data"], **verdict["balance"], **verdict["power"]}
    lines += [*markdown_table(("Floor item", "Holds"), [(k, str(v)) for k, v in items.items()]), ""]
    lines += [f"Verdict: **{verdict['verdict']}**"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    counts = sub.add_parser("counts", help="funnel, pairs, balance, power and floor")
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
    render_markdown(report)  # fail before anything is published, not after
    report |= {
        "pairs_sha256": publish_once(args.output_dir / "pairs.json", frozen),
        "code_revision": normalize_code_revision(args.code_revision),
        "working_tree_dirty": args.working_tree_dirty,
        "decision_days": len(decision_days()),
        "first_day": _day(FIRST_DECISION_MS),
    }
    digest = publish_once(args.output_dir / "counts.json", report)
    text = render_markdown(report)
    (args.output_dir / "counts.md").write_text(text, encoding="utf-8")
    sys.stdout.write(text + f"\ncounts sha256 {digest}\n")
