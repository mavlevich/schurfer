"""HYP-031 family, B1: can the pre-settlement funding signal be rebuilt at decision time?

Implements docs/research/bybit-presettlement-feasibility-v1.md. Reads no perpetual price
and computes no return.

- ``plan``: counts of requests, from the pinned minute discovery index (and, once
  fetched, the hourly premium snapshot); nothing is requested.
- ``fetch-hourly``: 60-minute premium index klines per instrument, discovery year.
- ``fetch-minutes``: 1-minute premium index klines for every superset interval and the
  validation sample.
- ``report``: S1 (the formula reproduces the settled rate), S2 and S3 (the decision-time
  signal and the rebuilt event list per decision time), from verified snapshots only.

The superset and every event are chosen from data available at the decision time; the
settled rate is used only by S1 and as a diagnostic.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import gzip
import json
import random
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from .bybit_carry_feasibility import publish_once
from .bybit_post_funding_screen import (
    DAY_MS,
    HOUR_MS,
    MINUTE_MS,
    STAGES,
    Index,
    SnapshotWriter,
    _pages,
    _run_fetch,
    _week,
    load_index,
    recovered,
    require_boundary,
    verified,
)
from .reporting import normalize_code_revision

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

VERSION: Final = "bybit_presettlement_feasibility_v1"
STAGE: Final = "discovery"
FIRST_MS, LAST_MS, _ = STAGES[STAGE]
# The minute discovery index, pinned in docs/research/evidence/bybit-post-funding-minute-v1.
INDEX_SHA256: Final = "f48fe09f7023a476e524cf70c6ec1b629eee30a9a07cff41c9d149dfb59e0cf5"
# Pinned by a commit after each fetch and before any report; None refuses the next step.
HOURLY_SHA256: Final[str | None] = None
MINUTES_SHA256: Final[str | None] = None

TRIGGER: Final = -0.003  # F_hat at most -0.30%
S1_RELEVANT: Final = -0.001  # S1 gates settlements at most -0.10%
SUPERSET_LOW: Final = -0.002  # an hourly premium low at most -0.20% enters the superset
CLAMP: Final = 0.0005  # clamp(I - P, -0.05%, +0.05%)
INTEREST_PER_8H: Final = 0.0001  # assumed, tested by S1
DECISION_OFFSETS_MIN: Final = (30, 15, 10)
PUBLICATION_LAG_MS: Final = MINUTE_MS  # a minute bar is used once it ended 1 min before t - d
RECENT_EXTREME_LOOKBACK_MS: Final = 7 * DAY_MS
TURNOVER_BARS: Final = 24
MIN_TURNOVER_USD: Final = 2_000_000.0
MAX_INTERVAL_MIN: Final = 480
S1_TOLERANCE: Final = 0.0002  # 0.02 percentage points (2 bps)
S1_STRICT_TOLERANCE: Final = 0.00005  # 0.005 points, a diagnostic
S1_MIN_WITHIN: Final = 0.95
S1_MIN_CLASS_AGREE: Final = 0.98  # overall
S1_MIN_SENSITIVITY: Final = 0.95  # rebuilt <= -0.30% among settled <= -0.30%
S1_MIN_SPECIFICITY: Final = 0.95  # rebuilt > -0.30% among settled in (-0.30%, -0.10%]
VALIDATION_SAMPLE: Final = 2000
VALIDATION_SEED: Final = 31
MAX_OUT_OF_HOUR_SHARE: Final = 0.001
S1_MIN_COVERAGE: Final = 0.95  # complete minute data among S1 intervals whose schedule held
S1_MIN_RELEVANT: Final = 300  # scored settlements at most -0.10%
S1_MIN_EACH_SIDE: Final = 50  # scored settlements on each side of the -0.30% trigger
MIN_CLUSTERS: Final = 150
MIN_SYMBOLS: Final = 40
MIN_WEEKS: Final = 26
# B2 for an interval class needs that class to pass on its own (S1 and the S3 floor);
# a pooled pass is not enough. 4 h is the class the program plan carries forward.
GATED_INTERVALS: Final = ("1h", "2h", "4h", "8h")
MAX_TOP5_SHARE: Final = 0.30
KLINE_PAGE_LIMIT: Final = 1000
CONCURRENCY: Final = 4


# ---------------------------------------------------------------- the formula


def interest(n_min: int) -> float:
    return INTEREST_PER_8H * n_min / 480


def funding_from(weighted: Sequence[tuple[int, float]], n_min: int) -> float:
    """F = P + clamp(I - P, -0.05%, +0.05%), with P the weight-renormalized average."""
    total = sum(w for w, _ in weighted)
    if total <= 0:
        raise ValueError("no weight")
    p = sum(w * v for w, v in weighted) / total
    return p + max(-CLAMP, min(CLAMP, interest(n_min) - p))


def observed_weight_share(n_min: int, d_min: int) -> float:
    """Share of the interval's linear weight 1..N in minutes 1..N - d."""
    known = max(0, n_min - d_min)
    return known * (known + 1) / (n_min * (n_min + 1))


