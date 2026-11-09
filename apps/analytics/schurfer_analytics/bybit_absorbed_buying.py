"""HYP-036: absorbed taker buying, short; scope, feature check, pilot and counts.

Implements the outcome-blind steps of docs/research/bybit-absorbed-buying-v1.md. Apart
from ``check-raw`` no request is made here, and no trade return is computed: each event,
control and feature uses data before its own t only. The power estimate's hourly scales
and rho use the window's hourly prices, which for a later decision include hours after
earlier ones. The tape is fetched by HYP-032's fetcher (``bybit-compressed-spring-tape``)
with a scope written by ``scope``; ``counts`` reads only a tape sealed for exactly that
scope.

- ``scope``: the tape scope of the pilot blocks or of the full window, with its size
  estimated from HYP-032's archive-size record (no request).
- ``check``: step 1, the feature check on HYP-032's sealed tape (no event rate, coverage
  or MDE is computed).
- ``check-raw``: step 1's independent recount: fetches the 20 sample days' raw trade
  files again and sums B and V straight from the trades, by a code path apart from the
  tape's; the ranks are recounted plainly over the stored 336 hours.
- ``seed``: a new full-scope run seeded with the pilot's verified ``ok`` parts.
- ``counts``: steps 2 to 4 on a sealed tape, for the pilot blocks (with the pilot gate)
  or the full window (with the floor).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import multiprocessing
import os
import shutil
import sys
import tempfile
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

import httpx

from .bybit_carry_feasibility import publish_once
from .bybit_compressed_spring import (
    ARCHIVE_FILE,
    DAY_MS,
    HOUR_MS,
    RANK_HOURS,
    RANK_MIN_DEFINED,
    WINDOW_END_MS,
    Features,
    RollingRank,
    Series,
    estimate,
    load_features,
    minute_masked,
)
from .bybit_compressed_spring import VERSION as TAPE_VERSION
from .bybit_compressed_spring_cond4 import Tape, load_tape
from .bybit_compressed_spring_tape import (
    HYP031_INDEX_SHA256,
    RULES,
    Budget,
    bind_run,
    download,
    ledger_spent,
    part_path,
)
from .bybit_post_funding_screen import file_sha256, verified
from .bybit_squeeze_continuation import _day, _iso, _week, control_shape, filled, shape
from .reporting import markdown_table, normalize_code_revision

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

VERSION: Final = "bybit_absorbed_buying_v1"
FEATURES_SHA256: Final = "a3b54d6ce092a73afbd4911e8ea72862a707846b45fbb23d05ec1d91f71ebab6"
HYP032_TAPE_MANIFEST_SHA256: Final = (
    "21bdf61989a9aac4ebf76e70d260e1aa3541cb66151c362f3189537389d26d50"
)
ARCHIVE_SIZES_SHA256: Final = "22e7c1196536a0db420c616138396022a924274aec73b31b84522fc290d41640"
BENCHMARK: Final = "BTCUSDT"


def _ms(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=UTC).timestamp() * 1000)


FIRST_DECISION_MS: Final = _ms(2024, 11, 1)
HOLD_H: Final = 4
LAST_DECISION_MS: Final = WINDOW_END_MS - HOLD_H * HOUR_MS - HOUR_MS
SECOND_HALF_MS: Final = _ms(2025, 5, 1)
MONTHS: Final = tuple(_ms(2024 + (10 + k) // 12, (10 + k) % 12 + 1, 1) for k in range(11))
# Pilot blocks: decision days, inclusive (UTC), fixed in the protocol by the calendar.
BLOCKS: Final = ((_ms(2025, 1, 13), _ms(2025, 1, 27)), (_ms(2025, 7, 7), _ms(2025, 7, 21)))

POOL_SIZE: Final = 150
POOL_LOOKBACK_MS: Final = 30 * DAY_MS
TERCILE: Final = 50
# A month's decisions need flow back to the hour starting M - 345 h (336 ranks of the
# hour before each decision, plus the 8 hours of the episode rule), on day M - 15 d.
SCOPE_LEAD_DAYS: Final = 15

QB_EVENT: Final = 0.95
QV_MIN: Final = 0.50
Z1_MAX: Final = 1.0
EPISODE_H: Final = 8
CONTROL_QB: Final = (0.25, 0.75)
SIGMA_RATIO_MAX: Final = 0.25
Z_CALIPER: Final = 0.5
MAX_CONTROLS: Final = 5
SMD_MAX: Final = 0.10
BALANCED: Final = ("z1", "z4", "ln_sigma1")

Z_POWER: Final = 1.645 + 0.842
INFLATION: Final = 1.5
DESIGN_EFFECT: Final = 1.2
INFLATIONS: Final = (1.0, 1.5, 2.0)
DESIGN_EFFECTS: Final = (1.0, 1.2, 1.5)
RHO_FACTORS: Final = (0.5, 1.0, 1.5)
RHO_MIN_HOURS: Final = math.ceil(0.8 * POOL_LOOKBACK_MS / HOUR_MS)
MDE_MAX: Final = 0.005

FLOOR_CLUSTERS: Final = 150
FLOOR_SYMBOLS: Final = 40
FLOOR_WEEKS: Final = 26
TOP_SHARE_MAX: Final = 0.30
CONTROL_SHARE_MIN: Final = 0.80
DETERMINABLE_MIN: Final = 0.90

PILOT_EVENTS_MIN: Final = 1500
PILOT_CONTROL_SHARE_MIN: Final = 0.85
PILOT_MDE_MAX: Final = 0.0045
CHECK_SAMPLE: Final = 20
# The raw recount's own mask, written apart from the tape's: minutes at offsets [0, 5)
# and [55, 60) of every hour never enter B or V.
RAW_MASK_MINUTES: Final = (5, 55)
RAW_RELATIVE: Final = 1e-9
SEED_VERSION: Final = f"{VERSION}_seed"


# ---------------------------------------------------------------- calendar and pool


def month_of(t: int) -> int:
    """The start of the calendar month containing t."""
    return next(m for m in reversed(MONTHS) if m <= t)


def pool(features: Features, month: int) -> list[str]:
    """The month's pool: the 150 largest by 1-hour kline turnover over the 30 days before
    it, BTCUSDT excluded, ties by symbol."""
    totals = []
    for symbol, series in features.instruments.items():
        if symbol == BENCHMARK:
            continue
        total = math.fsum(
            bar[3]
            for start, bar in series.bars.items()
            if month - POOL_LOOKBACK_MS <= start < month
        )
        if total > 0:
            totals.append((-total, symbol))
    return [symbol for _, symbol in sorted(totals)[:POOL_SIZE]]


def spans(mode: str) -> list[tuple[int, int]]:
    """Decision spans [first, end) of the pilot blocks or of the full window."""
    if mode == "blocks":
        return list(BLOCKS)
    return [(FIRST_DECISION_MS, LAST_DECISION_MS + HOUR_MS)]


def decision_days(mode: str) -> int:
    return sum((end - first + DAY_MS - 1) // DAY_MS for first, end in spans(mode))


def scope_days(features: Features, mode: str) -> dict[str, list[str]]:
    """Each member's days from 15 days before its month's first decision in a span to the
    span's last decision day (the listing decides later which days have a file)."""
    days: dict[str, set[str]] = defaultdict(set)
    for first, end in spans(mode):
        for month in MONTHS:
            month_end = next((m for m in MONTHS if m > month), WINDOW_END_MS)
            lo, hi = max(first, month), min(end, month_end)
            if lo >= hi:
                continue
            for symbol in pool(features, month):
                series = features.instruments[symbol]
                d = lo - lo % DAY_MS - SCOPE_LEAD_DAYS * DAY_MS
                while d < hi:
                    if series.launch_ms <= d + DAY_MS and d <= series.end_ms:
                        days[symbol].add(_day(d))
                    d += DAY_MS
    return {s: sorted(v) for s, v in sorted(days.items())}


