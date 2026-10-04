"""Edge-loss study, Gate tick tapes: Part A's timeline at seconds resolution.

docs/research/edge-loss-decomposition-v1.md ("Gate tick tapes" and amendment 1, point 5).
The population is the scanner's Gate pump sources first seen 2026-07-23..07-31 (the
scanner's records start on 07-23; the PR 3 boundary is 2026-08-01). Their July trade
files come once from Gate's public archive to the local machine, each with its sha256,
capped at 200 bases and 5 GiB.

Per source: the last second at or before `first_seen_at` at which the 24h change of the
last trade price crossed +20%, the 24h low before it (move start), the peak in the 24h
after it, the first 5-minute crossings of +3/+5/+10% between the low and the 24h
crossing, and the share of the move gone 0, 5, 15, 45 and 101 seconds after each
crossing, plus at the scanner's own `first_seen_at`. A source whose peak window leaves
July is censored. This describes Gate's own moves; it is not MEXC execution.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from array import array
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .edge_loss_study import (
    FIVE_MINUTE_THRESHOLDS,
    PUMP_THRESHOLD,
    SCANNER_NAME,
    quantiles,
    sha256_file,
)
from .source_lead_multi_source_report import load_verified, write_once

if TYPE_CHECKING:
    from collections.abc import Sequence

GATE_VERSION = "edge_loss_gate_tapes_v1"
ARCHIVE = "https://download.gatedata.org/futures_usdt/trades/202607/{symbol}-202607.csv.gz"
MONTH_START = datetime(2026, 7, 1, tzinfo=UTC)
MONTH_END = datetime(2026, 8, 1, tzinfo=UTC)
SOURCES_FROM = datetime(2026, 7, 23, tzinfo=UTC)
MAX_BASES = 200
MAX_BYTES = 5 * 1024**3
DELAYS_S = (0, 5, 15, 45, 101)
DAY_S = 86_400
TAPES_NAME = "gate-tapes.json"
RESULT_NAME = "gate-result.json"


_UNIFIED = re.compile(r"^([A-Z0-9]+)/USDT:USDT$")


def native_symbol(symbol: str) -> str | None:
    """The scanner stores Gate sources in the unified form (`IDOL/USDT:USDT`); the
    archive names files by Gate's native id (`IDOL_USDT`). Anything else is unmapped."""
    match = _UNIFIED.match(symbol)
    return f"{match.group(1)}_USDT" if match else None


def gate_sources(scanner: dict[str, Any]) -> list[dict[str, Any]]:
    """Gate sources of the population, keyed by the native id (None when unmapped)."""
    out = []
    for source in scanner["sources"]:
        seen = datetime.fromisoformat(source["first_seen_at"])
        if source["exchange"] == "gate" and SOURCES_FROM <= seen < MONTH_END:
            out.append({**source, "first_seen_at": seen, "native": native_symbol(source["symbol"])})
    return out


# ---------------------------------------------------------------- fetch


