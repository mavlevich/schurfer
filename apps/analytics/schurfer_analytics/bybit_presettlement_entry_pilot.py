"""Frozen B2 entry-only pilot. Conditional execution counts, never strategy returns."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import subprocess
import sys
import time
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from .bybit_carry_feasibility import publish_once
from .bybit_compressed_spring import minute_masked
from .bybit_presettlement_book_scope import END, EVENTS_SHA, FIRST
from .bybit_presettlement_bounded_fetch import clean_revision
from .bybit_presettlement_source_fetch import PLAN_SHA, body_path, file_sha
from .bybit_presettlement_source_quality import Book, InflatedBudget, trades

VERSION: Final = "b2_entry_only_pilot_v1"
MANIFEST_SHA: Final = "527b7446887d1f6c466c9891d9f1ef67134dc4b86aaf7a30d3f83c8ddc9bb0a0"
CONTRACT: Final = "docs/research/b2-entry-pilot-contract-v1.md"
EVENTS: Final = "docs/research/evidence/b1-prior-only-4h-2026-11-13/read/events.json.gz"
PLAN: Final = "docs/research/evidence/b2-archive-scope-2026-11-13/results/pilot-body-plan.json"
GAP_MS: Final = 2000


@dataclass(frozen=True)
class Config:
    name: str
    offset: str = "d30"
    dollars: int = 50
    arrival: int = 1000
    wait: int = 60_000
    zero_queue: bool = False


CONFIGS: Final = (
    Config("primary"),
    Config("d15", "d15"),
    Config("d10", "d10"),
    Config("wait300", wait=300_000),
    Config("zero_queue", zero_queue=True),
    Config("usd25", dollars=25),
    Config("usd100", dollars=100),
    Config("arrival5000", arrival=5000),
)


def trade_class(row: dict[str, Any]) -> str:
    flags = [row.get("BT"), row.get("RPI")]
    if any(v in (True, "true", "True", "1") for v in flags):
        return "special"
    if all(v in (False, "false", "False", "0") and v is not None for v in flags):
        return "ordinary"
    return "unknown"


@dataclass
class Trial:
    identity: str
    config: Config
    decision: int
    limit: Decimal | None = None
    quantity: Decimal | None = None
    ahead: Decimal | None = None
    reason: str | None = None
    known: Decimal = Decimal(0)
    assumed: Decimal = Decimal(0)
    counts: Counter[str] = field(default_factory=Counter)

    @property
    def arrival(self) -> int:
        return self.decision + self.config.arrival

    @property
    def end(self) -> int:
        return self.arrival + self.config.wait

    def capture(self, stage: str, book: Book, public_at: int | None, at: int) -> None:
        if self.reason is not None:
            return
        if (
            not book.valid
            or public_at is None
            or at - public_at > GAP_MS
            or book.best_bid is None
            or book.best_ask is None
            or book.best_bid >= book.best_ask
        ):
            self.reason = "incomplete_book_data"
            return
        if stage == "decision":
            self.limit = book.best_bid
            self.quantity = Decimal(self.config.dollars) / self.limit
        elif self.limit is None:
            self.reason = "incomplete_book_data"
        elif self.limit >= book.best_ask:
            self.reason = "post_only_rejected"
        elif self.limit < min(book.bids):
            self.reason = "outside_observed_depth"
        else:
            self.ahead = (
                Decimal(0) if self.config.zero_queue else book.bids.get(self.limit, Decimal(0))
            )

    def consume(self, row: dict[str, Any]) -> None:
        if self.reason is not None or self.limit is None or self.ahead is None:
            return
        at = row["at"]
        if at == self.arrival or at == self.end:
            self.counts["clock_ties"] += 1
            return
        if not self.arrival < at < self.end or row["side"] != "Sell":
            return
        if row["price"] > self.limit:
            self.counts["sell_above_limit"] += 1
            return
        label = trade_class(row)
        self.counts[label] += 1
        if label == "special":
            return
        self.counts["sell_at_limit" if row["price"] == self.limit else "sell_below_limit"] += 1
        if label == "ordinary":
            self.known += row["size"]
        self.assumed += row["size"]

    def outcome(self) -> dict[str, Any]:
        def status(volume: Decimal) -> str:
            assert self.quantity is not None and self.ahead is not None
            filled = min(self.quantity, max(Decimal(0), volume - self.ahead))
            return "full" if filled == self.quantity else "partial" if filled else "none"

        incomplete = self.reason or (
            "incomplete_book_data" if self.quantity is None or self.ahead is None else None
        )
        low = incomplete or status(self.known)
        primary = incomplete or ("unidentified_trade_class" if self.counts["unknown"] else low)
        return {
            "id": self.identity,
            "configuration": self.config.name,
            "primary": primary,
            "excluded_unknown_case": low,
            "unknown_eligible_sensitivity": incomplete or status(self.assumed),
            "diagnostic_counts": dict(self.counts),
            "native_order_sizing": "unverified",
        }


def reconstruct(
    path: Path, symbol: str, date: str, trials: list[Trial], budget: InflatedBudget
) -> None:
    book = Book(symbol)
    targets = sorted(
        [(t.decision, i, "decision") for i, t in enumerate(trials)]
        + [(t.arrival, i, "arrival") for i, t in enumerate(trials)]
    )
    index = 0
    last: int | None = None
    covered_through: int | None = None
    key: tuple[int, int] | None = None
    payload: Any = None
    start = int(datetime.fromisoformat(date).replace(tzinfo=UTC).timestamp() * 1000)
    maximum = max(t.end for t in trials)

    def poison(left: int, right: int) -> None:
        for t in trials:
            stop = t.arrival if t.reason == "post_only_rejected" else t.end
            if left < stop and right > t.decision:
                t.reason = "incomplete_book_data"

    with zipfile.ZipFile(path) as archive:
        member = [m for m in archive.infolist() if not m.is_dir()]
        if len(member) != 1:
            raise ValueError("book layout changed")
        with archive.open(member[0]) as stream:
            for raw in budget.lines(stream):
                message = json.loads(raw)
                at = int(message["ts"])
                if not start <= at < start + 86_400_000:
                    continue
                covered_through = max(covered_through or at, at)
                system_regressed = last is not None and at < last
                while index < len(targets) and targets[index][0] < at:
                    when, i, stage = targets[index]
                    trials[i].capture(stage, book, last, when)
                    index += 1
                if last is not None and (at < last or at - last > GAP_MS):
                    poison(min(last, at), max(last, at))
                if at > maximum:
                    break
                data = message["data"]
                current = (int(data["u"]), int(data["seq"]))
                conflicting = current == key and payload != (message["type"], data)
                reset = message["type"] == "snapshot" and last is not None and current != key
                if conflicting or reset:
                    poison(at - 1, at + 1)
                before = book.counts["clock_regressions"] + book.counts["sequence_regressions"]
                book.apply(message)
                after = book.counts["clock_regressions"] + book.counts["sequence_regressions"]
                if after != before or conflicting or system_regressed:
                    poison(at - 1, at + 1)
                    book.valid = False
                key, payload = current, (message["type"], data)
                last = max(last or at, at)
    while index < len(targets):
        when, i, stage = targets[index]
        trials[i].capture(stage, book, last, when)
        index += 1
    for t in trials:
        required = t.arrival if t.reason == "post_only_rejected" else t.end
        if covered_through is None or covered_through < required:
            t.reason = "incomplete_book_data"


def replay_tape(
    path: Path, symbol: str, date: str, trials: list[Trial], budget: InflatedBudget
) -> None:
    starts = sorted(trials, key=lambda t: t.arrival)
    active: list[Trial] = []
    index = 0
    counts: Counter[str] = Counter()
    maximum = max(t.end for t in starts)
    for row in trades(path, symbol, budget, counts, [], date):
        at = row["at"]
        if at > maximum:
            break
        while index < len(starts) and starts[index].arrival <= at:
            active.append(starts[index])
            index += 1
        active = [t for t in active if t.end >= at]
        for trial in active:
            trial.consume(row)
    if counts["clock_regressions"]:
        for t in trials:
            t.reason = "incomplete_trade_data"


def verify_main() -> str:
    # Fixed git object names; no shell or caller-controlled subprocess arguments.
    main = subprocess.check_output(  # noqa: S603
        ["git", "show", f"origin/main:{CONTRACT}"]  # noqa: S607
    )
    if main != Path(CONTRACT).read_bytes():
        raise ValueError("entry contract not published identically in origin/main")
    registry = subprocess.check_output(  # noqa: S603, RUF100 - fixed git arguments; Ruff version compatibility
        ["git", "show", "origin/main:docs/research/seen-windows-registry.md"]  # noqa: S607
    )
    if b"## B2 entry-pilot replay registration, 2026-11-13" not in registry:
        raise ValueError("entry registry not published in main")
    return hashlib.sha256(main).hexdigest()


def pilot(source: Path) -> dict[str, Any]:
    started = time.monotonic()
    started_at = datetime.now(UTC).isoformat()
    revision = clean_revision()
    registration = verify_main()
    if file_sha(source / "manifest.json") != MANIFEST_SHA or file_sha(Path(PLAN)) != PLAN_SHA:
        raise ValueError("source pilot binding changed")
    if file_sha(Path(EVENTS)) != EVENTS_SHA:
        raise ValueError("event export changed")
    plan = json.loads(Path(PLAN).read_bytes())
    rows = json.loads(gzip.decompress(Path(EVENTS).read_bytes()))["events_by_decision"]
    files = json.loads((source / "manifest.json").read_bytes())["files"]
    results = []
    budget = InflatedBudget()
    for day in plan["original_pilot_rows"]:
        symbol, date = day["symbol"], day["day"]
        trials = []
        for config in CONFIGS:
            for row in rows[config.offset]:
                when = row["decision_ms"]
                if (
                    row["symbol"] != symbol
                    or datetime.fromtimestamp(when / 1000, UTC).date().isoformat() != date
                ):
                    continue
                identity = f"{symbol}:{when}"
                t = Trial(identity, config, when)
                for minute in range(t.decision // 60_000, t.end // 60_000 + 1):
                    stamp = minute * 60_000
                    if minute_masked(stamp) or not FIRST <= stamp < END:
                        raise ValueError("pilot window would open a reserved or outside minute")
                trials.append(t)
        if not trials:
            results.append({"symbol": symbol, "day": date, "events": []})
            continue
        native: dict[str, Path] = {}
        for key, record in files.items():
            task = record["task"]
            if (task["symbol"], task["day"]) != (symbol, date):
                continue
            path = body_path(source, record)
            if file_sha(path) != record["sha256"] or key != task["id"]:
                raise ValueError("native input changed")
            native["trades" if task["kind"] == "trades" else "book"] = path
        reconstruct(native["book"], symbol, date, trials, budget)
        replay_tape(native["trades"], symbol, date, trials, budget)
        results.append({"symbol": symbol, "day": date, "events": [t.outcome() for t in trials]})
    return {
        "version": VERSION,
        "revision": revision,
        "working_tree_dirty": False,
        "started_at_utc": started_at,
        "completed_at_utc": datetime.now(UTC).isoformat(),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "registration_sha256": registration,
        "manifest_sha256": MANIFEST_SHA,
        "events_sha256": EVENTS_SHA,
        "source_sha256": file_sha(Path(__file__)),
        "reader_sha256": file_sha(Path(sys.modules[trades.__module__].__file__ or "")),
        "transport_helpers_sha256": file_sha(
            Path(__file__).with_name("bybit_presettlement_source_fetch.py")
        ),
        "mask_source_sha256": file_sha(Path(sys.modules[minute_masked.__module__].__file__ or "")),
        "pilot_days": results,
        "fills_are_conditional_model_outputs": True,
        "strategy_returns_or_funding_computed": False,
        "inflated_bytes": budget.total,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = pilot(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    digest = publish_once(args.output, result)
    sys.stdout.write(json.dumps({"sha256": digest, "days": len(result["pilot_days"])}) + "\n")


if __name__ == "__main__":
    main()
