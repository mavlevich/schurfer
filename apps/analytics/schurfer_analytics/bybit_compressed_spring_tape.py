"""HYP-032 power step 2: the masked Bybit trade tape.

Implements the tape fetch of docs/research/bybit-compressed-spring-v1.md. No return is
read here.

- ``fetch-tape``: after the mask gates pass, downloads each primary scope day's archive
  file to a temporary file (checking gzip EOF and CRC while it streams), aggregates the
  kept minutes per taker side with DuckDB, reconciles fully known hours with the hourly
  kline volume, writes one part per file and deletes the download. Restartable: a file
  with a part is skipped, so an interrupted run continues where it stopped.
- ``seal``: once every scope day has a part, publishes the manifest of all parts with
  their SHA-256 values and the status counts.

The mask is fixed: minutes [h - 5 min, h + 5 min) around every whole hour h never enter
S. Around an hour boundary that is a HYP-031 event time (some settlement there with
r_s <= -0.30%), those minutes are reserved and only their row count is kept. Around any
other boundary they are kept apart, for the mask sensitivity check and for the hourly
reconciliation, and never enter S.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import hashlib
import json
import multiprocessing
import os
import sys
import tempfile
import time
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import httpx

from .bybit_carry_feasibility import publish_once
from .bybit_compressed_spring import (
    ARCHIVE_FILE,
    BLOCK_DELAYS_SECONDS,
    BLOCK_STATUSES,
    DAY_MS,
    HOUR_MS,
    MASK_AFTER_MS,
    MASK_BEFORE_MS,
    VERSION,
    ArchiveBlockedError,
)
from .bybit_post_funding_screen import file_sha256, verified

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

EVENT_RATE: Final = -0.003
# The frozen HYP-031 discovery index (docs/research/evidence/bybit-post-funding-minute-v1/).
HYP031_INDEX_SHA256: Final = "f48fe09f7023a476e524cf70c6ec1b629eee30a9a07cff41c9d149dfb59e0cf5"
RECONCILE_RELATIVE: Final = 0.001  # 0.1% of the hour's kline volume
WORKERS: Final = 3
IN_FLIGHT_PER_WORKER: Final = 2
PROGRESS_SECONDS: Final = 60.0
ERROR_DELAYS_SECONDS: Final = (2.0, 5.0, 15.0, 60.0)
_sleep = time.sleep  # replaced in tests


# ---------------------------------------------------------------- the mask


def boundary_of(minute_ms: int) -> int | None:
    """The whole hour whose mask holds this minute, or None for a kept minute."""
    offset = minute_ms % HOUR_MS
    if offset < MASK_AFTER_MS:
        return minute_ms - offset
    if offset >= HOUR_MS - MASK_BEFORE_MS:
        return minute_ms - offset + HOUR_MS
    return None


def settlement_rates(path: Path) -> dict[tuple[str, int], float]:
    """Every settlement in a gzip JSON-lines snapshot with ``funding`` lines (this study's
    features snapshot and the HYP-031 index share the format), whatever else failed."""
    out: dict[tuple[str, int], float] = {}
    with gzip.open(path, "rb") as stream:
        for line_bytes in stream:
            line = json.loads(line_bytes)
            if line["kind"] != "funding":
                continue
            for item in line["response"]["result"]["list"]:
                out[(item["symbol"], int(item["fundingRateTimestamp"]))] = float(
                    item["fundingRate"]
                )
    return out


@dataclass(frozen=True)
class Gate:
    event_hours: frozenset[int]
    own_settlements: int
    hyp031_settlements: int
    own_event_hours: int
    hyp031_event_hours: int


def mask_gate(own: dict[tuple[str, int], float], hyp031: dict[tuple[str, int], float]) -> Gate:
    """Gates (a) and (b): every settlement of both snapshots is on a whole hour, so every
    HYP-031 event and control window lies inside the fixed mask. The event hours are the
    union of both snapshots, so a fetch gap in either cannot unreserve a boundary."""
    for source in (own, hyp031):
        for symbol, s in source:
            if s % HOUR_MS:
                raise SystemExit(f"{symbol} settlement {s} is not on a whole hour")

    def events(source: dict[tuple[str, int], float]) -> set[int]:
        return {s for (_, s), rate in source.items() if rate <= EVENT_RATE}

    mine, theirs = events(own), events(hyp031)
    return Gate(frozenset(mine | theirs), len(own), len(hyp031), len(mine), len(theirs))


# ---------------------------------------------------------------- one file


def _day_ms(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)


_AGGREGATE_SQL: Final = """
WITH raw AS (
    SELECT
        TRY_CAST(timestamp AS DECIMAL(18, 6)) AS ts,
        symbol AS sym,
        side,
        TRY_CAST(size AS DECIMAL(38, 12)) AS q,
        TRY_CAST(price AS DECIMAL(38, 12)) AS p,
        TRY_CAST(foreignNotional AS DECIMAL(38, 12)) AS usd
    FROM read_csv($path, header = true, all_varchar = true, compression = 'gzip')
), timed AS (
    SELECT *, CAST(floor(ts / 60) AS BIGINT) * 60000 AS minute FROM raw
), classified AS (
    SELECT *,
        COALESCE(
            ts IS NULL OR q IS NULL OR p IS NULL OR usd IS NULL OR sym IS NULL
            OR side IS NULL
            OR sym <> $symbol OR side NOT IN ('Buy', 'Sell') OR q <= 0 OR p <= 0
            OR usd <= 0 OR minute < $day_start OR minute >= $day_end,
            TRUE
        ) AS bad,
        minute % 3600000 < $after OR minute % 3600000 >= 3600000 - $before AS masked,
        CASE
            WHEN minute % 3600000 < $after THEN minute - minute % 3600000
            WHEN minute % 3600000 >= 3600000 - $before
                THEN minute - minute % 3600000 + 3600000
        END AS boundary
    FROM timed
), flagged AS (
    SELECT *,
        COALESCE(masked AND list_contains(CAST($events AS BIGINT[]), boundary), FALSE)
            AS reserved
    FROM classified
)
SELECT
    CASE WHEN reserved OR bad THEN NULL ELSE minute END AS minute,
    CASE WHEN reserved OR bad THEN NULL ELSE side END AS side,
    reserved,
    bad,
    sum(CASE WHEN reserved OR bad THEN 0 ELSE usd END) AS usd,
    sum(CASE WHEN reserved OR bad THEN 0 ELSE q END) AS base,
    count(*) AS n,
    min(CASE WHEN reserved OR bad THEN NULL ELSE q END) AS min_q