def load_archive_sizes(path: Path) -> tuple[dict[str, set[str]], dict[tuple[str, str], int]]:
    listed: dict[str, set[str]] = {}
    sizes: dict[tuple[str, str], int] = {}
    with gzip.open(path, "rb") as stream:
        for raw in stream:
            line = json.loads(raw)
            if line["kind"] == "listing":
                listed[line["symbol"]] = set(line["response"] or [])
            elif line["kind"] == "head" and line["params"].get("status") == 200:
                length = line["response"].get("content_length")
                if length is not None:
                    sizes[(line["symbol"], line["params"]["day"])] = int(length)
    return listed, sizes


# ---------------------------------------------------------------- features before t


def _log_change(series: Series, h: int, span_h: int) -> float | None:
    now, before = series.bars.get(h - HOUR_MS), series.bars.get(h - (span_h + 1) * HOUR_MS)
    if now is None or before is None or now[2] <= 0 or before[2] <= 0:
        return None
    return math.log(now[2] / before[2])


def _sd(values: Iterable[float | None]) -> float | None:
    defined = [v for v in values if v is not None]
    if len(defined) < RANK_MIN_DEFINED:
        return None
    mean = math.fsum(defined) / len(defined)
    var = math.fsum((v - mean) ** 2 for v in defined) / (len(defined) - 1)
    return math.sqrt(var) if var > 0 else None


def flow(tape: Tape | None, t: int) -> tuple[float, float] | str:
    """(B, V) over the kept minutes [t - 55 min, t - 5 min), or why it is undefined."""
    h = t - HOUR_MS
    if tape is None or h - h % DAY_MS not in tape.ok_days:
        return "tape_missing"
    if h in tape.unusable:
        return "tape_mismatch"
    buy, sell = tape.kept.get(h, (0.0, 0.0))
    total = buy + sell
    return ((buy - sell) / total, total) if total > 0 else "no_flow"


@dataclass(frozen=True)
class State:
    """One instrument at one decision hour t; a status is true, false or a reason."""

    symbol: str
    d: int
    b: float | None
    v: float | None
    qb: float | None
    qv: float | None
    sigma1: float | None
    z1: float | None
    z4: float | None
    reason: str | None  # why some item is undefined, None when all are defined

    @property
    def key(self) -> tuple[str, int]:
        return self.symbol, self.d

    @property
    def cluster(self) -> tuple[str, str]:
        return self.symbol, _day(self.d)

    @property
    def qualifies(self) -> str:
        if self.reason is not None:
            return self.reason
        assert self.qb is not None and self.qv is not None and self.b is not None
        assert self.z1 is not None
        hit = self.qb >= QB_EVENT and self.b > 0 and self.qv >= QV_MIN and abs(self.z1) <= Z1_MAX
        return "true" if hit else "false"

    @property
    def is_control_candidate(self) -> bool:
        if self.reason is not None:
            return False
        assert self.qb is not None and self.qv is not None and self.z1 is not None
        lo, hi = CONTROL_QB
        return lo <= self.qb <= hi and self.qv >= QV_MIN and abs(self.z1) <= Z1_MAX


def states(
    symbol: str, series: Series, tape: Tape | None, first: int, end: int
) -> dict[int, State]:
    """States at every hour of [first - 8 h, end), with ranks from the 336 hours before
    each; the walk starts 336 + 8 hours before ``first``."""
    ranks_b, ranks_v = RollingRank(), RollingRank()
    returns: deque[float | None] = deque()
    out: dict[int, State] = {}
    start = first - (RANK_HOURS + EPISODE_H) * HOUR_MS
    for t in range(start - RANK_HOURS * HOUR_MS, end, HOUR_MS):
        # sigma1 from the 336 hourly log returns of the bars ending t - 1 h back
        sigma1 = _sd(returns) if len(returns) == RANK_HOURS else None
        r1 = _log_change(series, t, 1)
        returns.append(r1)
        if len(returns) > RANK_HOURS:
            returns.popleft()
        if t < start:
            continue
        f = flow(tape, t)
        b, v = f if isinstance(f, tuple) else (None, None)
        qb = ranks_b.rank(b) if b is not None else None
        qv = ranks_v.rank(v) if v is not None else None
        ranks_b.push(b)
        ranks_v.push(v)
        if t < first - EPISODE_H * HOUR_MS:
            continue
        r4 = _log_change(series, t, 4)
        reason: str | None = None
        if isinstance(f, str):
            reason = f
        elif qb is None or qv is None:
            reason = "rank_history_short"
        elif r1 is None or r4 is None:
            reason = "kline_gap"
        elif sigma1 is None:
            reason = "sigma_history_short"
        z1 = r1 / sigma1 if r1 is not None and sigma1 else None
        z4 = r4 / (2 * sigma1) if r4 is not None and sigma1 else None
        out[t] = State(symbol, t, b, v, qb, qv, sigma1, z1, z4, reason)
    return out


def first_status(by_hour: dict[int, State], t: int) -> str:
    """No event-qualifying hour on the instrument in the 8 hours before t."""
    statuses = [by_hour[t - k * HOUR_MS].qualifies for k in range(1, EPISODE_H + 1)]
    if "true" in statuses:
        return "false"
    undefined = [s for s in statuses if s != "false"]
    return undefined[0] if undefined else "true"


# ---------------------------------------------------------------- decisions and controls


