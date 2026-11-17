"""I25 stage 1 (new-perp short, coverage level): contract, reducers, planner, fetcher.

Contract: docs/research/new-perp-stage1-request-list-v1.md and the frozen JSON next to it
(docs/research/evidence/new-perp-stage1-v1/contract.json). Protocol:
docs/research/new-perp-short-lab-protocol-v1.md, sections 2, 4, 10 and 11.

Coverage level means that no price is read. Every response physically holds prices or
rates, so each reducer keeps only a fixed list of fields (timestamps, existence, sizes,
hashes, a few catalog fields) and discards the rest unparsed. Everything dated from
2025-10-01 (the HYP-031 test year and the HYP-012 v2 blind window) is dropped inside the
reducer, before any other field of that row is read, and is never counted: no receipt
holds a total row count, only counts of rows that were kept.

This module never runs from tests against a network: the fetcher takes an injected
``httpx.Client``, and the tests give it a mock transport.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import zlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import httpx

from . import bybit_presettlement_bounded_fetch as transport
from . import bybit_presettlement_economics as b3
from .bybit_carry_feasibility import publish_once
from .catalyst_feasibility import asset

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

VERSION: Final = "new_perp_stage1_v1"
STUDY_ID: Final = "new-perp-short-v1"
PROTOCOL: Final = "docs/research/new-perp-short-lab-protocol-v1.md"
REQUEST_LIST_DOC: Final = "docs/research/new-perp-stage1-request-list-v1.md"
CONTRACT_PATH: Final = "docs/research/evidence/new-perp-stage1-v1/contract.json"
REGISTRY: Final = "docs/research/seen-windows-registry.md"
MODULE_PATH: Final = "apps/analytics/schurfer_analytics/new_perp_stage1.py"
PUBLISHED: Final = (
    PROTOCOL,
    REQUEST_LIST_DOC,
    CONTRACT_PATH,
    REGISTRY,
    MODULE_PATH,
    "apps/analytics/schurfer_analytics/new_perp_stage1_verdict.py",
    "apps/analytics/schurfer_analytics/catalyst_feasibility.py",
    "apps/analytics/schurfer_analytics/bybit_carry_feasibility.py",
    "apps/analytics/schurfer_analytics/bybit_presettlement_economics.py",
    "apps/analytics/schurfer_analytics/bybit_presettlement_bounded_fetch.py",
)

SECOND_MS: Final = 1_000
MINUTE_MS: Final = 60_000
HOUR_MS: Final = 3_600_000
DAY_MS: Final = 86_400_000


def _ms(text: str) -> int:
    return int(datetime.fromisoformat(text).astimezone(UTC).timestamp() * 1000)


# Discovery year only. CUT_MS is the first millisecond of the HYP-031 test year.
WINDOW_START_MS: Final = _ms("2024-10-01T00:00:00+00:00")
CUT_MS: Final = _ms("2025-10-01T00:00:00+00:00")
WINDOW_START_DAY: Final = "2024-10-01"
CUT_DAY: Final = "2025-10-01"

# Protocol section 5.1/5.2: V1 enters 7 days after the first public trade and holds 7 days.
AGE_DAYS: Final = 7
HOLD_DAYS: Final = 7
LOOKBACK_DAYS: Final = 30
PATH_FIRST_DAY: Final = 3  # M4, M5: days 3 to 21 after T0 (the union of every variant)
PATH_LAST_DAY: Final = 21

# The frozen limits.

MAX_LAUNCHES: Final = 300  # planning 263; more in-window rows than this stops the plan
MAX_ARCHIVE_ONLY: Final = 150  # archive directories absent from the catalog (M2 second share)
ATTEMPTS_PER_REQUEST: Final = 3
BACKOFF_S: Final = (10, 30, 60)
MAX_RETRY_AFTER_S: Final = 300
SPACING_S: Final = 1.0
MAX_SPACING_S: Final = 8.0  # after an HTTP 429 the spacing doubles up to this
# The last block. The transport (httpcore) reads at most 64 KiB per call, so no chunk of a
# response is larger than this. It is a RESERVE inside every byte budget: a request starts only
# if its response cap plus this reserve still fits in what remains of the step and class budget,
# so the bytes received can never exceed a stated ceiling.
READ_RESERVE_BYTES: Final = 64 * 1024
FUNDING_PAGE_LIMIT: Final = 200
FUNDING_MAX_PAGES: Final = 4
PER_FILE_CAP_BYTES: Final = 64 * 1024 * 1024
MIB: Final = 1024 * 1024
# The two gos. Go 1 reads metadata only; go 2 needs go 1 and its reviewed manifest.
GO_ATTEMPT_CAPS: Final = {1: 1000, 2: 1700}
GO_BYTE_CAPS: Final = {1: 300 * MIB, 2: 6656 * MIB}

BASE_API: Final = "https://api.bybit.com"
CATALOG_URL: Final = f"{BASE_API}/v5/market/instruments-info"
KLINE_URL: Final = f"{BASE_API}/v5/market/kline"
MARK_URL: Final = f"{BASE_API}/v5/market/mark-price-kline"
FUNDING_URL: Final = f"{BASE_API}/v5/market/funding/history"
ARCHIVE_ROOT: Final = "https://public.bybit.com/trading/"
CATALOG_STATUSES: Final = ("Trading", "Delivering", "Closed")
SYMBOL = re.compile(r"[A-Z0-9]{1,30}USDT")
LISTED = re.compile(r'href="([A-Z0-9]+)(\d{4}-\d{2}-\d{2})\.csv\.gz"')
ROOT_DIR = re.compile(r'href="([A-Z0-9]+USDT)/"')
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class ClassSpec:
    """One class of requests. All counts are requests, not attempts."""

    cls: str
    name: str
    phase: int
    method: str
    url: str
    params: str
    parsed: str
    discarded: str
    expected: int
    max_requests: int
    max_response_bytes: int
    max_class_bytes: int
    go: int = 1
    note: str = ""


CLASSES: Final[tuple[ClassSpec, ...]] = (
    ClassSpec(
        "C1",
        "catalog_linear",
        0,
        "GET",
        CATALOG_URL,
        "category=linear, status in (Trading, Delivering, Closed), limit=1000, cursor",
        "symbol, baseCoin, contractType, settleCoin, status, launchTime, deliveryTime, "
        "fundingInterval, isPreListing (presence), lotSizeFilter.minNotionalValue, "
        "lotSizeFilter.qtyStep, lotSizeFilter.minOrderQty, leverageFilter.maxLeverage",
        "priceFilter (every field), upperFundingRate, lowerFundingRate, "
        "preListingInfo contents, every other field",
        3,
        9,
        3 * MIB,
        27 * MIB,
        note="rows launched from 2025-10-01 are dropped before any other field is read",
    ),
    ClassSpec(
        "C3",
        "archive_root",
        0,
        "GET",
        ARCHIVE_ROOT,
        "none",
        "directory names ending USDT that are absent from the whole catalog and whose "
        "modification date is inside the window; for them the date, compared only with the "
        "window",
        "the page itself, every directory with no date or a date from the cut or a catalog "
        "symbol, and any count of directories",
        1,
        2,
        4 * MIB,
        9 * MIB,
        note="reduced in memory against the whole catalog; an unknown date is not kept; "
        "max 2 because an interrupted phase 0 repeats once",
    ),
    ClassSpec(
        "D1",
        "archive_listing",
        1,
        "GET",
        ARCHIVE_ROOT + "{SYMBOL}/",
        "one request per in-window launch and one per archive-only candidate",
        "file names matching SYMBOL + YYYY-MM-DD + .csv.gz and their dates, only dates "
        "before 2025-10-01 are kept",
        "file names and dates from 2025-10-01 (dropped uncounted), sizes, every other byte",
        293,
        MAX_LAUNCHES + MAX_ARCHIVE_ONLY,
        MIB,
        256 * MIB,
    ),
    ClassSpec(
        "D2h",
        "first_file_head",
        2,
        "HEAD",
        ARCHIVE_ROOT + "{SYMBOL}/{SYMBOL}{FIRST_DAY}.csv.gz",
        "none; only launches whose first listed day is within one day of launchTime",
        "HTTP status and Content-Length",
        "everything else (a HEAD has no body)",
        250,
        MAX_LAUNCHES,
        0,
        0,
        note="proves the first file exists and gives its size for the preflight of D2",
    ),
    ClassSpec(
        "D2",
        "first_file",
        2,
        "GET",
        ARCHIVE_ROOT + "{SYMBOL}/{SYMBOL}{FIRST_DAY}.csv.gz",
        "none; the whole first daily file, streamed and gunzipped in memory",
        "column 1 only (the trade timestamp): minimum, maximum, first, last, row count",
        "columns 2 and later of every row (side, size, price, ids, notionals), unparsed; "
        "the file is never stored",
        250,
        MAX_LAUNCHES,
        PER_FILE_CAP_BYTES,
        6144 * MIB,
        go=2,
        note="runs only if the summed Content-Length of D2h is at most the class byte cap",
    ),
    ClassSpec(
        "D3",
        "entry_trade_bar",
        3,
        "GET",
        KLINE_URL,
        "category=linear, symbol, interval=1, start=E, end=E, limit=1",
        "column 1 only (the bar start time), existence",
        "open, high, low, close, volume, turnover, unparsed",
        205,
        MAX_LAUNCHES,
        64 * 1024,
        24 * MIB,
        go=2,
        note="one V1 entry bar per entry candidate; decides entry_unavailable",
    ),
    ClassSpec(
        "D4",
        "mark_hourly",
        3,
        "GET",
        MARK_URL,
        "category=linear, symbol, interval=60, start=T0+3d, end=min(T0+21d, cut), limit=1000",
        "column 1 only (the bar start time): count, oldest, newest and the runs of missing hours",
        "open, high, low, close, unparsed",
        205,
        MAX_LAUNCHES,
        512 * 1024,
        150 * MIB,
        go=2,
        note="hourly mark-price series over days 3 to 21, one page per launch",
    ),
    ClassSpec(
        "D5",
        "trade_hourly",
        3,
        "GET",
        KLINE_URL,
        "category=linear, symbol, interval=60, start=T0+3d, end=min(T0+21d, cut), limit=1000",
        "column 1 only (the bar start time): count, oldest, newest and the runs of missing hours",
        "open, high, low, close, volume, turnover, unparsed",
        205,
        MAX_LAUNCHES,
        512 * 1024,
        150 * MIB,
        go=2,
        note="hourly trade-bar series over days 3 to 21, one page per launch",
    ),
    ClassSpec(
        "D6",
        "funding_history",
        3,
        "GET",
        FUNDING_URL,
        "category=linear, symbol, startTime=T0+3d, endTime paged backwards, limit=200",
        "fundingRateTimestamp only: per page the row count, oldest, newest and the largest "
        "gap between consecutive timestamps",
        "fundingRate (every row), unparsed",
        270,
        MAX_LAUNCHES * FUNDING_MAX_PAGES,
        256 * 1024,
        300 * MIB,
        go=2,
        note="paged by the HYP-036 rule; at most four pages per launch",
    ),
)
CLASS_BY_ID: Final = {c.cls: c for c in CLASSES}


def go_classes(go: int, contract: dict[str, Any] | None = None) -> tuple[str, ...]:
    """The classes of one go."""
    if contract is None:
        return tuple(c.cls for c in CLASSES if c.go == go)
    return tuple(c["cls"] for c in contract["classes"] if c["go"] == go)


# The frozen thresholds.

THRESHOLDS: Final[dict[str, Any]] = {
    "m1_aligned_from_ms": -5 * MINUTE_MS,
    "m1_aligned_to_ms": 60 * MINUTE_MS,
    "m1_delayed_to_ms": 24 * HOUR_MS,
    "m1_ambiguous_max": 0.10,
    "m1_readable_min": 0.90,
    "lifecycle_unrebuildable_max": 0.15,
    "m2_no_file_near_launch_max": 0.05,
    "m2_archive_only_max": 0.05,
    "m2_path_days_missing_max": 0.05,
    "m2_near_launch_days": 1,
    "ticker_reuse_first_file_days": 60,
    "ticker_reuse_gap_days": 30,
    "m3_unverifiable_max": 0.20,
    "m3_contradicting_max": 0.10,
    "m4_funding_uncovered_max": 0.05,
    "m5_bars_min": 0.95,
    "m5_entry_or_unresolved_max": 0.05,
    "m6_feasible_min": 0.90,
    "m6_order_notional_usd": 50,
    "m6_min_leverage": 2,
    "min_episodes": 100,
    "min_weeks": 26,
}

STOP_CODES: Final = (
    "not_reconstructible",
    "incomplete_coverage",
    "identity_unresolved",
    "insufficient_events",
    "capital_infeasible",
)

# Small helpers.


class SchemaMismatchError(ValueError):
    """A response does not have the expected shape. Holds a fixed reason, never a value."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class FetchStoppedError(RuntimeError):
    pass