FROM flagged
GROUP BY ALL
"""


_SIDES: Final = {"Buy": 0, "Sell": 1}


def aggregate(path: Path, symbol: str, day: str, event_hours: Iterable[int]) -> dict[str, Any]:
    """Per-minute taker buy and sell USD and base of one day file. Reserved minutes are
    collapsed into one row count inside the query; their sums never leave it. Sums are
    exact DECIMAL(38, 12), so the result does not depend on DuckDB's parallel order."""
    import duckdb

    start = _day_ms(day)
    events = sorted(h for h in event_hours if start - HOUR_MS <= h <= start + DAY_MS + HOUR_MS)
    rows = (
        duckdb.connect()
        .execute(
            _AGGREGATE_SQL,
            {
                "path": str(path),
                "symbol": symbol,
                "day_start": start,
                "day_end": start + DAY_MS,
                "after": MASK_AFTER_MS,
                "before": MASK_BEFORE_MS,
                "events": events,
            },
        )
        .fetchall()
    )
    kept: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0])
    counts: Counter[str] = Counter()
    for minute, side, reserved, bad, usd, base, n, _low in rows:
        if bad:  # checked first: a bad row in a reserved minute still marks the day
            counts["bad_rows"] += n
            continue
        if reserved:
            counts["reserved_rows"] += n
            continue
        cell = kept[minute]
        k = _SIDES[side]  # the query admits only these two
        cell[k] += float(usd)  # an exact decimal sum, rounded once
        cell[2 + k] += float(base)
        cell[4] += n
        counts["rows"] += n
    s_minutes = {m: v for m, v in kept.items() if boundary_of(m) is None}
    sensitivity = {m: v for m, v in kept.items() if boundary_of(m) is not None}
    return {
        "s_minutes": {str(m): v for m, v in sorted(s_minutes.items())},
        "sensitivity_minutes": {str(m): v for m, v in sorted(sensitivity.items())},
        "counts": dict(counts),
        "event_hours": events,
    }


