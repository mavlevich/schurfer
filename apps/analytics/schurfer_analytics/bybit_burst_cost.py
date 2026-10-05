"""Bybit 1-minute burst: executable cost history v1 (descriptive).

Protocol: docs/research/bybit-burst-cost-history-v1.md. Data before 2026-09-29 only.
Nothing here is a trading rule; quotes are not fills.

Phases:

- `fetch` downloads, once and locally, the Bybit order-book archive files of the
  instrument-days the frozen decay firings need (sha256, a 20 GiB cap that counts files
  on disk), and the public funding history of those instruments over the window.
- `read` rebuilds each book in file order, captures it at every entry moment and exit
  moment (never reading ahead of the moment), and writes the result once.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from statistics import fmean
from typing import TYPE_CHECKING, Any

from .clustered_inference import ClusterObservation, cluster_bootstrap_mean, derived_seed
from .edge_loss_study import BLIND_END, quantiles, sha256_file
from .source_lead_multi_source_report import load_verified, write_once

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

COST_VERSION = "bybit_burst_cost_history_v1"
ARCHIVE = "https://quote-saver.bycsi.com/orderbook/linear/{symbol}/{day}_{symbol}_ob200.data.zip"
FUNDING_URL = "https://api.bybit.com/v5/market/funding/history"
DECAY_FIRINGS_SHA256 = "cb3f8154bb43353b7869456dfcd89d1a951787dafc7d85ebfcb94c1f60a30211"
ENTRY_DELAYS_S = (0.0, 2.7, 5.0, 10.0, 46.0)
KEY_DELAYS_S = (2.7, 5.0)
EXIT_AFTER_S = 3600.0
NOTIONAL_USD = 50.0
FEE_BPS = 5.5
SCENARIO_ROUND_TRIP_BPS = 41.0
STALE_MS = 5_000
MAX_BYTES = 20 * 1024**3
BOOTSTRAP_ITERATIONS = 10_000
BOOTSTRAP_SEED = 20_261_006
BOOKS_NAME = "cost-books.json"
FUNDING_NAME = "cost-funding.json"
RESULT_NAME = "cost-result.json"
Levels = list[tuple[float, float]]


def contract_sha256() -> str:
    contract = {
        "version": COST_VERSION,
        "decay_firings_sha256": DECAY_FIRINGS_SHA256,
        "entry_delays_s": ENTRY_DELAYS_S,
        "key_delays_s": KEY_DELAYS_S,
        "exit_after_s": EXIT_AFTER_S,
        "notional_usd": NOTIONAL_USD,
        "fee_bps": FEE_BPS,
        "scenario_round_trip_bps": SCENARIO_ROUND_TRIP_BPS,
        "stale_ms": STALE_MS,
        "max_bytes": MAX_BYTES,
        "bootstrap": [BOOTSTRAP_ITERATIONS, BOOTSTRAP_SEED],
    }
    return hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()


def load_firings(path: Path) -> list[dict[str, Any]]:
    payload, sha = load_verified(path)
    if sha != DECAY_FIRINGS_SHA256:
        raise SystemExit("the firing list is not the decay study's frozen list; refusing")
    return list(payload["firings"])


def moments(firing: dict[str, Any]) -> dict[str, int]:
    """Entry and exit moments, epoch milliseconds."""
    b = (int(firing["bar_start"]) + 60) * 1000
    out = {f"entry_{d:g}": b + round(d * 1000) for d in ENTRY_DELAYS_S}
    out["exit"] = b + int(EXIT_AFTER_S * 1000)
    return out


def day_of(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).date().isoformat()


def book_days(firings: Iterable[dict[str, Any]]) -> list[tuple[str, str]]:
    return sorted({(f["symbol"], day_of(ms)) for f in firings for ms in moments(f).values()})


# ---------------------------------------------------------------- fetch


def fetch_books(firings: Sequence[dict[str, Any]], books_dir: Path) -> dict[str, Any]:
    import httpx

    needed = book_days(firings)
    late = [f"{s} {d}" for s, d in needed if date.fromisoformat(d) >= BLIND_END.date()]
    if late:
        raise SystemExit(f"days on or after the blind boundary: {', '.join(late)}")
    books_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, Any] = {}
    total = 0
    status: Counter[str] = Counter()

    def count(size: int) -> None:
        nonlocal total
        total += size
        if total > MAX_BYTES:
            raise SystemExit("the 20 GiB cap was reached (files on disk count too)")

    with httpx.Client(timeout=600, follow_redirects=True) as client:
        for symbol, day in needed:
            name = f"{day}_{symbol}_ob200.data.zip"
            path = books_dir / name
            if path.exists():
                count(path.stat().st_size)
            else:
                with client.stream("GET", ARCHIVE.format(symbol=symbol, day=day)) as response:
                    if response.status_code == 404:
                        files[name] = {"status": "not_in_archive"}
                        status["not_in_archive"] += 1
                        continue
                    response.raise_for_status()
                    partial = path.with_suffix(".partial")
                    try:
                        with partial.open("wb") as out:
                            for block in response.iter_bytes():
                                count(len(block))
                                out.write(block)
                    except SystemExit:
                        partial.unlink(missing_ok=True)
                        raise
                    partial.rename(path)
            files[name] = {
                "status": "ok",
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
            status["ok"] += 1
    return {"status": dict(status), "bytes": total, "files": files}


def fetch_funding(firings: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Settlements of each instrument over its firings' span, paged back from the end."""
    import httpx

    spans: dict[str, list[int]] = {}
    for f in firings:
        m = moments(f)
        lo, hi = m["entry_0"], m["exit"]
        span = spans.setdefault(f["symbol"], [lo, hi])
        span[0], span[1] = min(span[0], lo), max(span[1], hi)
    out: dict[str, Any] = {}
    with httpx.Client(timeout=60) as client:
        for symbol, (lo, hi) in sorted(spans.items()):
            if hi >= int(BLIND_END.timestamp() * 1000):
                raise SystemExit(f"{symbol}: a hold reaches the blind boundary")
            rows: dict[int, str] = {}
            end = hi
            bodies = []
            while True:
                response = client.get(
                    FUNDING_URL,
                    params={
                        "category": "linear",
                        "symbol": symbol,
                        "startTime": lo,
                        "endTime": end,
                        "limit": 200,
                    },
                )
                response.raise_for_status()
                bodies.append(hashlib.sha256(response.content).hexdigest())
                body = response.json()
                if body.get("retCode") != 0:
                    raise SystemExit(f"{symbol}: funding history retCode {body.get('retCode')}")
                page = body["result"]["list"]
                for item in page:
                    ts = int(item["fundingRateTimestamp"])
                    if item.get("symbol") == symbol and lo <= ts <= hi:
                        rows[ts] = item["fundingRate"]
                if len(page) < 200:
                    break
                end = min(int(item["fundingRateTimestamp"]) for item in page) - 1
                if end < lo:
                    break
            out[symbol] = {
                "span_ms": [lo, hi],
                "settlements": [[ts, rows[ts]] for ts in sorted(rows)],
                "response_sha256": bodies,
            }
    return out