@dataclass(frozen=True, slots=True)
class Interval:
    """An averaging interval as known after the settlement s1: [s1, predicted)."""

    symbol: str
    s1: int
    predicted: int
    n_min: int
    schedule_uncertain: bool  # the last two observed intervals differ
    actual_next: int | None  # diagnostic only

    @property
    def key(self) -> tuple[str, int]:
        return (self.symbol, self.s1)


def minute_starts(iv: Interval) -> list[int]:
    return [iv.s1 + k * MINUTE_MS for k in range(iv.n_min)]


def full_rate(iv: Interval, closes: Mapping[int, float]) -> float | None:
    """S1: the rate rebuilt from every minute of the interval, None on any gap."""
    values = [closes.get(m) for m in minute_starts(iv)]
    if any(v is None for v in values):
        return None
    return funding_from([(k + 1, v) for k, v in enumerate(values) if v is not None], iv.n_min)


def decision_rate(iv: Interval, closes: Mapping[int, float], d_min: int) -> float | None:
    """S2: the rate from the minutes that ended by t - d - lag, None on any gap."""
    cut = iv.predicted - d_min * MINUTE_MS - PUBLICATION_LAG_MS
    known = [(k + 1, m) for k, m in enumerate(minute_starts(iv)) if m + MINUTE_MS <= cut]
    if not known:
        return None
    values = [(w, closes.get(m)) for w, m in known]
    if any(v is None for _, v in values):
        return None
    return funding_from([(w, v) for w, v in values if v is not None], iv.n_min)


# ---------------------------------------------------------------- decision-time inputs


def intervals(index: Index) -> list[Interval]:
    """Every interval opened by a settlement inside the stage, with the next settlement
    predicted from the last observed interval only."""
    out: list[Interval] = []
    for symbol in sorted(index.instruments):
        inst = index.instruments[symbol]
        times = sorted(inst.funding)
        for k in range(1, len(times)):
            s0, s1 = times[k - 1], times[k]
            gap = s1 - s0
            predicted = s1 + gap
            n_min = gap // MINUTE_MS
            if gap % HOUR_MS or not 0 < n_min <= MAX_INTERVAL_MIN:
                continue
            if not FIRST_MS <= predicted <= min(LAST_MS, inst.end_ms):
                continue
            uncertain = k < 2 or (s0 - times[k - 2]) != gap
            actual = times[k + 1] if k + 1 < len(times) else None
            out.append(Interval(symbol, s1, predicted, n_min, uncertain, actual))
    return out


def recent_extreme(funding: Mapping[int, float], cut: int) -> bool:
    """Part of the signal's definition, not a proof of the current cap: the instrument
    settled at or below the trigger within the week before cut."""
    return any(
        cut - RECENT_EXTREME_LOOKBACK_MS <= ts < cut and r <= TRIGGER for ts, r in funding.items()
    )


def superseded(times: Sequence[int], iv: Interval, cut: int) -> bool:
    """A settlement after s1 and known by cut replaces this forecast: the averaging window
    restarted there, and that settlement opens its own interval. A settlement after the
    cut cannot cancel a forecast."""
    k = bisect.bisect_right(times, iv.s1)
    return k < len(times) and times[k] <= cut