def reconcile(
    day: str, aggregated: dict[str, Any], kline_volume: dict[int, tuple[float, float]]
) -> dict[str, Any]:
    """A status for every UTC hour of the day. An hour whose two mask boundaries are both
    unreserved is fully known: its tape base volume must match the hourly kline volume
    within 0.1% plus one unit of the kline volume's last published decimal."""
    start = _day_ms(day)
    events = set(aggregated["event_hours"])
    base: Counter[int] = Counter()
    for part in ("s_minutes", "sensitivity_minutes"):
        for minute, cell in aggregated[part].items():
            m = int(minute)
            base[m - m % HOUR_MS] += cell[2] + cell[3]
    hours: dict[str, str] = {}
    bins: Counter[str] = Counter()
    worst = 0.0
    for h in range(start, start + DAY_MS, HOUR_MS):
        kline = kline_volume.get(h)
        if h in events or h + HOUR_MS in events:
            hours[str(h)] = "unchecked_reserved"
            continue
        if kline is None:
            hours[str(h)] = "missing_kline"
            continue
        volume, unit = kline
        gap = abs(base[h] - volume)
        hours[str(h)] = "matched" if gap <= RECONCILE_RELATIVE * volume + unit else "mismatch"
        relative = gap / volume if volume > 0 else (0.0 if gap == 0 else float("inf"))
        bins[gap_bin(relative)] += 1
        worst = max(worst, relative)
    return {
        "hours": hours,
        "counts": dict(Counter(hours.values())),
        "gap_bins": dict(bins),
        "worst_relative_gap": round(worst, 6) if worst != float("inf") else "inf",
    }


GAP_EDGES: Final = ((0.0001, "<=0.01%"), (0.0005, "<=0.05%"), (0.001, "<=0.1%"),
                    (0.005, "<=0.5%"), (0.01, "<=1%"))  # fmt: skip


def gap_bin(relative: float) -> str:
    for edge, label in GAP_EDGES:
        if relative <= edge:
            return label
    return ">1%"


# ---------------------------------------------------------------- download


class BudgetExceededError(RuntimeError):
    """The run's byte budget is spent; the file is not finished."""