# ---------------------------------------------------------------- book


class Book:
    """One instrument's order book, rebuilt from snapshot and delta messages."""

    def __init__(self) -> None:
        self.bids: dict[str, float] = {}
        self.asks: dict[str, float] = {}
        self.update_id: int | None = None
        self.ts = 0
        self.broken = True  # until the first snapshot

    def apply(self, message: dict[str, Any]) -> None:
        data = message["data"]
        self.ts = int(message["ts"])
        uid = int(data["u"])
        if message["type"] == "snapshot":
            self.bids = {p: float(q) for p, q in data["b"]}
            self.asks = {p: float(q) for p, q in data["a"]}
            self.broken = False
        else:
            if self.update_id is None or uid != self.update_id + 1:
                self.broken = True
            for side, levels in ((self.bids, data["b"]), (self.asks, data["a"])):
                for price, size in levels:
                    if float(size) == 0:
                        side.pop(price, None)
                    else:
                        side[price] = float(size)
        self.update_id = uid

    def capture(self, at_ms: int) -> dict[str, Any]:
        if self.broken:
            return {"status": "book_broken"}
        if at_ms - self.ts > STALE_MS:
            return {"status": "book_stale"}
        bids = sorted(((float(p), q) for p, q in self.bids.items()), reverse=True)
        asks = sorted((float(p), q) for p, q in self.asks.items())
        if not bids or not asks:
            return {"status": "book_empty"}
        return {"status": "ok", "age_ms": at_ms - self.ts, "bids": bids, "asks": asks}