class BudgetExceededError(RuntimeError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(data: Any) -> bytes:
    return (json.dumps(data, sort_keys=True, indent=2) + "\n").encode()


def day_of(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).date().isoformat()


def day_ms(text: str) -> int:
    return _ms(f"{text}T00:00:00+00:00")


def day_diff(later: str, earlier: str) -> int:
    return (date.fromisoformat(later) - date.fromisoformat(earlier)).days


def add_days(text: str, days: int) -> str:
    return (date.fromisoformat(text) + timedelta(days=days)).isoformat()


def iso_week(ms: int) -> str:
    year, week, _ = datetime.fromtimestamp(ms / 1000, UTC).isocalendar()
    return f"{year}-W{week:02d}"


def _int_ms(value: Any) -> int:
    if isinstance(value, bool):
        raise TypeError("bool")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and re.fullmatch(r"\d{1,16}", value):
        number = int(value)
    else:
        raise TypeError("not an integer time")
    if number <= 0:
        raise ValueError("non positive time")
    return number


def key_tree(payload: Any, prefix: str = "") -> list[str]:
    """Key paths and value types only, never values (schema probe)."""
    out: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            out.add(f"{path}:{type(value).__name__}")
            out.update(key_tree(value, path))
    elif isinstance(payload, list):
        for value in payload:
            out.update(key_tree(value, f"{prefix}[]"))
    return sorted(out)


def _envelope(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("retCode") != 0:
        raise SchemaMismatchError("envelope")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise SchemaMismatchError("result")
    return result


# The reducers: the cut lives here.


def _filter(item: dict[str, Any], outer: str, inner: str) -> str | None:
    block = item.get(outer)
    if not isinstance(block, dict):
        return None
    value = block.get(inner)
    return value if isinstance(value, str) and value else None


def reduce_catalog_page(payload: Any) -> dict[str, Any]:
    """Linear USDT perpetuals of one catalog page.

    The launch time is read first. A row launched from CUT_MS on is skipped before any other
    field is read and is not counted. A row launched before the window keeps only its symbol,
    base and whether it was delivered before the window (it matters for `relist` and
    `multiplier_ambiguous`, never as a date).
    """
    result = _envelope(payload)
    items = result.get("list")
    cursor = result.get("nextPageCursor") or ""
    if not isinstance(items, list) or not isinstance(cursor, str):
        raise SchemaMismatchError("list")
    rows: list[dict[str, Any]] = []
    prior: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise SchemaMismatchError("item")
        try:
            launch = _int_ms(item["launchTime"])
        except KeyError as exc:
            raise SchemaMismatchError("missing:launchTime") from exc
        except (TypeError, ValueError) as exc:
            raise SchemaMismatchError("bad:launchTime") from exc
        if launch >= CUT_MS:
            continue
        if item.get("contractType") != "LinearPerpetual" or item.get("settleCoin") != "USDT":
            continue
        symbol = item.get("symbol")
        if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
            raise SchemaMismatchError("bad:symbol")
        base = asset(symbol[:-4])
        raw = item.get("deliveryTime")
        try:
            delivery = _int_ms(raw) if raw not in (None, "", "0", 0) else None
        except (TypeError, ValueError) as exc:
            raise SchemaMismatchError("bad:deliveryTime") from exc
        if launch < WINDOW_START_MS:
            before = delivery is not None and delivery < WINDOW_START_MS
            prior.append(
                {
                    "symbol": symbol,
                    "base": base,
                    "delivered_before_window": before,
                    # a delivery date inside the window only: it decides `relist` against
                    # `multiplier_ambiguous`; an earlier date is never kept
                    "delivery_ms": delivery
                    if delivery is not None and delivery < CUT_MS and not before
                    else None,
                }
            )
            continue
        interval = item.get("fundingInterval")
        rows.append(
            {
                "symbol": symbol,
                "base": base,
                "base_coin": item.get("baseCoin")
                if isinstance(item.get("baseCoin"), str)
                else None,
                "status": item.get("status") if isinstance(item.get("status"), str) else None,
                "launch_ms": launch,
                "delivery_ms": delivery if delivery is not None and delivery < CUT_MS else None,
                "funding_interval_min": interval if isinstance(interval, int) else None,
                "pre_listing_field": "isPreListing" in item,
                "min_notional": _filter(item, "lotSizeFilter", "minNotionalValue"),
                "qty_step": _filter(item, "lotSizeFilter", "qtyStep"),
                "min_order_qty": _filter(item, "lotSizeFilter", "minOrderQty"),
                "max_leverage": _filter(item, "leverageFilter", "maxLeverage"),
            }
        )
    return {
        "rows": sorted(rows, key=lambda r: (r["launch_ms"], r["symbol"])),
        "prior": sorted(prior, key=lambda r: r["symbol"]),
        "next_cursor": cursor,
    }


def reduce_root_listing(html: str, catalog_symbols: set[str]) -> dict[str, Any]:
    """Archive directories: only the candidates are kept, and nothing about the others.

    `catalog_symbols` holds every linear USDT symbol of the catalog, including the ones
    launched after the cut, in memory only. A candidate is a directory that is not in it
    and whose modification date is inside the window: its last file is before the cut, so
    it cannot be a launch of the test year. A directory with no date, a date from the cut,
    or a catalog symbol leaves no trace, and no count of directories is kept (an added
    post-cut directory must not change this output).
    """
    dirs: dict[str, str | None] = {}
    for line in html.splitlines():
        match = ROOT_DIR.search(line)
        if match:
            found = DATE.search(line[match.end() :])
            dirs[match.group(1)] = found.group(0) if found else None
    if not dirs:
        raise SchemaMismatchError("no_dirs")
    candidates = sorted(
        name
        for name, modified in dirs.items()
        if name not in catalog_symbols
        and modified is not None
        and WINDOW_START_DAY <= modified < CUT_DAY
    )
    # A yes or no, never a number: whether the page carries modification dates at all.
    return {
        "candidates": candidates,
        "modified_dates_present": any(m is not None for m in dirs.values()),
    }


def reduce_symbol_listing(symbol: str, html: str) -> dict[str, Any]:
    """File dates of one symbol. Dates from CUT_DAY on are dropped uncounted."""
    if 'href="' not in html:
        raise SchemaMismatchError("no_anchors")
    days = sorted({d for name, d in LISTED.findall(html) if name == symbol and d < CUT_DAY})
    gaps: list[list[Any]] = []
    for before, after in pairwise(days):
        if day_diff(after, before) > THRESHOLDS["ticker_reuse_gap_days"]:
            gaps.append([before, after, day_diff(after, before)])
    return {
        "present": True,
        "first_day": days[0] if days else None,
        "days_in_window": [d for d in days if d >= WINDOW_START_DAY],
        "gaps": gaps,
    }


def _timestamp_ms(field_bytes: bytes) -> int:
    """Column 1 of a trade row as milliseconds, without floats: '1704067200.123456'."""
    if not re.fullmatch(rb"\d{9,11}(?:\.\d{1,9})?", field_bytes):
        raise SchemaMismatchError("bad:timestamp")
    whole, _, frac = field_bytes.partition(b".")
    return int(whole) * 1000 + int((frac + b"000")[:3])


class FirstFileReducer:
    """Streaming reducer of one gzipped trade file: column 1 only, everything else unparsed."""

    def __init__(self) -> None:
        self.rows = 0
        self.min_ms: int | None = None
        self.max_ms: int | None = None
        self.first_ms: int | None = None
        self.last_ms: int | None = None
        self._tail = b""
        self._header_seen = False
        self._zlib = zlib.decompressobj(wbits=31)

    def _line(self, line: bytes) -> None:
        if not line:
            return
        head = line.split(b",", 1)[0]
        if not self._header_seen:
            self._header_seen = True
            if head == b"timestamp":
                return
        ts = _timestamp_ms(head)
        if ts >= CUT_MS:
            raise SchemaMismatchError("timestamp_after_cut")
        self.rows += 1
        self.first_ms = ts if self.first_ms is None else self.first_ms
        self.last_ms = ts
        self.min_ms = ts if self.min_ms is None else min(self.min_ms, ts)
        self.max_ms = ts if self.max_ms is None else max(self.max_ms, ts)

    def feed(self, chunk: bytes) -> None:
        try:
            data = self._tail + self._zlib.decompress(chunk)
        except zlib.error as exc:
            raise SchemaMismatchError("not_gzip") from exc
        *lines, self._tail = data.split(b"\n")
        for line in lines:
            self._line(line.rstrip(b"\r"))

    def finish(self) -> dict[str, Any]:
        try:
            self._tail += self._zlib.flush()
        except zlib.error as exc:
            raise SchemaMismatchError("not_gzip") from exc
        if not self._zlib.eof:
            raise SchemaMismatchError("truncated_gzip")
        self._line(self._tail.rstrip(b"\r\n"))
        if self.rows == 0:
            raise SchemaMismatchError("no_rows")
        return {
            "rows": self.rows,
            "min_ms": self.min_ms,
            "max_ms": self.max_ms,
            "first_ms": self.first_ms,
            "last_ms": self.last_ms,
        }


def _starts(rows: Any, width: int) -> list[int]:
    """Column 1 of each row of a kline list. Rows from the cut on are dropped uncounted."""
    if not isinstance(rows, list):
        raise SchemaMismatchError("list")
    out: list[int] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < width:
            raise SchemaMismatchError("row")
        try:
            start = _int_ms(row[0])
        except (TypeError, ValueError) as exc:
            raise SchemaMismatchError("bad:start") from exc
        if start < CUT_MS:
            out.append(start)
    return out


MAX_GAP_RUNS: Final = 20


def reduce_kline(
    payload: Any, symbol: str, width: int, step_ms: int = HOUR_MS, limit: int = 1000
) -> dict[str, Any]:
    """Start times of a bar series, and the runs of missing bars between them.

    `runs` lists [first missing start, number missing] for gaps between consecutive bars,
    at most MAX_GAP_RUNS of them (`runs_truncated` says more exist). `full` says the page
    holds as many rows as its limit, so it may not reach the span start.
    """
    result = _envelope(payload)
    if result.get("symbol") != symbol:
        raise SchemaMismatchError("symbol")
    rows = result.get("list")
    starts = sorted(set(_starts(rows, width)))
    runs: list[list[int]] = []
    for before, after in pairwise(starts):
        missing = (after - before) // step_ms - 1
        if missing > 0:
            runs.append([before + step_ms, missing])
    return {
        "n": len(starts),
        "oldest_ms": starts[0] if starts else None,
        "newest_ms": starts[-1] if starts else None,
        "runs": runs[:MAX_GAP_RUNS],
        "runs_truncated": len(runs) > MAX_GAP_RUNS,
        "full": isinstance(rows, list) and len(rows) >= limit,
    }


def reduce_funding(payload: Any, symbol: str) -> dict[str, Any]:
    """Timestamps of one funding page: boundaries and the largest gap, never a rate."""
    result = _envelope(payload)
    items = result.get("list")
    if not isinstance(items, list):
        raise SchemaMismatchError("list")
    stamps: list[int] = []
    for item in items:
        if not isinstance(item, dict) or item.get("symbol") != symbol:
            raise SchemaMismatchError("row")
        try:
            stamp = _int_ms(item["fundingRateTimestamp"])
        except KeyError as exc:
            raise SchemaMismatchError("missing:fundingRateTimestamp") from exc
        except (TypeError, ValueError) as exc:
            raise SchemaMismatchError("bad:fundingRateTimestamp") from exc
        if stamp < CUT_MS:
            stamps.append(stamp)
    stamps.sort()
    gaps = [b - a for a, b in pairwise(stamps)]
    return {
        "n": len(stamps),
        "oldest_ms": stamps[0] if stamps else None,
        "newest_ms": stamps[-1] if stamps else None,
        "max_gap_ms": max(gaps) if gaps else None,
        "full": len(items) >= FUNDING_PAGE_LIMIT,
    }


# The entry clock and V1 eligibility.


def entry_clock(t0_ms: int) -> tuple[int, int]:
    """E = T0 + 7 d rounded up to the next whole minute; X = E + 7 d (protocol 5.2)."""
    entry = -(-(t0_ms + AGE_DAYS * DAY_MS) // MINUTE_MS) * MINUTE_MS
    return entry, entry + HOLD_DAYS * DAY_MS


def v1_cuts(t0_ms: int) -> dict[str, Any]:
    entry, exit_ = entry_clock(t0_ms)
    return {
        "entry_ms": entry,
        "exit_ms": exit_,
        "window_censored": exit_ > CUT_MS,
        "lookback_censored": entry - LOOKBACK_DAYS * DAY_MS < WINDOW_START_MS,
    }


def t0_class(first_trade_ms: int, launch_ms: int) -> str:
    d = first_trade_ms - launch_ms
    if THRESHOLDS["m1_aligned_from_ms"] <= d <= THRESHOLDS["m1_aligned_to_ms"]:
        return "aligned"
    if THRESHOLDS["m1_aligned_to_ms"] < d <= THRESHOLDS["m1_delayed_to_ms"]:
        return "delayed_open"
    return "premarket_or_ambiguous"


def class_from_first_day(first_day: str | None, launch_ms: int) -> str | None:
    """M1 class decided from file-name arithmetic alone, or None when the file must be read.

    A first file two or more days away from the launch day cannot hold a first trade within
    24 hours of launchTime, so no download is needed for it.
    """
    if first_day is None:
        return None
    gap = day_diff(first_day, day_of(launch_ms))
    if abs(gap) >= 2:
        return "premarket_or_ambiguous"
    return None


def needs_first_file(first_day: str | None, launch_ms: int) -> bool:
    return first_day is not None and class_from_first_day(first_day, launch_ms) is None


def funding_span(t0_ms: int) -> tuple[int, int]:
    return (
        t0_ms + PATH_FIRST_DAY * DAY_MS,
        min(t0_ms + PATH_LAST_DAY * DAY_MS, CUT_MS - 1),
    )


# Requests and the planner.


@dataclass(frozen=True)
class Request:
    cls: str
    method: str
    url: str
    params: tuple[tuple[str, str], ...] = ()
    symbol: str = ""
    meta: tuple[tuple[str, str], ...] = ()

    @property
    def key(self) -> str:
        return digest(
            json.dumps(
                [self.cls, self.method, self.url, sorted(self.params), sorted(self.meta)]
            ).encode()
        )

    def meta_dict(self) -> dict[str, str]:
        return dict(self.meta)


def _check_symbol(symbol: str) -> str:
    if not SYMBOL.fullmatch(symbol):
        raise ValueError("symbol outside the contract")
    return symbol


MAX_CATALOG_PAGES: Final = 3  # per status: contract cap 9 requests for three statuses


def catalog_request(status: str, cursor: str = "", round_no: int = 1) -> Request:
    params = [("category", "linear"), ("status", status), ("limit", "1000")]
    if cursor:
        params.append(("cursor", cursor))
    meta = (("round", str(round_no)), ("status", status))
    return Request("C1", "GET", CATALOG_URL, tuple(params), meta=meta)


def root_request(round_no: int = 1) -> Request:
    return Request("C3", "GET", ARCHIVE_ROOT, meta=(("round", str(round_no)),))


def static_requests() -> list[Request]:
    """The first requests of phase 0, in order. Later catalog pages follow their cursors."""
    return [
        *(catalog_request(status) for status in CATALOG_STATUSES),
        root_request(),
    ]


def in_window_launches(phase0: dict[str, Any]) -> list[dict[str, Any]]:
    rows = {r["symbol"]: r for r in phase0["catalog"]["rows"]}
    return sorted(rows.values(), key=lambda r: (r["launch_ms"], r["symbol"]))


def listing_requests(phase0: dict[str, Any]) -> list[Request]:
    launches = in_window_launches(phase0)
    candidates = phase0["archive"]["candidates"]
    if len(launches) > MAX_LAUNCHES or len(candidates) > MAX_ARCHIVE_ONLY:
        raise FetchStoppedError("plan_exceeds_cap")
    out = [
        Request(
            "D1",
            "GET",
            f"{ARCHIVE_ROOT}{_check_symbol(r['symbol'])}/",
            symbol=r["symbol"],
            meta=(("kind", "launch"),),
        )
        for r in launches
    ]
    out += [
        Request(
            "D1",
            "GET",
            f"{ARCHIVE_ROOT}{_check_symbol(name)}/",
            symbol=name,
            meta=(("kind", "candidate"),),
        )
        for name in candidates
    ]
    return out


def first_file_url(symbol: str, first_day: str) -> str:
    return f"{ARCHIVE_ROOT}{_check_symbol(symbol)}/{symbol}{first_day}.csv.gz"


def head_requests(phase0: dict[str, Any], listings: dict[str, dict[str, Any]]) -> list[Request]:
    out: list[Request] = []
    for row in in_window_launches(phase0):
        listing = listings.get(row["symbol"])
        if not listing or listing.get("outcome") != "ok":
            continue
        first_day = listing["facts"]["first_day"]
        if needs_first_file(first_day, row["launch_ms"]):
            url = first_file_url(row["symbol"], first_day)
            meta = (("first_day", first_day),)
            out.append(Request("D2h", "HEAD", url, symbol=row["symbol"], meta=meta))
    return out


def preflight(heads: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Bytes of the D2 class from the Content-Length of every HEAD, before a byte is read."""
    sizes: dict[str, int] = {}
    over: list[str] = []
    missing: list[str] = []
    for symbol, result in sorted(heads.items()):
        length = (result.get("facts") or {}).get("content_length")
        if result.get("outcome") != "ok" or not isinstance(length, int):
            missing.append(symbol)
        elif length > PER_FILE_CAP_BYTES:
            over.append(symbol)
        else:
            sizes[symbol] = length
    total = sum(sizes.values())
    cap = CLASS_BY_ID["D2"].max_class_bytes
    return {
        "files": len(sizes),
        "bytes": total,
        "over_per_file_cap": over,
        "no_size": missing,
        "class_cap_bytes": cap,
        "within_cap": total <= cap,
        "sizes": sizes,
    }


def first_file_requests(heads: dict[str, dict[str, Any]], plan: dict[str, Any]) -> list[Request]:
    out: list[Request] = []
    for symbol in sorted(plan["sizes"]):
        first_day = heads[symbol]["meta"]["first_day"]
        meta = (("first_day", first_day),)
        out.append(
            Request("D2", "GET", first_file_url(symbol, first_day), symbol=symbol, meta=meta)
        )
    return out


def t0_of(
    row: dict[str, Any],
    listing: dict[str, Any] | None,
    first: dict[str, Any] | None,
    head: dict[str, Any] | None = None,
) -> tuple[int | None, str]:
    """(T0 in ms or None, M1 class or an unreadable reason) for one launch.

    `t0_over_cap` is a first file above the per-file cap: it is not downloaded, and it is
    counted apart from the other unreadable reasons so the bias against big launches shows.
    """
    if not listing or listing.get("outcome") != "ok" or listing["facts"]["first_day"] is None:
        return None, "no_archive"
    cls = class_from_first_day(listing["facts"]["first_day"], row["launch_ms"])
    if cls is not None:
        return None, cls
    if not first or first.get("outcome") != "ok":
        length = ((head or {}).get("facts") or {}).get("content_length")
        if isinstance(length, int) and length > PER_FILE_CAP_BYTES:
            return None, "t0_over_cap"
        return None, "t0_unreadable"
    first_ms = first["facts"]["min_ms"]
    if day_of(first_ms) != listing["facts"]["first_day"]:
        return None, "premarket_or_ambiguous"
    return first_ms, t0_class(first_ms, row["launch_ms"])


def ticker_reuse(listing: dict[str, Any], launch_ms: int) -> bool:
    """`ticker_reuse_suspected`: files months before the launch, or a long gap before it."""
    first_day = listing["first_day"]
    if first_day is None:
        return False
    launch_day = day_of(launch_ms)
    if day_diff(launch_day, first_day) > THRESHOLDS["ticker_reuse_first_file_days"]:
        return True
    return any(before < launch_day for before, _after, _gap in listing["gaps"])


def _live_at(prior: dict[str, Any], launch_ms: int) -> bool:
    """Did a symbol launched before the window still exist at `launch_ms`?"""
    if prior["delivered_before_window"]:
        return False
    delivery = prior.get("delivery_ms")
    return delivery is None or delivery > launch_ms


def catalog_contradictions(phase0: dict[str, Any]) -> dict[str, str]:
    """`relist` and `multiplier_ambiguous` from catalog rows alone (protocol 4, M3).

    A launch has an earlier symbol of the same base. If that symbol still existed at the
    launch (not delivered before it) the base had two symbols at once, otherwise the launch
    is a relist.
    """
    prior_by_base: dict[str, list[dict[str, Any]]] = {}
    for p in phase0["catalog"]["prior"]:
        prior_by_base.setdefault(p["base"], []).append(p)
    seen: dict[str, list[dict[str, Any]]] = {}
    out: dict[str, str] = {}
    for row in in_window_launches(phase0):
        earlier = prior_by_base.get(row["base"], [])
        before = seen.get(row["base"], [])
        if earlier or before:
            live = any(_live_at(p, row["launch_ms"]) for p in earlier) or any(
                e["delivery_ms"] is None or e["delivery_ms"] > row["launch_ms"] for e in before
            )
            out[row["symbol"]] = "multiplier_ambiguous" if live else "relist"
        seen.setdefault(row["base"], []).append(row)
    return out


def entry_candidates(
    phase0: dict[str, Any],
    listings: dict[str, dict[str, Any]],
    firsts: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Launches that get bar, mark and funding requests.

    A readable, unambiguous T0, inside the window and the lookback cut for V1, and not
    contradicted by the catalog or the file list (`relist`, `multiplier_ambiguous`,
    `ticker_reuse_suspected`)."""
    contradicted = catalog_contradictions(phase0)
    out: list[dict[str, Any]] = []
    for row in in_window_launches(phase0):
        symbol = row["symbol"]
        listing = listings.get(symbol)
        t0, cls = t0_of(row, listing, firsts.get(symbol))
        if t0 is None or cls not in ("aligned", "delayed_open") or symbol in contradicted:
            continue
        if listing is not None and ticker_reuse(listing["facts"], row["launch_ms"]):
            continue
        cuts = v1_cuts(t0)
        if cuts["window_censored"] or cuts["lookback_censored"]:
            continue
        out.append({"symbol": symbol, "launch_ms": row["launch_ms"], "t0_ms": t0, **cuts})
    return out


def _kline_params(
    symbol: str, interval: str, start: int, end: int, limit: int
) -> tuple[tuple[str, str], ...]:
    return (
        ("category", "linear"),
        ("symbol", symbol),
        ("interval", interval),
        ("start", str(start)),
        ("end", str(end)),
        ("limit", str(limit)),
    )


def bar_requests(candidates: list[dict[str, Any]]) -> list[Request]:
    out: list[Request] = []
    for c in candidates:
        symbol = _check_symbol(c["symbol"])
        start, end = funding_span(c["t0_ms"])
        entry = c["entry_ms"]
        out.append(
            Request("D3", "GET", KLINE_URL, _kline_params(symbol, "1", entry, entry, 1), symbol)
        )
        for cls, url in (("D4", MARK_URL), ("D5", KLINE_URL)):
            params = _kline_params(symbol, "60", start, end, 1000)
            out.append(Request(cls, "GET", url, params, symbol))
    return out


FUNDING_MAX_STEP_MS: Final = 8 * HOUR_MS  # the longest Bybit funding interval


def funding_state(
    pages: list[dict[str, Any]], start: int, end: int, delivery_ms: int | None = None
) -> dict[str, Any]:
    """Funding coverage of [start, end]: pagination finished AND no gap longer than 8 h.

    Finishing the pagination (a short page, or a full page that reaches `start`) is not
    enough. The coarse check passes only when the timestamps of the pages form one chain with
    no step above the longest funding interval (8 h), start at most one step after `start`,
    and reach at most one step before `end` unless the catalog delivered the symbol by then
    (the end is an event, not a gap). A full page covers only from its oldest row (HYP-036).
    This is **not** proof that every settlement is present: for a symbol whose real interval
    is shorter than 8 h, missing settlements below the step go unseen, and the real historical
    interval is unknown (the catalog `fundingInterval` is a current snapshot). The verdict
    labels the result "no gap longer than 8 h" and reports the interval strength apart.
    `pages` are the final results, newest page first; a page that did not come back ends the
    launch as `unavailable`.
    """
    step = FUNDING_MAX_STEP_MS
    next_end = end
    oldest: int | None = None
    newest: int | None = None
    finished = False

    def decided(covered: bool, reason: str) -> dict[str, Any]:
        return {"decided": True, "covered": covered, "next_end": next_end, "reason": reason}

    for index, page in enumerate(pages):
        if page.get("outcome") != "ok":
            return decided(False, "unavailable")
        facts = page["facts"]
        if facts["n"]:
            if (facts["max_gap_ms"] or 0) > step:
                return decided(False, "gap_inside")
            if oldest is not None and oldest - facts["newest_ms"] > step:
                return decided(False, "gap_between")
            newest = facts["newest_ms"] if newest is None else newest
            oldest = facts["oldest_ms"]
        if not facts["full"]:
            finished = True
            break
        if oldest is None:
            return decided(False, "bad_page")
        if oldest <= start:
            finished = True
            break
        next_end = oldest - 1
        if index + 1 >= FUNDING_MAX_PAGES:
            return decided(False, "pagination_exhausted")
    if not finished:
        return {"decided": False, "covered": False, "next_end": next_end, "reason": "pending"}
    if newest is None or oldest is None:
        return decided(False, "no_rows")
    if oldest > start + step:
        return decided(False, "starts_late")
    ended = delivery_ms is not None and delivery_ms <= newest + step
    if newest < end - step and not ended:
        return decided(False, "ends_early")
    return decided(True, "no_gap_over_8h")


def deliveries(phase0: dict[str, Any]) -> dict[str, int | None]:
    return {r["symbol"]: r["delivery_ms"] for r in phase0["catalog"]["rows"]}


def funding_requests(
    candidates: list[dict[str, Any]],
    pages: dict[str, list[dict[str, Any]]],
    delivered: dict[str, int | None],
) -> list[Request]:
    """The next funding page of every launch whose coverage is not yet decided."""
    out: list[Request] = []
    for c in candidates:
        symbol = _check_symbol(c["symbol"])
        start, end = funding_span(c["t0_ms"])
        done = pages.get(symbol, [])
        state = funding_state(done, start, end, delivered.get(symbol))
        if state["decided"]:
            continue
        params = (
            ("category", "linear"),
            ("symbol", symbol),
            ("startTime", str(start)),
            ("endTime", str(state["next_end"])),
            ("limit", str(FUNDING_PAGE_LIMIT)),
        )
        meta = (("page", str(len(done) + 1)),)
        out.append(Request("D6", "GET", FUNDING_URL, params, symbol, meta))
    return out


# The contract.


def contract_classes() -> list[dict[str, Any]]:
    return [
        {
            "cls": c.cls,
            "name": c.name,
            "go": c.go,
            "phase": c.phase,
            "method": c.method,
            "url": c.url,
            "params": c.params,
            "parsed": c.parsed,
            "discarded": c.discarded,
            "expected_requests": c.expected,
            "max_requests": c.max_requests,
            "max_response_bytes": c.max_response_bytes,
            "max_class_bytes": c.max_class_bytes,
            "note": c.note,
        }
        for c in CLASSES
    ]


def static_request_list() -> list[dict[str, Any]]:
    return [
        {
            "cls": r.cls,
            "method": r.method,
            "url": r.url,
            "params": [list(p) for p in r.params],
        }
        for r in static_requests()
    ]


def _go_limits() -> dict[str, dict[str, int]]:
    return {
        str(go): {"max_attempts": GO_ATTEMPT_CAPS[go], "max_bytes": GO_BYTE_CAPS[go]}
        for go in (1, 2)
    }


def build_contract() -> dict[str, Any]:
    """The frozen contract.

    One file with two gos that cannot run out of order: go 2 needs a finished go 1 and the
    SHA-256 of the manifest that go 1 wrote. `go_sha256` is what the owner approves for each
    go (the shared rules plus that go's classes and caps); `request_list_sha256` covers the
    whole file and is what every run is bound to.
    """
    shared: dict[str, Any] = {
        "rules": VERSION,
        "study": STUDY_ID,
        "protocol": PROTOCOL,
        "windows": {
            "discovery_start": "2024-10-01T00:00:00+00:00",
            "cut": "2025-10-01T00:00:00+00:00",
            "note": "nothing dated from the cut is read, recorded or counted",
        },
        "limits": {
            "attempts_per_request": ATTEMPTS_PER_REQUEST,
            "per_file_cap_bytes": PER_FILE_CAP_BYTES,
            "max_launches": MAX_LAUNCHES,
            "max_archive_only": MAX_ARCHIVE_ONLY,
            "spacing_s": SPACING_S,
            "max_spacing_s": MAX_SPACING_S,
            "backoff_s": list(BACKOFF_S),
            "max_retry_after_s": MAX_RETRY_AFTER_S,
            "funding_page_limit": FUNDING_PAGE_LIMIT,
            "funding_max_pages": FUNDING_MAX_PAGES,
            "funding_max_step_ms": FUNDING_MAX_STEP_MS,
            "read_reserve_bytes": READ_RESERVE_BYTES,
            "byte_rule": "a request starts only if used + response cap + read reserve fits the "
            "step budget and the class budget; every received byte is accounted; a declared "
            "length over the cap reads nothing; the transport reads at most the reserve per call",
        },
        "thresholds": THRESHOLDS,
        "stop_codes": list(STOP_CODES),
    }
    classes = contract_classes()
    go_limits = _go_limits()
    body: dict[str, Any] = {
        **shared,
        "go_limits": go_limits,
        "classes": classes,
        "static_requests": static_request_list(),
    }
    body["go_sha256"] = {str(go): go_hash(body, go) for go in (1, 2)}
    body["request_list_sha256"] = digest(canonical(body))
    return body


def go_hash(contract: dict[str, Any], go: int) -> str:
    """What the owner approves for one go: the shared rules, that go's classes and its caps."""
    shared = {
        k: contract[k]
        for k in ("rules", "study", "protocol", "windows", "limits", "thresholds", "stop_codes")
    }
    return digest(
        canonical(
            {
                "shared": shared,
                "go": go,
                "classes": [c for c in contract["classes"] if c["go"] == go],
                "limits": contract["go_limits"][str(go)],
            }
        )
    )


def contract_hash(contract: dict[str, Any]) -> str:
    body = {k: v for k, v in contract.items() if k != "request_list_sha256"}
    return digest(canonical(body))


# The attempt ledger.


def ledger_totals(
    attempts: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
    classes: frozenset[str] | None = None,
) -> tuple[int, int, dict[str, int], dict[str, int]]:
    """Attempts and bytes used, in total and by class. An attempt with no receipt (a crash
    between its charge and its result) is charged the full response cap of its class."""
    done = {r["seq"]: int(r["body_bytes"]) for r in receipts}
    by_n: dict[str, int] = {}
    by_b: dict[str, int] = {}
    for a in attempts:
        if classes is not None and a["cls"] not in classes:
            continue
        cap = reserve(CLASS_BY_ID[a["cls"]])  # a crashed send may have received up to this
        by_n[a["cls"]] = by_n.get(a["cls"], 0) + 1
        by_b[a["cls"]] = by_b.get(a["cls"], 0) + done.get(a["seq"], cap)
    return sum(by_n.values()), sum(by_b.values()), by_n, by_b


def prior_usage(roots: Iterable[Path]) -> dict[str, dict[str, int]]:
    """Earlier roots' usage, kept apart per go: the cost of go 2 files never counts against go 1."""
    out = {str(go): {"attempts": 0, "bytes": 0} for go in (1, 2)}
    for root in roots:
        a = [json.loads(p.read_text()) for p in sorted((root / "attempts").glob("*.json"))]
        r = [json.loads(p.read_text()) for p in sorted((root / "receipts").glob("*.json"))]
        for go in (1, 2):
            count, used, _, _ = ledger_totals(a, r, frozenset(go_classes(go)))
            out[str(go)]["attempts"] += count
            out[str(go)]["bytes"] += used
    return out


# The fetcher.

RETRYABLE_STATUS: Final = frozenset({429, 500, 502, 503, 504})
RATE_LIMIT_RET_CODE: Final = 10006
TERMINAL: Final = frozenset({"ok", "absent", "too_large"})
FATAL_OUTCOMES: Final = frozenset({"forbidden", "schema_mismatch"})
FILE_CLASSES: Final = frozenset({"D1", "D2h", "D2"})


def classify_status(request: Request, status: int, body: bytes) -> str | None:
    """None for a usable response, else the outcome. A Bybit rate-limit code is retried."""
    if status == 403:
        return "forbidden"
    if status == 404:
        return None if request.cls in FILE_CLASSES else "schema_mismatch"
    if status in RETRYABLE_STATUS:
        return "error"
    if status != 200:
        return "schema_mismatch"
    if request.url.startswith(BASE_API) and body:
        try:
            payload = json.loads(body)
        except ValueError:
            return "schema_mismatch"
        if isinstance(payload, dict) and payload.get("retCode") == RATE_LIMIT_RET_CODE:
            return "error"
    return None


def reserve(spec: ClassSpec) -> int:
    """Bytes a request may add to a budget: its response cap plus the last block."""
    return spec.max_response_bytes + (READ_RESERVE_BYTES if spec.max_response_bytes else 0)


def final_from_receipts(
    receipts: list[dict[str, Any]], attempts: list[dict[str, Any]] | None = None
) -> dict[str, dict[str, Any]]:
    """The final result of each request key: its terminal receipt, or `unavailable` once the
    journal shows all attempts spent without one. Attempts are counted from the journal, so
    a send whose process died before its receipt was written still counts. A key still in
    progress has no entry."""
    by_key: dict[str, list[dict[str, Any]]] = {}
    for r in receipts:
        by_key.setdefault(r["key"], []).append(r)
    sent: dict[str, list[dict[str, Any]]] = {}
    for a in attempts if attempts is not None else receipts:
        sent.setdefault(a["key"], []).append(a)
    out: dict[str, dict[str, Any]] = {}
    for key, items in by_key.items():
        done = next((r for r in items if r["outcome"] in TERMINAL), None)
        if done is not None:
            out[key] = done
    for key, journal in sent.items():
        if key not in out and len(journal) >= ATTEMPTS_PER_REQUEST:
            last = journal[-1]
            out[key] = {
                "cls": last["cls"],
                "key": key,
                "symbol": last.get("symbol", ""),
                "meta": last.get("meta", {}),
                "outcome": "unavailable",
                "facts": None,
            }
    return out


class Fetcher:
    """Sequential, resumable after a crash, never persists a response body.

    A stop (HTTP 403, a schema mismatch, a cap, a failed preflight) is written to
    `stop.json` and ends the root: nothing continues in it and no verdict of it is a pass.
    """

    def __init__(
        self,
        root: Path,
        contract: dict[str, Any],
        client: httpx.Client,
        *,
        go: int = 1,
        code: dict[str, Any] | None = None,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        prior: dict[str, dict[str, int]] | None = None,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.root, self.client, self.now, self.sleep, self.go = root, client, now, sleep, go
        self.wall = wall
        self.contract = json.loads(json.dumps(contract))
        if contract_hash(self.contract) != self.contract["request_list_sha256"]:
            raise ValueError("contract does not match its own hash")
        if go not in (1, 2):
            raise ValueError("go must be 1 or 2")
        stop = root / "stop.json"
        if stop.exists():
            raise FetchStoppedError(f"this run was stopped: {json.loads(stop.read_text())}")
        self.enabled = frozenset(go_classes(go, self.contract))
        self.prior = prior or {}
        binding = {
            "request_list_sha256": self.contract["request_list_sha256"],
            "prior": self.prior,
            "code": code or {},
        }
        run = root / "run.json"
        if run.exists():
            # the full binding: a parser fix leaves the contract unchanged, so the code
            # revision and its hashes are part of what must match
            if json.loads(run.read_text()) != json.loads(json.dumps(binding)):
                raise ValueError("run binding differs; a corrected revision starts a new root")
        else:
            publish_once(run, binding)
            publish_once(root / "contract.json", self.contract)
        self.attempts = self._load("attempts")
        self.receipts = self._load("receipts")
        if [a["seq"] for a in self.attempts] != list(range(1, len(self.attempts) + 1)):
            raise ValueError("attempt reservations are not a contiguous sequence")
        if not {r["seq"] for r in self.receipts} <= {a["seq"] for a in self.attempts}:
            raise ValueError("receipt without a charged attempt")
        # A fatal receipt is written before stop.json, so a crash between the two writes leaves
        # a receipt and no stop file. The receipt alone is enough: recovery writes the missing
        # stop and refuses to continue (every stop kind that has a receipt is covered here).
        for r in self.receipts:
            if r["outcome"] in FATAL_OUTCOMES:
                what = "HTTP 403" if r["outcome"] == "forbidden" else "schema mismatch"
                reason = f"{r['cls']}: {what} (recovered from its receipt)"
                self.record_stop(reason)
                raise FetchStoppedError(f"this run was stopped: {reason}")
        # the slowdown of every earlier HTTP 429 persists across a resume
        slowed = sum(1 for r in self.receipts if r.get("http_status") == 429)
        # and so does the server's Retry-After: the next permitted request time is persisted
        self.not_before = max(
            (
                float(r["at_epoch"]) + min(int(r["retry_after_s"]), MAX_RETRY_AFTER_S)
                for r in self.receipts
                if r["outcome"] == "error" and "retry_after_s" in r and "at_epoch" in r
            ),
            default=0.0,
        )
        self.spacing = min(MAX_SPACING_S, SPACING_S * 2**slowed)
        self.last = -self.spacing
        self.probed = {r["cls"] for r in self.receipts if "schema" in r}
        self.final: dict[str, dict[str, Any]] = final_from_receipts(self.receipts, self.attempts)

    def _load(self, kind: str) -> list[dict[str, Any]]:
        folder = self.root / kind
        return [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))]

    def record_stop(self, reason: str) -> None:
        """Persist the terminal reason once. A global stop outranks any class rule."""
        path = self.root / "stop.json"
        if not path.exists():
            publish_once(path, {"go": self.go, "reason": reason})

    def totals(self) -> tuple[int, int, dict[str, int], dict[str, int]]:
        """Usage of this go's classes, plus earlier roots, and by class."""
        count, used, by_n, by_b = ledger_totals(self.attempts, self.receipts, self.enabled)
        earlier = self.prior.get(str(self.go), {})
        return (
            count + int(earlier.get("attempts", 0)),
            used + int(earlier.get("bytes", 0)),
            by_n,
            by_b,
        )

    def next_round(self) -> int:
        """Phase 0 is atomic, so an interrupted one is repeated as a new round."""
        folder = self.root / "rounds"
        number = len(list(folder.glob("*.json"))) + 1 if folder.exists() else 1
        publish_once(folder / f"{number:02d}.json", {"round": number})
        return number

    def _charge(self, request: Request) -> int:
        spec = CLASS_BY_ID[request.cls]
        if request.cls not in self.enabled:
            raise FetchStoppedError(f"class {request.cls} is not in go {self.go}")
        count, used, by_n, by_b = self.totals()
        limits = self.contract["go_limits"][str(self.go)]
        reason = ""
        if count >= int(limits["max_attempts"]):
            reason = "attempt cap reached"
        elif used + reserve(spec) > int(limits["max_bytes"]):
            reason = "byte cap reached"
        elif by_n.get(request.cls, 0) >= spec.max_requests * ATTEMPTS_PER_REQUEST:
            reason = f"{request.cls}: attempts above the class cap"
        elif (
            spec.max_class_bytes and by_b.get(request.cls, 0) + reserve(spec) > spec.max_class_bytes
        ):
            reason = f"{request.cls}: class byte cap reached"
        if reason:
            self.record_stop(reason)
            raise BudgetExceededError(reason)
        seq = len(self.attempts) + 1
        attempt = {
            "seq": seq,
            "cls": request.cls,
            "key": request.key,
            "symbol": request.symbol,
            "meta": request.meta_dict(),
        }
        publish_once(self.root / "attempts" / f"{seq:05d}.json", attempt)  # charge before I/O
        self.attempts.append(attempt)
        return seq

    def _retry_delay(self, tries: int, asked: int | None) -> float:
        if isinstance(asked, int) and 0 < asked <= MAX_RETRY_AFTER_S:
            return float(asked)
        return float(BACKOFF_S[min(tries, len(BACKOFF_S)) - 1])

    def run_request(
        self,
        request: Request,
        reduce: Callable[[bytes], dict[str, Any]] | None = None,
        *,
        keep_facts: bool = True,
    ) -> dict[str, Any]:
        """One request to a final outcome, with at most three sends.

        Sends are counted from the attempt journal, so a send that crashed before its
        receipt still counts. The result carries the reduced `facts` in memory; they reach
        the receipt on disk only when `keep_facts` is true.
        """
        if request.key in self.final:
            return self.final[request.key]
        tries = sum(1 for a in self.attempts if a["key"] == request.key)
        asked: int | None = None
        credit = 0.0
        earlier = [r for r in self.receipts if r["key"] == request.key]
        if tries and earlier:
            # a recovered request: the server's Retry-After and the time already waited since
            # its last receipt come from the persisted receipt, not from this process
            asked = earlier[-1].get("retry_after_s")
            credit = max(0.0, self.wall() - float(earlier[-1].get("at_epoch", self.wall())))
        while True:
            if tries:
                wait = self._retry_delay(tries, asked) - credit
                credit = 0.0
                if wait > 0:
                    self.sleep(wait)
            permitted = self.not_before - self.wall()
            if permitted > 0:
                self.sleep(permitted)
            delay = self.last + self.spacing - self.now()
            if delay > 0:
                self.sleep(delay)
            seq = self._charge(request)
            self.last = self.now()
            receipt, facts, asked = self._attempt(seq, request, reduce)
            if asked is not None and receipt["outcome"] == "error" and asked <= MAX_RETRY_AFTER_S:
                self.not_before = max(self.not_before, self.wall() + asked)
            disk = dict(receipt)
            if keep_facts and receipt["outcome"] in ("ok", "absent"):
                disk["facts"] = facts
            publish_once(self.root / "receipts" / f"{seq:05d}.json", disk)
            self.receipts.append(disk)
            tries += 1
            outcome = receipt["outcome"]
            if receipt.get("reserve_exceeded"):
                reason = f"{request.cls}: a transport chunk was larger than the read reserve"
                self.record_stop(reason)
                raise FetchStoppedError(reason)
            if outcome == "forbidden":
                self.record_stop(f"{request.cls}: HTTP 403")
                raise FetchStoppedError(f"{request.cls}: HTTP 403, stopped without retry")
            if outcome == "schema_mismatch":
                reason = f"{request.cls}: schema mismatch ({receipt['reason']})"
                self.record_stop(reason)
                raise FetchStoppedError(reason)
            if outcome == "error":
                if receipt["http_status"] == 429:
                    self.spacing = min(MAX_SPACING_S, self.spacing * 2)
                if tries < ATTEMPTS_PER_REQUEST:
                    continue
                result = {**receipt, "outcome": "unavailable", "facts": None}
            else:
                result = {**receipt, "facts": facts}
            self.final[request.key] = result
            return result

    def _attempt(
        self,
        seq: int,
        request: Request,
        reduce: Callable[[bytes], dict[str, Any]] | None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None, int | None]:
        spec = CLASS_BY_ID[request.cls]
        receipt: dict[str, Any] = {
            "seq": seq,
            "cls": request.cls,
            "key": request.key,
            "symbol": request.symbol,
            "meta": request.meta_dict(),
        }
        status, size, asked = 0, 0, None
        error: str | None = None
        reason = ""
        sha = hashlib.sha256()
        body = bytearray()
        streamed = FirstFileReducer() if request.cls == "D2" else None
        facts: dict[str, Any] | None = None
        try:
            params = dict(request.params) or None
            headers = {"Accept-Encoding": "identity"}  # the bytes read are the bytes counted
            with self.client.stream(
                request.method, request.url, params=params, headers=headers
            ) as resp:
                status = resp.status_code
                header = resp.headers.get("retry-after", "")
                asked = int(header) if header.isdigit() else None
                length = resp.headers.get("content-length", "")
                if request.method == "HEAD":
                    facts = {"content_length": int(length) if length.isdigit() else None}
                elif status == 200:
                    if resp.headers.get("content-encoding", "identity") not in ("", "identity"):
                        raise SchemaMismatchError("content_encoding")
                    cap = spec.max_response_bytes
                    if length.isdigit() and int(length) > cap:
                        error = "too_large"  # the declared length is over the cap: read nothing
                    else:
                        # Raw transport chunks, not re-chunked, so what the transport delivered
                        # is exactly what is counted. Every received byte is accounted, and the
                        # read ends at the first chunk that takes the total over the cap.
                        for piece in resp.iter_raw():
                            size += len(piece)
                            sha.update(piece)
                            if len(piece) > READ_RESERVE_BYTES:
                                # the transport broke its 64 KiB read: the hard ceiling can no
                                # longer be guaranteed. The bytes are counted, and the run stops.
                                receipt["reserve_exceeded"] = True
                            if size > cap:
                                error = "too_large"
                                break
                            if streamed is not None:
                                streamed.feed(piece)
                            else:
                                body.extend(piece)
        except httpx.HTTPError:
            error = "error"  # never the exception text, URL or body
        except SchemaMismatchError as exc:
            error, reason = "schema_mismatch", exc.reason
        receipt.update(
            http_status=status,
            body_bytes=size,
            body_sha256=sha.hexdigest() if size else "",
            at_epoch=self.wall(),
        )
        if asked is not None:
            receipt["retry_after_s"] = asked
        if error is None:
            error = classify_status(request, status, bytes(body))
        if error is None and status == 200 and request.method == "GET":
            try:
                facts = (
                    streamed.finish() if streamed is not None else (reduce or _empty)(bytes(body))
                )
                if request.cls not in self.probed and request.url.startswith(BASE_API):
                    receipt["schema"] = key_tree_of(bytes(body))
                    self.probed.add(request.cls)
            except SchemaMismatchError as exc:
                error, reason = "schema_mismatch", exc.reason
        if error is None:
            receipt["outcome"] = "absent" if status == 404 else "ok"
        else:
            receipt["outcome"] = error
            if reason:
                receipt["reason"] = reason
            if status == 200 and error == "schema_mismatch":
                receipt["schema"] = key_tree_of(bytes(body))
        return receipt, facts, asked

    def summary(self) -> dict[str, Any]:
        count, used, by_n, by_b = self.totals()
        outcomes: dict[str, dict[str, int]] = {}
        for r in self.final.values():
            per = outcomes.setdefault(r["cls"], {})
            per[r["outcome"]] = per.get(r["outcome"], 0) + 1
        return {
            "go": self.go,
            "attempts": count,
            "bytes": used,
            "attempts_by_class": by_n,
            "bytes_by_class": by_b,
            "outcomes": outcomes,
        }


def _empty(_body: bytes) -> dict[str, Any]:
    return {}


def key_tree_of(body: bytes) -> list[str]:
    try:
        return key_tree(json.loads(body))
    except ValueError:
        return ["<not json>"]


def _json_reducer(fn: Callable[[Any], dict[str, Any]]) -> Callable[[bytes], dict[str, Any]]:
    def reduce(body: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise SchemaMismatchError("not_json") from exc
        return fn(payload)

    return reduce


def _listing_reducer(symbol: str) -> Callable[[bytes], dict[str, Any]]:
    def reduce(body: bytes) -> dict[str, Any]:
        return reduce_symbol_listing(symbol, _text(body))

    return reduce


def _kline_reducer(
    symbol: str, width: int, step_ms: int, limit: int
) -> Callable[[bytes], dict[str, Any]]:
    return _json_reducer(lambda payload: reduce_kline(payload, symbol, width, step_ms, limit))


def _funding_reducer(symbol: str) -> Callable[[bytes], dict[str, Any]]:
    return _json_reducer(lambda payload: reduce_funding(payload, symbol))


def _text(body: bytes) -> str:
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SchemaMismatchError("not_utf8") from exc


# The phases.


def _all_linear_symbols(payload: Any) -> set[str]:
    out: set[str] = set()
    for item in _envelope(payload).get("list", []):
        symbol = item.get("symbol") if isinstance(item, dict) else None
        if (
            isinstance(symbol, str)
            and SYMBOL.fullmatch(symbol)
            and item.get("contractType") == "LinearPerpetual"
        ):
            out.add(symbol)
    return out


def _catalog_reducer(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise SchemaMismatchError("not_json") from exc
    page = reduce_catalog_page(payload)
    # In memory only: the whole catalog, post-cut symbols included, so that the root
    # listing can be reduced against it. keep_facts=False keeps it out of every receipt.
    return {**page, "all_symbols": sorted(_all_linear_symbols(payload))}


def run_phase0(fetcher: Fetcher) -> dict[str, Any]:
    """C1 and C3 as one atomic phase whose facts live in one file, written once.

    The root listing is reduced in memory against the whole catalog (post-cut symbols
    included), so no receipt of this phase holds a reduced fact and an interrupted phase
    starts again from its first request as a new round.
    """
    path = fetcher.root / "phase0.json"
    if path.exists():
        return json.loads(path.read_text())  # type: ignore[no-any-return]
    round_no = fetcher.next_round()
    rows: dict[str, dict[str, Any]] = {}
    prior: dict[str, dict[str, Any]] = {}
    symbols: set[str] = set()
    for status in CATALOG_STATUSES:
        cursor = ""
        for _ in range(MAX_CATALOG_PAGES):
            result = fetcher.run_request(
                catalog_request(status, cursor, round_no), _catalog_reducer, keep_facts=False
            )
            if result["outcome"] != "ok":
                reason = f"C1 {status}: {result['outcome']}"
                fetcher.record_stop(reason)
                raise FetchStoppedError(reason)
            facts = result["facts"]
            symbols.update(facts["all_symbols"])
            rows.update({r["symbol"]: r for r in facts["rows"]})
            prior.update({r["symbol"]: r for r in facts["prior"]})
            cursor = facts["next_cursor"]
            if not cursor:
                break
        else:
            reason = f"C1 {status}: more pages than the contract allows"
            fetcher.record_stop(reason)
            raise FetchStoppedError(reason)
    result = fetcher.run_request(
        root_request(round_no),
        lambda body: reduce_root_listing(_text(body), symbols),
        keep_facts=False,
    )
    if result["outcome"] != "ok":
        reason = f"C3: {result['outcome']}"
        fetcher.record_stop(reason)
        raise FetchStoppedError(reason)
    phase0 = {
        "catalog": {
            "rows": sorted(rows.values(), key=lambda r: (r["launch_ms"], r["symbol"])),
            "prior": sorted(prior.values(), key=lambda r: r["symbol"]),
        },
        "archive": result["facts"],
    }
    publish_once(path, phase0)
    return phase0


def results_by_symbol(fetcher: Fetcher, cls: str) -> dict[str, dict[str, Any]]:
    return {r["symbol"]: r for r in fetcher.final.values() if r["cls"] == cls and r["symbol"]}


def funding_pages(fetcher: Fetcher) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    pages = (r for r in fetcher.final.values() if r["cls"] == "D6")
    for r in sorted(pages, key=lambda r: (r["symbol"], int(r["meta"]["page"]))):
        out.setdefault(r["symbol"], []).append(r)
    return out


def _publish_plan(fetcher: Fetcher, name: str, requests: list[Request]) -> None:
    """The derived request list of a phase: count and key hash, written before it runs."""
    path = fetcher.root / "plans" / f"{name}.json"
    if not path.exists():
        keys = sorted(r.key for r in requests)
        publish_once(path, {"requests": len(requests), "keys_sha256": digest(canonical(keys))})


def go2_upper_bounds(phase0: dict[str, Any], listings: dict[str, dict[str, Any]]) -> int:
    """Launches that could still become entry candidates: a first file within a day of
    launchTime and no catalog or file-list contradiction. Used only to size go 2."""
    contradicted = catalog_contradictions(phase0)
    n = 0
    for row in in_window_launches(phase0):
        listing = listings.get(row["symbol"])
        if not listing or listing.get("outcome") != "ok" or row["symbol"] in contradicted:
            continue
        facts = listing["facts"]
        if needs_first_file(facts["first_day"], row["launch_ms"]) and not ticker_reuse(
            facts, row["launch_ms"]
        ):
            n += 1
    return n


def run_go1(fetcher: Fetcher) -> dict[str, Any]:
    """Go 1, metadata only: catalog, archive names, listings, HEAD sizes. No file body.

    It ends by writing `manifest.json` (the exact D2 list and the preflight) and
    `go1_done.json`. The owner reviews the manifest before go 2.
    """
    if fetcher.go != 1:
        raise ValueError("run_go1 needs a go 1 fetcher")
    try:
        phase0 = run_phase0(fetcher)
        requests = listing_requests(phase0)
        _publish_plan(fetcher, "phase1_listings", requests)
        for request in requests:
            fetcher.run_request(request, _listing_reducer(request.symbol))
        listings = results_by_symbol(fetcher, "D1")
        requests = head_requests(phase0, listings)
        _publish_plan(fetcher, "phase2_heads", requests)
        for request in requests:
            fetcher.run_request(request)
        heads = results_by_symbol(fetcher, "D2h")
    except (FetchStoppedError, BudgetExceededError) as exc:
        fetcher.record_stop(str(exc))
        raise
    plan = preflight(heads)
    first_files = first_file_requests(heads, plan)
    summary_plan: dict[str, Any] = {k: v for k, v in plan.items() if k != "sizes"}
    summary_plan["over_per_file_cap"] = len(plan["over_per_file_cap"])
    summary_plan["no_size"] = len(plan["no_size"])
    manifest: dict[str, Any] = {
        "launches": len(in_window_launches(phase0)),
        "archive_candidates": len(phase0["archive"]["candidates"]),
        "d2_requests": len(first_files),
        "d2_keys_sha256": digest(canonical(sorted(r.key for r in first_files))),
        "preflight": summary_plan,
        "preflight_sizes_sha256": digest(canonical(plan["sizes"])),
        "go2_entry_candidates_upper_bound": go2_upper_bounds(phase0, listings),
    }
    for name, body in (("preflight.json", plan), ("manifest.json", manifest)):
        if not (fetcher.root / name).exists():
            publish_once(fetcher.root / name, body)
    if not (fetcher.root / "go1_done.json").exists():
        publish_once(fetcher.root / "go1_done.json", {"go": 1})
    return {**fetcher.summary(), "manifest": manifest}


def manifest_sha256(root: Path) -> str:
    return digest((root / "manifest.json").read_bytes())


def run_go2(fetcher: Fetcher, approved_manifest_sha256: str) -> dict[str, Any]:
    """Go 2: first files, entry bars, hourly series and funding. Needs a finished go 1
    and the SHA-256 of the manifest the owner reviewed."""
    if fetcher.go != 2:
        raise ValueError("run_go2 needs a go 2 fetcher")
    root = fetcher.root
    if not (root / "go1_done.json").exists() or not (root / "manifest.json").exists():
        raise FetchStoppedError("go 2 needs a finished go 1 in this root")
    if manifest_sha256(root) != approved_manifest_sha256:
        raise FetchStoppedError("the manifest is not the one that was approved")
    manifest = json.loads((root / "manifest.json").read_text())
    if not manifest["preflight"]["within_cap"]:
        fetcher.record_stop("preflight: D2 bytes above the class cap, no file read")
        raise FetchStoppedError("preflight: D2 bytes above the class cap, no file read")
    try:
        phase0 = json.loads((root / "phase0.json").read_text())
        listings = results_by_symbol(fetcher, "D1")
        heads = results_by_symbol(fetcher, "D2h")
        plan = preflight(heads)
        requests = first_file_requests(heads, plan)
        if digest(canonical(sorted(r.key for r in requests))) != manifest["d2_keys_sha256"]:
            raise FetchStoppedError("the D2 list differs from the approved manifest")
        _publish_plan(fetcher, "phase2_first_files", requests)
        for request in requests:
            fetcher.run_request(request)
        firsts = results_by_symbol(fetcher, "D2")
        candidates = entry_candidates(phase0, listings, firsts)
        requests = bar_requests(candidates)
        _publish_plan(fetcher, "phase3_bars", requests)
        shape = {"D3": (7, MINUTE_MS, 1), "D4": (5, HOUR_MS, 1000), "D5": (7, HOUR_MS, 1000)}
        for request in requests:
            width, step, limit = shape[request.cls]
            fetcher.run_request(request, _kline_reducer(request.symbol, width, step, limit))
        delivered = deliveries(phase0)
        for round_no in range(1, FUNDING_MAX_PAGES + 1):
            requests = funding_requests(candidates, funding_pages(fetcher), delivered)
            if not requests:
                break
            _publish_plan(fetcher, f"phase3_funding_round{round_no}", requests)
            for request in requests:
                fetcher.run_request(request, _funding_reducer(request.symbol))
    except (FetchStoppedError, BudgetExceededError) as exc:
        fetcher.record_stop(str(exc))
        raise
    if not (root / "go2_done.json").exists():
        publish_once(root / "go2_done.json", {"go": 2})
    return fetcher.summary()


# Binding and command line.


def binding(prior_roots: Iterable[Path] = ()) -> dict[str, Any]:
    published = b3.publication(list(PUBLISHED))
    if f"`{STUDY_ID}`" not in Path(REGISTRY).read_text():
        raise ValueError("no seen-windows registry row names this study")
    saved = json.loads(Path(CONTRACT_PATH).read_text())
    if saved != build_contract():
        raise ValueError("the checked-in contract differs from this code")
    return {
        "rules": VERSION,
        "revision": transport.clean_revision(),
        "publication": published,
        "request_list_sha256": saved["request_list_sha256"],
        "prior_roots": [str(p) for p in prior_roots],
    }


def _emit(text: str) -> None:
    sys.stdout.write(text + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan", help="print the contract summary and its hashes; no network")
    write = sub.add_parser("write-contract", help="write or check the frozen contract JSON")
    write.add_argument("--check", action="store_true")
    fetch = sub.add_parser("fetch", help="run one go; needs the owner's go for that go")
    fetch.add_argument("--go", type=int, choices=(1, 2), required=True)
    fetch.add_argument("--approved-sha256", required=True, help="the go_sha256 the owner approved")
    fetch.add_argument("--manifest-sha256", help="go 2 only: the reviewed manifest hash")
    fetch.add_argument("--root", type=Path, required=True)
    fetch.add_argument("--prior-root", action="append", type=Path, default=[])
    args = parser.parse_args(argv)
    contract = build_contract()
    if args.command == "plan":
        summary = {
            "rules": VERSION,
            "request_list_sha256": contract["request_list_sha256"],
            "go_sha256": contract["go_sha256"],
            "go_limits": contract["go_limits"],
            "classes": {
                c["cls"]: {
                    "go": c["go"],
                    "expected": c["expected_requests"],
                    "max": c["max_requests"],
                }
                for c in contract["classes"]
            },
        }
        _emit(json.dumps(summary, indent=2))
        return 0
    if args.command == "write-contract":
        path = Path(CONTRACT_PATH)
        if args.check:
            # Prettier reformats the file, so the check compares the parsed contract.
            return 0 if path.exists() and json.loads(path.read_text()) == contract else 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical(contract))
        return 0
    if args.approved_sha256 != contract["go_sha256"][str(args.go)]:
        raise ValueError("the approved hash is not this contract's hash for that go")
    bound = binding(args.prior_root)
    root: Path = args.root
    root.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=60.0, follow_redirects=False) as client:
        fetcher = Fetcher(
            root,
            contract,
            client,
            go=args.go,
            code=bound,
            prior=prior_usage(args.prior_root),
        )
        if args.go == 1:
            _emit(json.dumps(run_go1(fetcher), indent=2, sort_keys=True))
        else:
            if not args.manifest_sha256:
                raise ValueError("go 2 needs --manifest-sha256")
            _emit(json.dumps(run_go2(fetcher, args.manifest_sha256), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
