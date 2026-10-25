"""HYP-031: post-funding reaction on Bybit USDT perpetuals, minute variant.

Implements docs/research/bybit-post-funding-minute-v1.md (v1 with Amendments 1 and 2).
Each stage (discovery, test) is fetched and read on its own, so the test year has no
prices on disk until discovery has passed:

- ``fetch-index``: catalog (live and delisted), every settlement and hourly klines for
  the stage, stored once in a gzip JSON-lines snapshot.
- ``fetch-windows``: from the verified index, the events and controls, then one
  1-minute window [t - 60 min, t + 65 min) per (symbol, t), plus BTCUSDT per t.
- ``report``: reads only the two verified snapshots and applies the fixed reading.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from .bybit_carry_feasibility import publish_once
from .clustered_inference import ClusterObservation, cluster_bootstrap_mean
from .exchange_registry import EXCHANGE_FACTORIES
from .reporting import markdown_table, normalize_code_revision

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

VERSION: Final = "bybit_post_funding_minute_v1_a2"
MINUTE_MS: Final = 60_000
HOUR_MS: Final = 60 * MINUTE_MS
DAY_MS: Final = 24 * HOUR_MS


def _ms(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=UTC).timestamp() * 1000)


DATA_UNTIL_MS: Final = _ms(2026, 9, 29) - 1  # clear of the HYP-012 v2 blind window
STAGES: Final = {
    # name: (first settlement, last settlement, start of the second half)
    "discovery": (_ms(2024, 10, 1), _ms(2025, 10, 1) - 1, _ms(2025, 4, 1)),
    "test": (_ms(2025, 10, 1), DATA_UNTIL_MS, _ms(2026, 4, 1)),
}

TRIGGER: Final = -0.003  # r_t at most -0.30% per settlement
DIAGNOSTIC_TRIGGERS: Final = (-0.001, -0.005)
POSITIVE_TRIGGER: Final = 0.003
FETCH_TRIGGER: Final = -0.001  # windows are fetched for the widest diagnostic
CALM_RATE: Final = 0.0001
MIN_TURNOVER_USD: Final = 2_000_000.0
TURNOVER_BARS: Final = 24
CONTROLS_PER_EVENT: Final = 5
WINDOW_BEFORE_MS: Final = 60 * MINUTE_MS
WINDOW_AFTER_MS: Final = 65 * MINUTE_MS
ENTRY_OFFSET_MS: Final = MINUTE_MS
HOLD_MS: Final = 30 * MINUTE_MS
LAST_EXIT_BAR_OFFSET_MS: Final = 39 * MINUTE_MS  # an exit bar may end by t + 40 min
COST_BPS: Final = 30.0
SENSITIVITY_COST_BPS: Final = 50.0
MIN_NET_BPS: Final = 20.0
MIN_EXCESS_BPS: Final = 30.0
MIN_CLUSTERS: Final = 300
MIN_SYMBOLS: Final = 40
MIN_WEEKS: Final = 26
MIN_COMPLETE_SHARE: Final = 0.95
MIN_CONTROL_SHARE: Final = 0.8
TOP_SYMBOLS_REMOVED: Final = 5
BOOTSTRAP_RESAMPLES: Final = 10_000
BOOTSTRAP_SEED: Final = 31
SECONDS_VARIANT_FIRST_MINUTE_BPS: Final = 30.0
NOTIONAL_USD: Final = 50.0
FUNDING_PAGE_LIMIT: Final = 200
KLINE_PAGE_LIMIT: Final = 1000
MAX_PAGES: Final = 2000
CONCURRENCY: Final = 4


# ---------------------------------------------------------------- fetching


def require_boundary(until_ms: int) -> None:
    if until_ms > DATA_UNTIL_MS:
        raise SystemExit(f"refusing a request ending after {DATA_UNTIL_MS} ms")


def _ok_list(response: Any, what: str) -> list[Any]:
    if not isinstance(response, dict) or str(response.get("retCode")) != "0":
        message = response.get("retMsg") if isinstance(response, dict) else None
        raise RuntimeError(f"{what}: bybit error {message!r}")
    result = response.get("result")
    items = result.get("list") if isinstance(result, dict) else None
    if not isinstance(items, list):
        raise RuntimeError(f"{what}: no result.list")
    return items


async def _pages(
    call: Any,
    params: dict[str, Any],
    *,
    what: str,
    limit: int,
    oldest: Callable[[Any], int],
    record: Callable[[dict[str, Any], Any], None],
) -> None:
    """Page backwards from the end parameter until a short page."""
    end_key = "endTime" if "endTime" in params else "end"
    start_key = "startTime" if "startTime" in params else "start"
    require_boundary(int(params[end_key]))
    for _ in range(MAX_PAGES):
        response = await call(dict(params))
        items = _ok_list(response, what)
        record(dict(params), response)
        if len(items) < limit:
            return
        params[end_key] = min(oldest(item) for item in items) - 1
        if params[end_key] < params[start_key]:
            return
    raise RuntimeError(f"{what}: more than {MAX_PAGES} pages")


async def fetch_linear_catalog(client: Any, status: str | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursor = ""
    while True:
        params: dict[str, Any] = {"category": "linear", "limit": 1000}
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        page = await client.public_get_v5_market_instruments_info(params)
        rows.extend(_ok_list(page, f"catalog {status or 'live'}"))
        cursor = page["result"].get("nextPageCursor") or ""
        if not cursor:
            return rows
        if cursor in seen:
            raise RuntimeError("linear catalog cursor repeated")
        seen.add(cursor)


def universe(catalog: Iterable[dict[str, Any]], stage: str) -> dict[str, int]:
    """USDT linear perpetuals trading in the stage: symbol -> last usable ms."""
    first, last, _ = STAGES[stage]
    out: dict[str, int] = {}
    for item in catalog:
        if item.get("contractType") != "LinearPerpetual" or item.get("settleCoin") != "USDT":
            continue
        launch = int(item.get("launchTime") or 0)
        delivery = int(item.get("deliveryTime") or 0)
        end = min(last, delivery - 1) if delivery else last
        if launch <= last and end >= first:
            out[item["symbol"]] = max(out.get(item["symbol"], end), end)
    return out


class SnapshotWriter:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = gzip.open(path, "xb", compresslevel=6)  # noqa: SIM115 closed by close()
        self.written = 0

    def record(self, kind: str, symbol: str | None, params: Any, response: Any) -> None:
        line = {"kind": kind, "symbol": symbol, "params": params, "response": response}
        self.stream.write(json.dumps(line, sort_keys=True, separators=(",", ":")).encode())
        self.stream.write(b"\n")
        self.written += 1

    def close(self) -> None:
        self.stream.close()


async def fetch_index(client: Any, path: Path, stage: str) -> dict[str, Any]:
    first = STAGES[stage][0]
    live = await fetch_linear_catalog(client, None)
    closed = await fetch_linear_catalog(client, "Closed")
    symbols = universe(live + closed, stage)
    writer = SnapshotWriter(path)
    writer.record("stage", None, {}, {"stage": stage})
    writer.record("catalog", None, {"status": "live"}, {"list": live})
    writer.record("catalog", None, {"status": "Closed"}, {"list": closed})
    errors: dict[str, str] = {}
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def one(symbol: str, end: int) -> None:
        async with semaphore:
            try:
                await _pages(
                    client.public_get_v5_market_funding_history,
                    {
                        "category": "linear",
                        "symbol": symbol,
                        "startTime": first - DAY_MS,
                        "endTime": end,
                        "limit": FUNDING_PAGE_LIMIT,
                    },
                    what=f"funding {symbol}",
                    limit=FUNDING_PAGE_LIMIT,
                    oldest=lambda item: int(item["fundingRateTimestamp"]),
                    record=lambda p, r: writer.record("funding", symbol, p, r),
                )
                await _pages(
                    client.public_get_v5_market_kline,
                    {
                        "category": "linear",
                        "symbol": symbol,
                        "interval": "60",
                        "start": first - DAY_MS,
                        "end": end,
                        "limit": KLINE_PAGE_LIMIT,
                    },
                    what=f"kline 60 {symbol}",
                    limit=KLINE_PAGE_LIMIT,
                    oldest=lambda item: int(item[0]),
                    record=lambda p, r: writer.record("kline_60", symbol, p, r),
                )
            except Exception as exc:  # one symbol's failure is counted, not a stop
                errors[symbol] = str(exc)[:300]
                writer.record("fetch_error", symbol, {}, {"error": errors[symbol]})

    try:
        await asyncio.gather(*(one(s, end) for s, end in sorted(symbols.items())))
    finally:
        writer.close()
    return {"stage": stage, "symbols": len(symbols), "errors": errors, "responses": writer.written}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verified(path: Path, expected: str) -> Path:
    digest = file_sha256(path)
    if digest != expected:
        raise SystemExit(f"{path.name} sha256 {digest} does not match {expected}")
    return path


# ---------------------------------------------------------------- index and events


@dataclass
class Instrument:
    end_ms: int
    funding: dict[int, float] = field(default_factory=dict)
    turnover: dict[int, float] = field(default_factory=dict)  # hourly bar start -> USD


@dataclass(frozen=True)
class Index:
    stage: str
    instruments: dict[str, Instrument]
    failed: tuple[str, ...]


def load_index(path: Path) -> Index:
    stage = ""
    catalog: list[dict[str, Any]] = []
    raw: dict[str, Instrument] = {}
    failed: set[str] = set()
    with gzip.open(path, "rb") as stream:
        for line_bytes in stream:
            line = json.loads(line_bytes)
            kind, symbol = line["kind"], line["symbol"]
            if kind == "stage":
                stage = line["response"]["stage"]
            elif kind == "catalog":
                catalog.extend(line["response"]["list"])
            elif kind == "fetch_error":
                failed.add(symbol)
            elif kind == "funding":
                target = raw.setdefault(symbol, Instrument(0))
                for item in line["response"]["result"]["list"]:
                    if item.get("symbol") != symbol:
                        failed.add(symbol)
                        continue
                    target.funding[int(item["fundingRateTimestamp"])] = float(item["fundingRate"])
            elif kind == "kline_60":
                target = raw.setdefault(symbol, Instrument(0))
                if line["response"]["result"].get("symbol") not in (None, symbol):
                    failed.add(symbol)
                    continue
                for item in line["response"]["result"]["list"]:
                    target.turnover[int(item[0])] = float(item[6])
    if stage not in STAGES:
        raise SystemExit(f"index snapshot has no valid stage: {stage!r}")
    ends = universe(catalog, stage)
    instruments = {
        symbol: Instrument(ends[symbol], inst.funding, inst.turnover)
        for symbol, inst in raw.items()
        if symbol in ends and symbol not in failed
    }
    return Index(stage, instruments, tuple(sorted(failed)))


def turnover_24h(inst: Instrument, t: int) -> float | None:
    """USD turnover of the 24 hourly bars ending at t, or None if any bar is missing."""
    bars = [inst.turnover.get(t - (k + 1) * HOUR_MS) for k in range(TURNOVER_BARS)]
    if any(bar is None for bar in bars):
        return None
    return sum(bar for bar in bars if bar is not None)


@dataclass(frozen=True)
class Settlement:
    symbol: str
    t: int
    rate: float
    prev_rate: float | None
    prev_t: int | None
    next_t: int | None
    turnover: float | None


def settlements(index: Index) -> list[Settlement]:
    first, last, _ = STAGES[index.stage]
    out: list[Settlement] = []
    for symbol in sorted(index.instruments):
        inst = index.instruments[symbol]
        times = sorted(inst.funding)
        for k, t in enumerate(times):
            if not first <= t <= min(last, inst.end_ms):
                continue
            prev_t = times[k - 1] if k else None
            out.append(
                Settlement(
                    symbol,
                    t,
                    inst.funding[t],
                    inst.funding[prev_t] if prev_t is not None else None,
                    prev_t,
                    times[k + 1] if k + 1 < len(times) else None,
                    turnover_24h(inst, t),
                )
            )
    return out


def is_liquid(s: Settlement) -> bool:
    return s.turnover is not None and s.turnover >= MIN_TURNOVER_USD


def is_calm(s: Settlement) -> bool:
    return abs(s.rate) <= CALM_RATE


def controls_for(event: Settlement, same_t: Sequence[Settlement]) -> list[Settlement]:
    """At most CONTROLS_PER_EVENT calm, liquid settlements at the same t, nearest in
    turnover (smallest absolute log ratio, ties by symbol)."""
    assert event.turnover is not None
    pool = [
        s for s in same_t if s.symbol != event.symbol and is_liquid(s) and is_calm(s) and s.turnover
    ]

    def distance(s: Settlement) -> tuple[float, str]:
        assert s.turnover is not None and event.turnover is not None
        return abs(math.log(s.turnover / event.turnover)), s.symbol

    return sorted(pool, key=distance)[:CONTROLS_PER_EVENT]


@dataclass(frozen=True)
class Plan:
    """What the windows snapshot must contain, derived only from the index."""

    first: int
    last: int
    events: list[Settlement]  # fetched triggers (the widest diagnostic and positives)
    controls: dict[tuple[str, int], list[Settlement]]  # primary events only
    windows: list[tuple[str, int]]


def plan(index: Index) -> Plan:
    rows = settlements(index)
    by_t: dict[int, list[Settlement]] = defaultdict(list)
    for s in rows:
        by_t[s.t].append(s)
    events = [
        s
        for s in rows
        if is_liquid(s)
        and (
            s.rate <= FETCH_TRIGGER
            or s.rate >= POSITIVE_TRIGGER
            or (s.prev_rate is not None and s.prev_rate <= TRIGGER)
        )
    ]
    controls = {(e.symbol, e.t): controls_for(e, by_t[e.t]) for e in events if e.rate <= TRIGGER}
    windows = {(e.symbol, e.t) for e in events}
    for chosen in controls.values():
        windows.update((c.symbol, c.t) for c in chosen)
    windows.update(("BTCUSDT", e.t) for e in events)
    first, last, _ = STAGES[index.stage]
    return Plan(first, last, events, controls, sorted(windows, key=lambda w: (w[1], w[0])))


async def fetch_windows(
    client: Any, index_path: Path, index_sha256: str, path: Path
) -> dict[str, Any]:
    index = load_index(verified(index_path, index_sha256))
    todo = plan(index)
    ends = {symbol: inst.end_ms for symbol, inst in index.instruments.items()}
    writer = SnapshotWriter(path)
    writer.record("index", None, {}, {"sha256": index_sha256, "stage": index.stage})
    errors: dict[str, str] = {}
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def one(symbol: str, t: int) -> None:
        start = t - WINDOW_BEFORE_MS
        # Cap at delivery: backward paging from a later end returns an empty first page.
        end = min(t + WINDOW_AFTER_MS - 1, ends.get(symbol, DATA_UNTIL_MS), DATA_UNTIL_MS)
        params = {
            "category": "linear",
            "symbol": symbol,
            "interval": "1",
            "start": start,
            "end": end,
            "limit": KLINE_PAGE_LIMIT,
        }
        async with semaphore:
            try:
                require_boundary(end)
                response = await client.public_get_v5_market_kline(dict(params))
                _ok_list(response, f"window {symbol} {t}")
                writer.record("window", symbol, params | {"t": t}, response)
            except Exception as exc:
                errors[f"{symbol}@{t}"] = str(exc)[:300]
                writer.record("fetch_error", symbol, {"t": t}, {"error": str(exc)[:300]})

    try:
        await asyncio.gather(*(one(symbol, t) for symbol, t in todo.windows))
    finally:
        writer.close()
    return {
        "stage": index.stage,
        "events": len(todo.events),
        "windows": len(todo.windows),
        "errors": len(errors),
        "responses": writer.written,
    }


# ---------------------------------------------------------------- outcomes


Bars = dict[int, tuple[float, float, float, float]]  # minute start -> open, high, low, close


def load_windows(path: Path, index_sha256: str) -> dict[tuple[str, int], Bars]:
    windows: dict[tuple[str, int], Bars] = {}
    with gzip.open(path, "rb") as stream:
        for line_bytes in stream:
            line = json.loads(line_bytes)
            if line["kind"] == "index":
                if line["response"]["sha256"] != index_sha256:
                    raise SystemExit("windows snapshot was built from another index")
                continue
            if line["kind"] != "window":
                continue
            result = line["response"]["result"]
            if result.get("symbol") not in (None, line["symbol"]):
                continue  # another symbol's rows: left missing, never used
            bars: Bars = {}
            for item in result["list"]:
                bars[int(item[0])] = (
                    float(item[1]),
                    float(item[2]),
                    float(item[3]),
                    float(item[4]),
                )
            windows[(line["symbol"], int(line["params"]["t"]))] = bars
    return windows


@dataclass(frozen=True)
class HeldPath:
    """A short (or long) held from the open of the bar at entry to the close of the
    bar starting at exit_bar."""

    gross_bps: float
    exit_delayed: bool
    adverse_bps: float  # worst move against the position inside the hold


def held(bars: Bars, t: int, *, side: int, hold_ms: int = HOLD_MS) -> HeldPath | str:
    """side = -1 for a short, +1 for a long."""
    entry_bar = bars.get(t + ENTRY_OFFSET_MS)
    if entry_bar is None:
        return "entry_missing"
    entry = entry_bar[0]
    exit_start = t + ENTRY_OFFSET_MS + hold_ms - MINUTE_MS
    last_start = max(exit_start, t + LAST_EXIT_BAR_OFFSET_MS) if hold_ms == HOLD_MS else exit_start
    start = exit_start
    while start <= last_start and start not in bars:
        start += MINUTE_MS
    if start > last_start:
        return "exit_missing"
    exit_ = bars[start][3]
    inside = [
        bars[m] for m in range(t + ENTRY_OFFSET_MS, start + MINUTE_MS, MINUTE_MS) if m in bars
    ]
    if side < 0:
        adverse = max(bar[1] for bar in inside) / entry - 1
    else:
        adverse = 1 - min(bar[2] for bar in inside) / entry
    return HeldPath(side * (exit_ / entry - 1) * 10_000, start != exit_start, adverse * 10_000)


def first_minute_short_bps(bars: Bars, t: int) -> float | None:
    bar = bars.get(t)
    return None if bar is None else -(bar[3] / bar[0] - 1) * 10_000


def pre_drift_long_bps(bars: Bars, t: int) -> float | None:
    """Price change from t - 30 min to t (the round trip predicts a rise)."""
    first, last = bars.get(t - 30 * MINUTE_MS), bars.get(t - MINUTE_MS)
    return None if first is None or last is None else (last[3] / first[0] - 1) * 10_000


# ---------------------------------------------------------------- statistics


def _day(ms: int) -> int:
    return ms // DAY_MS


def _week(ms: int) -> str:
    year, week, _ = datetime.fromtimestamp(ms / 1000, tz=UTC).isocalendar()
    return f"{year}-W{week:02d}"


def _month(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m")


@dataclass(frozen=True)
class Cluster:
    symbol: str
    day: int
    week: str
    t_first: int
    value: float  # mean over the cluster's events


def clusters(rows: Iterable[tuple[str, int, float]]) -> list[Cluster]:
    """Average (symbol, t, value) rows within each symbol-day."""
    grouped: dict[tuple[str, int], list[tuple[int, float]]] = defaultdict(list)
    for symbol, t, value in rows:
        grouped[(symbol, _day(t))].append((t, value))
    out = []
    for (symbol, day), items in sorted(grouped.items()):
        t_first = min(t for t, _ in items)
        out.append(
            Cluster(symbol, day, _week(t_first), t_first, statistics.fmean(v for _, v in items))
        )
    return out


def mean_of(cs: Sequence[Cluster]) -> float | None:
    return statistics.fmean(c.value for c in cs) if cs else None


def week_block_lower_bound(cs: Sequence[Cluster]) -> float | None:
    """Lower bound of the 90% interval of the pooled cluster mean, resampling whole UTC
    weeks (each week keeps all of its symbol-day clusters)."""
    if len({c.week for c in cs}) < 2:
        return None
    computation = cluster_bootstrap_mean(
        tuple(ClusterObservation(cluster_key=c.week, value=c.value) for c in cs),
        iterations=BOOTSTRAP_RESAMPLES,
        seed=BOOTSTRAP_SEED,
        confidence_level=0.90,
    )
    return computation.estimate.lower_bound


def without_top_symbols(cs: Sequence[Cluster]) -> list[Cluster]:
    totals: dict[str, float] = defaultdict(float)
    for c in cs:
        totals[c.symbol] += c.value
    top = set(sorted(totals, key=lambda s: (-totals[s], s))[:TOP_SYMBOLS_REMOVED])
    return [c for c in cs if c.symbol not in top]


# ---------------------------------------------------------------- report


@dataclass
class Outcomes:
    eligible: int = 0
    complete: list[tuple[Settlement, HeldPath]] = field(default_factory=list)
    incomplete: Counter[str] = field(default_factory=Counter)


def outcomes(
    events: Iterable[Settlement],
    windows: dict[tuple[str, int], Bars],
    *,
    side: int,
    hold_ms: int = HOLD_MS,
) -> Outcomes:
    out = Outcomes()
    for e in events:
        out.eligible += 1
        bars = windows.get((e.symbol, e.t))
        if bars is None:
            out.incomplete["window_missing"] += 1
            continue
        result = held(bars, e.t, side=side, hold_ms=hold_ms)
        if isinstance(result, str):
            out.incomplete[result] += 1
        else:
            out.complete.append((e, result))
    return out


def summary(rows: Sequence[tuple[str, int, float]]) -> dict[str, Any]:
    cs = clusters(rows)
    return {
        "events": len(rows),
        "clusters": len(cs),
        "symbols": len({c.symbol for c in cs}),
        "weeks": len({c.week for c in cs}),
        "mean_bps": mean_of(cs),
    }


def read_stage(index: Index, windows: dict[tuple[str, int], Bars]) -> dict[str, Any]:
    first, last, half = STAGES[index.stage]
    todo = plan(index)
    primary = [e for e in todo.events if e.rate <= TRIGGER]
    triggered = [s for s in settlements(index) if s.rate <= TRIGGER]
    main = outcomes(primary, windows, side=-1)
    net_rows = [(e.symbol, e.t, p.gross_bps - COST_BPS) for e, p in main.complete]
    gross_rows = [(e.symbol, e.t, p.gross_bps) for e, p in main.complete]
    cs = clusters(net_rows)
    complete_share = len(main.complete) / main.eligible if main.eligible else 0.0

    excess_rows: list[tuple[str, int, float]] = []
    by_hour_type: dict[str, list[tuple[str, int, float]]] = defaultdict(list)
    for e, p in main.complete:
        values = []
        for c in todo.controls.get((e.symbol, e.t), []):
            bars = windows.get((c.symbol, c.t))
            result = held(bars, c.t, side=-1) if bars is not None else "window_missing"
            if not isinstance(result, str):
                values.append(result.gross_bps)
        if values:
            row = (e.symbol, e.t, p.gross_bps - statistics.fmean(values))
            excess_rows.append(row)
            boundary = (e.t // HOUR_MS) % 4 == 0
            by_hour_type["four_hour_boundary" if boundary else "other_hours"].append(row)
    control_share = len(excess_rows) / len(main.complete) if main.complete else 0.0

    mean_net = mean_of(cs)
    lower = week_block_lower_bound(cs)
    trimmed = mean_of(without_top_symbols(cs))
    halves = {
        "first_half": mean_of([c for c in cs if c.t_first < half]),
        "second_half": mean_of([c for c in cs if c.t_first >= half]),
    }
    excess = mean_of(clusters(excess_rows))
    floor = {
        "clusters": len(cs) >= MIN_CLUSTERS,
        "symbols": len({c.symbol for c in cs}) >= MIN_SYMBOLS,
        "weeks": len({c.week for c in cs}) >= MIN_WEEKS,
        "complete_share": complete_share >= MIN_COMPLETE_SHARE,
    }
    conditions = {
        "size": mean_net is not None and mean_net >= MIN_NET_BPS,
        "uncertainty": lower is not None and lower > 0,
        "without_top_symbols": trimmed is not None and trimmed > 0,
        "both_halves": all(v is not None and v > 0 for v in halves.values()),
        "excess_over_control": excess is not None and excess >= MIN_EXCESS_BPS,
        "control_share": control_share >= MIN_CONTROL_SHARE,
    }
    if not all(floor.values()):
        verdict = "insufficient_data"
    elif mean_net is not None and mean_net < 0:
        verdict = "fail"
    elif all(conditions.values()):
        verdict = "pass"
    else:
        verdict = "not_passed"

    first_minute = [
        (e.symbol, e.t, v)
        for e in primary
        if (bars := windows.get((e.symbol, e.t))) is not None
        and (v := first_minute_short_bps(bars, e.t)) is not None
    ]
    first_minute_mean = mean_of(clusters(first_minute))
    return {
        "stage": index.stage,
        "bounds_ms": {"first": first, "last": last, "second_half_from": half},
        "triggered": {
            "settlements": len(triggered),
            "liquid_eligible": sum(is_liquid(s) for s in triggered),
            "below_turnover": sum(s.turnover is not None and not is_liquid(s) for s in triggered),
            "turnover_unknown": sum(s.turnover is None for s in triggered),
        },
        "eligible": main.eligible,
        "complete": len(main.complete),
        "exit_delayed": sum(p.exit_delayed for _, p in main.complete),
        "complete_share": complete_share,
        "incomplete": dict(main.incomplete),
        "clusters": len(cs),
        "symbols": len({c.symbol for c in cs}),
        "weeks": len({c.week for c in cs}),
        "mean_gross_bps_break_even": mean_of(clusters(gross_rows)),
        "mean_net_bps": mean_net,
        "mean_net_bps_at_sensitivity": (
            mean_net - (SENSITIVITY_COST_BPS - COST_BPS) if mean_net is not None else None
        ),
        "net_lower_bound_90": lower,
        "mean_net_without_top_symbols": trimmed,
        "halves": halves,
        "excess_over_control_bps": excess,
        "control_share": control_share,
        "floor": floor,
        "conditions": conditions,
        "verdict": verdict,
        "seconds_variant_permitted": verdict == "pass"
        or (
            first_minute_mean is not None and first_minute_mean >= SECONDS_VARIANT_FIRST_MINUTE_BPS
        ),
        "diagnostics": diagnostics(todo, primary, main, windows, first_minute, by_hour_type),
    }


def diagnostics(
    todo: Plan,
    primary: Sequence[Settlement],
    main: Outcomes,
    windows: dict[tuple[str, int], Bars],
    first_minute: Sequence[tuple[str, int, float]],
    by_hour_type: dict[str, list[tuple[str, int, float]]],
) -> dict[str, Any]:
    """Reported, decide nothing."""

    def net_summary(events: Iterable[Settlement], *, side: int, hold_ms: int = HOLD_MS) -> Any:
        result = outcomes(events, windows, side=side, hold_ms=hold_ms)
        rows = [(e.symbol, e.t, p.gross_bps - COST_BPS) for e, p in result.complete]
        return summary(rows) | {"eligible": result.eligible}

    def fits(e: Settlement, hold_ms: int) -> bool:
        return e.next_t is None or e.next_t > e.t + ENTRY_OFFSET_MS + hold_ms

    out: dict[str, Any] = {
        "first_minute_short_gross": summary(first_minute),
        "hold_5_min": net_summary(primary, side=-1, hold_ms=5 * MINUTE_MS),
        "hold_10_min": net_summary(primary, side=-1, hold_ms=10 * MINUTE_MS),
        "hold_60_min": net_summary(
            [e for e in primary if fits(e, 60 * MINUTE_MS)], side=-1, hold_ms=60 * MINUTE_MS
        ),
        "excess_by_hour_type": {k: summary(v) for k, v in sorted(by_hour_type.items())},
    }
    for trigger in DIAGNOSTIC_TRIGGERS:
        out[f"trigger_{trigger:.3%}"] = net_summary(
            [e for e in todo.events if e.rate <= trigger], side=-1
        )
    out["trigger_r_prev_-0.300%"] = net_summary(
        [e for e in todo.events if e.prev_rate is not None and e.prev_rate <= TRIGGER],
        side=-1,
    )
    out["positive_side_long"] = net_summary(
        [e for e in todo.events if e.rate >= POSITIVE_TRIGGER], side=1
    )
    by_interval: dict[str, list[tuple[str, int, float]]] = defaultdict(list)
    by_month: dict[str, list[tuple[str, int, float]]] = defaultdict(list)
    btc_adjusted: list[tuple[str, int, float]] = []
    adverse: list[float] = []
    for e, p in main.complete:
        row = (e.symbol, e.t, p.gross_bps - COST_BPS)
        hours = round((e.t - e.prev_t) / HOUR_MS) if e.prev_t is not None else None
        by_interval[f"{hours}h" if hours else "unknown"].append(row)
        by_month[_month(e.t)].append(row)
        adverse.append(p.adverse_bps)
        btc = windows.get(("BTCUSDT", e.t))
        btc_path = held(btc, e.t, side=-1) if btc is not None else "window_missing"
        if not isinstance(btc_path, str):
            btc_adjusted.append((e.symbol, e.t, p.gross_bps - btc_path.gross_bps))
    pre = [
        (e.symbol, e.t, v)
        for e in primary
        if (bars := windows.get((e.symbol, e.t))) is not None
        and (v := pre_drift_long_bps(bars, e.t)) is not None
    ]
    adverse.sort()
    out |= {
        "by_interval": {k: summary(v) for k, v in sorted(by_interval.items())},
        "by_month": {k: summary(v) for k, v in sorted(by_month.items())},
        "pre_settlement_drift_long_gross": summary(pre),
        "btc_adjusted_short_gross": summary(btc_adjusted),
        "adverse_p99_bps": adverse[int(0.99 * (len(adverse) - 1))] if adverse else None,
        "one_position": one_position(main, stage_months(todo)),
    }
    return out


def stage_months(todo: Plan) -> int:
    return len({_month(t) for t in range(todo.first, todo.last + 1, DAY_MS)})


def one_position(main: Outcomes, months: int) -> dict[str, Any]:
    """USD 50, one position at a time: the most negative r_t at each settlement time.
    A 30-minute hold never overlaps the next settlement time, which is at least an hour
    later, so every chosen time is taken."""
    best: dict[int, tuple[Settlement, HeldPath]] = {}
    for e, p in main.complete:
        current = best.get(e.t)
        if current is None or (e.rate, e.symbol) < (current[0].rate, current[0].symbol):
            best[e.t] = (e, p)
    by_month: dict[str, list[float]] = defaultdict(list)
    for t, (_, p) in best.items():
        by_month[_month(t)].append((p.gross_bps - COST_BPS) * NOTIONAL_USD / 10_000)
    return {
        "trades": len(best),
        "trades_per_month": len(best) / months,
        "net_usd": sum(sum(v) for v in by_month.values()),
        "net_usd_per_month": sum(sum(v) for v in by_month.values()) / months,
        "by_month_usd": {k: sum(v) for k, v in sorted(by_month.items())},
    }


def build_report(
    index_path: Path,
    index_sha256: str,
    windows_path: Path,
    windows_sha256: str,
    *,
    code_revision: str,
    working_tree_dirty: bool,
) -> dict[str, Any]:
    index = load_index(verified(index_path, index_sha256))
    windows = load_windows(verified(windows_path, windows_sha256), index_sha256)
    return {
        "version": VERSION,
        "protocol": "docs/research/bybit-post-funding-minute-v1.md",
        "run": {
            "code_revision": normalize_code_revision(code_revision),
            "working_tree_dirty": working_tree_dirty,
        },
        "snapshots": {
            "index": {"file": index_path.name, "sha256": index_sha256},
            "windows": {"file": windows_path.name, "sha256": windows_sha256},
        },
        "coverage": {
            "instruments": len(index.instruments),
            "fetch_failed_or_mismatched": list(index.failed),
            "windows": len(windows),
        },
        "result": read_stage(index, windows),
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def render_markdown(report: dict[str, Any]) -> str:
    r = report["result"]
    rows = [
        (
            "eligible / complete",
            f"{r['eligible']} / {r['complete']} ({100 * r['complete_share']:.1f}%)",
        ),
        ("clusters / symbols / weeks", f"{r['clusters']} / {r['symbols']} / {r['weeks']}"),
        ("mean gross (break-even cost) bps", _fmt(r["mean_gross_bps_break_even"])),
        ("mean net at 30 bps", _fmt(r["mean_net_bps"])),
        ("mean net at 50 bps", _fmt(r["mean_net_bps_at_sensitivity"])),
        ("90% lower bound, week blocks", _fmt(r["net_lower_bound_90"])),
        ("without top 5 symbols", _fmt(r["mean_net_without_top_symbols"])),
        ("halves", f"{_fmt(r['halves']['first_half'])} / {_fmt(r['halves']['second_half'])}"),
        (
            "excess over control / share",
            f"{_fmt(r['excess_over_control_bps'])} / {100 * r['control_share']:.1f}%",
        ),
        (
            "one position, USD per month",
            _fmt(r["diagnostics"]["one_position"]["net_usd_per_month"]),
        ),
        (
            "one position, trades per month",
            _fmt(r["diagnostics"]["one_position"]["trades_per_month"]),
        ),
    ]
    failed = [k for k, ok in (r["floor"] | r["conditions"]).items() if not ok]
    return "\n".join(
        [
            f"# {report['version']}: {r['stage']}",
            "",
            f"Code `{report['run']['code_revision']}`, index "
            f"`{report['snapshots']['index']['sha256']}`, windows "
            f"`{report['snapshots']['windows']['sha256']}`.",
            "",
            *markdown_table(("measure", "value"), rows),
            "",
            f"Verdict: `{r['verdict']}`; failed: {', '.join(failed) or 'none'}. "
            f"Seconds-level variant permitted: {r['seconds_variant_permitted']}.",
            "",
        ]
    )


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    index = sub.add_parser("fetch-index", help="catalog, settlements and hourly klines")
    index.add_argument("--stage", choices=sorted(STAGES), required=True)
    index.add_argument("--snapshot", type=Path, required=True)
    windows = sub.add_parser("fetch-windows", help="1-minute windows of events and controls")
    windows.add_argument("--index", type=Path, required=True)
    windows.add_argument("--index-sha256", required=True)
    windows.add_argument("--snapshot", type=Path, required=True)
    report = sub.add_parser("report", help="the fixed reading from verified snapshots")
    report.add_argument("--index", type=Path, required=True)
    report.add_argument("--index-sha256", required=True)
    report.add_argument("--windows", type=Path, required=True)
    report.add_argument("--windows-sha256", required=True)
    report.add_argument("--output-dir", type=Path, required=True)
    report.add_argument("--code-revision", required=True)
    dirty = report.add_mutually_exclusive_group(required=True)
    dirty.add_argument("--working-tree-dirty", dest="working_tree_dirty", action="store_true")
    dirty.add_argument("--no-working-tree-dirty", dest="working_tree_dirty", action="store_false")
    return parser


def _run_fetch(job: Callable[[Any], Any], snapshot: Path) -> None:
    if snapshot.exists():
        raise SystemExit(f"{snapshot} exists; a snapshot is written once")

    async def collect() -> Any:
        client = EXCHANGE_FACTORIES["bybit"]()
        try:
            return await job(client)
        finally:
            await client.close()

    coverage = asyncio.run(collect())
    sys.stdout.write(json.dumps(coverage | {"sha256": file_sha256(snapshot)}) + "\n")


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command == "fetch-index":
        _run_fetch(lambda c: fetch_index(c, args.snapshot, args.stage), args.snapshot)
        return
    if args.command == "fetch-windows":
        _run_fetch(
            lambda c: fetch_windows(c, args.index, args.index_sha256, args.snapshot),
            args.snapshot,
        )
        return
    report = build_report(
        args.index,
        args.index_sha256,
        args.windows,
        args.windows_sha256,
        code_revision=args.code_revision,
        working_tree_dirty=args.working_tree_dirty,
    )
    digest = publish_once(args.output_dir / "result.json", report)
    (args.output_dir / "tables.md").write_text(render_markdown(report), encoding="utf-8")
    sys.stdout.write(render_markdown(report) + f"\nresult sha256 {digest}\n")