def read_messages(path: Path) -> Iterable[dict[str, Any]]:
    with zipfile.ZipFile(path) as archive:
        (member,) = archive.namelist()
        with archive.open(member) as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)


def capture_file(path: Path, targets: Sequence[int]) -> dict[int, dict[str, Any]]:
    """The book at each target moment (ms), from one day's file in order."""
    pending = sorted(set(targets))
    out: dict[int, dict[str, Any]] = {}
    book = Book()
    i = 0
    for message in read_messages(path):
        ts = int(message["ts"])
        while i < len(pending) and pending[i] < ts:
            out[pending[i]] = book.capture(pending[i])  # state before this message
            i += 1
        if i == len(pending):
            break
        book.apply(message)
    while i < len(pending):
        out[pending[i]] = book.capture(pending[i])
        i += 1
    return out


# ---------------------------------------------------------------- measures


def vwap(
    levels: Levels, *, notional: float | None = None, base: float | None = None
) -> tuple[float, float] | None:
    """(average price, base quantity) for a notional or a base quantity walked through the
    levels; None when the levels are too thin."""
    remaining_notional, remaining_base = notional, base
    cost = qty = 0.0
    for price, size in levels:
        take = size
        if remaining_notional is not None:
            take = min(size, remaining_notional / price)
            remaining_notional -= take * price
        else:
            assert remaining_base is not None
            take = min(size, remaining_base)
            remaining_base -= take
        cost += take * price
        qty += take
        if (remaining_notional is not None and remaining_notional <= 1e-9) or (
            remaining_base is not None and remaining_base <= 1e-12
        ):
            return cost / qty, qty
    return None


def funding_bps(settlements: Sequence[Sequence[Any]], start_ms: int, end_ms: int) -> float:
    """A long pays positive funding: the sum of rates settled inside the hold, in bps."""
    return sum(float(rate) for ts, rate in settlements if start_ms < int(ts) <= end_ms) * 1e4


def measure(
    firing: dict[str, Any], books: dict[int, dict[str, Any]], settlements: Sequence[Sequence[Any]]
) -> dict[str, Any]:
    m = moments(firing)
    exit_book = books.get(m["exit"], {"status": "no_book"})
    out: dict[str, Any] = {"exit_status": exit_book["status"], "entries": {}}
    for d in ENTRY_DELAYS_S:
        key = f"{d:g}"
        entry_book = books.get(m[f"entry_{key}"], {"status": "no_book"})
        if entry_book["status"] != "ok":
            out["entries"][key] = {"status": entry_book["status"]}
            continue
        best_bid, best_ask = entry_book["bids"][0][0], entry_book["asks"][0][0]
        mid = (best_bid + best_ask) / 2
        bought = vwap(entry_book["asks"], notional=NOTIONAL_USD)
        if bought is None:
            out["entries"][key] = {"status": "depth_short"}
            continue
        entry_price, qty = bought
        cell: dict[str, Any] = {
            "status": "ok",
            "half_spread_bps": (best_ask - best_bid) / 2 / mid * 1e4,
            "entry_impact_bps": (entry_price / mid - 1) * 1e4,
        }
        if exit_book["status"] == "ok":
            sold = vwap(exit_book["bids"], base=qty)
            if sold is None:
                cell["status"] = "exit_depth_short"
            else:
                exit_mid = (exit_book["bids"][0][0] + exit_book["asks"][0][0]) / 2
                exit_price = sold[0]
                fund = funding_bps(settlements, m[f"entry_{key}"], m["exit"])
                cell["exit_impact_bps"] = (1 - exit_price / exit_mid) * 1e4
                cell["funding_bps"] = fund
                cell["round_trip_cost_bps"] = (
                    cell["entry_impact_bps"] + cell["exit_impact_bps"] + 2 * FEE_BPS + fund
                )
                cell["gross_exec_bps"] = (exit_price / entry_price - 1) * 1e4
                cell["net_bps"] = cell["gross_exec_bps"] - 2 * FEE_BPS - fund
        else:
            cell["status"] = exit_book["status"]
        out["entries"][key] = cell
    return out


