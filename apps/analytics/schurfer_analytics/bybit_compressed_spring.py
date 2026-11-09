"""HYP-032: compressed spring on Bybit USDT perpetuals, power step 1.

Implements step 1 of docs/research/bybit-compressed-spring-v1.md: features and counts,
no trade tape and no return.

- ``fetch-features``: catalog (live and delisted), every settlement, 1-hour klines and
  1-hour open interest in the discovery window, stored once in a gzip JSON-lines snapshot.
- ``counts``: reads only the verified snapshot and publishes the funnel of
  instrument-hours, the decisions passing conditions 1 to 3, the HYP-031 event-time
  count and the tape scope. Counts only: no feature value, price or return.

The HYP-031 tape mask is fixed (minutes :55 to :05 of every hour, ``minute_masked``);
the tape fetch of power step 2 applies it.
- ``fetch-archive-sizes``: for the published scope, the trade archive listings (names
  inside the window only) and HEAD sizes of a fixed sample of scope days.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import gzip
import hashlib
import json
import math
import re
import sys
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import httpx

from .bybit_carry_feasibility import publish_once
from .bybit_post_funding_screen import (
    SnapshotWriter,
    _pages,
    fetch_linear_catalog,
    file_sha256,
    recovered,
    verified,
)
from .exchange_registry import EXCHANGE_FACTORIES
from .reporting import markdown_table, normalize_code_revision

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

VERSION: Final = "bybit_compressed_spring_v1"
MINUTE_MS: Final = 60_000
HOUR_MS: Final = 60 * MINUTE_MS
DAY_MS: Final = 24 * HOUR_MS


def _ms(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=UTC).timestamp() * 1000)


WINDOW_FIRST_MS: Final = _ms(2024, 10, 1)
WINDOW_END_MS: Final = _ms(2025, 10, 1)  # exclusive
FIRST_DECISION_MS: Final = _ms(2024, 10, 16)
SECOND_HALF_MS: Final = _ms(2025, 4, 1)
HORIZONS_H: Final = (4, 12, 24)

MIN_TURNOVER_USD: Final = 2_000_000.0
TURNOVER_BARS: Final = 24
RANK_HOURS: Final = 336
RANK_MIN_DEFINED: Final = math.ceil(0.8 * RANK_HOURS)
C_MAX_PCT: Final = 0.20
G_MIN_PCT: Final = 0.80
HYP031_EVENT_RATE: Final = -0.003
MASK_BEFORE_MS: Final = 5 * MINUTE_MS
MASK_AFTER_MS: Final = 5 * MINUTE_MS
S_HOURS: Final = 6
S_KEPT_MINUTES: Final = S_HOURS * (60 - (MASK_BEFORE_MS + MASK_AFTER_MS) // MINUTE_MS)
TAPE_LOOKBACK_MS: Final = 14 * DAY_MS + S_HOURS * HOUR_MS
TOP_SYMBOLS: Final = 5

FUNDING_PAGE_LIMIT: Final = 200
KLINE_PAGE_LIMIT: Final = 1000
OI_PAGE_LIMIT: Final = 200
CONCURRENCY: Final = 4

ARCHIVE_LISTING: Final = "https://public.bybit.com/trading/{symbol}/"
ARCHIVE_FILE: Final = "https://public.bybit.com/trading/{symbol}/{symbol}{day}.csv.gz"
SIZE_SAMPLE_EVERY: Final = 20
SIZE_SAMPLE_MIN: Final = 2
MAX_PRIMARY_SCOPE_BYTES: Final = 10**12
HTTP_RETRY_DELAYS_SECONDS: Final = (1.0, 2.0, 4.0, 8.0)
# The archive sits behind CloudFront, which answered 403 to every request from this IP
# after about 2,700 requests in 4 minutes (2026-11-06). Requests are paced across all
# tasks, and a 403 or 429 is treated as a block: long pauses, then a clean stop.
ARCHIVE_REQUESTS_PER_SECOND: Final = 3.0
BLOCK_DELAYS_SECONDS: Final = (30.0, 60.0, 120.0, 300.0)
BLOCK_STATUSES: Final = (403, 429)
_sleep = asyncio.sleep  # replaced in tests
_monotonic = time.monotonic  # replaced in tests

REASONS: Final = ("kline_gap", "oi_gap", "no_settlement", "rank_history_short")


def last_decision_ms(horizon_h: int) -> int:
    """The last hourly t whose exit bar [t + H, t + H + 1 min) ends inside the window."""
    return WINDOW_END_MS - horizon_h * HOUR_MS - HOUR_MS


GRID_LAST_MS: Final = last_decision_ms(min(HORIZONS_H))


# ---------------------------------------------------------------- fetching


def require_window(until_ms: int) -> None:
    if until_ms >= WINDOW_END_MS:
        raise SystemExit(f"refusing a request ending at or after {WINDOW_END_MS} ms")


def universe(catalog: Iterable[dict[str, Any]]) -> dict[str, tuple[int, int]]:
    """USDT linear perpetuals trading in the window: symbol -> (launch ms, last usable ms)."""
    out: dict[str, tuple[int, int]] = {}
    for item in catalog:
        if item.get("contractType") != "LinearPerpetual" or item.get("settleCoin") != "USDT":
            continue
        launch = int(item.get("launchTime") or 0)
        delivery = int(item.get("deliveryTime") or 0)
        end = min(WINDOW_END_MS - 1, delivery - 1) if delivery else WINDOW_END_MS - 1
        if launch < WINDOW_END_MS and end >= WINDOW_FIRST_MS:
            old = out.get(item["symbol"])
            out[item["symbol"]] = (launch, max(end, old[1])) if old else (launch, end)
    return out


async def fetch_features(client: Any, path: Path) -> dict[str, Any]:
    recovered.clear()
    live = await fetch_linear_catalog(client, None)
    closed = await fetch_linear_catalog(client, "Closed")
    symbols = universe(live + closed)
    writer = SnapshotWriter(path)
    writer.record("version", None, {}, {"version": VERSION})
    writer.record("catalog", None, {"status": "live"}, {"list": live})
    writer.record("catalog", None, {"status": "Closed"}, {"list": closed})
    errors: dict[str, str] = {}
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def one(symbol: str, end: int) -> None:
        require_window(end)
        start = WINDOW_FIRST_MS
        async with semaphore:
            try:
                await _pages(
                    client.public_get_v5_market_funding_history,
                    {"category": "linear", "symbol": symbol, "startTime": start,
                     "endTime": end, "limit": FUNDING_PAGE_LIMIT},
                    what=f"funding {symbol}",
                    limit=FUNDING_PAGE_LIMIT,
                    oldest=lambda item: int(item["fundingRateTimestamp"]),
                    record=lambda p, r: writer.record("funding", symbol, p, r),
                )  # fmt: skip
                await _pages(
                    client.public_get_v5_market_kline,
                    {"category": "linear", "symbol": symbol, "interval": "60",
                     "start": start, "end": end, "limit": KLINE_PAGE_LIMIT},
                    what=f"kline 60 {symbol}",
                    limit=KLINE_PAGE_LIMIT,
                    oldest=lambda item: int(item[0]),
                    record=lambda p, r: writer.record("kline_60", symbol, p, r),
                )  # fmt: skip
                await _pages(
                    client.public_get_v5_market_open_interest,
                    {"category": "linear", "symbol": symbol, "intervalTime": "1h",
                     "startTime": start, "endTime": end, "limit": OI_PAGE_LIMIT},
                    what=f"oi 1h {symbol}",
                    limit=OI_PAGE_LIMIT,
                    oldest=lambda item: int(item["timestamp"]),
                    record=lambda p, r: writer.record("oi_60", symbol, p, r),
                )  # fmt: skip
            except Exception as exc:  # one symbol's failure is counted, not a stop
                errors[symbol] = str(exc)[:300]
                writer.record("fetch_error", symbol, {}, {"error": errors[symbol]})

    try:
        await asyncio.gather(*(one(s, end) for s, (_, end) in sorted(symbols.items())))
    finally:
        writer.close()
    return {
        "version": VERSION,
        "symbols": len(symbols),
        "errors": errors,
        "responses": writer.written,
        "recovered_requests": dict(recovered),
    }


# ---------------------------------------------------------------- snapshot


@dataclass
class Series:
    launch_ms: int = 0
    end_ms: int = 0
    bars: dict[int, tuple[float, float, float, float]] = field(default_factory=dict)
    oi: dict[int, float] = field(default_factory=dict)
    funding: dict[int, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Features:
    instruments: dict[str, Series]
    failed: tuple[str, ...]


def _in_window(ms: int) -> bool:
    return WINDOW_FIRST_MS <= ms < WINDOW_END_MS


def load_features(path: Path) -> Features:
    version = ""
    catalog: list[dict[str, Any]] = []
    raw: dict[str, Series] = defaultdict(Series)
    failed: set[str] = set()
    with gzip.open(path, "rb") as stream:
        for line_bytes in stream:
            line = json.loads(line_bytes)
            kind, symbol = line["kind"], line["symbol"]
            if kind == "version":
                version = line["response"]["version"]
            elif kind == "catalog":
                catalog.extend(line["response"]["list"])
            elif kind == "fetch_error":
                failed.add(symbol)
            elif kind == "funding":
                for item in line["response"]["result"]["list"]:
                    if item.get("symbol") != symbol:
                        failed.add(symbol)
                    elif _in_window(ts := int(item["fundingRateTimestamp"])):
                        raw[symbol].funding[ts] = float(item["fundingRate"])
            elif kind in ("kline_60", "oi_60"):
                result = line["response"]["result"]
                if result.get("symbol") not in (None, symbol):
                    failed.add(symbol)
                    continue
                for item in result["list"]:
                    if kind == "kline_60" and _in_window(start := int(item[0])):
                        high, low, close, turnover = (float(item[k]) for k in (2, 3, 4, 6))
                        raw[symbol].bars[start] = (high, low, close, turnover)
                    elif kind == "oi_60" and _in_window(stamp := int(item["timestamp"])):
                        raw[symbol].oi[stamp] = float(item["openInterest"])
    if version != VERSION:
        raise SystemExit(f"features snapshot is not {VERSION}: {version!r}")
    ends = universe(catalog)
    instruments: dict[str, Series] = {}
    for symbol, series in raw.items():
        if symbol in ends and symbol not in failed:
            series.launch_ms, series.end_ms = ends[symbol]
            instruments[symbol] = series
    return Features(instruments, tuple(sorted(failed)))


# ---------------------------------------------------------------- features at t


def turnover_and_range(series: Series, t: int) -> tuple[float, float] | None:
    """24 h turnover and C over the 24 hourly bars ending at t; None if any bar is missing."""
    bars = [series.bars.get(t - (k + 1) * HOUR_MS) for k in range(TURNOVER_BARS)]
    if any(bar is None for bar in bars):
        return None
    present = [bar for bar in bars if bar is not None]
    close = present[0][2]  # the bar ending at t
    if close <= 0:
        return None
    high = max(bar[0] for bar in present)
    low = min(bar[1] for bar in present)
    return sum(bar[3] for bar in present), (high - low) / close


def oi_growth(series: Series, t: int) -> float | None:
    """G = OI(t) / OI(t - 24 h) - 1, OI(t) being the record stamped t - 1 h."""
    now = series.oi.get(t - HOUR_MS)
    before = series.oi.get(t - 25 * HOUR_MS)
    if now is None or before is None or before <= 0:
        return None
    return now / before - 1


def last_rate_before(times: Sequence[int], rates: Sequence[float], t: int) -> float | None:
    k = bisect.bisect_left(times, t)
    return rates[k - 1] if k else None


class RollingRank:
    """Mid-rank percentile of a value against the previous RANK_HOURS hourly values."""

    def __init__(self) -> None:
        self.window: deque[float | None] = deque()
        self.ordered: list[float] = []

    def rank(self, value: float) -> float | None:
        if len(self.ordered) < RANK_MIN_DEFINED:
            return None
        below = bisect.bisect_left(self.ordered, value)
        equal = bisect.bisect_right(self.ordered, value) - below
        return (below + equal / 2) / len(self.ordered)

    def push(self, value: float | None) -> None:
        self.window.append(value)
        if value is not None:
            bisect.insort(self.ordered, value)
        if len(self.window) > RANK_HOURS:
            old = self.window.popleft()
            if old is not None:
                del self.ordered[bisect.bisect_left(self.ordered, old)]


@dataclass(frozen=True)
class Hour:
    """One liquid-or-not grid hour of one instrument; statuses are true, false or a reason."""

    symbol: str
    t: int
    liquidity: str  # liquid, illiquid, kline_gap
    c1: str
    c2: str
    c3: str

    @property
    def passes(self) -> bool:
        return self.c1 == self.c2 == self.c3 == "true"

    @property
    def two_of_three(self) -> bool:
        return (self.c1, self.c2, self.c3).count("true") >= 2

    @property
    def setup13(self) -> str:
        statuses = (self.c1, self.c2, self.c3)
        if "false" in statuses:
            return "false"
        return "true" if self.passes else "undefined"


def _status(
    value: float | None, ranker: RollingRank, gap: str, holds: Callable[[float, float], bool]
) -> str:
    if value is None:
        return gap
    rank = ranker.rank(value)
    if rank is None:
        return "rank_history_short"
    return "true" if holds(value, rank) else "false"


def grid_hours(symbol: str, series: Series) -> list[Hour]:
    """Every grid hour of the instrument from its own data strictly before t."""
    times = sorted(series.funding)
    rates = [series.funding[s] for s in times]
    c_rank, g_rank = RollingRank(), RollingRank()
    out: list[Hour] = []
    first = FIRST_DECISION_MS - RANK_HOURS * HOUR_MS
    last = min(GRID_LAST_MS, series.end_ms - series.end_ms % HOUR_MS)
    for t in range(first, last + 1, HOUR_MS):
        tr = turnover_and_range(series, t)
        c = tr[1] if tr else None
        g = oi_growth(series, t)
        if t >= FIRST_DECISION_MS and t >= series.launch_ms:
            if tr is None:
                liquidity = "kline_gap"
            else:
                liquidity = "liquid" if tr[0] >= MIN_TURNOVER_USD else "illiquid"
            rate = last_rate_before(times, rates, t)
            out.append(
                Hour(
                    symbol,
                    t,
                    liquidity,
                    _status(c, c_rank, "kline_gap", lambda _v, r: r <= C_MAX_PCT),
                    _status(g, g_rank, "oi_gap", lambda v, r: v > 0 and r >= G_MIN_PCT),
                    "no_settlement" if rate is None else ("true" if rate <= 0 else "false"),
                )
            )
        c_rank.push(c)
        g_rank.push(g)
    return out


# ---------------------------------------------------------------- the HYP-031 tape mask


def minute_masked(ms: int) -> bool:
    """The fixed mask: minutes [h - 5 min, h + 5 min) around every whole hour h."""
    offset = ms % HOUR_MS
    return offset >= HOUR_MS - MASK_BEFORE_MS or offset < MASK_AFTER_MS


def kept_minutes(start: int, end: int) -> int:
    """Minutes of [start, end) that the mask keeps."""
    return sum(1 for m in range(start, end, MINUTE_MS) if not minute_masked(m))


def hyp031_event_times(features: Features) -> list[int]:
    """Settlement times with any r_s <= -0.30%; stops if any settlement is off the hour,
    since the fixed mask covers HYP-031's windows only for whole-hour settlements."""
    out: set[int] = set()
    for symbol, series in features.instruments.items():
        for s, rate in series.funding.items():
            if s % HOUR_MS:
                raise SystemExit(f"{symbol} settlement {s} is not on a whole hour")
            if rate <= HYP031_EVENT_RATE:
                out.add(s)
    return sorted(out)


