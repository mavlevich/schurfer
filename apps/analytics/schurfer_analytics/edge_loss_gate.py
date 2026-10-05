"""Edge-loss study, Gate tick tapes: Part A's timeline at seconds resolution.

docs/research/edge-loss-decomposition-v1.md ("Gate tick tapes", amendment 1 point 5 and
amendment 2). The population is the scanner's Gate pump sources first seen
2026-07-23..07-31 (the scanner's records start on 07-23; the PR 3 boundary is
2026-08-01).

**Identity** comes from the scanner's stored `market_id`, never from the ticker's
spelling. A source with no `market_id`, an identity conflict, a market type other than
`swap`, or an id outside Gate's `BASE_USDT` form is counted apart by reason. The July
trade files of the remaining ids come once from Gate's public archive to the local
machine, each with its sha256, capped at 200 ids and 5 GiB.

**Prices are point-in-time.** The price at a moment is the last trade at or before it,
never a later trade in the same second. Its age (the moment minus that trade's time)
is kept:

- a moment price older than MAX_PRICE_AGE_S is missing, and counted;
- the 24h-ago reference may be up to MAX_REFERENCE_AGE_S old.

Moments sit on whole-second boundaries.

**Per source:**

- the last boundary at or before `first_seen_at` at which the 24h change crossed +20%
  (the boundary before it valid and below +20%);
- the lowest trade in the 24h before that crossing (move start) and the highest trade
  in the 24h after it;
- the first 5-minute crossings of +3/+5/+10% between the low and the 24h crossing;
- the share of the move gone 0, 5, 15, 45 and 101 seconds after each crossing, and at
  the scanner's `first_seen_at`.

A source whose peak window leaves July is censored. This describes Gate's own moves; it
is not MEXC execution.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import os
import re
import sys
from array import array
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .edge_loss_study import FIVE_MINUTE_THRESHOLDS, PUMP_THRESHOLD, quantiles, sha256_file
from .source_lead_multi_source_report import load_verified, write_once

if TYPE_CHECKING:
    from collections.abc import Sequence

GATE_VERSION = "edge_loss_gate_tapes_v2"
ARCHIVE = "https://download.gatedata.org/futures_usdt/trades/202607/{symbol}-202607.csv.gz"
MONTH_START = datetime(2026, 7, 1, tzinfo=UTC)
MONTH_END = datetime(2026, 8, 1, tzinfo=UTC)
SOURCES_FROM = datetime(2026, 7, 23, tzinfo=UTC)
MAX_BASES = 200
MAX_BYTES = 5 * 1024**3
DELAYS_S = (0, 5, 15, 45, 101)
DAY_S = 86_400
FIVE_MINUTES_S = 300
MAX_PRICE_AGE_S = 60
MAX_REFERENCE_AGE_S = 3_600
IDENTITY_NAME = "gate-identity.json"
TAPES_NAME = "gate-tapes.json"
RESULT_NAME = "gate-result.json"
IDENTITY_SQL = """
SELECT event_id, symbol, market_id, market_type, identity_conflict, first_seen_at
FROM app.pump_event_sources
WHERE exchange = 'gate' AND first_seen_at >= :since AND first_seen_at < :until
ORDER BY first_seen_at, event_id
"""
_NATIVE = re.compile(r"^[A-Z0-9]+_USDT$")


def resolve_identity(row: dict[str, Any]) -> tuple[str | None, str]:
    """The stored market id, or None with the reason it cannot be used."""
    market_id = row.get("market_id")
    if not market_id:
        return None, "no_market_id"
    if row.get("identity_conflict"):
        return None, "identity_conflict"
    if row.get("market_type") != "swap":
        return None, "not_swap"
    if not _NATIVE.match(market_id):
        return None, "unexpected_market_id_form"
    return str(market_id), "ok"


def gate_sources(identity: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for row in identity["sources"]:
        native, reason = resolve_identity(row)
        seen = datetime.fromisoformat(row["first_seen_at"])
        out.append({**row, "first_seen_at": seen, "native": native, "identity": reason})
    return out


# ---------------------------------------------------------------- phases: identity, fetch


async def export_identity(db_url: str) -> list[dict[str, Any]]:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from .outcome_repository import async_database_url

    engine = create_async_engine(async_database_url(db_url))
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(IDENTITY_SQL), {"since": SOURCES_FROM, "until": MONTH_END}
            )
            return [
                {
                    "event_id": r.event_id,
                    "symbol": r.symbol,
                    "market_id": r.market_id,
                    "market_type": r.market_type,
                    "identity_conflict": bool(r.identity_conflict),
                    "first_seen_at": r.first_seen_at.astimezone(UTC).isoformat(),
                }
                for r in result
            ]
    finally:
        await engine.dispose()


def fetch_tapes(stage_dir: Path, tapes_dir: Path) -> dict[str, Any]:
    import httpx

    identity, identity_sha = load_verified(stage_dir / IDENTITY_NAME)
    sources = gate_sources(identity)
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
        "identity_sha256": identity_sha,
        "bases_in_population": len(symbols),
        "identity": dict(Counter(s["identity"] for s in sources)),
        "bases_over_cap": max(0, len(symbols) - MAX_BASES),
        "status": dict(status),
        "files": files,
    }


# ---------------------------------------------------------------- tape


class Tape:
    """One July trade tape, with point-in-time prices on whole-second boundaries.

    `price[k]` is the last trade at or before boundary k (MONTH_START + k seconds) and
    `age[k]` its age in seconds; a boundary before the first trade has no price.
    Lows and highs over a window are taken from the trades themselves.

    Trades are `(time, deal id, price)`. Trades with the same time keep Gate's own
    sequence (the deal id), never an order by price.
    """

    def __init__(self, trades: Sequence[tuple[float, int, float]]) -> None:
        self.start = MONTH_START.timestamp()
        self.n = int((MONTH_END - MONTH_START).total_seconds()) + 1
        ordered = sorted(
            ((t, deal, p) for t, deal, p in trades if p > 0), key=lambda x: (x[0], x[1])
        )
        self.times = array("d", (t for t, _, _ in ordered))
        self.prices = array("d", (p for _, _, p in ordered))
        self.price = array("d", [0.0]) * self.n
        self.age = array("d", [-1.0]) * self.n
        j, count = -1, len(self.times)
        for k in range(self.n):
            boundary = self.start + k
            while j + 1 < count and self.times[j + 1] <= boundary:
                j += 1
            if j >= 0:
                self.price[k] = self.prices[j]
                self.age[k] = boundary - self.times[j]

    def boundary(self, at: float) -> int:
        """The last whole-second boundary at or before `at`."""
        return int(at - self.start)

    def at(self, k: int, max_age: float = MAX_PRICE_AGE_S) -> float | None:
        if 0 <= k < self.n and 0 <= self.age[k] <= max_age:
            return self.price[k]
        return None

    def change(self, k: int, back: int, reference_age: float) -> float | None:
        now, then = self.at(k), self.at(k - back, reference_age)
        return now / then - 1 if now and then else None

    def extreme(self, lo: float, hi: float, *, lowest: bool) -> tuple[float, float] | None:
        """(price, time) of the lowest or highest trade with lo <= time <= hi."""
        i = bisect.bisect_left(self.times, lo)
        j = bisect.bisect_right(self.times, hi)
        if i >= j:
            return None
        window = self.prices[i:j]
        target = min(window) if lowest else max(window)
        index = i + window.index(target)
        return target, self.times[index]


def read_tape(path: Path) -> list[tuple[float, int, float]]:
    import duckdb

    rows = (
        duckdb.connect()
        .execute(
            "SELECT column0::DOUBLE, column1::BIGINT, column2::DOUBLE"
            " FROM read_csv(?, header = false,"
            " columns = {'column0': 'VARCHAR', 'column1': 'VARCHAR', 'column2': 'VARCHAR',"
            " 'column3': 'VARCHAR'}) WHERE TRY_CAST(column0 AS DOUBLE) IS NOT NULL",
            [str(path)],
        )
        .fetchall()
    )
    return [(float(t), int(deal), float(p)) for t, deal, p in rows]


def timeline(tape: Tape, first_seen: datetime) -> dict[str, Any]:
    seen = tape.boundary(first_seen.timestamp())
    if seen - 2 * DAY_S < 0:
        return {"status": "insufficient_history"}
    cross = None
    for k in range(seen, seen - DAY_S, -1):
        now = tape.change(k, DAY_S, MAX_REFERENCE_AGE_S)
        prior = tape.change(k - 1, DAY_S, MAX_REFERENCE_AGE_S)
        if now is not None and prior is not None and now >= PUMP_THRESHOLD > prior:
            cross = k
            break
    if cross is None:
        return {"status": "no_tape_crossing"}
    if cross + DAY_S >= tape.n:
        return {"status": "censored_peak"}
    cross_t = tape.start + cross
    low = tape.extreme(cross_t - DAY_S, cross_t, lowest=True)
    peak = tape.extreme(cross_t, cross_t + DAY_S, lowest=False)
    if low is None or peak is None or peak[0] <= low[0]:
        return {"status": "no_move"}
    low_price, low_t = low
    span = peak[0] - low_price

    def share(k: int) -> float | None:
        price = tape.at(k)
        return None if price is None else (price - low_price) / span

    first_after_low = tape.boundary(low_t) + 1
    crossings: dict[str, int | None] = {}
    for threshold in FIVE_MINUTE_THRESHOLDS:
        crossings[f"5m_{threshold:g}"] = next(
            (
                k
                for k in range(first_after_low, cross + 1)
                if (r := tape.change(k, FIVE_MINUTES_S, MAX_PRICE_AGE_S)) is not None
                and r >= threshold
            ),
            None,
        )
    crossings["24h_0.2"] = cross
    moments: dict[str, dict[str, float | None]] = {}
    for name, at in crossings.items():
        if at is None:
            moments[name] = {"status_not_crossed": 1.0}
            continue
        row: dict[str, float | None] = {"seconds_after_move_start": tape.start + at - low_t}
        for delay in DELAYS_S:
            k = at + delay
            row[f"after_{delay}s"] = share(k)
            row[f"after_{delay}s_price_age_s"] = tape.age[k] if tape.age[k] >= 0 else None
        if name == "24h_0.2":
            row["scanner_first_seen"] = share(seen)
            row["scanner_lag_s"] = first_seen.timestamp() - (tape.start + at)
        moments[name] = row
    return {"status": "ok", "moments": moments}


def read(stage_dir: Path, tapes_dir: Path) -> dict[str, Any]:
    if (stage_dir / RESULT_NAME).exists():
        raise SystemExit(f"{stage_dir / RESULT_NAME} exists: read once")
    tapes, tapes_sha = load_verified(stage_dir / TAPES_NAME)
    identity, identity_sha = load_verified(stage_dir / IDENTITY_NAME)
    if tapes["identity_sha256"] != identity_sha:
        raise ValueError("the tapes were fetched for another identity export")
    statuses: Counter[str] = Counter()
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for source in gate_sources(identity):
        if source["native"] is None:
            statuses[f"identity_{source['identity']}"] += 1
        else:
            by_symbol[source["native"]].append(source)
    values: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    missing: dict[str, Counter[str]] = defaultdict(Counter)
    not_crossed: Counter[str] = Counter()
    for symbol, sources in sorted(by_symbol.items()):
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
                    if value is None:
                        missing[threshold][moment] += 1
                    else:
                        values[threshold][moment].append(value)
    result = {
        "gate_version": GATE_VERSION,
        "tapes_sha256": tapes_sha,
        "identity_sha256": identity_sha,
        "max_price_age_s": MAX_PRICE_AGE_S,
        "max_reference_age_s": MAX_REFERENCE_AGE_S,
        "scanner_sources": len(identity["sources"]),
        "statuses": dict(statuses),
        "not_crossed_before_24h_crossing": dict(not_crossed),
        "missing_or_stale": {t: dict(c) for t, c in sorted(missing.items())},
        "moments": {
            threshold: {moment: quantiles(v) for moment, v in sorted(moments.items())}
            for threshold, moments in sorted(values.items())
        },
    }
    write_once(stage_dir / RESULT_NAME, result)
    return result


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--phase", choices=("identity-export", "fetch", "read"), required=True)
    parser.add_argument("--stage-dir", type=Path, required=True)
    parser.add_argument("--tapes-dir", type=Path)
    args = parser.parse_args(argv)
    args.stage_dir.mkdir(parents=True, exist_ok=True)
    if args.phase == "identity-export":
        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            raise SystemExit("DATABASE_URL is required")
        sources = asyncio.run(export_identity(db_url))
        digest = write_once(
            args.stage_dir / IDENTITY_NAME,
            {
                "gate_version": GATE_VERSION,
                "exported_at": datetime.now(UTC).isoformat(),
                "sources": sources,
            },
        )
        reasons = Counter(resolve_identity(s)[1] for s in sources)
        sys.stdout.write(json.dumps({"sha256": digest, "identity": dict(reasons)}) + "\n")
        return
    if args.tapes_dir is None:
        raise SystemExit("--tapes-dir is required")
    if args.phase == "fetch":
        if (args.stage_dir / TAPES_NAME).exists():
            raise SystemExit("the tapes are recorded once")
        tapes = fetch_tapes(args.stage_dir, args.tapes_dir)
        digest = write_once(args.stage_dir / TAPES_NAME, tapes)
        out = {k: tapes[k] for k in ("bases_in_population", "identity", "status")}
        sys.stdout.write(json.dumps({"sha256": digest, **out}) + "\n")
        return
    result = read(args.stage_dir, args.tapes_dir)
    sys.stdout.write(json.dumps({"statuses": result["statuses"]}) + "\n")


if __name__ == "__main__":
    main()