class Budget:
    """A hard byte budget shared by every worker process. Each download attempt reserves
    its whole Content-Length before the first byte is read: the reservation is appended
    (and fsynced) to the run's ledger, then added to the shared counter ``spent`` (a
    multiprocessing.Value('q')). Spent is the ledger's sum, so a broken attempt counts
    in full and a crash loses nothing."""

    def __init__(self, cap: int, spent: Any, ledger: Path) -> None:
        self.cap = cap
        self.spent = spent
        self.ledger = ledger

    def reserve(self, count: int, what: str) -> None:
        with self.spent.get_lock():
            if self.spent.value + count > self.cap:
                spent = self.spent.value
                raise BudgetExceededError(f"{what}: {count} more bytes pass {self.cap} ({spent})")
            line = json.dumps({"bytes": count, "what": what}) + "\n"
            fd = os.open(self.ledger, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                os.write(fd, line.encode())
                os.fsync(fd)
            finally:
                os.close(fd)
            self.spent.value += count


def ledger_spent(ledger: Path) -> int:
    if not ledger.exists():
        return 0
    return sum(int(json.loads(line)["bytes"]) for line in ledger.read_text().splitlines() if line)


_worker_budget: Budget | None = None


def _init_worker(budget: Budget) -> None:
    global _worker_budget
    _worker_budget = budget


def download(
    client: httpx.Client, url: str, target: Path, budget: Budget | None = None
) -> tuple[int, str] | None:
    """Stream to ``target``; return (bytes, SHA-256), or None for 404. Raises
    ValueError when the gzip stream is truncated or fails its CRC, and
    BudgetExceededError before reading when the reservation would pass the cap."""
    errors = iter((*ERROR_DELAYS_SECONDS, None))
    blocks = iter((*BLOCK_DELAYS_SECONDS, None))
    while True:
        try:
            with client.stream("GET", url) as response:
                if response.status_code == 404:
                    return None
                if response.status_code in BLOCK_STATUSES:
                    delay = next(blocks)
                    if delay is None:
                        raise ArchiveBlockedError(f"{response.status_code} for {url}")
                    _sleep(delay)
                    continue
                response.raise_for_status()
                length = response.headers.get("content-length")
                if length is None:
                    raise httpx.TransportError(f"{url}: no Content-Length to reserve")
                reserved = int(length)
                if budget is not None:
                    budget.reserve(reserved, url)
                digest = hashlib.sha256()
                inflater = zlib.decompressobj(31)
                size = 0
                with target.open("wb") as out:
                    for chunk in response.iter_raw():
                        if size + len(chunk) > reserved and budget is not None:
                            budget.reserve(size + len(chunk) - reserved, url + " (beyond length)")
                            reserved = size + len(chunk)
                        out.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                        inflater.decompress(chunk, 0)  # CRC and EOF check only
                        while inflater.unconsumed_tail:
                            inflater.decompress(inflater.unconsumed_tail, 0)
                if not inflater.eof or inflater.unused_data:
                    raise ValueError(f"{url}: gzip stream truncated or followed by data")
                return size, digest.hexdigest()
        except (httpx.TransportError, httpx.HTTPStatusError):
            delay = next(errors)
            if delay is None:
                raise
            _sleep(delay)
        except zlib.error as exc:
            raise ValueError(f"{url}: {exc}") from exc


@dataclass(frozen=True)
class Task:
    symbol: str
    day: str
    part: Path
    kline_volume: dict[int, tuple[float, float]]
    event_hours: tuple[int, ...]


def run_task(
    task: Task,
    tmp_dir: Path,
    transport: httpx.BaseTransport | None = None,
    budget: Budget | None = None,
) -> str:
    """Fetch, check, aggregate and write one part; returns its status: ok (with a
    status per hour), missing (404) or invalid (gzip, format or a bad row)."""
    budget = budget or _worker_budget
    url = ARCHIVE_FILE.format(symbol=task.symbol, day=task.day)
    part: dict[str, Any] = {"version": VERSION, "symbol": task.symbol, "day": task.day}
    fd, name = tempfile.mkstemp(prefix=f"{task.symbol}{task.day}.", suffix=".csv.gz", dir=tmp_dir)
    os.close(fd)
    target = Path(name)
    try:
        with httpx.Client(timeout=300, follow_redirects=True, transport=transport) as client:
            try:
                got = download(client, url, target, budget)
            except ValueError as exc:
                part |= {"status": "invalid", "error": str(exc)[:300]}
                got = None
            if got is None and "status" not in part:
                part["status"] = "missing"
            elif got is not None:
                part |= {"file_bytes": got[0], "file_sha256": got[1]}
                try:
                    aggregated = aggregate(target, task.symbol, task.day, task.event_hours)
                except Exception as exc:  # a parse error marks the file, not the run
                    part |= {"status": "invalid", "error": str(exc)[:300]}
                else:
                    check = reconcile(task.day, aggregated, task.kline_volume)
                    bad = aggregated["counts"].get("bad_rows", 0)
                    part |= aggregated | {"reconciliation": check}
                    part["status"] = "invalid" if bad else "ok"
    finally:
        target.unlink(missing_ok=True)
    write_part(task.part, part)
    return str(part["status"])


def write_part(path: Path, part: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(part, sort_keys=True, separators=(",", ":")).encode()
    payload = gzip.compress(text, mtime=0)  # byte-reproducible parts
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(payload)
    tmp.replace(path)


# ---------------------------------------------------------------- the run


def _unit(text: str) -> float:
    """One unit of the last published decimal of a volume string."""
    return 10.0 ** -len(text.split(".")[1]) if "." in text else 1.0


def kline_volumes(path: Path, symbols: set[str]) -> dict[str, dict[int, tuple[float, float]]]:
    out: dict[str, dict[int, tuple[float, float]]] = defaultdict(dict)
    with gzip.open(path, "rb") as stream:
        for line_bytes in stream:
            line = json.loads(line_bytes)
            if line["kind"] != "kline_60" or line["symbol"] not in symbols:
                continue
            for item in line["response"]["result"]["list"]:
                out[line["symbol"]][int(item[0])] = (float(item[5]), _unit(str(item[5])))
    return out


def part_path(out_dir: Path, symbol: str, day: str) -> Path:
    return out_dir / "parts" / symbol / f"{day}.json.gz"


def pilot_days(scope: dict[str, list[str]], count: int) -> list[tuple[str, str]]:
    """A fixed pilot: the scope days with the lowest SHA-256 of 'version|pilot|SYMBOL|day'."""

    def key(item: tuple[str, str]) -> str:
        return hashlib.sha256(f"{VERSION}|pilot|{item[0]}|{item[1]}".encode()).hexdigest()

    every = [(symbol, day) for symbol, days in scope.items() for day in days]
    return sorted(sorted(every, key=key)[:count])


def build_tasks(
    days: Sequence[tuple[str, str]],
    out_dir: Path,
    volumes: dict[str, dict[int, tuple[float, float]]],
    event_hours: frozenset[int],
) -> list[Task]:
    tasks = []
    for symbol, day in days:
        start = _day_ms(day)
        hours = {h: v for h, v in volumes.get(symbol, {}).items() if start <= h < start + DAY_MS}
        near = tuple(
            sorted(h for h in event_hours if start - HOUR_MS <= h <= start + DAY_MS + HOUR_MS)
        )
        tasks.append(Task(symbol, day, part_path(out_dir, symbol, day), hours, near))
    return tasks


RULES: Final = {
    "rules": "tape-v3",
    "mask_before_ms": MASK_BEFORE_MS,
    "mask_after_ms": MASK_AFTER_MS,
    "event_rate": EVENT_RATE,
    "reconcile_relative": RECONCILE_RELATIVE,
}


def bind_run(out_dir: Path, binding: dict[str, Any], *, create: bool) -> None:
    """A run directory belongs to one set of inputs and rules, fixed before its first
    part: a resume or a seal with anything else is refused."""
    path = out_dir / "run.json"
    if path.exists():
        if json.loads(path.read_bytes()) != binding:
            raise SystemExit(f"{out_dir} is bound to other inputs or rules; use a new directory")
        return
    if not create:
        raise SystemExit(f"{out_dir} has no run.json; nothing was fetched into it")
    if (out_dir / "parts").exists():
        raise SystemExit(f"{out_dir} has parts but no run.json; use a new directory")
    publish_once(path, binding)


def _log(line: dict[str, Any]) -> None:
    stamp = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    sys.stdout.write(json.dumps({"at": stamp} | line) + "\n")
    sys.stdout.flush()


def fetch_tape(
    tasks: Sequence[Task], out_dir: Path, max_bytes: int, workers: int
) -> dict[str, Any]:
    """Every byte read from the archive, retries included, is spent from one budget
    shared by the workers and kept across resumes in budget.json."""
    todo = [t for t in tasks if not t.part.exists()]
    done_before = len(tasks) - len(todo)
    tmp_dir = out_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    for leftover in tmp_dir.iterdir():
        leftover.unlink()
    context = multiprocessing.get_context("spawn")
    ledger = out_dir / "budget-ledger.jsonl"
    spent_before = ledger_spent(ledger)
    budget = Budget(max_bytes, context.Value("q", spent_before), ledger)
    statuses: Counter[str] = Counter()
    started = time.monotonic()
    last_log = 0.0
    stop: str | None = None
    queue = iter(todo)
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=workers, mp_context=context, initializer=_init_worker, initargs=(budget,)
    ) as pool:
        running: dict[concurrent.futures.Future[str], Task] = {}
        while True:
            while stop is None and len(running) < workers * IN_FLIGHT_PER_WORKER:
                if budget.spent.value >= max_bytes:
                    stop = "byte cap reached"
                    break
                task = next(queue, None)
                if task is None:
                    break
                running[pool.submit(run_task, task, tmp_dir)] = task
            if not running:
                break
            finished, _ = concurrent.futures.wait(
                running, timeout=PROGRESS_SECONDS, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in finished:
                task = running.pop(future)
                try:
                    statuses[future.result()] += 1
                except ArchiveBlockedError as exc:
                    stop = f"archive blocked: {exc}"
                    statuses["blocked"] += 1
                except BudgetExceededError:
                    stop = "byte cap reached"
                    statuses["over_budget"] += 1
                except Exception as exc:  # recorded; the file is retried on the next run
                    statuses["error"] += 1
                    _log({"error": f"{task.symbol} {task.day}: {str(exc)[:200]}"})
            now = time.monotonic()
            if now - last_log >= PROGRESS_SECONDS:
                last_log = now
                finished_files = sum(
                    v for k, v in statuses.items() if k in ("ok", "missing", "invalid")
                )
                done = done_before + finished_files
                read = budget.spent.value - spent_before
                rate = read / max(now - started, 1.0)
                left = (len(tasks) - done) * read / max(finished_files, 1)
                _log(
                    {
                        "files_done": done,
                        "files_total": len(tasks),
                        "gb_read_this_run": round(read / 1e9, 2),
                        "gb_read_all_runs": round(budget.spent.value / 1e9, 2),
                        "mb_per_s": round(rate / 1e6, 2),
                        "eta_hours": round(left / rate / 3600, 1) if rate else None,
                        "statuses": dict(statuses),
                    }
                )
    summary = {
        "files_total": len(tasks),
        "files_done_before": done_before,
        "statuses_this_run": dict(statuses),
        "bytes_read_this_run": budget.spent.value - spent_before,
        "bytes_read_all_runs": budget.spent.value,
        "seconds": round(time.monotonic() - started, 1),
        "stopped": stop,
    }
    _log({"summary": summary})
    return summary


def seal(
    tasks: Sequence[Task], out_dir: Path, binding: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    bind_run(out_dir, binding, create=False)
    seeded = seeded_parts(out_dir, binding)
    missing = [f"{t.symbol} {t.day}" for t in tasks if not t.part.exists()]
    if missing:
        raise SystemExit(f"{len(missing)} scope days have no part yet, e.g. {missing[:3]}")
    statuses: Counter[str] = Counter()
    hours: Counter[str] = Counter()
    gaps: Counter[str] = Counter()
    counts: Counter[str] = Counter()
    file_bytes = 0
    parts = []
    for task in tasks:
        part = json.loads(gzip.decompress(task.part.read_bytes()))
        if part.get("symbol") != task.symbol or part.get("day") != task.day:
            raise SystemExit(f"{task.part} does not hold {task.symbol} {task.day}")
        statuses[part["status"]] += 1
        counts.update(part.get("counts", {}))
        if part["status"] == "ok":  # hours of rejected days are not coverage
            hours.update(part.get("reconciliation", {}).get("counts", {}))
            gaps.update(part.get("reconciliation", {}).get("gap_bins", {}))
        file_bytes += int(part.get("file_bytes", 0))
        entry = {
            "symbol": task.symbol,
            "day": task.day,
            "status": part["status"],
            "part_sha256": file_sha256(task.part),
        }
        if seeded is not None:
            origin = seeded[1].get((task.symbol, task.day))
            if origin is not None and origin != entry["part_sha256"]:
                raise SystemExit(f"{task.part} is not the part seed.json imported")
            entry["origin"] = "seeded" if origin is not None else "fetched"
        parts.append(entry)
    manifest = {
        "version": VERSION,
        "binding": binding,
        "statuses": dict(sorted(statuses.items())),
        "row_counts": dict(sorted(counts.items())),
        "hours_in_ok_days": dict(sorted(hours.items())),
        "matched_share_of_ok_hours": round(hours["matched"] / max(sum(hours.values()), 1), 4),
        "matched_share_of_checked_hours": round(
            hours["matched"] / max(hours["matched"] + hours["mismatch"], 1), 4
        ),
        "gap_bins_in_ok_days": {label: gaps[label] for _, label in GAP_EDGES}
        | {">1%": gaps[">1%"]},
        "file_bytes": file_bytes,
        "bytes_reserved": ledger_spent(out_dir / "budget-ledger.jsonl"),
        "parts": parts,
    }
    if seeded is not None:  # imported bytes apart from this run's own traffic
        manifest["seed"] = seeded[0]
    digest = publish_once(out_dir / "manifest.json", manifest)
    return manifest, digest


def seeded_parts(
    out_dir: Path, binding: dict[str, Any]
) -> tuple[dict[str, Any], dict[tuple[str, str], str]] | None:
    """The seed record of a run seeded from earlier verified parts (HYP-036), or None."""
    path = out_dir / "seed.json"
    if not path.exists():
        return None
    record = json.loads(path.read_bytes())
    if record.get("binding") != binding:
        raise SystemExit(f"{path} was written for other inputs or rules")
    origins = {(p["symbol"], p["day"]): p["part_sha256"] for p in record["parts"]}
    summary = {
        "seed_sha256": file_sha256(path),
        "source_manifest_sha256": record["source_manifest_sha256"],
        "imported_parts": record["imported_parts"],
        "imported_file_bytes": record["imported_file_bytes"],
    }
    return summary, origins


# ---------------------------------------------------------------- command line


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("fetch-tape", "seal"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--scope", type=Path, required=True)
        cmd.add_argument("--scope-sha256", required=True)
        cmd.add_argument("--features", type=Path, required=True)
        cmd.add_argument("--features-sha256", required=True)
        cmd.add_argument("--hyp031-index", type=Path, required=True)
        cmd.add_argument("--hyp031-index-sha256", required=True)
        cmd.add_argument("--out-dir", type=Path, required=True)
        cmd.add_argument("--pilot", type=int, help="only this many fixed pilot days")
        if name == "fetch-tape":
            cmd.add_argument("--max-bytes", type=int, required=True)
            cmd.add_argument("--workers", type=int, default=WORKERS)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.hyp031_index_sha256 != HYP031_INDEX_SHA256:
        raise SystemExit(f"the HYP-031 index is pinned to {HYP031_INDEX_SHA256}")
    scope = json.loads(verified(args.scope, args.scope_sha256).read_bytes())
    if scope.get("version") != VERSION:
        raise SystemExit(f"scope is not {VERSION}")
    binding = {
        "version": VERSION,
        **RULES,
        "scope_sha256": args.scope_sha256,
        "features_sha256": args.features_sha256,
        "hyp031_index_sha256": args.hyp031_index_sha256,
        "pilot": args.pilot or 0,
    }
    bind_run(args.out_dir, binding, create=args.command == "fetch-tape")
    primary: dict[str, list[str]] = scope["primary"]
    features = verified(args.features, args.features_sha256)
    gate = mask_gate(
        settlement_rates(features),
        settlement_rates(verified(args.hyp031_index, args.hyp031_index_sha256)),
    )
    _log(
        {
            "gate": "passed: every settlement on a whole hour",
            "event_hours": len(gate.event_hours),
            "own_event_hours": gate.own_event_hours,
            "hyp031_event_hours": gate.hyp031_event_hours,
        }
    )
    days = (
        pilot_days(primary, args.pilot)
        if args.pilot
        else [(symbol, day) for symbol in sorted(primary) for day in primary[symbol]]
    )
    volumes = kline_volumes(features, {symbol for symbol, _ in days})
    tasks = build_tasks(days, args.out_dir, volumes, gate.event_hours)
    if args.command == "fetch-tape":
        summary = fetch_tape(tasks, args.out_dir, args.max_bytes, args.workers)
        if summary["stopped"] or summary["statuses_this_run"].get("error"):
            raise SystemExit(1)
        return
    manifest, digest = seal(tasks, args.out_dir, binding)
    _log(
        {
            "sealed": manifest["statuses"],
            "hours_in_ok_days": manifest["hours_in_ok_days"],
            "manifest_sha256": digest,
        }
    )