@dataclass(frozen=True)
class Member:
    state: State
    tercile: int

    @property
    def symbol(self) -> str:
        return self.state.symbol

    @property
    def d(self) -> int:
        return self.state.d

    @property
    def key(self) -> tuple[str, int]:
        return self.state.key

    @property
    def cluster(self) -> tuple[str, str]:
        return self.state.cluster


def match(
    events: Sequence[Member],
    candidates: Sequence[Member],
    *,
    z1_caliper: float = Z_CALIPER,
    by_tercile: bool = True,
) -> dict[tuple[str, int], list[Member]]:
    """Same t and tercile, another instrument, the sigma1 ratio and z1, z4 calipers; the
    5 nearest by (z1, z4), ties by the log sigma1 ratio, then by symbol. The keywords
    exist for the matching check (bybit_absorbed_buying_match_check); v1 uses the
    defaults."""
    by_key: dict[tuple[int, int], list[Member]] = defaultdict(list)
    for c in candidates:
        by_key[(c.d, c.tercile if by_tercile else 0)].append(c)
    pairs: dict[tuple[str, int], list[Member]] = {}
    for e in events:
        es = e.state
        assert es.z1 is not None and es.z4 is not None and es.sigma1 is not None
        ranked = []
        for c in by_key[(e.d, e.tercile if by_tercile else 0)]:
            cs = c.state
            assert cs.z1 is not None and cs.z4 is not None and cs.sigma1 is not None
            if c.symbol == e.symbol:
                continue
            ratio = abs(math.log(cs.sigma1 / es.sigma1))
            dz1, dz4 = cs.z1 - es.z1, cs.z4 - es.z4
            if ratio > SIGMA_RATIO_MAX or abs(dz1) > z1_caliper or abs(dz4) > Z_CALIPER:
                continue
            ranked.append((math.hypot(dz1, dz4), ratio, c.symbol, c))
        ranked.sort(key=lambda r: r[:3])
        pairs[e.key] = [r[3] for r in ranked[:MAX_CONTROLS]]
    return pairs


def smd_of(
    weights: dict[tuple[str, int], float], value: Callable[[tuple[str, int]], float]
) -> float | None:
    """Weighted SMD on leg (b)'s weights: events carry their positive weights, controls
    the absolute value of their negative ones; the pooled sd is the root of the mean of
    the two weighted variances (HYP-034 Amendment 1). With no spread on either side it is
    0 for equal means and None (never balanced) for different ones."""
    sides = []
    for sign in (1, -1):
        items = [(abs(w), value(k)) for k, w in weights.items() if w * sign > 0]
        total = math.fsum(a for a, _ in items)
        mean = math.fsum(a * v for a, v in items) / total
        var = math.fsum(a * (v - mean) ** 2 for a, v in items) / total
        sides.append((mean, var))
    (me, ve), (mc, vc) = sides
    pooled = math.sqrt((ve + vc) / 2)
    if pooled > 0:
        return (me - mc) / pooled
    return 0.0 if me == mc else None


def balance(
    weights: dict[tuple[str, int], float], members: dict[tuple[str, int], Member]
) -> dict[str, Any]:
    """``smd_of`` for each balance item, with the two weighted means."""

    def value(m: Member, name: str) -> float:
        s = m.state
        raw = {"z1": s.z1, "z4": s.z4, "ln_sigma1": math.log(s.sigma1) if s.sigma1 else None,
               "qv": s.qv, "ln_v": math.log(s.v) if s.v else None}[name]  # fmt: skip
        assert raw is not None
        return raw

    out: dict[str, Any] = {}
    for name in (*BALANCED, "qv", "ln_v"):
        means = []
        for sign in (1, -1):
            items = [(abs(w), value(members[k], name)) for k, w in weights.items() if w * sign > 0]
            means.append(math.fsum(a * x for a, x in items) / math.fsum(a for a, _ in items))
        smd = smd_of(weights, partial(_member_value, members, value, name))
        out[name] = {"event_mean": round(means[0], 4), "control_mean": round(means[1], 4),
                     "smd": round(smd, 4) if smd is not None else None,
                     "smd_unrounded": smd}  # fmt: skip
    return out


def _member_value(
    members: dict[tuple[str, int], Member],
    value: Callable[[Member, str], float],
    name: str,
    key: tuple[str, int],
) -> float:
    return value(members[key], name)


def balance_passes(bal: dict[str, Any]) -> bool:
    return all(
        bal[name]["smd_unrounded"] is not None and abs(bal[name]["smd_unrounded"]) <= SMD_MAX
        for name in BALANCED
    )


# ---------------------------------------------------------------- power


def weights_net(events: Sequence[Member]) -> dict[tuple[str, int], float]:
    """Unit weights of the cluster mean over the events (as HYP-033)."""
    clusters = Counter(e.cluster for e in events)
    return {e.key: 1 / (len(clusters) * clusters[e.cluster]) for e in events}


def weights_excess(
    events: Sequence[Member], pairs: dict[tuple[str, int], list[Member]]
) -> dict[tuple[str, int], float]:
    """Unit weights of the cluster mean excess of the short: the event's return enters
    with a positive weight and its controls' mean with a negative one, as in HYP-033
    (the short's sign is applied at reading time); a control's weight sums over its
    pairs."""
    paired = [e for e in events if pairs.get(e.key)]
    clusters = Counter(e.cluster for e in paired)
    out: dict[tuple[str, int], float] = defaultdict(float)
    for e in paired:
        w = 1 / (len(clusters) * clusters[e.cluster])
        out[e.key] += w
        for c in pairs[e.key]:
            out[c.key] -= w / len(pairs[e.key])
    return dict(out)


def scale(series: Series, benchmark: Series, t: int, *, relative: bool) -> float | None:
    """SD of the 4-hour log returns at each hourly close of the 14 days up to t;
    BTC-relative when ``relative``."""
    values: list[float | None] = []
    for k in range(RANK_HOURS):
        h = t - k * HOUR_MS
        r = _log_change(series, h, HOLD_H)
        if r is not None and relative:
            b = _log_change(benchmark, h, HOLD_H)
            r = None if b is None else r - b
        values.append(r)
    return _sd(values)