def liquid_at(turnover: Mapping[int, float], cut: int) -> bool:
    """USD turnover of the 24 hourly bars that ended at or before cut."""
    last_start = (cut // HOUR_MS) * HOUR_MS - HOUR_MS
    bars = [turnover.get(last_start - k * HOUR_MS) for k in range(TURNOVER_BARS)]
    if any(b is None for b in bars):
        return False
    return sum(b for b in bars if b is not None) >= MIN_TURNOVER_USD


def in_superset(iv: Interval, lows: Mapping[int, float]) -> bool:
    """F_hat <= -0.30% needs P_hat <= -0.25%, so some minute, and so some hourly low,
    at or below it. A missing hour cannot exclude the interval."""
    start = (iv.s1 // HOUR_MS) * HOUR_MS
    hours = range(start, iv.predicted, HOUR_MS)
    values = [lows.get(h) for h in hours]
    return any(v is None or v <= SUPERSET_LOW for v in values)


def validation_sample(all_intervals: Sequence[Interval]) -> list[Interval]:
    """Drawn independently of any rate or premium."""
    pool = sorted(all_intervals, key=lambda iv: iv.key)
    k = min(VALIDATION_SAMPLE, len(pool))
    rng = random.Random(VALIDATION_SEED)  # noqa: S311 a reproducible sample, not security
    return sorted(rng.sample(pool, k), key=lambda iv: iv.key)


# ---------------------------------------------------------------- snapshots


@dataclass(frozen=True)
class HourlyPremium:
    low: dict[str, dict[int, float]]
    high: dict[str, dict[int, float]]
    failed: tuple[str, ...]


@dataclass(frozen=True)
class MinutePremium:
    closes: dict[str, dict[int, float]]
    failed: tuple[tuple[str, int], ...]


def _lines(path: Path, kind_version: str) -> Iterable[dict[str, Any]]:
    version = ""
    with gzip.open(path, "rb") as stream:
        for raw in stream:
            line = json.loads(raw)
            if line["kind"] == "version":
                version = line["response"]["version"]
                continue
            if version != kind_version:
                raise SystemExit(f"{path.name} is not {kind_version}: {version!r}")
            yield line


def load_hourly(path: Path) -> HourlyPremium:
    low: dict[str, dict[int, float]] = defaultdict(dict)
    high: dict[str, dict[int, float]] = defaultdict(dict)
    failed: set[str] = set()
    for line in _lines(path, VERSION):
        symbol = line["symbol"]
        if line["kind"] == "fetch_error":
            failed.add(symbol)
        elif line["kind"] == "premium_60":
            result = line["response"]["result"]
            if result.get("symbol") not in (None, symbol):
                failed.add(symbol)
                continue
            for item in result["list"]:
                low[symbol][int(item[0])] = float(item[3])
                high[symbol][int(item[0])] = float(item[2])
    keep = {s for s in low if s not in failed}
    return HourlyPremium(
        {s: low[s] for s in keep}, {s: high[s] for s in keep}, tuple(sorted(failed))
    )


def load_minutes(path: Path) -> MinutePremium:
    closes: dict[str, dict[int, float]] = defaultdict(dict)
    failed: set[tuple[str, int]] = set()
    for line in _lines(path, VERSION):
        symbol = line["symbol"]
        if line["kind"] == "fetch_error":
            failed.add((symbol, int(line["params"]["s1"])))
        elif line["kind"] == "premium_1":
            result = line["response"]["result"]
            if result.get("symbol") not in (None, symbol):
                continue
            for item in result["list"]:
                closes[symbol][int(item[0])] = float(item[4])
    return MinutePremium(dict(closes), tuple(sorted(failed)))


# ---------------------------------------------------------------- fetching


async def fetch_hourly(client: Any, index: Index, path: Path) -> dict[str, Any]:
    recovered.clear()
    writer = SnapshotWriter(path)
    writer.record("version", None, {}, {"version": VERSION})
    errors: dict[str, str] = {}
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def one(symbol: str, end: int) -> None:
        async with semaphore:
            try:
                await _pages(
                    client.public_get_v5_market_premium_index_price_kline,
                    {"category": "linear", "symbol": symbol, "interval": "60",
                     "start": FIRST_MS - DAY_MS, "end": end, "limit": KLINE_PAGE_LIMIT},
                    what=f"premium 60 {symbol}",
                    limit=KLINE_PAGE_LIMIT,
                    oldest=lambda item: int(item[0]),
                    record=lambda p, r: writer.record("premium_60", symbol, p, r),
                )  # fmt: skip
            except Exception as exc:  # one symbol's failure is counted, not a stop
                errors[symbol] = str(exc)[:300]
                writer.record("fetch_error", symbol, {}, {"error": errors[symbol]})

    items = sorted((s, min(i.end_ms, LAST_MS)) for s, i in index.instruments.items())
    try:
        await asyncio.gather(*(one(s, end) for s, end in items))
    finally:
        writer.close()
    return {"version": VERSION, "symbols": len(items), "errors": errors,
            "responses": writer.written, "recovered_requests": dict(recovered)}  # fmt: skip


async def fetch_minutes(client: Any, todo: Sequence[Interval], path: Path) -> dict[str, Any]:
    recovered.clear()
    writer = SnapshotWriter(path)
    writer.record("version", None, {}, {"version": VERSION})
    errors: dict[str, str] = {}
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def one(iv: Interval) -> None:
        end = iv.predicted - 1
        require_boundary(end)
        if end > LAST_MS:
            raise SystemExit(f"refusing a request ending after {LAST_MS} ms")
        async with semaphore:
            try:
                await _pages(
                    client.public_get_v5_market_premium_index_price_kline,
                    {"category": "linear", "symbol": iv.symbol, "interval": "1",
                     "start": iv.s1, "end": end, "limit": KLINE_PAGE_LIMIT},
                    what=f"premium 1 {iv.symbol} {iv.s1}",
                    limit=KLINE_PAGE_LIMIT,
                    oldest=lambda item: int(item[0]),
                    record=lambda p, r: writer.record("premium_1", iv.symbol, p, r),
                )  # fmt: skip
            except Exception as exc:
                key = f"{iv.symbol} {iv.s1}"
                errors[key] = str(exc)[:300]
                writer.record("fetch_error", iv.symbol, {"s1": iv.s1}, {"error": errors[key]})

    try:
        await asyncio.gather(*(one(iv) for iv in todo))
    finally:
        writer.close()
    return {"version": VERSION, "intervals": len(todo), "errors": errors,
            "responses": writer.written, "recovered_requests": dict(recovered)}  # fmt: skip


# ---------------------------------------------------------------- planning


def hourly_requests(index: Index) -> int:
    span = [(min(i.end_ms, LAST_MS) - (FIRST_MS - DAY_MS)) // HOUR_MS + 1
            for i in index.instruments.values()]  # fmt: skip
    return sum(-(-hours // KLINE_PAGE_LIMIT) for hours in span if hours > 0)


def minute_todo(index: Index, hourly: HourlyPremium) -> tuple[list[Interval], dict[str, int]]:
    every = intervals(index)
    superset = [iv for iv in every if in_superset(iv, hourly.low.get(iv.symbol, {}))]
    sample = validation_sample(every)
    todo = {iv.key: iv for iv in (*superset, *sample)}
    counts = {"intervals": len(every), "superset": len(superset), "sample": len(sample),
              "requests": len(todo)}  # fmt: skip
    return [todo[k] for k in sorted(todo)], counts


# ---------------------------------------------------------------- report


def _settled(index: Index, iv: Interval) -> float | None:
    """The rate settled at the predicted time, if the schedule held (S1 and diagnostics)."""
    if iv.actual_next != iv.predicted:
        return None
    return index.instruments[iv.symbol].funding.get(iv.predicted)


def possible_caps(index: Index) -> dict[str, float]:
    """Diagnostic: the most negative settled rate of a symbol if it repeats 3+ times."""
    out: dict[str, float] = {}
    for symbol, inst in index.instruments.items():
        if not inst.funding:
            continue
        lowest = min(inst.funding.values())
        if lowest < 0 and sum(1 for r in inst.funding.values() if r == lowest) >= 3:
            out[symbol] = lowest
    return out


def _minutes_against_hours(
    pool: Sequence[Interval], hourly: HourlyPremium, minutes: MinutePremium
) -> tuple[int, int]:
    """Check unique minutes in this pool's interval union, not unrelated classes.

    Merge ranges per symbol rather than materializing millions of minute keys or
    rescanning the entire snapshot for every settlement class.
    """
    ranges: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for iv in pool:
        ranges[iv.symbol].append((iv.s1, iv.predicted))
    checked = outside = 0
    for symbol, own in ranges.items():
        closes = minutes.closes.get(symbol, {})
        if not closes:
            continue
        lows, highs = hourly.low.get(symbol, {}), hourly.high.get(symbol, {})
        ordered = sorted(own)
        merged: list[tuple[int, int]] = []
        start, end = ordered[0]
        for left, right in ordered[1:]:
            if left <= end:
                end = max(end, right)
            else:
                merged.append((start, end))
                start, end = left, right
        merged.append((start, end))
        for start, end in merged:
            for minute in range(start, end, MINUTE_MS):
                value = closes.get(minute)
                hour = (minute // HOUR_MS) * HOUR_MS
                if value is not None and hour in lows and hour in highs:
                    checked += 1
                    outside += not lows[hour] <= value <= highs[hour]
    return checked, outside


def s1(
    index: Index, pool: Sequence[Interval], hourly: HourlyPremium, minutes: MinutePremium
) -> dict[str, Any]:
    """The formula against settled rates, on the superset and the validation sample.
    Missing data stays in the coverage denominator; too few scored settlements, or too
    few on either side of the trigger, is insufficient_data, never a pass."""
    caps = possible_caps(index)
    scheduled = complete = pinned = relevant = within = strict = agree = below = above = 0
    hit = correct_reject = 0
    errors: list[float] = []
    for iv in pool:
        settled = _settled(index, iv)
        if settled is None:
            continue
        scheduled += 1
        rebuilt = full_rate(iv, minutes.closes.get(iv.symbol, {}))
        if rebuilt is None:
            continue
        complete += 1
        if settled > S1_RELEVANT:
            continue
        if caps.get(iv.symbol) == settled:
            pinned += 1
            continue
        relevant += 1
        below += settled <= TRIGGER
        above += settled > TRIGGER
        errors.append(abs(rebuilt - settled))
        within += abs(rebuilt - settled) <= S1_TOLERANCE
        strict += abs(rebuilt - settled) <= S1_STRICT_TOLERANCE
        agree += (rebuilt <= TRIGGER) == (settled <= TRIGGER)
        hit += settled <= TRIGGER and rebuilt <= TRIGGER
        correct_reject += settled > TRIGGER and rebuilt > TRIGGER
    total_minutes, out_of_hour = _minutes_against_hours(pool, hourly, minutes)
    coverage = complete / scheduled if scheduled else None
    within_share = within / relevant if relevant else None
    agree_share = agree / relevant if relevant else None
    sensitivity = hit / below if below else None
    specificity = correct_reject / above if above else None
    enough = (
        coverage is not None
        and coverage >= S1_MIN_COVERAGE
        and relevant >= S1_MIN_RELEVANT
        and below >= S1_MIN_EACH_SIDE
        and above >= S1_MIN_EACH_SIDE
    )
    if not enough:
        status = "insufficient_data"
    elif (
        within_share is not None
        and agree_share is not None
        and sensitivity is not None
        and specificity is not None
        and within_share >= S1_MIN_WITHIN
        and agree_share >= S1_MIN_CLASS_AGREE
        and sensitivity >= S1_MIN_SENSITIVITY
        and specificity >= S1_MIN_SPECIFICITY
    ):
        status = "pass"
    else:
        status = "fail"
    return {
        "pool": len(pool),
        "schedule_held": scheduled,
        "complete": complete,
        "coverage": coverage,
        "relevant": relevant,
        "relevant_at_or_below_trigger": below,
        "relevant_above_trigger": above,
        "at_possible_cap": pinned,
        "at_possible_cap_share": pinned / (pinned + relevant) if pinned + relevant else None,
        "within_tolerance_share": within_share,
        "within_strict_share": strict / relevant if relevant else None,
        "class_agreement_share": agree_share,
        "sensitivity_at_trigger": sensitivity,
        "specificity_above_trigger": specificity,
        "max_abs_error": max(errors) if errors else None,
        "minutes_checked_against_hour": total_minutes,
        "minutes_outside_hour_share": out_of_hour / total_minutes if total_minutes else None,
        "superset_needs_rebuild": bool(total_minutes)
        and out_of_hour / total_minutes > MAX_OUT_OF_HOUR_SHARE,
        "status": status,
    }


def classify(
    index: Index,
    iv: Interval,
    closes: Mapping[int, float],
    d_min: int,
    times: Sequence[int] | None = None,
) -> tuple[str, float | None]:
    """The decision-time state of one forecast interval at t - d."""
    inst = index.instruments[iv.symbol]
    cut = iv.predicted - d_min * MINUTE_MS
    known = sorted(inst.funding) if times is None else times
    if superseded(known, iv, cut):
        return "superseded", None
    rate = decision_rate(iv, closes, d_min)
    if rate is None:
        return "premium_gap", None
    if rate > TRIGGER:
        return "no_trigger", rate
    if iv.schedule_uncertain:
        return "schedule_uncertain", rate
    if not liquid_at(inst.turnover, cut):
        return "illiquid", rate
    if not recent_extreme(inst.funding, cut):
        return "no_recent_extreme", rate
    return "admitted", rate


def event_counts(rows: Sequence[Interval]) -> dict[str, Any]:
    clusters = {(iv.symbol, iv.predicted // DAY_MS) for iv in rows}
    by_symbol = Counter(symbol for symbol, _ in clusters)
    top5 = sum(n for _, n in by_symbol.most_common(5))
    weeks = {_week(day * DAY_MS) for _, day in clusters}
    return {
        "events": len(rows),
        "clusters": len(clusters),
        "symbols": len(by_symbol),
        "weeks": len(weeks),
        "top5_symbol_share": top5 / len(clusters) if clusters else None,
        "by_interval_min": dict(sorted(Counter(iv.n_min for iv in rows).items())),
    }


def floor_ok(counts: Mapping[str, Any]) -> bool:
    share = counts["top5_symbol_share"]
    return (
        counts["clusters"] >= MIN_CLUSTERS
        and counts["symbols"] >= MIN_SYMBOLS
        and counts["weeks"] >= MIN_WEEKS
        and share is not None
        and share <= MAX_TOP5_SHARE
    )


def interval_class(n_min: int) -> str:
    return {60: "1h", 120: "2h", 240: "4h", 480: "8h"}.get(n_min, "other")


def _decision_block(
    index: Index, rows: Sequence[Interval], minutes: MinutePremium, d_min: int
) -> dict[str, Any]:
    status: Counter[str] = Counter()
    admitted: list[Interval] = []
    tp = fp = fn = 0
    times = {s: sorted(index.instruments[s].funding) for s in {iv.symbol for iv in rows}}
    for iv in rows:
        label, _ = classify(index, iv, minutes.closes.get(iv.symbol, {}), d_min, times[iv.symbol])
        status[label] += 1
        if label == "admitted":
            admitted.append(iv)
        settled = _settled(index, iv)
        if settled is not None and label not in ("premium_gap", "superseded"):
            fired = label != "no_trigger"
            tp += fired and settled <= TRIGGER
            fp += fired and settled > TRIGGER
            fn += (not fired) and settled <= TRIGGER
    counts = event_counts(admitted)
    return {
        "status": dict(sorted(status.items())),
        "admitted": counts,
        "floor": floor_ok(counts),
        "precision_vs_settled": tp / (tp + fp) if tp + fp else None,
        "recall_vs_settled": tp / (tp + fn) if tp + fn else None,
    }


def s3(index: Index, superset: Sequence[Interval], minutes: MinutePremium) -> dict[str, Any]:
    """Per decision time: the pooled list and each settlement interval apart."""
    by_class: dict[str, list[Interval]] = defaultdict(list)
    for iv in superset:
        by_class[interval_class(iv.n_min)].append(iv)
    out: dict[str, Any] = {}
    for d_min in DECISION_OFFSETS_MIN:
        pooled = _decision_block(index, superset, minutes, d_min)
        pooled["observed_weight_share"] = {
            n: observed_weight_share(n, d_min) for n in sorted({iv.n_min for iv in superset})
        }
        pooled["by_interval"] = {
            name: _decision_block(index, rows, minutes, d_min)
            for name, rows in sorted(by_class.items())
        }
        out[f"d{d_min}"] = pooled
    return out


def build_report(index: Index, hourly: HourlyPremium, minutes: MinutePremium) -> dict[str, Any]:
    every = intervals(index)
    superset = [iv for iv in every if in_superset(iv, hourly.low.get(iv.symbol, {}))]
    pool = {iv.key: iv for iv in (*superset, *validation_sample(every))}
    pooled_ivs = [pool[k] for k in sorted(pool)]
    check = s1(index, pooled_ivs, hourly, minutes)
    decision = s3(index, superset, minutes)
    passing = [d for d, row in decision.items() if row["floor"]]
    verdict = _verdict(check, passing)
    by_interval = interval_gates(index, pooled_ivs, hourly, minutes, decision, verdict)
    return {
        "version": VERSION,
        "intervals": len(every),
        "superset": len(superset),
        "hourly_failed": list(hourly.failed),
        "minutes_failed": len(minutes.failed),
        "s1": check,
        "s3": decision,
        "passing_decision_offsets": passing,
        "verdict": verdict,
        "by_interval": by_interval,
    }


def interval_gates(
    index: Index,
    pool: Sequence[Interval],
    hourly: HourlyPremium,
    minutes: MinutePremium,
    decision: Mapping[str, Any],
    pooled_verdict: str,
) -> dict[str, Any]:
    """Each gated interval class on its own: S1 on that class's settlements with the
    same thresholds and minimum counts, and that class's own S3 floor. A class never
    passes where the pooled formula check does not."""
    inherited = pooled_verdict in (
        "insufficient_validation", "not_reconstructible", "superset_rebuild_needed"
    )  # fmt: skip
    out: dict[str, Any] = {}
    for name in GATED_INTERVALS:
        group = [iv for iv in pool if interval_class(iv.n_min) == name]
        check = s1(index, group, hourly, minutes)
        passing = [
            d for d, row in decision.items() if row["by_interval"].get(name, {}).get("floor")
        ]
        out[name] = {
            "s1": check,
            "passing_decision_offsets": passing,
            "verdict": pooled_verdict if inherited else _verdict(check, passing),
        }
    return out


def _verdict(check: Mapping[str, Any], passing: Sequence[str]) -> str:
    if check["status"] == "insufficient_data":
        return "insufficient_validation"
    if check["status"] != "pass":
        return "not_reconstructible"
    if check["superset_needs_rebuild"]:
        return "superset_rebuild_needed"
    if not passing:
        return "insufficient_events"
    return "pass"


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "fetch-hourly", "fetch-minutes", "report"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--index", type=Path, required=True)
        cmd.add_argument("--hourly", type=Path)
        cmd.add_argument("--hourly-sha256")
        if name in ("fetch-hourly", "fetch-minutes"):
            cmd.add_argument("--snapshot", type=Path, required=True)
        if name == "report":
            cmd.add_argument("--minutes", type=Path, required=True)
            cmd.add_argument("--minutes-sha256", required=True)
            cmd.add_argument("--output-dir", type=Path, required=True)
            cmd.add_argument("--code-revision", required=True)
            dirty = cmd.add_mutually_exclusive_group(required=True)
            dirty.add_argument("--working-tree-dirty", dest="working_tree_dirty",
                               action="store_true")  # fmt: skip
            dirty.add_argument("--no-working-tree-dirty", dest="working_tree_dirty",
                               action="store_false")  # fmt: skip
    return parser


def _pinned_hourly(args: argparse.Namespace) -> HourlyPremium:
    if HOURLY_SHA256 is None or args.hourly is None or args.hourly_sha256 != HOURLY_SHA256:
        raise SystemExit(f"the hourly snapshot must be pinned in code first ({HOURLY_SHA256})")
    return load_hourly(verified(args.hourly, args.hourly_sha256))


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    index = load_index(verified(args.index, INDEX_SHA256))
    if index.stage != STAGE:
        raise SystemExit(f"the index is not the {STAGE} stage")
    if args.command == "plan":
        out: dict[str, Any] = {"hourly_requests": hourly_requests(index)}
        if args.hourly is not None:
            _, counts = minute_todo(index, _pinned_hourly(args))
            out["minute_requests"] = counts
        sys.stdout.write(json.dumps(out) + "\n")
        return
    if args.command == "fetch-hourly":
        _run_fetch(lambda c: fetch_hourly(c, index, args.snapshot), args.snapshot)
        return
    hourly = _pinned_hourly(args)
    if args.command == "fetch-minutes":
        todo, _ = minute_todo(index, hourly)
        _run_fetch(lambda c: fetch_minutes(c, todo, args.snapshot), args.snapshot)
        return
    if MINUTES_SHA256 is None or args.minutes_sha256 != MINUTES_SHA256:
        raise SystemExit(f"the minutes snapshot must be pinned in code first ({MINUTES_SHA256})")
    minutes = load_minutes(verified(args.minutes, args.minutes_sha256))
    report = build_report(index, hourly, minutes) | {
        "index_sha256": INDEX_SHA256,
        "hourly_sha256": args.hourly_sha256,
        "minutes_sha256": args.minutes_sha256,
        "code_revision": normalize_code_revision(args.code_revision),
        "working_tree_dirty": args.working_tree_dirty,
    }
    digest = publish_once(args.output_dir / "result.json", report)
    by_class = {name: row["verdict"] for name, row in report["by_interval"].items()}
    sys.stdout.write(
        json.dumps({"verdict": report["verdict"], "by_interval": by_class, "sha256": digest}) + "\n"
    )