# ---------------------------------------------------------------- counts


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def _week(ms: int) -> str:
    iso = datetime.fromtimestamp(ms / 1000, UTC).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def sample_shape(hours: Sequence[Hour]) -> dict[str, Any]:
    clusters = Counter((h.symbol, _day(h.t)) for h in hours)
    per_symbol = Counter(symbol for symbol, _ in clusters)
    top = sum(n for _, n in per_symbol.most_common(TOP_SYMBOLS))
    return {
        "decisions": len(hours),
        "clusters": len(clusters),
        "symbols": len(per_symbol),
        "iso_weeks": len({_week(h.t) for h in hours}),
        "top5_cluster_share": round(top / len(clusters), 4) if clusters else None,
        "first_half_clusters": sum(1 for _, d in clusters if d < _day(SECOND_HALF_MS)),
    }


def non_overlapping(hours: Sequence[Hour], horizon_h: int) -> int:
    """Greedy one-hold-at-a-time count per instrument: an upper bound on events at H."""
    count = 0
    free_at: dict[str, int] = {}
    for h in sorted(hours, key=lambda x: (x.symbol, x.t)):
        if h.t >= free_at.get(h.symbol, 0):
            count += 1
            free_at[h.symbol] = h.t + MINUTE_MS + horizon_h * HOUR_MS  # the exit time
    return count