def summarize(
    rows: Sequence[dict[str, Any]], seed: int, trade_means: dict[str, float | None]
) -> dict[str, Any]:
    out: dict[str, Any] = {"firings": len(rows), "by_entry_delay": {}}
    out["exit_status"] = dict(Counter(r["m"]["exit_status"] for r in rows))
    for d in ENTRY_DELAYS_S:
        key = f"{d:g}"
        cells = [(r, r["m"]["entries"][key]) for r in rows]
        done = [(r, c) for r, c in cells if "net_bps" in c]
        report: dict[str, Any] = {"status": dict(Counter(c["status"] for _, c in cells))}
        for field in (
            "half_spread_bps",
            "entry_impact_bps",
            "exit_impact_bps",
            "round_trip_cost_bps",
            "net_bps",
        ):
            values = [c[field] for _, c in done]
            report[field] = {"mean": fmean(values) if values else None, **quantiles(values)}
        report["share_cost_above_scenario"] = (
            sum(c["round_trip_cost_bps"] > SCENARIO_ROUND_TRIP_BPS for _, c in done) / len(done)
            if done
            else None
        )
        gross = [c["gross_exec_bps"] for _, c in done]
        report["gross_exec_mean_bps"] = fmean(gross) if gross else None
        report["trade_proxy_mean_bps"] = trade_means.get(key)
        if d in KEY_DELAYS_S and len(done) >= 2:
            for scheme, field in (("instrument", "symbol"), ("utc_day", "day")):
                boot = cluster_bootstrap_mean(
                    tuple(ClusterObservation(str(r[field]), c["net_bps"]) for r, c in done),
                    iterations=BOOTSTRAP_ITERATIONS,
                    seed=derived_seed(seed, f"{key}:{scheme}"),
                )
                report[f"net_ci95_by_{scheme}"] = [
                    boot.estimate.lower_bound,
                    boot.estimate.upper_bound,
                ]
            by_symbol: dict[str, float] = defaultdict(float)
            for r, c in done:
                by_symbol[r["symbol"]] += c["net_bps"]
            total = sum(by_symbol.values())
            top5 = sum(sorted(by_symbol.values(), reverse=True)[:5])
            report["top5_instrument_share_of_net"] = top5 / total if total else None
        out["by_entry_delay"][key] = report
    five = out["by_entry_delay"]["5"]
    median_cost = five["round_trip_cost_bps"]["median"]
    cis = [five.get(f"net_ci95_by_{s}") for s in ("instrument", "utc_day")]
    park = (median_cost is not None and median_cost > SCENARIO_ROUND_TRIP_BPS) or (
        all(ci is not None and ci[1] < 0 for ci in cis)
    )
    out["decision"] = "park_hyp030" if park else "no_decision"
    return out