def month_rho(features: Features, month: int, members: Sequence[str], *, relative: bool) -> float:
    """Mean pairwise correlation of 1-hour log returns among the month's pool over the 30
    days before it: sum over hours of ((sum z)^2 - sum z^2) over the sum of n (n - 1),
    with each instrument standardized over the span (population sd, 80% of hours defined)."""
    benchmark = features.instruments[BENCHMARK]
    hours = range(month - POOL_LOOKBACK_MS + HOUR_MS, month + HOUR_MS, HOUR_MS)
    by_hour: dict[int, list[float]] = defaultdict(list)
    for symbol in members:
        series = features.instruments[symbol]
        values: dict[int, float] = {}
        for h in hours:
            r = _log_change(series, h, 1)
            if r is not None and relative:
                b = _log_change(benchmark, h, 1)
                r = None if b is None else r - b
            if r is not None:
                values[h] = r
        if len(values) < RHO_MIN_HOURS:
            continue
        mean = math.fsum(values.values()) / len(values)
        sd = math.sqrt(math.fsum((v - mean) ** 2 for v in values.values()) / len(values))
        if sd <= 0:
            continue
        for h, v in values.items():
            by_hour[h].append((v - mean) / sd)
    num = math.fsum(math.fsum(zs) ** 2 - math.fsum(z * z for z in zs) for zs in by_hour.values())
    den = math.fsum(len(zs) * (len(zs) - 1) for zs in by_hour.values())
    return num / den if den else 0.0


def block_variance(
    weights: dict[tuple[str, int], float],
    scales: dict[tuple[str, int], float],
    rho_of: dict[int, float],
    rho_factor: float = 1.0,
) -> float:
    """Sum over hourly blocks h of rho_h (sum of w s)^2 + (1 - rho_h) sum over instruments
    of (sum of w s)^2, with s = sigma / 2 and the four blocks of each hold."""
    common: dict[int, float] = defaultdict(float)
    own: dict[tuple[int, str], float] = defaultdict(float)
    for (symbol, t), w in weights.items():
        ws = w * scales[(symbol, t)] / 2
        for j in range(HOLD_H):
            h = t + j * HOUR_MS
            common[h] += ws
            own[(h, symbol)] += ws
    total = 0.0
    for h, c in common.items():
        total += block_rho(h, rho_of, rho_factor) * c * c
    for (h, _symbol), o in own.items():
        total += (1 - block_rho(h, rho_of, rho_factor)) * o * o
    return total


def block_rho(h: int, rho_of: dict[int, float], rho_factor: float = 1.0) -> float:
    """rho of the block starting h: the month of the earliest decision whose hold can
    cover it (t = h - 3 h, not before the first decision), known at every decision that
    covers the block, so no hold uses prices after its own decision."""
    earliest = max(h - (HOLD_H - 1) * HOUR_MS, FIRST_DECISION_MS)
    return min(1.0, max(0.0, rho_factor * rho_of[month_of(earliest)]))


def mde(variance: float, inflation: float, design_effect: float, days_scale: float = 1.0) -> float:
    return Z_POWER * inflation * math.sqrt(variance * design_effect * days_scale)


def power(
    features: Features,
    events: Sequence[Member],
    pairs: dict[tuple[str, int], list[Member]],
    rhos: dict[str, dict[int, float]],
    days_scale: float,
) -> dict[str, Any]:
    benchmark = features.instruments[BENCHMARK]
    legs = {
        "a_net": (weights_net(events), False),
        "b_excess": (weights_excess(events, pairs), True),
    }
    out: dict[str, Any] = {}
    for name, (weights, relative) in legs.items():
        if not weights:
            out[name] = None
            continue
        raw = {
            (s, t): scale(features.instruments[s], benchmark, t, relative=relative)
            for s, t in weights
        }
        scales, missing = filled(raw)
        rho_of = rhos["b" if relative else "a"]
        variance = block_variance(weights, scales, rho_of)
        primary = mde(variance, INFLATION, DESIGN_EFFECT, days_scale)
        grid = {
            f"inflation_{i}_deff_{k}": round(mde(variance, i, k, days_scale) * 10_000, 2)
            for i in INFLATIONS
            for k in DESIGN_EFFECTS
        }
        grid |= {
            f"rho_x{f}": round(
                mde(
                    block_variance(weights, scales, rho_of, f), INFLATION, DESIGN_EFFECT, days_scale
                )
                * 10_000,
                2,
            )
            for f in RHO_FACTORS
        }
        out[name] = {
            "units": len(weights),
            "scales_substituted": missing,
            "days_scale": days_scale,
            "mde_bps": round(primary * 10_000, 2),
            "mde_unrounded": primary,
            "sensitivity_bps": grid,
        }
    return out


# ---------------------------------------------------------------- gates


def _fraction(numerator: int, denominator: int, empty: float) -> float:
    return numerator / denominator if denominator else empty


def floor(
    events_shape: dict[str, Any],
    controls: dict[str, Any],
    determinable: tuple[int, int],
    bal: dict[str, Any] | None,
    mdes: dict[str, Any],
) -> dict[str, Any]:
    """Data, then balance, then power; unrounded fractions of the integer counts."""
    clusters = events_shape["clusters"]
    data = {
        "clusters": clusters >= FLOOR_CLUSTERS,
        "symbols": events_shape["symbols"] >= FLOOR_SYMBOLS,
        "iso_weeks": events_shape["iso_weeks"] >= FLOOR_WEEKS,
        "top5_symbols": _fraction(events_shape["top5_symbol_clusters"], clusters, 1.0)
        <= TOP_SHARE_MAX,
        "control_share": _fraction(controls["events_with_control"], controls["events"], 0.0)
        >= CONTROL_SHARE_MIN,
        "determinable": _fraction(*determinable, 0.0) >= DETERMINABLE_MIN,
    }
    balanced = bal is not None and balance_passes(bal)
    powered = {
        leg: bool(mdes.get(leg)) and mdes[leg]["mde_unrounded"] <= MDE_MAX
        for leg in ("a_net", "b_excess")
    }
    if not all(data.values()):
        verdict = "insufficient_data"
    elif not balanced:
        verdict = "insufficient_balance"
    elif not all(powered.values()):
        verdict = "insufficient_power"
    else:
        verdict = "pass_to_reading"
    return {"data": data, "balance": balanced, "power": powered, "verdict": verdict}


def pilot_gate(
    projected_events: float,
    controls: dict[str, Any],
    determinable: tuple[int, int],
    bal: dict[str, Any] | None,
    mdes: dict[str, Any],
) -> dict[str, Any]:
    checks = {
        "determinable": _fraction(*determinable, 0.0) >= DETERMINABLE_MIN,
        "projected_events": projected_events >= PILOT_EVENTS_MIN,
        "control_share": _fraction(controls["events_with_control"], controls["events"], 0.0)
        >= PILOT_CONTROL_SHARE_MIN,
        "balance": bal is not None and balance_passes(bal),
        "a_net_mde": bool(mdes.get("a_net")) and mdes["a_net"]["mde_unrounded"] <= PILOT_MDE_MAX,
        "b_excess_mde": bool(mdes.get("b_excess"))
        and mdes["b_excess"]["mde_unrounded"] <= PILOT_MDE_MAX,
    }
    return {"checks": checks, "verdict": "propose_full_fetch" if all(checks.values()) else "stop"}


# ---------------------------------------------------------------- the run


