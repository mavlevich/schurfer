"""Stage 2C measurement v1: ETHUSDT funding mean (I35) and dated-futures basis (I26).

Contract: docs/research/stage-2c-measure-v1.md. Every threshold, window and request budget
below is copied from it and fixed before any request. The module reads two facts and nothing
else: the settled ETHUSDT funding of the discovery year, and the entry basis of Bybit's dated
BTC and ETH futures against spot. No trade return is computed.

Nothing here talks to a network from tests: the runner takes an injected ``httpx.Client``.
Every request is written to a journal (URL, status, byte count, SHA-256 of the body) and the
raw body is stored before any value is read from it. The run needs the owner's go for the
exact ``go_sha256`` that ``plan`` prints; a different hash refuses to start.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any, Final

import httpx

from .bybit_carry_feasibility import publish_once

VERSION: Final = "stage_2c_measure_v1"
DOC: Final = "docs/research/stage-2c-measure-v1.md"
BASE_URL: Final = "https://api.bybit.com"

DAY_MS: Final = 86_400_000
SETTLE_MS: Final = 8 * 3_600_000
BAR_MS: Final = 4 * 3_600_000
YEAR_DAYS: Final = 365
ROLLING_DAYS: Final = 90
ROLLING_SETTLEMENTS: Final = ROLLING_DAYS * 3

# The discovery year: 2024-10-01 00:00 UTC inclusive to 2025-10-01 00:00 UTC exclusive.
WINDOW_START_MS: Final = int(datetime(2024, 10, 1, tzinfo=UTC).timestamp() * 1000)
WINDOW_END_MS: Final = int(datetime(2025, 10, 1, tzinfo=UTC).timestamp() * 1000)

FUNDING_LIMIT: Final = 200
KLINE_LIMIT: Final = 1000
FUNDING_MAX_PAGES: Final = 8
KLINE_MAX_PAGES: Final = 4
CATALOG_MAX_PAGES: Final = 2
MAX_CYCLES: Final = 12
QUARTER_MONTHS: Final = (3, 6, 9, 12)
FRIDAY: Final = 4
CYCLE_RULE: Final = "quarterly_last_friday_one_per_expiry_earliest_listed"
ENTRY_SEARCH_MS: Final = 7 * DAY_MS
ENTRY_LIMIT: Final = 50

BUDGET: Final = 100
MAX_ATTEMPTS: Final = 3
MAX_BODY_BYTES: Final = 2 * 1024 * 1024

# I35 decision levels, percent a year (screens document, H = 8%, no interest on the leftover).
I35_PASS_F: Final = 14.50
I35_FAIL_F: Final = 5.64
# I26 decision levels, percent a year (alpha 0.50 conservative, alpha 0.90 favorable).
I26_PASS_A: Final = 11.82
I26_FAIL_A: Final = 6.62

ASSETS: Final = ("BTC", "ETH")


class StopRunError(RuntimeError):
    """The whole run stops without a verdict (transport, budget or size)."""


class PartStopError(RuntimeError):
    """One part (I35 or I26) stops without a verdict; the other still runs."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def canonical(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def ceil_bar(ms: int) -> int:
    return -(-ms // BAR_MS) * BAR_MS


# Pure analytics.


def funding_stats(
    rows: list[tuple[int, float]],
    start_ms: int = WINDOW_START_MS,
    end_ms: int = WINDOW_END_MS,
) -> dict[str, Any]:
    """Annualized mean funding and the worst rolling 90 day mean, percent a year.

    ``rows`` are (settlement ms, rate) with the short receiving a positive rate. A gap longer
    than one settlement interval inside the window, or an edge further than one interval from
    the window edge, is ``funding_gap`` and gives no statistic.
    """
    ordered = sorted(set(rows))
    stamps = [stamp for stamp, _ in ordered]
    if len(stamps) != len({stamp for stamp, _ in rows}):
        raise PartStopError("funding_conflict", "one settlement carries two different rates")
    if not ordered:
        raise PartStopError("funding_gap", "no settlements")
    if stamps[0] - start_ms > SETTLE_MS or end_ms - stamps[-1] > SETTLE_MS:
        raise PartStopError("funding_gap", "the series does not reach a window edge")
    for before, after in pairwise(stamps):
        if after - before > SETTLE_MS:
            raise PartStopError("funding_gap", f"gap before {after}")
    rates = [rate for _, rate in ordered]
    years = (end_ms - start_ms) / (YEAR_DAYS * DAY_MS)
    mean = sum(rates) / years * 100
    worst: float | None = None
    if len(rates) >= ROLLING_SETTLEMENTS:
        window = sum(rates[:ROLLING_SETTLEMENTS])
        low = window
        for index in range(ROLLING_SETTLEMENTS, len(rates)):
            window += rates[index] - rates[index - ROLLING_SETTLEMENTS]
            low = min(low, window)
        worst = low * (YEAR_DAYS / ROLLING_DAYS) * 100
    return {"n": len(rates), "f_mean_pct": mean, "f_90min_pct": worst}


def i35_outcome(f_mean: float, f_90min: float | None) -> str:
    if f_mean >= I35_PASS_F and f_90min is not None and f_90min > 0:
        return "i35_pass_protocol"
    if f_mean < I35_FAIL_F:
        return "i35_fail"
    return "i35_account_dependent"


def entry_basis(future: float, spot: float, days: float) -> float:
    """Annualized basis on notional, percent: (F / S - 1) * 365 / d."""
    if spot <= 0 or future <= 0 or days <= 0:
        raise ValueError("positive prices and days to expiry are required")
    return (future / spot - 1) * YEAR_DAYS / days * 100


def i26_asset_outcome(bases: list[float | None]) -> dict[str, Any]:
    """``None`` is a cycle with no usable entry: it counts in the denominator and never passes."""
    cycles = len(bases)
    over_pass = sum(1 for a in bases if a is not None and a >= I26_PASS_A)
    over_fail = sum(1 for a in bases if a is not None and a >= I26_FAIL_A)
    negative = sum(1 for a in bases if a is not None and a < 0)
    if cycles == 0:
        outcome = "i26_no_cycles"
    elif over_pass * 2 >= cycles and negative == 0:
        outcome = "i26_pass_protocol"
    elif over_fail * 2 < cycles:
        outcome = "i26_fail"
    else:
        outcome = "i26_regime_dependent"
    return {
        "cycles": cycles,
        "at_least_pass_level": over_pass,
        "at_least_fail_level": over_fail,
        "negative": negative,
        "unusable": sum(1 for a in bases if a is None),
        "outcome": outcome,
    }


# Parsing, strict about shape, silent about everything the contract does not use.


def _result(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("retCode") != 0:
        raise ValueError("not a successful Bybit envelope")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ValueError("no result object")
    return result


def parse_funding(payload: Any, symbol: str) -> list[tuple[int, float]]:
    rows = _result(payload).get("list")
    if not isinstance(rows, list):
        raise ValueError("funding list")
    out: list[tuple[int, float]] = []
    for item in rows:
        if not isinstance(item, dict) or item.get("symbol") != symbol:
            raise ValueError("funding row")
        out.append((int(item["fundingRateTimestamp"]), float(item["fundingRate"])))
    return out


def parse_klines(payload: Any, symbol: str) -> list[tuple[int, float]]:
    result = _result(payload)
    rows = result.get("list")
    if result.get("symbol") != symbol or not isinstance(rows, list):
        raise ValueError("kline result")
    out: list[tuple[int, float]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 5:
            raise ValueError("kline row")
        out.append((int(row[0]), float(row[4])))
    return out


@dataclass(frozen=True)
class Instrument:
    symbol: str
    base: str
    launch_ms: int
    delivery_ms: int


def parse_catalog(payload: Any, base: str) -> tuple[list[Instrument], str]:
    result = _result(payload)
    rows = result.get("list")
    if not isinstance(rows, list):
        raise ValueError("catalog list")
    found: list[Instrument] = []
    for item in rows:
        if not isinstance(item, dict):
            raise ValueError("catalog row")
        if item.get("contractType") != "LinearFutures" or item.get("baseCoin") != base:
            continue
        found.append(
            Instrument(
                symbol=str(item["symbol"]),
                base=base,
                launch_ms=int(item["launchTime"]),
                delivery_ms=int(item["deliveryTime"]),
            )
        )
    return found, str(result.get("nextPageCursor") or "")


def is_quarterly(delivery_ms: int) -> bool:
    """Delivery on the last Friday of March, June, September or December (UTC)."""
    day = datetime.fromtimestamp(delivery_ms / 1000, UTC)
    return (
        day.month in QUARTER_MONTHS and day.weekday() == FRIDAY and day.day + 7 > _month_days(day)
    )


def _month_days(day: datetime) -> int:
    first_next = datetime(day.year + (day.month == 12), day.month % 12 + 1, 1, tzinfo=UTC)
    return (first_next - timedelta(days=1)).day


def quarterly_cycles(instruments: list[Instrument]) -> tuple[list[Instrument], dict[str, int]]:
    """Amendment 1: one quarterly contract per asset and expiry, the earliest listed.

    Bybit lists weekly, monthly and quarterly contracts, and two settle-coin series for the
    same expiry. The cycle is the quarterly expiry; the instrument that listed first is kept.
    """
    excluded: dict[str, int] = {}
    chosen: dict[tuple[str, int], Instrument] = {}
    for item in sorted(instruments, key=lambda i: (i.base, i.delivery_ms, i.launch_ms, i.symbol)):
        if not is_quarterly(item.delivery_ms):
            excluded["not_quarterly"] = excluded.get("not_quarterly", 0) + 1
        elif (item.base, item.delivery_ms) in chosen:
            excluded["duplicate_expiry"] = excluded.get("duplicate_expiry", 0) + 1
        else:
            chosen[(item.base, item.delivery_ms)] = item
    return list(chosen.values()), excluded


def cycle_entry(instrument: Instrument) -> tuple[int | None, str | None]:
    """The first allowed entry bar start and, if it is outside the window, the reason."""
    target = ceil_bar(max(instrument.delivery_ms - ROLLING_DAYS * DAY_MS, instrument.launch_ms))
    if target < WINDOW_START_MS:
        return None, "entry_before_window"
    if target >= WINDOW_END_MS:
        return None, "entry_after_window"
    return target, None


# Requests, journal and budget.


@dataclass
class Run:
    root: Path
    client: httpx.Client
    pace_s: float = 0.25
    count: int = 0
    labels: list[str] = field(default_factory=list)

    def _journal(self, entry: dict[str, Any]) -> None:
        with (self.root / "journal.jsonl").open("a") as stream:
            stream.write(json.dumps(entry, sort_keys=True) + "\n")

    def get(self, label: str, path: str, params: dict[str, Any], *, soft: bool = False) -> Any:
        """One page. ``soft`` turns a definitive API refusal into ``None`` instead of a stop."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if self.count >= BUDGET:
                raise StopRunError("budget_exceeded")
            self.count += 1
            if self.pace_s:
                time.sleep(self.pace_s)
            status, body = self._fetch(path, params)
            digest = hashlib.sha256(body).hexdigest()
            n = self.count
            (self.root / "raw").mkdir(exist_ok=True)
            (self.root / "raw" / f"{n:03d}.json").write_bytes(body)
            self._journal(
                {
                    "n": n,
                    "label": label,
                    "attempt": attempt,
                    "url": f"{path}?{httpx.QueryParams(params)}",
                    "status": status,
                    "bytes": len(body),
                    "sha256": digest,
                    "at": datetime.now(UTC).isoformat(),
                }
            )
            if status == 200:
                try:
                    payload = json.loads(body)
                except ValueError:
                    continue
                if isinstance(payload, dict) and payload.get("retCode") == 0:
                    self.labels.append(label)
                    return payload
                if soft and isinstance(payload, dict) and "retCode" in payload:
                    return None
        raise StopRunError(f"fetch_error: {label}")

    def _fetch(self, path: str, params: dict[str, Any]) -> tuple[int, bytes]:
        chunks: list[bytes] = []
        size = 0
        with self.client.stream("GET", BASE_URL + path, params=params) as response:
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    raise StopRunError("response_too_large")
                chunks.append(chunk)
            return response.status_code, b"".join(chunks)


def fetch_funding(run: Run, symbol: str = "ETHUSDT") -> list[tuple[int, float]]:
    rows: list[tuple[int, float]] = []
    end = WINDOW_END_MS - 1
    for page in range(1, FUNDING_MAX_PAGES + 1):
        payload = run.get(
            f"funding:{symbol}:{page}",
            "/v5/market/funding/history",
            {
                "category": "linear",
                "symbol": symbol,
                "startTime": WINDOW_START_MS,
                "endTime": end,
                "limit": FUNDING_LIMIT,
            },
        )
        batch = parse_funding(payload, symbol)
        rows.extend(batch)
        if len(batch) < FUNDING_LIMIT:
            return rows
        end = min(stamp for stamp, _ in batch) - 1
        if end < WINDOW_START_MS:
            return rows
    raise PartStopError("funding_pages_exceeded")


def fetch_series(run: Run, category: str, symbol: str) -> dict[int, float]:
    """The 4 h closes of one symbol over the window, keyed by bar start."""
    series: dict[int, float] = {}
    end = WINDOW_END_MS - 1
    for page in range(1, KLINE_MAX_PAGES + 1):
        payload = run.get(
            f"kline:{category}:{symbol}:{page}",
            "/v5/market/kline",
            {
                "category": category,
                "symbol": symbol,
                "interval": "240",
                "start": WINDOW_START_MS,
                "end": end,
                "limit": KLINE_LIMIT,
            },
        )
        batch = parse_klines(payload, symbol)
        series.update(batch)
        if len(batch) < KLINE_LIMIT:
            return series
        end = min(stamp for stamp, _ in batch) - 1
        if end < WINDOW_START_MS:
            return series
    raise PartStopError("kline_pages_exceeded", f"{category}:{symbol}")


def series_facts(series: dict[int, float]) -> dict[str, Any]:
    starts = sorted(series)
    missing = 0
    for before, after in pairwise(starts):
        missing += (after - before) // BAR_MS - 1
    return {
        "bars": len(starts),
        "missing_bars": missing,
        "first_ms": starts[0] if starts else None,
        "last_ms": starts[-1] if starts else None,
    }


def fetch_catalog(run: Run, base: str) -> list[Instrument]:
    found: list[Instrument] = []
    cursor = ""
    for page in range(1, CATALOG_MAX_PAGES + 1):
        params: dict[str, Any] = {
            "category": "linear",
            "baseCoin": base,
            "status": "Closed",
            "limit": 1000,
        }
        if cursor:
            params["cursor"] = cursor
        payload = run.get(f"catalog:{base}:{page}", "/v5/market/instruments-info", params)
        batch, cursor = parse_catalog(payload, base)
        found.extend(batch)
        if not cursor:
            return found
    raise PartStopError("catalog_too_large", base)


def fetch_entry_bar(run: Run, instrument: Instrument, entry_ms: int) -> tuple[int, float] | None:
    payload = run.get(
        f"entry:{instrument.symbol}",
        "/v5/market/kline",
        {
            "category": "linear",
            "symbol": instrument.symbol,
            "interval": "240",
            "start": entry_ms,
            "end": entry_ms + ENTRY_SEARCH_MS,
            "limit": ENTRY_LIMIT,
        },
        soft=True,
    )
    if payload is None:
        return None
    bars = [bar for bar in parse_klines(payload, instrument.symbol) if bar[0] >= entry_ms]
    return min(bars) if bars else None


# The two parts.


def measure_i35(run: Run) -> dict[str, Any]:
    stats = funding_stats(fetch_funding(run))
    coverage = series_facts(fetch_series(run, "linear", "ETHUSDT"))
    return {
        **stats,
        "outcome": i35_outcome(stats["f_mean_pct"], stats["f_90min_pct"]),
        "eth_kline_coverage": coverage,
    }


def measure_i26(run: Run) -> dict[str, Any]:
    instruments: list[Instrument] = []
    for base in ASSETS:
        listed = fetch_catalog(run, base)
        if not listed:
            raise PartStopError("dated_catalog_empty", base)
        instruments.extend(listed)
    cycles: list[tuple[Instrument, int]] = []
    quarterly, excluded = quarterly_cycles(instruments)
    for instrument in quarterly:
        entry, reason = cycle_entry(instrument)
        if entry is None:
            excluded[str(reason)] = excluded.get(str(reason), 0) + 1
        else:
            cycles.append((instrument, entry))
    if len(cycles) > MAX_CYCLES:
        raise PartStopError("too_many_cycles", str(len(cycles)))
    if not cycles:
        raise PartStopError("no_cycles_in_window")
    # The dated-history probe comes first: the first entry bar of the first cycle.
    bars: list[tuple[int, float] | None] = []
    for number, (instrument, entry) in enumerate(cycles):
        bar = fetch_entry_bar(run, instrument, entry)
        if bar is None and number == 0:
            raise PartStopError("dated_history_unavailable", instrument.symbol)
        bars.append(bar)
    spot = {base: fetch_series(run, "spot", f"{base}USDT") for base in ASSETS}
    rows: list[dict[str, Any]] = []
    by_asset: dict[str, list[float | None]] = {base: [] for base in ASSETS}
    for (instrument, _), bar in zip(cycles, bars, strict=True):
        basis: float | None = None
        note = "no_bar_near_entry"
        days: float | None = None
        if bar is not None:
            start, future = bar
            days = (instrument.delivery_ms - start) / DAY_MS
            note = "spot_bar_missing"
            if start in spot[instrument.base] and days > 0:
                basis = entry_basis(future, spot[instrument.base][start], days)
                note = "ok"
        by_asset[instrument.base].append(basis)
        rows.append(
            {
                "contract": instrument.symbol,
                "entry_bar_ms": bar[0] if bar else None,
                "days_to_expiry": days,
                "a_pct": basis,
                "note": note,
            }
        )
    per_asset = {base: i26_asset_outcome(values) for base, values in by_asset.items()}
    return {
        "cycles": rows,
        "excluded_cycles": excluded,
        "assets": per_asset,
        "idea_passes": all(item["outcome"] == "i26_pass_protocol" for item in per_asset.values()),
    }


# Plan, approval hash and run.


def plan() -> dict[str, Any]:
    return {
        "version": VERSION,
        "doc": DOC,
        "amendment": 1,
        "cycle_rule": CYCLE_RULE,
        "base_url": BASE_URL,
        "window_ms": [WINDOW_START_MS, WINDOW_END_MS],
        "budget": BUDGET,
        "max_attempts": MAX_ATTEMPTS,
        "max_body_bytes": MAX_BODY_BYTES,
        "thresholds": {
            "i35_pass_f": I35_PASS_F,
            "i35_fail_f": I35_FAIL_F,
            "i26_pass_a": I26_PASS_A,
            "i26_fail_a": I26_FAIL_A,
        },
        "endpoints": [
            "/v5/market/funding/history",
            "/v5/market/kline",
            "/v5/market/instruments-info",
        ],
        "symbols": {"funding": "ETHUSDT", "spot": [f"{b}USDT" for b in ASSETS], "dated": "Closed"},
        "expected_requests": {
            "i35": 9,
            "i26_catalog": 2,
            "i26_spot": 6,
            "i26_entry_max": MAX_CYCLES,
        },
    }


def go_sha256() -> str:
    return hashlib.sha256(canonical(plan())).hexdigest()


def prepare_root(root: Path) -> None:
    if root.resolve().is_relative_to(repo_root()):
        raise ValueError("the run root must be outside the repository")
    if root.exists() and any(root.iterdir()):
        raise ValueError("the run root must be new and empty")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)


def run_measurement(run: Run) -> dict[str, Any]:
    """Both parts, each stopping alone on its own reason; a transport stop ends the run."""
    report: dict[str, Any] = {
        "version": VERSION,
        "go_sha256": go_sha256(),
        "module_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "thresholds": plan()["thresholds"],
        "i35": None,
        "i26": None,
        "part_stops": {},
        "run_stop": None,
    }
    parts = (("i35", measure_i35), ("i26", measure_i26))
    try:
        for name, part in parts:
            try:
                report[name] = part(run)
            except PartStopError as exc:
                report["part_stops"][name] = exc.reason
    except StopRunError as exc:
        report["run_stop"] = str(exc)
    report["requests"] = run.count
    publish_once(run.root / "report.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan", help="print the plan and its go hash; no network")
    fetch = sub.add_parser("fetch", help="the one go; needs the owner's approval of the hash")
    fetch.add_argument("--approved-sha256", required=True)
    fetch.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "plan":
        sys.stdout.write(json.dumps({"plan": plan(), "go_sha256": go_sha256()}, indent=2) + "\n")
        return 0
    if args.approved_sha256 != go_sha256():
        raise ValueError("the approved hash is not this plan's go hash")
    prepare_root(args.root)
    with httpx.Client(timeout=60.0, follow_redirects=False) as client:
        report = run_measurement(Run(args.root, client))
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
