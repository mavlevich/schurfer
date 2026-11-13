"""B2 source quality only. Emit schemas/counts/clocks, never prices, fills or returns."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import sys
import time
import zipfile
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from .bybit_carry_feasibility import publish_once
from .bybit_compressed_spring import minute_masked
from .bybit_presettlement_book_scope import END, FIRST
from .bybit_presettlement_bounded_fetch import clean_revision
from .bybit_presettlement_source_fetch import VERSION as FETCH_VERSION
from .bybit_presettlement_source_fetch import file_sha

if TYPE_CHECKING:
    from collections.abc import Iterator

VERSION = "b2_source_quality_v1"
MAX_LINE = 1024 * 1024
MAX_FILE_INFLATED = 4 * 1024**3
MAX_TOTAL_INFLATED = 32 * 1024**3


def number(value: Any) -> Decimal:
    n = Decimal(str(value))
    if not n.is_finite():
        raise ValueError("nonfinite native number")
    return n


def quantile(histogram: Counter[int], q: float) -> int | None:
    want = math.ceil(sum(histogram.values()) * q)
    if not want:
        return None
    total = 0
    for value, count in sorted(histogram.items()):
        total += count
        if total >= want:
            return value
    raise AssertionError("histogram exhausted")


class BinaryLines(Protocol):
    def readline(self, size: int = -1) -> bytes: ...


class InflatedBudget:
    def __init__(self) -> None:
        self.total = 0

    def lines(self, handle: BinaryLines) -> Iterator[bytes]:
        used = 0
        while raw := handle.readline(MAX_LINE + 1):
            used += len(raw)
            self.total += len(raw)
            if len(raw) > MAX_LINE or used > MAX_FILE_INFLATED or self.total > MAX_TOTAL_INFLATED:
                raise ValueError("source inflation/line cap exceeded")
            yield raw


class Book:
    """Source reconstruction with sequence diagnostics, without the u+1 assumption."""

    def __init__(self, symbol: str, date: str | None = None) -> None:
        self.symbol = symbol
        self.date = date
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.ts: int | None = None
        self.uid: int | None = None
        self.seq: int | None = None
        self.valid = False
        self.clock: str | None = None
        self.counts: Counter[str] = Counter()
        self.gaps: Counter[int] = Counter()
        self.lags: Counter[int] = Counter()
        self.best_bid: Decimal | None = None
        self.best_ask: Decimal | None = None

    def apply(self, message: dict[str, Any]) -> None:
        data = message["data"]
        if data["s"] != self.symbol:
            raise ValueError("book native symbol mismatch")
        kind = message["type"]
        if kind not in ("snapshot", "delta"):
            raise ValueError("unknown book message type")
        self.counts[kind] += 1
        self.counts["messages"] += 1
        ts = int(message["ts"])
        cts = message.get("cts")
        if not FIRST <= ts < END or (cts is not None and not FIRST <= int(cts) < END):
            raise ValueError("book clock outside registered discovery")
        if (
            self.date is not None
            and datetime.fromtimestamp(ts / 1000, UTC).date().isoformat() != self.date
        ):
            raise ValueError("book clock outside native file day")
        self.counts["cts_present"] += cts is not None
        if self.clock is None:
            self.clock = "cts" if cts is not None else "ts_proxy"
        if cts is not None:
            self.lags[ts - int(cts)] += 1
        if self.clock == "cts":
            if cts is None:
                raise ValueError("matching-engine clock missing within file")
            at = int(cts)
        else:
            at = ts
        uid, seq = int(data["u"]), int(data["seq"])
        if self.ts is not None:
            if at < self.ts:
                self.counts["clock_regressions"] += 1
                self.valid = False
            else:
                self.gaps[at - self.ts] += 1
        if self.uid is not None:
            self.counts["update_id_jumps"] += uid > self.uid + 1
            if kind != "snapshot" and (uid < self.uid or seq < (self.seq or 0)):
                self.counts["sequence_regressions"] += 1
                self.valid = False
        if kind == "snapshot":
            self.bids.clear()
            self.asks.clear()
            self.best_bid = self.best_ask = None
            self.valid = True
        elif not self.valid:
            self.counts["delta_without_valid_snapshot"] += 1
        for name, side in (("b", self.bids), ("a", self.asks)):
            best = self.best_bid if name == "b" else self.best_ask
            recalc = kind == "snapshot"
            for raw_p, raw_q in data[name]:
                price, quantity = number(raw_p), number(raw_q)
                if price <= 0 or quantity < 0:
                    raise ValueError("invalid native book level")
                if quantity == 0:
                    side.pop(price, None)
                    recalc |= price == best
                else:
                    side[price] = quantity
                    if best is None or (price > best if name == "b" else price < best):
                        best = price
            if recalc:
                best = max(side, default=None) if name == "b" else min(side, default=None)
            if name == "b":
                self.best_bid = best
            else:
                self.best_ask = best
        # Top-of-book checks only, no price or depth value leaves the inspector.
        if not minute_masked(at):
            if self.best_bid is None or self.best_ask is None:
                self.counts["empty_book_unmasked"] += 1
            elif self.best_bid >= self.best_ask:
                self.counts["locked_or_crossed_unmasked"] += 1
        self.ts, self.uid, self.seq = at, uid, seq
        self.counts["max_bid_levels"] = max(self.counts["max_bid_levels"], len(self.bids))
        self.counts["max_ask_levels"] = max(self.counts["max_ask_levels"], len(self.asks))


def trades(
    path: Path,
    symbol: str,
    budget: InflatedBudget,
    counts: Counter[str],
    schema: list[str],
    date: str | None = None,
) -> Iterator[dict[str, Any]]:
    with gzip.open(path, "rb") as source:
        reader = csv.DictReader(line.decode("utf-8") for line in budget.lines(source))
        schema.extend(reader.fieldnames or [])
        if not {"timestamp", "symbol", "side", "size", "price"} <= set(schema):
            raise ValueError("trade schema unsupported")
        last: Decimal | None = None
        for row in reader:
            counts["rows"] += 1
            at = number(row["timestamp"]) * 1000
            if not FIRST <= at < END:
                raise ValueError("trade clock outside registered discovery")
            if (
                date is not None
                and datetime.fromtimestamp(float(at) / 1000, UTC).date().isoformat() != date
            ):
                raise ValueError("trade clock outside native file day")
            if row["symbol"] != symbol:
                raise ValueError("trade native symbol mismatch")
            if last is not None and at < last:
                counts["clock_regressions"] += 1
            last = at
            if minute_masked(int(at)):
                counts["reserved_rows_only_counted"] += 1
                continue  # never convert or retain masked tape price/size/turnover
            price, size = number(row["price"]), number(row["size"])
            if price <= 0 or size <= 0 or row["side"] not in ("Buy", "Sell"):
                raise ValueError("invalid unmasked native trade")
            counts["unmasked_rows"] += 1
            flags = [row.get("BT"), row.get("RPI")]
            if all(v in ("true", "false", "True", "False") for v in flags):
                ordinary = all(str(v).lower() == "false" for v in flags)
                counts["explicit_ordinary" if ordinary else "explicit_special"] += 1
            else:
                counts["class_unknown"] += 1
            if "homeNotional" in row and "foreignNotional" in row:
                home, foreign = number(row["homeNotional"]), number(row["foreignNotional"])
                unit = Decimal(1).scaleb(int(foreign.as_tuple().exponent))
                counts["unit_checks"] += 1
                counts["unit_mismatches"] += home != size or abs(foreign - price * size) > unit
            yield {"at": at, "price": price, "side": row["side"]}


def inspect_pair(
    book_path: Path,
    trade_path: Path,
    symbol: str,
    budget: InflatedBudget,
    date: str | None = None,
) -> dict[str, Any]:
    book = Book(symbol, date)
    trade_counts: Counter[str] = Counter()
    alignment: Counter[str] = Counter()
    schema: list[str] = []
    stream = trades(trade_path, symbol, budget, trade_counts, schema, date)
    pending = next(stream, None)

    def compare(row: dict[str, Any]) -> None:
        if not book.valid or book.ts is None:
            alignment["no_valid_prior_book"] += 1
        elif row["at"] <= book.ts:
            alignment["ambiguous_same_clock"] += 1
        elif row["at"] - book.ts > 5000:
            alignment["prior_book_over_5s"] += 1
        elif book.best_bid is None or book.best_ask is None:
            alignment["empty_prior_book"] += 1
        else:
            consistent = (
                row["price"] >= book.best_ask
                if row["side"] == "Buy"
                else row["price"] <= book.best_bid
            )
            alignment["consistent" if consistent else "violates_prior_quote"] += 1

    with zipfile.ZipFile(book_path) as archive:
        members = [m for m in archive.infolist() if not m.is_dir()]
        if len(members) != 1 or members[0].file_size > MAX_FILE_INFLATED:
            raise ValueError("book archive layout/inflated size unsupported")
        with archive.open(members[0]) as source:
            for line in budget.lines(source):
                message = json.loads(line)
                # Fix a clock per file; never switch axes after seeing an alignment.
                key = (
                    "cts"
                    if (book.clock == "cts" or (book.clock is None and "cts" in message))
                    else "ts"
                )
                at = int(message[key])
                while pending is not None and pending["at"] <= at:
                    compare(pending)
                    pending = next(stream, None)
                book.apply(message)
    while pending is not None:
        compare(pending)
        pending = next(stream, None)
    return {
        "symbol": symbol,
        "book_clock": book.clock,
        "book_counts": dict(book.counts),
        "trade_columns": schema,
        "trade_counts": dict(trade_counts),
        "strict_prior_quote_alignment": dict(alignment),
        "book_gap_ms": {
            "p50": quantile(book.gaps, 0.5),
            "p99": quantile(book.gaps, 0.99),
            "max": max(book.gaps, default=None),
        },
        "system_minus_engine_ms": {
            "p50": quantile(book.lags, 0.5),
            "p99": quantile(book.lags, 0.99),
        },
        "maker_volume_identified": trade_counts["class_unknown"] == 0
        and trade_counts["explicit_ordinary"] > 0,
        "native_lot_constraints_identified": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    revision = clean_revision()
    manifest_path = args.run / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    files = manifest["files"]
    run_path = args.run / "run.json"
    contract = json.loads(run_path.read_bytes())
    tasks = {t["id"]: t for t in contract["tasks"]}
    if (
        manifest["version"] != FETCH_VERSION
        or manifest["binding"] != file_sha(run_path)
        or set(files) != set(tasks)
        or manifest["complete"] != 24
    ):
        raise ValueError("source manifest binding differs")
    grouped: dict[tuple[str, str], dict[str, Path]] = {}
    for key, record in files.items():
        path = Path(record["path"])
        if (
            record["binding"] != manifest["binding"]
            or record["task"] != tasks[key]
            or path.resolve().parent != (args.run / "bodies").resolve()
        ):
            raise ValueError("native receipt binding differs")
        if file_sha(path) != record["sha256"]:
            raise ValueError("native source checksum differs")
        task = record["task"]
        grouped.setdefault((task["symbol"], task["day"]), {})[
            "trades" if task["kind"] == "trades" else "book"
        ] = path
    if len(grouped) != 12 or len(files) != 24:
        raise ValueError("pilot incomplete")
    started = time.monotonic()
    budget = InflatedBudget()
    results = []
    for (symbol, date), pair in sorted(grouped.items()):
        try:
            result = inspect_pair(pair["book"], pair["trades"], symbol, budget, date)
            result["status"] = "inspected"
        except (
            ValueError,
            InvalidOperation,
            KeyError,
            TypeError,
            OSError,
            zipfile.BadZipFile,
            EOFError,
        ) as exc:
            result = {
                "symbol": symbol,
                "status": "invalid_or_unsupported_source",
                "failure_type": type(exc).__name__,
                "failure_reason": str(exc) if isinstance(exc, KeyError | ValueError) else None,
            }
        result["day"] = date
        results.append(result)
    report = {
        "version": VERSION,
        "revision": revision,
        "working_tree_dirty": False,
        "recorded_at_utc": datetime.now(UTC).isoformat(),
        "source_sha256": file_sha(Path(__file__)),
        "manifest_sha256": file_sha(manifest_path),
        "pairs": results,
        "inflated_bytes": budget.total,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "masked_tape_rule": "minutes :55 through :04; row count only",
        "fills_or_strategy_returns_computed": False,
    }
    digest = publish_once(args.output, report)
    sys.stdout.write(
        json.dumps(
            {
                "pairs": len(results),
                "statuses": dict(Counter(r["status"] for r in results)),
                "sha256": digest,
            }
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