def pools_of(features: Features) -> dict[int, list[str]]:
    return {m: pool(features, m) for m in MONTHS}


def _months_in(first: int, end: int) -> list[int]:
    ends = (*MONTHS[1:], WINDOW_END_MS)
    return [m for m, m_end in zip(MONTHS, ends, strict=True) if m < end and m_end > first]


def _tradable(series: Series, t: int) -> bool:
    return series.launch_ms <= t and t + (HOLD_H + 1) * HOUR_MS <= series.end_ms


def planned_member_hours(features: Features, pools: dict[int, list[str]], mode: str) -> int:
    """Pool member-hours of the decision spans whose instrument is listed and whose hold
    ends before it is delisted: the determinability denominator."""
    total = 0
    for first, end in spans(mode):
        for t in range(first, min(end, LAST_DECISION_MS + HOUR_MS), HOUR_MS):
            total += sum(1 for s in pools[month_of(t)] if _tradable(features.instruments[s], t))
    return total


def decide(
    features: Features,
    tapes: dict[str, Tape],
    pools: dict[int, list[str]],
    mode: str,
    is_candidate: Callable[[State], bool] | None = None,
) -> tuple[list[Member], list[Member], Counter[str]]:
    """Events (first in their episode) and control candidates over the decision spans;
    the candidate rule is v1's unless the matching check passes its own."""
    funnel: Counter[str] = Counter()
    events: list[Member] = []
    candidates: list[Member] = []
    for first, end in spans(mode):
        end = min(end, LAST_DECISION_MS + HOUR_MS)
        symbols = sorted({s for m in _months_in(first, end) for s in pools[m]})
        for symbol in symbols:
            series = features.instruments[symbol]
            by_hour = states(symbol, series, tapes.get(symbol), first, end)
            for t in range(first, end, HOUR_MS):
                ranked = pools[month_of(t)]
                if symbol not in ranked or not _tradable(series, t):
                    continue
                member = Member(by_hour[t], ranked.index(symbol) // TERCILE)
                funnel["member_hours"] += 1
                status = member.state.qualifies
                funnel[f"qualifies_{status}"] += 1
                if (
                    is_candidate(member.state)
                    if is_candidate
                    else member.state.is_control_candidate
                ):
                    candidates.append(member)
                if status != "true":
                    continue
                first_ok = first_status(by_hour, t)
                funnel[f"first_{first_ok}"] += 1
                if first_ok == "true":
                    events.append(member)
    return events, candidates, funnel


def determined_hours(funnel: Counter[str]) -> int:
    """Member-hours whose decision is known: not an event, or an event-qualifying hour
    whose "first in its episode" is true or false. A qualifying hour with an undefined
    hour among the 8 before it is undetermined, not a non-event."""
    return funnel["qualifies_false"] + funnel["first_true"] + funnel["first_false"]


def build(
    features: Features, tapes: dict[str, Tape], mode: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    if BENCHMARK not in features.instruments:
        raise SystemExit(f"{BENCHMARK} is not in the snapshot")
    pools = pools_of(features)
    events, candidates, funnel = decide(features, tapes, pools, mode)
    pairs = match(events, candidates)
    members = {m.key: m for m in (*events, *candidates)}
    weights_b = weights_excess(events, pairs)
    bal = balance(weights_b, members) if weights_b else None
    rhos = {
        leg: {m: month_rho(features, m, pools[m], relative=leg == "b") for m in MONTHS}
        for leg in ("a", "b")
    }
    days = decision_days(mode)
    full_days = decision_days("full")
    days_scale = days / full_days
    mdes = power(features, events, pairs, rhos, days_scale) if events else {}
    events_shape = shape(cast("Sequence[Any]", events))
    controls = control_shape(cast("Sequence[Any]", events), cast("dict[Any, Any]", pairs))
    planned = planned_member_hours(features, pools, mode)
    determined = determined_hours(funnel)
    report: dict[str, Any] = {
        "version": VERSION,
        "mode": mode,
        "step": "outcome-blind: counts, pairs and power, no trade return",
        "features_sha256": FEATURES_SHA256,
        "spans": [[_iso(a), _iso(b)] for a, b in spans(mode)],
        "decision_days": days,
        "planned_member_hours": planned,
        "funnel": dict(sorted(funnel.items())),
        "determinable_share": round(determined / planned, 4) if planned else None,
        "events": events_shape,
        "controls": controls,
        "events_by_iso_week": dict(sorted(Counter(_week(e.d) for e in events).items())),
        "rho": {leg: {_iso(m): round(v, 4) for m, v in r.items()} for leg, r in rhos.items()},
        "balance": bal,
        "power": mdes,
    }
    if mode == "blocks":
        full_planned = planned_member_hours(features, pools, "full")
        projected = len(events) * full_planned / planned if planned else 0.0
        report["full_planned_member_hours"] = full_planned
        report["projected_events"] = round(projected, 1)
        report["by_block"] = {
            _iso(a): shape(cast("Sequence[Any]", [m for m in events if a <= m.d < b]))
            for a, b in spans(mode)
        }
        report["gate"] = pilot_gate(projected, controls, (determined, planned), bal, mdes)
    else:
        report["floor"] = floor(events_shape, controls, (determined, planned), bal, mdes)
    frozen = {
        "version": VERSION,
        "mode": mode,
        "events": [
            {
                "symbol": e.symbol,
                "t": _iso(e.d),
                "controls": [c.symbol for c in pairs[e.key]],
            }
            for e in sorted(events, key=lambda x: (x.d, x.symbol))
        ],
    }
    return report, frozen


# ---------------------------------------------------------------- step 1: feature check


def check_sample(tapes: dict[str, Tape]) -> list[tuple[str, str]]:
    """20 fixed symbol-days with an ok part: lowest SHA-256 of 'v1|check|SYMBOL|day'."""

    def key(item: tuple[str, str]) -> str:
        return hashlib.sha256(f"{VERSION}|check|{item[0]}|{item[1]}".encode()).hexdigest()

    every = [(s, _day(d)) for s, tape in tapes.items() for d in tape.ok_days]
    return sorted(sorted(every, key=key)[:CHECK_SAMPLE])


def mask_violations(parts_dir: Path, manifest: dict[str, Any]) -> tuple[int, int]:
    """Kept minutes inside the fixed mask, over every ok part (must be 0), and minutes read."""
    bad = seen = 0
    for entry in manifest["parts"]:
        path = parts_dir / entry["symbol"] / f"{entry['day']}.json.gz"
        part = json.loads(gzip.decompress(path.read_bytes()))
        if part["status"] != "ok":
            continue
        for minute in part["s_minutes"]:
            seen += 1
            bad += minute_masked(int(minute))
    return bad, seen


def build_check(
    features: Features, tapes: dict[str, Tape], violations: tuple[int, int]
) -> dict[str, Any]:
    """Undefined reasons of B, V and their ranks on every hour of the tape's ok days;
    no event, rate, coverage or MDE."""
    reasons: Counter[str] = Counter()
    for symbol, tape in sorted(tapes.items()):
        series = features.instruments.get(symbol)
        if series is None or not tape.ok_days:
            reasons["no_series"] += 1
            continue
        first = min(tape.ok_days) + (RANK_HOURS + EPISODE_H) * HOUR_MS
        end = max(tape.ok_days) + DAY_MS
        if first >= end:
            reasons["span_short"] += 1
            continue
        for state in states(symbol, series, tape, first, end).values():
            reasons[state.reason or "defined"] += 1
    return {
        "version": VERSION,
        "step": "step 1: feature check on HYP-032's tape, no event rate, coverage or MDE",
        "hyp032_manifest_sha256": HYP032_TAPE_MANIFEST_SHA256,
        "hours_by_reason": dict(sorted(reasons.items())),
        "kept_minutes_read": violations[1],
        "kept_minutes_inside_mask": violations[0],
        "check_sample": [f"{s} {d}" for s, d in check_sample(tapes)],
    }


# ---------------------------------------------------------------- step 1: raw recount


def raw_hourly(path: Path, symbol: str, day: str) -> dict[int, tuple[float, float]]:
    """Taker buy and sell USD per hour over the kept minutes, summed straight from one
    raw trade file with csv and Decimal, a code path apart from the tape's DuckDB query."""
    start = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)
    sums: dict[int, list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])
    lo, hi = RAW_MASK_MINUTES
    with gzip.open(path, "rt", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["symbol"] != symbol or row["side"] not in ("Buy", "Sell"):
                continue
            ms = int(Decimal(row["timestamp"]) * 1000)
            if not start <= ms < start + DAY_MS:
                continue
            minute_of_hour = ms % HOUR_MS // 60_000
            if minute_of_hour < lo or minute_of_hour >= hi:
                continue
            sums[ms - ms % HOUR_MS][row["side"] == "Sell"] += Decimal(row["foreignNotional"])
    return {h: (float(b), float(s)) for h, (b, s) in sums.items()}


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= RAW_RELATIVE * max(abs(a), abs(b), 1.0)


def plain_ranks(tape: Tape, t: int) -> tuple[float | None, float | None]:
    """qB and qV at t counted plainly over the stored 336 hours before t."""
    now = flow(tape, t)
    if not isinstance(now, tuple):
        return None, None
    history = [flow(tape, t - k * HOUR_MS) for k in range(1, RANK_HOURS + 1)]
    defined = [f for f in history if isinstance(f, tuple)]
    if len(defined) < RANK_MIN_DEFINED:
        return None, None
    out = []
    for i in (0, 1):
        below = sum(1 for f in defined if f[i] < now[i])
        equal = sum(1 for f in defined if f[i] == now[i])
        out.append((below + equal / 2) / len(defined))
    return out[0], out[1]


def recount_day(
    features: Features, tape: Tape, symbol: str, day: str, raw: dict[int, tuple[float, float]]
) -> dict[str, Any]:
    """One sample day: the stored hourly buy and sell (and so B and V) against the raw
    recount, and the stored ranks against the plain recount, for the 24 decision hours
    whose flow hour is in the day."""
    start = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)
    flow_bad = []
    for h in range(start, start + DAY_MS, HOUR_MS):
        stored = tape.kept.get(h, (0.0, 0.0))
        fresh = raw.get(h, (0.0, 0.0))
        if not (_close(stored[0], fresh[0]) and _close(stored[1], fresh[1])):
            flow_bad.append(_iso(h))
    series = features.instruments.get(symbol)
    rank_bad, rank_defined = [], 0
    if series is not None:
        by_hour = states(symbol, series, tape, start + HOUR_MS + EPISODE_H * HOUR_MS,
                         start + DAY_MS + HOUR_MS)  # fmt: skip
        for t in range(start + HOUR_MS, start + DAY_MS + HOUR_MS, HOUR_MS):
            qb, qv = plain_ranks(tape, t)
            st = by_hour[t]
            if qb is None or st.qb is None or st.qv is None or qv is None:
                if (qb is None) != (st.qb is None):
                    rank_bad.append(_iso(t))
                continue
            rank_defined += 1
            if not (_close(qb, st.qb) and _close(qv, st.qv)):
                rank_bad.append(_iso(t))
    return {
        "flow_hours": 24,
        "flow_hours_differing": flow_bad,
        "rank_hours_defined": rank_defined,
        "rank_hours_differing": rank_bad,
    }