def fetch_tapes(stage_dir: Path, tapes_dir: Path) -> dict[str, Any]:
    import httpx

    scanner, scanner_sha = load_verified(stage_dir / SCANNER_NAME)
    sources = gate_sources(scanner)
    symbols = sorted({s["native"] for s in sources if s["native"]})
    tapes_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, Any] = {}
    total = 0
    status: Counter[str] = Counter()
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        for symbol in symbols[:MAX_BASES]:
            path = tapes_dir / f"{symbol}-202607.csv.gz"
            if not path.exists():
                with client.stream("GET", ARCHIVE.format(symbol=symbol)) as response:
                    if response.status_code == 404:
                        files[symbol] = {"status": "not_in_archive"}
                        status["not_in_archive"] += 1
                        continue
                    response.raise_for_status()
                    partial = path.with_suffix(".partial")
                    with partial.open("wb") as out:
                        for block in response.iter_bytes():
                            total += len(block)
                            if total > MAX_BYTES:
                                partial.unlink()
                                raise SystemExit("the 5 GiB download cap was reached")
                            out.write(block)
                    partial.rename(path)
            else:
                total += path.stat().st_size
            files[symbol] = {
                "status": "ok",
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
            status["ok"] += 1
    return {
        "gate_version": GATE_VERSION,
        "scanner_sha256": scanner_sha,
        "bases_in_population": len(symbols),
        "sources_unmapped": sum(1 for s in sources if not s["native"]),
        "bases_over_cap": max(0, len(symbols) - MAX_BASES),
        "status": dict(status),
        "files": files,
    }


# ---------------------------------------------------------------- tape


class Tape:
    """Per-second last, min and max trade price over July, as compact arrays."""

    def __init__(self, trades: Sequence[tuple[float, float]]) -> None:
        n = int((MONTH_END - MONTH_START).total_seconds())
        self.start = MONTH_START.timestamp()
        self.last = array("d", [math.nan]) * n
        self.low = array("d", [math.inf]) * n
        self.high = array("d", [-math.inf]) * n
        for ts, price in trades:
            i = int(ts - self.start)
            if 0 <= i < n and price > 0:
                self.last[i] = price
                self.low[i] = min(self.low[i], price)
                self.high[i] = max(self.high[i], price)
        carried = math.nan
        for i in range(n):  # last trade price at or before each second
            if math.isnan(self.last[i]):
                self.last[i] = carried
            else:
                carried = self.last[i]

    def second(self, at: float) -> int:
        return int(at - self.start)

    def price(self, i: int) -> float | None:
        if 0 <= i < len(self.last) and not math.isnan(self.last[i]):
            return self.last[i]
        return None

    def change(self, i: int, back: int) -> float | None:
        now, then = self.price(i), self.price(i - back)
        return now / then - 1 if now and then else None


def read_tape(path: Path) -> list[tuple[float, float]]:
    import duckdb

    rows = (
        duckdb.connect()
        .execute(
            "SELECT column0::DOUBLE, column2::DOUBLE FROM read_csv(?, header = false,"
            " columns = {'column0': 'VARCHAR', 'column1': 'VARCHAR', 'column2': 'VARCHAR',"
            " 'column3': 'VARCHAR'}) WHERE TRY_CAST(column0 AS DOUBLE) IS NOT NULL"
            " ORDER BY 1",
            [str(path)],
        )
        .fetchall()
    )
    return [(float(t), float(p)) for t, p in rows]


def timeline(tape: Tape, first_seen: datetime) -> dict[str, Any]:
    seen = tape.second(first_seen.timestamp())
    if seen - 2 * DAY_S < 0:
        return {"status": "insufficient_history"}
    cross = None
    for i in range(seen, seen - DAY_S, -1):
        now, prior = tape.change(i, DAY_S), tape.change(i - 1, DAY_S)
        if now is not None and prior is not None and now >= PUMP_THRESHOLD > prior:
            cross = i
            break
    if cross is None:
        return {"status": "no_tape_crossing"}
    if cross + DAY_S >= len(tape.last):
        return {"status": "censored_peak"}
    low_i = min(range(cross - DAY_S, cross + 1), key=lambda i: tape.low[i])
    low = tape.low[low_i]
    peak = max(tape.high[cross : cross + DAY_S + 1])
    if not math.isfinite(low) or not math.isfinite(peak) or peak <= low:
        return {"status": "no_move"}

    def share(i: int) -> float | None:
        p = tape.price(i)
        return None if p is None else (p - low) / (peak - low)

    crossings: dict[str, int | None] = {}
    for k in FIVE_MINUTE_THRESHOLDS:
        crossings[f"5m_{k:g}"] = next(
            (
                i
                for i in range(low_i, cross + 1)
                if (r := tape.change(i, 300)) is not None and r >= k
            ),
            None,
        )
    crossings["24h_0.2"] = cross
    moments: dict[str, dict[str, float | None]] = {}
    for name, at in crossings.items():
        if at is None:
            moments[name] = {"status_not_crossed": 1.0}
            continue
        row: dict[str, float | None] = {"seconds_after_move_start": float(at - low_i)}
        for delay in DELAYS_S:
            row[f"after_{delay}s"] = share(at + delay)
        if name == "24h_0.2":
            row["scanner_first_seen"] = share(seen)
            row["scanner_lag_s"] = first_seen.timestamp() - (tape.start + at)
        moments[name] = row
    return {"status": "ok", "moments": moments}


def read(stage_dir: Path, tapes_dir: Path) -> dict[str, Any]:
    if (stage_dir / RESULT_NAME).exists():
        raise SystemExit(f"{stage_dir / RESULT_NAME} exists: read once")
    tapes, tapes_sha = load_verified(stage_dir / TAPES_NAME)
    scanner, scanner_sha = load_verified(stage_dir / SCANNER_NAME)
    if tapes["scanner_sha256"] != scanner_sha:
        raise ValueError("the tapes were fetched for another scanner export")
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for source in gate_sources(scanner):
        by_symbol[source["native"] or "unmapped"].append(source)
    statuses: Counter[str] = Counter()
    values: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    not_crossed: Counter[str] = Counter()
    for symbol, sources in sorted(by_symbol.items()):
        if symbol == "unmapped":
            statuses["unmapped_symbol"] += len(sources)
            continue
        entry = tapes["files"].get(symbol)
        if entry is None or entry.get("status") != "ok":
            statuses["no_tape"] += len(sources)
            continue
        path = tapes_dir / f"{symbol}-202607.csv.gz"
        if sha256_file(path) != entry["sha256"]:
            raise ValueError(f"{path.name} does not match its recorded sha256")
        tape = Tape(read_tape(path))
        for source in sources:
            got = timeline(tape, source["first_seen_at"])
            statuses[got["status"]] += 1
            if got["status"] != "ok":
                continue
            for threshold, row in got["moments"].items():
                if "status_not_crossed" in row:
                    not_crossed[threshold] += 1
                    continue
                for moment, value in row.items():
                    if value is not None:
                        values[threshold][moment].append(value)
    result = {
        "gate_version": GATE_VERSION,
        "tapes_sha256": tapes_sha,
        "scanner_sources": sum(len(v) for v in by_symbol.values()),
        "statuses": dict(statuses),
        "not_crossed_before_24h_crossing": dict(not_crossed),
        "moments": {
            threshold: {moment: quantiles(v) for moment, v in sorted(moments.items())}
            for threshold, moments in sorted(values.items())
        },
    }
    write_once(stage_dir / RESULT_NAME, result)
    return result


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--phase", choices=("fetch", "read"), required=True)
    parser.add_argument("--stage-dir", type=Path, required=True)
    parser.add_argument("--tapes-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.phase == "fetch":
        if (args.stage_dir / TAPES_NAME).exists():
            raise SystemExit("the tapes are recorded once")
        tapes = fetch_tapes(args.stage_dir, args.tapes_dir)
        digest = write_once(args.stage_dir / TAPES_NAME, tapes)
        out = {k: tapes[k] for k in ("bases_in_population", "bases_over_cap", "status")}
        sys.stdout.write(json.dumps({"sha256": digest, **out}) + "\n")
        return
    result = read(args.stage_dir, args.tapes_dir)
    sys.stdout.write(json.dumps({"statuses": result["statuses"]}) + "\n")


if __name__ == "__main__":
    main()