def scope_days(hours: Iterable[Hour]) -> dict[str, list[str]]:
    """Per symbol, the UTC days of [t - 14 d - 6 h, t) over the given decisions."""
    days: dict[str, set[str]] = defaultdict(set)
    for h in hours:
        start = h.t - TAPE_LOOKBACK_MS
        day = start - start % DAY_MS
        while day < h.t:
            days[h.symbol].add(_day(day))
            day += DAY_MS
    return {symbol: sorted(d) for symbol, d in sorted(days.items())}


def build_counts(features: Features) -> tuple[dict[str, Any], dict[str, Any]]:
    """The power step 1 report and the tape scope, both counts only."""
    times = hyp031_event_times(features)
    funnel: Counter[str] = Counter()
    by_hour: dict[int, Counter[str]] = defaultdict(Counter)
    by_week: dict[str, Counter[str]] = defaultdict(Counter)
    by_symbol: dict[str, Counter[str]] = defaultdict(Counter)
    passing: list[Hour] = []
    ablation: list[Hour] = []
    for symbol in sorted(features.instruments):
        series = features.instruments[symbol]
        for h in grid_hours(symbol, series):
            funnel["grid"] += 1
            funnel[f"liquidity_{h.liquidity}"] += 1
            if h.liquidity != "liquid":
                continue
            for name, status in (("c1", h.c1), ("c2", h.c2), ("c3", h.c3)):
                funnel[f"{name}_{status}"] += 1
            funnel[f"setup13_{h.setup13}"] += 1
            hour_of_day = (h.t // HOUR_MS) % 24
            by_hour[hour_of_day]["liquid"] += 1
            by_week[_week(h.t)]["liquid"] += 1
            by_symbol[symbol]["liquid"] += 1
            if h.two_of_three:
                ablation.append(h)
            if not h.passes:
                continue
            passing.append(h)
            by_hour[hour_of_day]["setup13"] += 1
            by_week[_week(h.t)]["setup13"] += 1
            by_symbol[symbol]["setup13"] += 1
    per_horizon = {}
    for horizon in HORIZONS_H:
        kept = [h for h in passing if h.t <= last_decision_ms(horizon)]
        per_horizon[f"{horizon}h"] = sample_shape(kept) | {
            "last_decision": datetime.fromtimestamp(last_decision_ms(horizon) / 1000, UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "events_upper_bound": non_overlapping(kept, horizon),
        }
    primary = scope_days(passing)
    wide = scope_days(ablation)
    report = {
        "version": VERSION,
        "step": "power step 1: features and counts, no tape, no return",
        "instruments": len(features.instruments),
        "failed_instruments": list(features.failed),
        "funnel": dict(sorted(funnel.items())),
        "decisions_passing_1_to_3": per_horizon,
        "mask": {
            "hyp031_event_times": len(times),
            "s_kept_minutes": S_KEPT_MINUTES,
            "s_minutes": S_HOURS * 60,
        },
        "tape_scope": {
            "primary_symbols": len(primary),
            "primary_symbol_days": sum(len(d) for d in primary.values()),
            "ablation_symbols": len(wide),
            "ablation_symbol_days": sum(len(d) for d in wide.values()),
        },
        "by_hour_of_day": {f"{k:02d}": dict(v) for k, v in sorted(by_hour.items())},
        "by_iso_week": {k: dict(v) for k, v in sorted(by_week.items())},
        "top_symbols_by_setup13": [
            {"symbol": s, **dict(c)}
            for s, c in sorted(by_symbol.items(), key=lambda x: (-x[1]["setup13"], x[0]))[:20]
        ],
    }
    return report, {"version": VERSION, "primary": primary, "ablation": wide}


def render_markdown(report: dict[str, Any]) -> str:
    funnel = [(k, f"{v:,}") for k, v in report["funnel"].items()]
    horizons = report["decisions_passing_1_to_3"]
    keys = ("decisions", "clusters", "symbols", "iso_weeks", "top5_cluster_share",
            "first_half_clusters", "events_upper_bound", "last_decision")  # fmt: skip
    shape = [(k, *(str(horizons[h][k]) for h in horizons)) for k in keys]
    rest = [("mask " + k, f"{v:,}") for k, v in report["mask"].items()]
    rest += [("scope " + k, f"{v:,}") for k, v in report["tape_scope"].items()]
    lines = [f"# {VERSION}: power step 1 counts", ""]
    lines += [*markdown_table(("Funnel item", "Instrument-hours"), funnel), ""]
    lines += [*markdown_table(("Conditions 1 to 3", *horizons), shape), ""]
    lines += markdown_table(("Mask and tape scope", "Count"), rest)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- archive sizes

_LISTED = re.compile(r'href="([A-Z0-9]+)(\d{4}-\d{2}-\d{2})\.csv\.gz"')


def listed_days(symbol: str, html: str) -> set[str]:
    """Days with a file for this symbol, inside the window only; later names are dropped."""
    first, end = _day(WINDOW_FIRST_MS), _day(WINDOW_END_MS)
    return {day for name, day in _LISTED.findall(html) if name == symbol and first <= day < end}


def size_sample(symbol: str, days: Sequence[str]) -> list[str]:
    """The fixed HEAD sample: lowest SHA-256 of 'version|SYMBOL|day', 1 in 20, at least 2."""
    want = min(len(days), max(SIZE_SAMPLE_MIN, math.ceil(len(days) / SIZE_SAMPLE_EVERY)))

    def key(day: str) -> str:
        return hashlib.sha256(f"{VERSION}|{symbol}|{day}".encode()).hexdigest()

    return sorted(sorted(days, key=key)[:want])


class Pacer:
    """At most ``rate`` request starts per second across every task sharing it."""

    def __init__(self, rate: float) -> None:
        self.interval = 1.0 / rate
        self.lock = asyncio.Lock()
        self.next_at = 0.0

    async def wait(self) -> None:
        async with self.lock:
            now = _monotonic()
            if self.next_at > now:
                await _sleep(self.next_at - now)
                now = self.next_at
            self.next_at = now + self.interval


class ArchiveBlockedError(RuntimeError):
    """The archive kept refusing every request; the run stops instead of guessing."""


async def _http(client: httpx.AsyncClient, pacer: Pacer, method: str, url: str) -> httpx.Response:
    errors = iter((*HTTP_RETRY_DELAYS_SECONDS, None))
    blocks = iter((*BLOCK_DELAYS_SECONDS, None))
    while True:
        await pacer.wait()
        try:
            response = await client.request(method, url)
        except httpx.TransportError:
            delay = next(errors)
            if delay is None:
                raise
            await _sleep(delay)
            continue
        if response.status_code in (200, 404):
            return response
        if response.status_code in BLOCK_STATUSES:
            delay = next(blocks)
            if delay is None:
                raise ArchiveBlockedError(f"{response.status_code} for {url} after pauses")
        else:
            delay = next(errors)
            if delay is None:
                response.raise_for_status()
                raise RuntimeError(f"{response.status_code} for {url}")
        await _sleep(delay)


def estimate(scope: dict[str, list[str]], listed: dict[str, set[str]],
             sizes: dict[tuple[str, str], int]) -> dict[str, Any]:  # fmt: skip
    """Each symbol's mean sampled size, over its whole fixed sample (the sample is drawn
    from the union of both scopes), times its scope days with a file."""
    by_symbol: dict[str, list[int]] = defaultdict(list)
    for (symbol, _day), size in sizes.items():
        by_symbol[symbol].append(size)
    total = 0.0
    with_file = 0
    unsized: list[str] = []
    for symbol, days in scope.items():
        present = [d for d in days if d in listed.get(symbol, set())]
        with_file += len(present)
        sampled = by_symbol.get(symbol, [])
        if present and not sampled:
            unsized.append(symbol)
        elif sampled:
            total += sum(sampled) / len(sampled) * len(present)
    return {
        "symbol_days": sum(len(d) for d in scope.values()),
        "symbol_days_with_file": with_file,
        "estimated_bytes": round(total),
        "symbols_without_size": unsized,
    }


def cap_status(scope_estimate: dict[str, Any]) -> str:
    """within or over the cap; unknown when a symbol with files has no sampled size."""
    if scope_estimate["symbols_without_size"]:
        return "unknown"
    return "within" if scope_estimate["estimated_bytes"] <= MAX_PRIMARY_SCOPE_BYTES else "over"


async def fetch_archive_sizes(
    scope: dict[str, Any], path: Path, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    union: dict[str, set[str]] = defaultdict(set)
    for part in ("primary", "ablation"):
        for symbol, days in scope[part].items():
            union[symbol].update(days)
    writer = SnapshotWriter(path)
    writer.record("version", None, {}, {"version": VERSION})
    listed: dict[str, set[str]] = {}
    sizes: dict[tuple[str, str], int] = {}
    semaphore = asyncio.Semaphore(CONCURRENCY)
    pacer = Pacer(ARCHIVE_REQUESTS_PER_SECOND)
    async with httpx.AsyncClient(timeout=60, follow_redirects=True, transport=transport) as client:

        async def one(symbol: str) -> None:
            async with semaphore:
                listing = await _http(client, pacer, "GET", ARCHIVE_LISTING.format(symbol=symbol))
                days = listed_days(symbol, listing.text) if listing.status_code == 200 else set()
                listed[symbol] = days
                writer.record("listing", symbol, {"status": listing.status_code}, sorted(days))
                present = sorted(union[symbol] & days)
                for day in size_sample(symbol, present):
                    url = ARCHIVE_FILE.format(symbol=symbol, day=day)
                    head = await _http(client, pacer, "HEAD", url)
                    length = head.headers.get("content-length")
                    if head.status_code == 200 and length is not None:
                        sizes[(symbol, day)] = int(length)
                    writer.record("head", symbol, {"day": day, "status": head.status_code},
                                  {"content_length": length})  # fmt: skip

        try:
            await asyncio.gather(*(one(symbol) for symbol in sorted(union)))
        finally:
            writer.close()
    primary = estimate(scope["primary"], listed, sizes)
    return {
        "version": VERSION,
        "heads": len(sizes),
        "primary": primary,
        "ablation": estimate(scope["ablation"], listed, sizes),
        "primary_cap": cap_status(primary),
    }


# ---------------------------------------------------------------- command line


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("fetch-features", help="catalog, funding, 1 h klines and 1 h OI")
    fetch.add_argument("--snapshot", type=Path, required=True)
    counts = sub.add_parser("counts", help="funnel, conditions 1 to 3, mask and tape scope")
    counts.add_argument("--features", type=Path, required=True)
    counts.add_argument("--features-sha256", required=True)
    counts.add_argument("--output-dir", type=Path, required=True)
    counts.add_argument("--code-revision", required=True)
    dirty = counts.add_mutually_exclusive_group(required=True)
    dirty.add_argument("--working-tree-dirty", dest="working_tree_dirty", action="store_true")
    dirty.add_argument("--no-working-tree-dirty", dest="working_tree_dirty", action="store_false")
    sizes = sub.add_parser("fetch-archive-sizes", help="listings and sampled HEAD sizes")
    sizes.add_argument("--scope", type=Path, required=True)
    sizes.add_argument("--scope-sha256", required=True)
    sizes.add_argument("--snapshot", type=Path, required=True)
    return parser


def _write_once(path: Path) -> None:
    if path.exists():
        raise SystemExit(f"{path} exists; a snapshot is written once")


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command == "fetch-features":
        _write_once(args.snapshot)

        async def collect() -> dict[str, Any]:
            client = EXCHANGE_FACTORIES["bybit"]()
            try:
                return await fetch_features(client, args.snapshot)
            finally:
                await client.close()

        coverage = asyncio.run(collect())
        sys.stdout.write(json.dumps(coverage | {"sha256": file_sha256(args.snapshot)}) + "\n")
        return
    if args.command == "counts":
        features = load_features(verified(args.features, args.features_sha256))
        report, scope = build_counts(features)
        report |= {
            "features_sha256": args.features_sha256,
            "code_revision": normalize_code_revision(args.code_revision),
            "working_tree_dirty": args.working_tree_dirty,
        }
        scope_digest = publish_once(args.output_dir / "scope.json", scope)
        report["scope_sha256"] = scope_digest
        digest = publish_once(args.output_dir / "counts.json", report)
        (args.output_dir / "counts.md").write_text(render_markdown(report), encoding="utf-8")
        sys.stdout.write(render_markdown(report) + f"\ncounts sha256 {digest}\n")
        return
    _write_once(args.snapshot)
    scope = json.loads(verified(args.scope, args.scope_sha256).read_bytes())
    if scope.get("version") != VERSION:
        raise SystemExit(f"scope is not {VERSION}")
    summary = asyncio.run(fetch_archive_sizes(scope, args.snapshot))
    sys.stdout.write(json.dumps(summary | {"sha256": file_sha256(args.snapshot)}) + "\n")