def read(
    stage_dir: Path, books_dir: Path, firings_path: Path, decay_result: Path, revision: str
) -> dict[str, Any]:
    if (stage_dir / RESULT_NAME).exists():
        raise SystemExit(f"{stage_dir / RESULT_NAME} exists: read once")
    firings = load_firings(firings_path)
    books_record, books_sha = load_verified(stage_dir / BOOKS_NAME)
    funding_record, funding_sha = load_verified(stage_dir / FUNDING_NAME)
    for record in (books_record, funding_record):
        if record.get("contract_sha256") != contract_sha256():
            raise SystemExit("an input was made under another contract; refusing")
    trade_means = trade_proxy_means(decay_result)
    targets: dict[tuple[str, str], list[int]] = defaultdict(list)
    for f in firings:
        for ms in moments(f).values():
            targets[(f["symbol"], day_of(ms))].append(ms)
    captured: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for (symbol, day), wanted in sorted(targets.items()):
        name = f"{day}_{symbol}_ob200.data.zip"
        entry = books_record["files"].get(name, {})
        if entry.get("status") != "ok":
            continue
        path = books_dir / name
        if sha256_file(path) != entry["sha256"]:
            raise ValueError(f"{name} does not match its recorded sha256")
        captured[symbol].update(capture_file(path, wanted))
    rows = []
    for f in firings:
        settlements = funding_record["instruments"].get(f["symbol"], {}).get("settlements", [])
        m = measure(f, captured.get(f["symbol"], {}), settlements)
        rows.append(
            {
                "symbol": f["symbol"],
                "day": datetime.fromtimestamp(f["bar_start"], UTC).date().isoformat(),
                "m": m,
            }
        )
    result = {
        "cost_version": COST_VERSION,
        "contract_sha256": contract_sha256(),
        "reader_code_revision": revision,
        "books_sha256": books_sha,
        "funding_sha256": funding_sha,
        **summarize(rows, BOOTSTRAP_SEED, trade_means),
    }
    write_once(stage_dir / RESULT_NAME, result)
    return result


DECAY_RESULT_SHA256 = "30356acb870adea8bb1bd0d89d15abe37a13d41df1eb4ed2e50133fbd4c797a3"


def trade_proxy_means(decay_result: Path) -> dict[str, float | None]:
    """The decay study's mean g_trade(d), verified against its published sha256. It holds
    aggregates only, so the comparison is of means over possibly different resolved sets."""
    payload, sha = load_verified(decay_result)
    if sha != DECAY_RESULT_SHA256:
        raise SystemExit("the decay result is not the published one; refusing")
    curve = payload["decay_first_trade_after"]
    return {f"{d:g}": curve.get(f"{d:g}", {}).get("mean_bps") for d in ENTRY_DELAYS_S}


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--phase", choices=("fetch", "read"), required=True)
    parser.add_argument("--stage-dir", type=Path, required=True)
    parser.add_argument("--books-dir", type=Path, required=True)
    parser.add_argument("--firings", type=Path, required=True, help="decay-firings.json")
    parser.add_argument("--decay-result", type=Path, required=True, help="decay-result.json")
    parser.add_argument("--code-revision", default=None)
    args = parser.parse_args(argv)
    args.stage_dir.mkdir(parents=True, exist_ok=True)
    firings = load_firings(args.firings)
    if args.phase == "fetch":
        for name in (BOOKS_NAME, FUNDING_NAME):
            if (args.stage_dir / name).exists():
                raise SystemExit(f"{name} is recorded once")
        books = fetch_books(firings, args.books_dir)
        write_once(args.stage_dir / BOOKS_NAME, {"contract_sha256": contract_sha256(), **books})
        funding = fetch_funding(firings)
        write_once(
            args.stage_dir / FUNDING_NAME,
            {"contract_sha256": contract_sha256(), "instruments": funding},
        )
        sys.stdout.write(
            json.dumps({"books": books["status"], "funding_instruments": len(funding)}) + "\n"
        )
        return
    from .mexc_early_trigger_hyp029 import verified_revision

    result = read(
        args.stage_dir,
        args.books_dir,
        args.firings,
        args.decay_result,
        verified_revision(args.code_revision),
    )
    sys.stdout.write(json.dumps({"decision": result["decision"]}) + "\n")


if __name__ == "__main__":
    main()