def build_check_raw(
    features: Features,
    tapes: dict[str, Tape],
    parts_dir: Path,
    out_dir: Path,
    max_bytes: int,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Fetch the sample's raw files under a ledgered cap, check each is the file the part
    was built from (its SHA-256), and recount."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger = out_dir / "budget-ledger.jsonl"
    budget = Budget(max_bytes, multiprocessing.Value("q", ledger_spent(ledger)), ledger)
    tmp = out_dir / "tmp"
    tmp.mkdir(exist_ok=True)
    days: list[dict[str, Any]] = []
    with httpx.Client(timeout=300, follow_redirects=True, transport=transport) as client:
        for symbol, day in check_sample(tapes):
            part = json.loads(gzip.decompress((parts_dir / symbol / f"{day}.json.gz").read_bytes()))
            target = tmp / f"{symbol}{day}.csv.gz"
            row: dict[str, Any] = {"symbol": symbol, "day": day}
            try:
                got = download(client, ARCHIVE_FILE.format(symbol=symbol, day=day), target, budget)
                if got is None:
                    days.append(row | {"status": "missing"})
                    continue
                row["same_file"] = got[1] == part.get("file_sha256")
                row |= recount_day(features, tapes[symbol], symbol, day,
                                   raw_hourly(target, symbol, day))  # fmt: skip
            finally:
                target.unlink(missing_ok=True)
            agree = row["same_file"] and not row["flow_hours_differing"]
            agree = agree and not row["rank_hours_differing"]
            days.append(row | {"status": "agrees" if agree else "differs"})
    return {
        "version": VERSION,
        "step": "step 1: independent recount of B, V and their ranks on 20 symbol-days",
        "hyp032_manifest_sha256": HYP032_TAPE_MANIFEST_SHA256,
        "bytes_read": ledger_spent(ledger),
        "max_bytes": max_bytes,
        "days": days,
        "verdict": "agrees" if all(d["status"] == "agrees" for d in days) else "differs",
    }


# ---------------------------------------------------------------- seeding the full fetch


def tape_binding(scope_sha256: str) -> dict[str, Any]:
    """The run binding HYP-032's fetcher writes for an unpiloted run of this study."""
    return {
        "version": TAPE_VERSION,
        **RULES,
        "scope_sha256": scope_sha256,
        "features_sha256": FEATURES_SHA256,
        "hyp031_index_sha256": HYP031_INDEX_SHA256,
        "pilot": 0,
    }


def bound_tape(
    manifest_path: Path,
    manifest_sha256: str,
    scope: dict[str, Any],
    scope_sha256: str,
    features: Features,
    mode: str,
) -> None:
    """Refuses, before any part is read, a tape that is not this study's sealed run of
    exactly the scope the protocol fixes for ``mode``: the scope's days are recomputed,
    the run binding must be the one ``tape_binding`` gives for that scope (rules, inputs,
    no pilot subset), and the manifest must hold exactly the scope's symbol-days."""
    if (scope.get("version"), scope.get("study"), scope.get("mode")) != (
        TAPE_VERSION,
        VERSION,
        mode,
    ):
        raise SystemExit(f"the scope is not this study's {mode} scope")
    if scope.get("primary") != scope_days(features, mode):
        raise SystemExit(f"the scope's days are not the {mode} days the protocol fixes")
    manifest = json.loads(verified(manifest_path, manifest_sha256).read_bytes())
    if manifest.get("version") != TAPE_VERSION or manifest.get("binding") != tape_binding(
        scope_sha256
    ):
        raise SystemExit("the tape is bound to other inputs, rules or scope")
    held = sorted((p["symbol"], p["day"]) for p in manifest["parts"])
    wanted = sorted((s, d) for s, days in scope["primary"].items() for d in days)
    if held != wanted:
        raise SystemExit("the tape's symbol-days are not exactly the scope's")


def seed(
    source_manifest: Path,
    source_manifest_sha256: str,
    source_scope: dict[str, Any],
    source_scope_sha256: str,
    target_scope: dict[str, Any],
    target_scope_sha256: str,
    out_dir: Path,
) -> dict[str, Any]:
    """A new run bound to the full scope, holding the pilot's ok parts that are in it.
    Every input must match before anything is written; a part that fails its own checks
    is left for the fetch. seed.json keeps each imported part's origin. The run is built
    in a staging directory beside ``out_dir`` and published by one rename, so an
    interrupted seed leaves no ``out_dir`` (at most a hidden staging directory)."""
    manifest = json.loads(verified(source_manifest, source_manifest_sha256).read_bytes())
    if source_scope.get("study") != VERSION or source_scope.get("mode") != "blocks":
        raise SystemExit("the source scope is not this study's pilot blocks")
    if (
        target_scope.get("study") != VERSION
        or target_scope.get("mode") != "full"
        or target_scope.get("version") != TAPE_VERSION
    ):
        raise SystemExit("the target scope is not this study's full scope")
    source_binding = manifest.get("binding", {})
    if manifest.get("version") != TAPE_VERSION or source_binding != tape_binding(
        source_scope_sha256
    ):
        raise SystemExit("the source run is bound to other inputs, rules or scope")
    if out_dir.exists() and any(out_dir.iterdir()):
        raise SystemExit(f"{out_dir} is not empty; seed only into a new directory")
    wanted = {(s, d) for s, days in target_scope["primary"].items() for d in days}
    pilot = {(s, d) for s, days in source_scope["primary"].items() for d in days}
    source_dir = source_manifest.parent
    imports: list[tuple[dict[str, Any], bytes]] = []
    skipped: Counter[str] = Counter()
    for entry in manifest["parts"]:
        key = (entry["symbol"], entry["day"])
        if key not in wanted or key not in pilot:
            skipped["outside_scope"] += 1
            continue
        if entry["status"] != "ok":
            skipped["not_ok"] += 1
            continue
        path = part_path(source_dir, *key)
        if not path.exists() or file_sha256(path) != entry["part_sha256"]:
            skipped["sha256_differs"] += 1
            continue
        payload = path.read_bytes()
        part = json.loads(gzip.decompress(payload))
        if (part.get("version"), part.get("symbol"), part.get("day"), part.get("status")) != (
            TAPE_VERSION,
            *key,
            "ok",
        ):
            skipped["content_differs"] += 1
            continue
        record = {"symbol": key[0], "day": key[1], "part_sha256": entry["part_sha256"],
                  "file_bytes": int(part.get("file_bytes", 0))}  # fmt: skip
        imports.append((record, payload))
    binding = tape_binding(target_scope_sha256)
    body = {
        "version": SEED_VERSION,
        "binding": binding,
        "source_manifest_sha256": source_manifest_sha256,
        "source_binding": source_binding,
        "imported_parts": len(imports),
        "imported_file_bytes": sum(r["file_bytes"] for r, _ in imports),
        "skipped": dict(sorted(skipped.items())),
        "parts": [r for r, _ in imports],
    }
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{out_dir.name}.seed-", dir=out_dir.parent))
    try:
        bind_run(staging, binding, create=True)
        digest = publish_once(staging / "seed.json", body)
        for record, payload in imports:
            target = part_path(staging, record["symbol"], record["day"])
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        if out_dir.exists():
            out_dir.rmdir()  # empty, checked above; fails if anything appeared since
        staging.rename(out_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return body | {"seed_sha256": digest}


# ---------------------------------------------------------------- output


def render_markdown(report: dict[str, Any]) -> str:
    lines = [f"# {VERSION}: {report['step']}", ""]
    rows: list[tuple[str, ...]]
    if "hours_by_reason" in report:
        rows = [(k, f"{v:,}") for k, v in report["hours_by_reason"].items()]
        lines += [*markdown_table(("Hour status", "Hours"), rows), ""]
        lines += [f"Kept minutes inside the mask: {report['kept_minutes_inside_mask']}"]
        return "\n".join(lines) + "\n"
    funnel = [(k, f"{v:,}") for k, v in report["funnel"].items()]
    lines += [*markdown_table(("Funnel item", "Member-hours"), funnel), ""]
    lines += [
        *markdown_table(("Events", "Value"), [(k, str(v)) for k, v in report["events"].items()]),
        "",
    ]
    lines += [
        *markdown_table(
            ("Controls", "Value"), [(k, str(v)) for k, v in report["controls"].items()]
        ),
        "",
    ]
    if report["balance"]:
        rows = [(k, str(v["smd"])) for k, v in report["balance"].items()]
        lines += [*markdown_table(("Balance", "SMD"), rows), ""]
    rows = [
        (leg, str(v["units"]), str(v["scales_substituted"]), f"{v['mde_bps']:.2f}")
        if v
        else (leg, "0", "0", "not estimable")
        for leg, v in report["power"].items()
    ]
    lines += [*markdown_table(("Leg", "Units", "Scales substituted", "MDE, bps"), rows), ""]
    lines += [f"Determinable share: {report['determinable_share']}", ""]
    gate = report.get("gate") or report["floor"]
    if "projected_events" in report:
        lines += [f"Projected events: {report['projected_events']}", ""]
        rows = [(k, str(v)) for k, v in gate["checks"].items()]
        lines += [*markdown_table(("Pilot gate", "Holds"), rows), ""]
    else:
        rows = [
            (k, str(v))
            for k, v in {**gate["data"], "balance": gate["balance"], **gate["power"]}.items()
        ]
        lines += [*markdown_table(("Floor item", "Holds"), rows), ""]
    lines += [f"Verdict: **{gate['verdict']}**"]
    return "\n".join(lines) + "\n"


def _provenance(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--code-revision", required=True)
    dirty = parser.add_mutually_exclusive_group(required=True)
    dirty.add_argument("--working-tree-dirty", dest="working_tree_dirty", action="store_true")
    dirty.add_argument("--no-working-tree-dirty", dest="working_tree_dirty", action="store_false")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    scope = sub.add_parser("scope", help="the tape scope and its estimated size")
    scope.add_argument("--mode", choices=("blocks", "full"), required=True)
    scope.add_argument("--archive-sizes", type=Path, required=True)
    scope.add_argument("--archive-sizes-sha256", required=True)
    check = sub.add_parser("check", help="step 1 on HYP-032's sealed tape")
    check.add_argument("--manifest", type=Path, required=True)
    check.add_argument("--manifest-sha256", required=True)
    counts = sub.add_parser("counts", help="steps 2 to 4 on a sealed tape")
    counts.add_argument("--mode", choices=("blocks", "full"), required=True)
    counts.add_argument("--manifest", type=Path, required=True)
    counts.add_argument("--manifest-sha256", required=True)
    counts.add_argument("--scope", type=Path, required=True)
    counts.add_argument("--scope-sha256", required=True)
    raw = sub.add_parser("check-raw", help="step 1's recount from 20 raw files (fetches)")
    raw.add_argument("--manifest", type=Path, required=True)
    raw.add_argument("--manifest-sha256", required=True)
    raw.add_argument("--max-bytes", type=int, required=True)
    seeding = sub.add_parser("seed", help="a full-scope run seeded with the pilot's parts")
    seeding.add_argument("--source-manifest", type=Path, required=True)
    seeding.add_argument("--source-manifest-sha256", required=True)
    seeding.add_argument("--source-scope", type=Path, required=True)
    seeding.add_argument("--source-scope-sha256", required=True)
    seeding.add_argument("--scope", type=Path, required=True)
    seeding.add_argument("--scope-sha256", required=True)
    for cmd in (scope, check, counts, raw, seeding):
        cmd.add_argument("--features", type=Path, required=True)
        cmd.add_argument("--features-sha256", required=True)
        _provenance(cmd)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.features_sha256 != FEATURES_SHA256:
        raise SystemExit(f"the protocol pins the features snapshot {FEATURES_SHA256}")
    if (
        args.command in ("check", "check-raw")
        and args.manifest_sha256 != HYP032_TAPE_MANIFEST_SHA256
    ):
        raise SystemExit(f"step 1 pins HYP-032's tape manifest {HYP032_TAPE_MANIFEST_SHA256}")
    if args.command == "scope" and args.archive_sizes_sha256 != ARCHIVE_SIZES_SHA256:
        raise SystemExit(f"the size estimate pins HYP-032's record {ARCHIVE_SIZES_SHA256}")
    provenance = {
        "code_revision": normalize_code_revision(args.code_revision),
        "working_tree_dirty": args.working_tree_dirty,
    }
    if args.command == "seed":
        source_scope = json.loads(
            verified(args.source_scope, args.source_scope_sha256).read_bytes()
        )
        target_scope = json.loads(verified(args.scope, args.scope_sha256).read_bytes())
        done = seed(args.source_manifest, args.source_manifest_sha256, source_scope,
                    args.source_scope_sha256, target_scope, args.scope_sha256,
                    args.output_dir)  # fmt: skip
        summary = {k: v for k, v in done.items() if k != "parts"} | provenance
        sys.stdout.write(json.dumps(summary, indent=2) + "\n")
        return
    features = load_features(verified(args.features, args.features_sha256))
    if args.command == "scope":
        days = scope_days(features, args.mode)
        listed, sizes = load_archive_sizes(verified(args.archive_sizes, args.archive_sizes_sha256))
        # HYP-032's fetcher reads the version and the "primary" days of a scope.
        body = {"version": TAPE_VERSION, "study": VERSION, "mode": args.mode, "primary": days}
        digest = publish_once(args.output_dir / "scope.json", body)
        size = estimate(days, listed, sizes) | {"scope_sha256": digest, "mode": args.mode}
        publish_once(args.output_dir / "scope-size.json", size | provenance)
        sys.stdout.write(json.dumps(size, indent=2) + "\n")
        return
    if args.command == "counts":
        scope = json.loads(verified(args.scope, args.scope_sha256).read_bytes())
        bound_tape(args.manifest, args.manifest_sha256, scope, args.scope_sha256, features,
                   args.mode)  # fmt: skip
    tapes, meta = load_tape(args.manifest, args.manifest_sha256)
    if args.command == "check-raw":
        report = build_check_raw(features, tapes, args.manifest.parent / "parts",
                                 args.output_dir, args.max_bytes) | provenance  # fmt: skip
        digest = publish_once(args.output_dir / "check-raw.json", report)
        sys.stdout.write(f"check-raw.json sha256 {digest}\nverdict: {report['verdict']}\n")
        return
    if args.command == "check":
        manifest = json.loads(args.manifest.read_bytes())
        report = build_check(
            features, tapes, mask_violations(args.manifest.parent / "parts", manifest)
        )
        frozen: dict[str, Any] | None = None
    else:
        report, frozen = build(features, tapes, args.mode)
        report["manifest"] = meta
        report["scope_sha256"] = args.scope_sha256
    render_markdown(report)  # fail before anything is published, not after
    if frozen is not None:
        report["pairs_sha256"] = publish_once(args.output_dir / "pairs.json", frozen)
    report |= provenance
    name = "check" if args.command == "check" else "counts"
    digest = publish_once(args.output_dir / f"{name}.json", report)
    (args.output_dir / f"{name}.md").write_text(render_markdown(report))
    sys.stdout.write(f"{name}.json sha256 {digest}\n")
    if "gate" in report or "floor" in report:
        sys.stdout.write(f"verdict: {(report.get('gate') or report['floor'])['verdict']}\n")
