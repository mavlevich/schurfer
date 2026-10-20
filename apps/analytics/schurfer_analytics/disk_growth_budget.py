"""Disk growth from the daily readings, and the Gate collection budget recomputed (PR 2b).

Reads the `reading-*.json` series of `infra/scripts/disk_growth_reading.py` (each
checked against its `.sha256`) and answers what PR 5's correction left undetermined:

- how fast the disk fills outside the hot bars, measured on the filesystem itself, and
  which parts make that growth (the database outside the bars, `runtime/`, Docker
  images and build cache, the rest);
- how much the hot bars retention is still due to release;
- whether each PR 5 universe fits a six-month collection above the 10 GiB reserve,
  with PR 5's own `headroom` arithmetic and its committed, hash-checked sizes.

Each part gets the estimator that fits how it grows:

- the database outside the bars and the `rest` grow continuously: least-squares slope;
- `runtime/` grows in steps (a study downloads its dataset once): the median of the
  daily interval rates, so one download is not projected as a daily rate; every step
  above `STEP_MIB` in one interval is listed instead;
- Docker images, build cache and containers rise and fall with deploys and are what
  PR 5's 10 GiB reserve is for: not a growth rate, their range is reported.

**Central** adds the parts as measured (a shrinking part may offset another).
**Conservative** counts a shrinking part as zero. A universe fits only if it fits at
the conservative rate; a series shorter than a week gives no verdict at all.

It reads only the readings and PR 5's artifact: no table row, no market data.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .gate_collection_budget import BARS_HYPERTABLE, DAYS_PER_MONTH, GIB, MIB, headroom
from .reporting import normalize_code_revision

if TYPE_CHECKING:
    from collections.abc import Sequence

VERSION = "disk_growth_budget_v1"
READING_VERSION = "disk_growth_reading_v1"
# PR 5's committed artifact (docs/research/evidence/gate-collection-budget-v1); its
# sizes and universes are evidence of record, so any other file is refused.
GATE_BUDGET_SHA256 = "4b204640e293048b50afc9aeb2123be3c00e2551b48be3054f3dfb4aa343d3d5"
RESERVE_GIB = 10.0
HORIZON_MONTHS = 6
MIN_SERIES_DAYS = 7.0
# A gap longer than this between two readings is reported (the timer is daily).
MAX_READING_GAP = timedelta(hours=36)
# PR 5's stop condition: free disk projected below the reserve within 30 days.
STOP_PROJECTION_DAYS = 30.0
STEADY_PARTS = ("database_outside_bars", "rest")
STEPWISE_PARTS = ("runtime_readable",)
RESERVE_PARTS = ("docker_images", "docker_build_cache", "docker_containers")
PARTS = (*STEADY_PARTS, *STEPWISE_PARTS, *RESERVE_PARTS)
# An interval shorter than this is not a daily rate (the first day had two readings).
MIN_INTERVAL_DAYS = 0.5
# A rise of one part above this between two readings is reported as a step.
STEP_MIB = 1024.0
# A what-if, reported beside the verdict and never replacing it: the growth without the
# two tables the closed pump-short line still writes (its `pump_short_*` decisions and
# their outcomes). HYP-012 v2's shadow also writes a few rows into trade_decisions, so
# this slightly overstates what stopping pump-short would save.
WHAT_IF_WITHOUT = ("app.trade_decisions", "app.trade_decision_outcomes")
_DOCKER_TYPES = {
    "Images": "docker_images",
    "Build Cache": "docker_build_cache",
    "Containers": "docker_containers",
}
# `docker system df` prints decimal units (go-units HumanSize).
_DOCKER_UNITS = {"B": 1, "kB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12}
_DOCKER_SIZE = re.compile(r"^\s*([0-9.]+)\s*([kMGT]?B)")


class SeriesError(ValueError):
    """A reading or the PR 5 artifact failed a check; nothing is computed from it."""


def docker_bytes(size: str) -> int:
    match = _DOCKER_SIZE.match(size)
    if not match:
        raise SeriesError(f"unreadable docker size {size!r}")
    return round(float(match.group(1)) * _DOCKER_UNITS[match.group(2)])


def load_series(directory: Path) -> list[dict[str, Any]]:
    """Every reading in `directory`, oldest first, each with its verified hash. One
    reading without a matching `.sha256`, of another version, or a repeated time stops
    the whole analysis: a series with a silently skipped day is not the measured one."""
    paths = sorted(directory.glob("reading-*.json"))
    if not paths:
        raise SeriesError(f"no reading in {directory}")
    series = []
    for path in paths:
        sidecar = path.with_name(path.name + ".sha256")
        if not sidecar.exists():
            raise SeriesError(f"{path.name} has no .sha256")
        body = path.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        if digest != sidecar.read_text().strip():
            raise SeriesError(f"{path.name} does not match its .sha256")
        payload = json.loads(body)
        if payload.get("version") != READING_VERSION:
            raise SeriesError(f"{path.name} is {payload.get('version')!r}, not {READING_VERSION!r}")
        series.append({"name": path.name, "sha256": digest, "reading": payload})
    series.sort(key=lambda item: item["reading"]["taken_at"])
    times = [item["reading"]["taken_at"] for item in series]
    if len(set(times)) != len(times):
        raise SeriesError("two readings share a taken_at")
    return series


def _bars(reading: dict[str, Any]) -> dict[str, Any]:
    for hypertable in reading["database"]["hypertables"]:
        if hypertable["hypertable"] == BARS_HYPERTABLE:
            found: dict[str, Any] = hypertable
            return found
    raise SeriesError(f"{BARS_HYPERTABLE} missing from a reading")


def parts(reading: dict[str, Any]) -> dict[str, int]:
    """The filesystem's used bytes split into parts. `rest` is what no other part
    accounts for (WAL, logs, the system, directories the reading could not read)."""
    bars = sum(chunk["bytes"] for chunk in _bars(reading)["chunks"])
    docker = {name: 0 for name in _DOCKER_TYPES.values()}
    for row in reading["docker"]:
        name = _DOCKER_TYPES.get(row["Type"])
        if name:
            docker[name] = docker_bytes(row["Size"])
    out = {
        "filesystem_used": reading["filesystem"]["used"],
        "filesystem_free": reading["filesystem"]["free"],
        "bars": bars,
        "used_outside_bars": reading["filesystem"]["used"] - bars,
        "database_outside_bars": reading["database"]["database_bytes"] - bars,
        "runtime_readable": sum(reading["runtime_dirs"]["sizes"].values()),
        **docker,
    }
    known = sum(out[name] for name in PARTS if name != "rest")
    out["rest"] = out["used_outside_bars"] - known
    return out


def _days(series: list[dict[str, Any]]) -> list[float]:
    first = datetime.fromisoformat(series[0]["reading"]["taken_at"])
    return [
        (datetime.fromisoformat(item["reading"]["taken_at"]) - first).total_seconds() / 86400
        for item in series
    ]


def slope(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Least-squares slope (units of y per unit of x)."""
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    spread = sum((x - mean_x) ** 2 for x in xs)
    if spread == 0:
        raise SeriesError("a slope needs readings at two different times")
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / spread


def _intervals(days: Sequence[float], values: Sequence[int]) -> list[float]:
    """Rates (bytes/day) between consecutive readings at least MIN_INTERVAL_DAYS apart."""
    out = []
    anchor = 0
    for i in range(1, len(days)):
        span = days[i] - days[anchor]
        if span >= MIN_INTERVAL_DAYS:
            out.append((values[i] - values[anchor]) / span)
            anchor = i
    return out


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _hypertable_names(reading: dict[str, Any]) -> set[str]:
    return {h["hypertable"] for h in reading["database"]["hypertables"]}


def relation_bytes(reading: dict[str, Any], name: str) -> int:
    """One relation's bytes in a reading: a hypertable is the sum of its chunks (its
    parent table holds no rows), a plain table its total relation size."""
    for hypertable in reading["database"]["hypertables"]:
        if hypertable["hypertable"] == name:
            return int(sum(chunk["bytes"] for chunk in hypertable["chunks"]))
    for table in reading["database"]["tables"]:
        if table["table"] == name:
            return int(table["total_bytes"])
    raise SeriesError(f"{name} missing from the reading taken at {reading['taken_at']}")


def rates(series: list[dict[str, Any]], *, without: Sequence[str] = ()) -> dict[str, Any]:
    """Each part's rate. `without` removes those relations' bytes from the database part
    (and from the filesystem total) in every reading before any rate is estimated, so a
    what-if goes through exactly the same per-part estimators as the verdict."""
    days = _days(series)
    rows = [parts(item["reading"]) for item in series]
    for row, item in zip(rows, series, strict=True):
        removed = sum(relation_bytes(item["reading"], name) for name in without)
        row["database_outside_bars"] -= removed
        row["used_outside_bars"] -= removed
    per_part: dict[str, float] = {}
    for name in STEADY_PARTS:
        per_part[name] = slope(days, [row[name] for row in rows]) / MIB
    for name in STEPWISE_PARTS:
        intervals = _intervals(days, [row[name] for row in rows])
        if not intervals:
            raise SeriesError("a stepwise rate needs readings at least half a day apart")
        per_part[name] = _median(intervals) / MIB
    growing = (*STEADY_PARTS, *STEPWISE_PARTS)
    steps = [
        {
            "part": name,
            "before": series[i - 1]["name"],
            "after": series[i]["name"],
            "mib": (rows[i][name] - rows[i - 1][name]) / MIB,
        }
        for name in growing
        for i in range(1, len(rows))
        if (rows[i][name] - rows[i - 1][name]) / MIB > STEP_MIB
    ]
    return {
        "central_mib_per_day": sum(per_part[name] for name in growing),
        "conservative_mib_per_day": sum(max(0.0, per_part[name]) for name in growing),
        "parts_mib_per_day": per_part,
        "steps": steps,
        # Not a rate: the reserve covers these (deploy builds, build cache).
        "reserve_parts_gib": {
            name: {
                "min": min(row[name] for row in rows) / GIB,
                "max": max(row[name] for row in rows) / GIB,
            }
            for name in RESERVE_PARTS
        },
        # For reference: the filesystem itself, all parts together, least squares.
        "used_outside_bars_slope_mib_per_day": slope(
            days, [row["used_outside_bars"] for row in rows]
        )
        / MIB,
        "bars_slope_mib_per_day": slope(days, [row["bars"] for row in rows]) / MIB,
    }


def relations(series: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each plain table and each hypertable other than the bars: its size slope and,
    between the first and last reading, the rows inserted, updated and deleted per day.
    Rows inserted while the size stays flat is space PostgreSQL reuses. A counter that
    went down (statistics reset by a restart) is reported as None, not as a rate."""
    days = _days(series)
    span = days[-1]
    sizes: dict[str, list[tuple[float, int]]] = {}
    for day, item in zip(days, series, strict=True):
        database = item["reading"]["database"]
        hypertables = _hypertable_names(item["reading"])
        for table in database["tables"]:
            if table["table"] in hypertables:
                continue  # a hypertable's parent row; its chunks are counted below
            sizes.setdefault(table["table"], []).append((day, table["total_bytes"]))
        for hypertable in database["hypertables"]:
            if hypertable["hypertable"] != BARS_HYPERTABLE:
                total = sum(chunk["bytes"] for chunk in hypertable["chunks"])
                sizes.setdefault(hypertable["hypertable"], []).append((day, total))
    first = {t["table"]: t for t in series[0]["reading"]["database"]["tables"]}
    last = {t["table"]: t for t in series[-1]["reading"]["database"]["tables"]}
    for name in _hypertable_names(series[-1]["reading"]):
        first.pop(name, None)  # parent counters say nothing about the chunks
        last.pop(name, None)
    out = []
    for name, points in sizes.items():
        row: dict[str, Any] = {
            "relation": name,
            "readings": len(points),
            "last_gib": points[-1][1] / GIB,
            "mib_per_day": (
                slope([p[0] for p in points], [p[1] for p in points]) / MIB
                if len(points) > 1
                else None
            ),
        }
        if name in first and name in last:
            for counter in ("n_tup_ins", "n_tup_upd", "n_tup_del"):
                delta = last[name][counter] - first[name][counter]
                row[f"{counter}_per_day"] = delta / span if delta >= 0 and span > 0 else None
            row["n_dead_tup"] = last[name]["n_dead_tup"]
        out.append(row)
    out.sort(key=lambda row: -(row["mib_per_day"] or 0.0))
    return out


def bars_release(reading: dict[str, Any], cutoff_days: int) -> dict[str, Any]:
    """Bytes of hot bar chunks that end on or before the cutoff at this reading: the
    retention job is due to drop them (at most 3 days a night)."""
    taken = datetime.fromisoformat(reading["taken_at"])
    edge = taken - timedelta(days=cutoff_days)
    chunks = _bars(reading)["chunks"]
    due = [c for c in chunks if datetime.fromisoformat(c["range_end"]) <= edge]
    return {
        "cutoff_days": cutoff_days,
        "chunks": len(chunks),
        "chunks_due": len(due),
        "due_gib": sum(c["bytes"] for c in due) / GIB,
    }


def gaps(series: list[dict[str, Any]]) -> list[dict[str, str]]:
    out = []
    for before, after in itertools.pairwise(series):
        start = datetime.fromisoformat(before["reading"]["taken_at"])
        end = datetime.fromisoformat(after["reading"]["taken_at"])
        if end - start > MAX_READING_GAP:
            out.append({"after": before["name"], "before": after["name"]})
    return out


def select_window(
    series: list[dict[str, Any]], *, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """The readings taken in `[start, end]`. The window is an explicit input of every
    analysis, so a recompute (for example on the 2026-10-31 decision) is defined in
    advance rather than by whatever readings happen to exist."""
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise SeriesError("the readings window needs two UTC instants, start before end")
    chosen = [
        item
        for item in series
        if start <= datetime.fromisoformat(item["reading"]["taken_at"]) <= end
    ]
    if not chosen:
        raise SeriesError(f"no reading taken in [{start.isoformat()}, {end.isoformat()}]")
    return chosen


def load_gate_budget(path: Path) -> tuple[dict[str, Any], str]:
    body = path.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    if digest != GATE_BUDGET_SHA256:
        raise SeriesError(f"{path} is not PR 5's committed budget artifact ({digest})")
    return json.loads(body), digest


def verdicts(
    scenarios: dict[str, dict[str, Any]], *, series_days: float
) -> dict[str, dict[str, Any]]:
    """Per universe, after the due bars release: `fits` at the conservative rate,
    `fits_central_only`, or `does_not_fit`; `undetermined` while the series is shorter
    than a week, and `no_verdict` for a universe PR 5 could not measure."""
    out = {}
    for universe, conservative in scenarios["after_release_conservative"].items():
        central = scenarios["after_release_central"][universe]
        if series_days < MIN_SERIES_DAYS:
            verdict = "undetermined"
        elif conservative["fits"] is None:
            verdict = "no_verdict"
        elif conservative["fits"]:
            verdict = "fits"
        elif central["fits"]:
            verdict = "fits_central_only"
        else:
            verdict = "does_not_fit"
        out[universe] = {
            "verdict": verdict,
            "need_gib_conservative": conservative.get("need_gib"),
            "usable_gib": conservative.get("usable_gib"),
            "max_other_growth_mib_per_day": conservative.get("max_other_growth_mib_per_day"),
        }
    return out


def analyse(
    series: list[dict[str, Any]],
    gate_budget: dict[str, Any],
    *,
    cutoff_days: int,
    reserve_gib: float = RESERVE_GIB,
    horizon_months: int = HORIZON_MONTHS,
) -> dict[str, Any]:
    latest = series[-1]["reading"]
    series_days = _days(series)[-1]
    growth = rates(series)
    release = bars_release(latest, cutoff_days)
    free_gib = latest["filesystem"]["free"] / GIB
    scenarios = {
        f"{state}_{rate}": headroom(
            gate_budget["budget"],
            free_gib=free_gib,
            reserve_gib=reserve_gib,
            other_growth_gib_per_day=max(0.0, growth[f"{rate}_mib_per_day"]) * MIB / GIB,
            release_gib=release["due_gib"] if state == "after_release" else 0.0,
            horizon_months=horizon_months,
        )
        for state in ("now", "after_release")
        for rate in ("central", "conservative")
    }
    what_if: dict[str, Any] = {"relations": list(WHAT_IF_WITHOUT)}
    try:
        what_if_growth = rates(series, without=WHAT_IF_WITHOUT)
    except SeriesError as exc:
        # Not every database has these tables in every reading; the verdict stands alone.
        what_if |= {"available": False, "reason": str(exc)}
    else:
        what_if_scenarios = {
            f"after_release_{rate}": headroom(
                gate_budget["budget"],
                free_gib=free_gib,
                reserve_gib=reserve_gib,
                other_growth_gib_per_day=max(0.0, what_if_growth[f"{rate}_mib_per_day"])
                * MIB
                / GIB,
                release_gib=release["due_gib"],
                horizon_months=horizon_months,
            )
            for rate in ("central", "conservative")
        }
        what_if |= {
            "available": True,
            "removed_mib_per_day": growth["parts_mib_per_day"]["database_outside_bars"]
            - what_if_growth["parts_mib_per_day"]["database_outside_bars"],
            "central_mib_per_day": what_if_growth["central_mib_per_day"],
            "conservative_mib_per_day": what_if_growth["conservative_mib_per_day"],
            "verdicts": verdicts(what_if_scenarios, series_days=series_days),
        }
    usable_now = free_gib - reserve_gib
    conservative_gib_per_day = growth["conservative_mib_per_day"] * MIB / GIB
    days_to_reserve = (
        usable_now / conservative_gib_per_day if conservative_gib_per_day > 0 else None
    )
    return {
        "series_days": series_days,
        "readings": len(series),
        "gaps": gaps(series),
        "free_gib": free_gib,
        "growth": growth,
        "bars_release": release,
        "no_collection_days_to_reserve_conservative": days_to_reserve,
        "stop_condition_now": usable_now <= 0
        or (days_to_reserve is not None and days_to_reserve < STOP_PROJECTION_DAYS),
        "headroom": scenarios,
        "verdicts": verdicts(scenarios, series_days=series_days),
        "what_if_without_pump_short": what_if,
        "relations": relations(series),
        "series": [
            {
                "name": item["name"],
                "taken_at": item["reading"]["taken_at"],
                **parts(item["reading"]),
            }
            for item in series
        ],
    }


def write_once(out_dir: Path, record: dict[str, Any]) -> Path:
    stamp = datetime.fromisoformat(record["latest_reading_at"]).strftime("%Y%m%dT%H%M%SZ")
    target = out_dir / f"disk-growth-budget-{stamp}.json"
    sidecar = target.with_name(target.name + ".sha256")
    if target.exists() or sidecar.exists():
        raise FileExistsError(f"{target} already exists; an analysis is written once")
    body = (json.dumps(record, indent=1, sort_keys=True) + "\n").encode()
    out_dir.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as handle:
        handle.write(body)
    sidecar.write_text(hashlib.sha256(body).hexdigest() + "\n")
    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--readings", type=Path, required=True)
    parser.add_argument("--readings-from", type=datetime.fromisoformat, required=True)
    parser.add_argument("--readings-until", type=datetime.fromisoformat, required=True)
    parser.add_argument("--gate-budget", type=Path, required=True)
    parser.add_argument("--bars-cutoff-days", type=int, required=True)
    parser.add_argument("--code-revision", required=True)
    dirty = parser.add_mutually_exclusive_group(required=True)
    dirty.add_argument("--working-tree-dirty", action="store_true")
    dirty.add_argument("--no-working-tree-dirty", action="store_false", dest="working_tree_dirty")
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        series = select_window(
            load_series(args.readings), start=args.readings_from, end=args.readings_until
        )
        gate_budget, gate_sha = load_gate_budget(args.gate_budget)
        result = analyse(series, gate_budget, cutoff_days=args.bars_cutoff_days)
    except SeriesError as exc:
        sys.stderr.write(f"disk growth budget refused: {exc}\n")
        return 1
    record = {
        "version": VERSION,
        "code_revision": normalize_code_revision(args.code_revision),
        "working_tree_dirty": args.working_tree_dirty,
        "params": {
            "reserve_gib": RESERVE_GIB,
            "horizon_months": HORIZON_MONTHS,
            "days_per_month": DAYS_PER_MONTH,
            "min_series_days": MIN_SERIES_DAYS,
            "bars_cutoff_days": args.bars_cutoff_days,
            "readings_from": args.readings_from.isoformat(),
            "readings_until": args.readings_until.isoformat(),
        },
        "inputs": {
            "readings": [{"name": item["name"], "sha256": item["sha256"]} for item in series],
            "gate_budget_sha256": gate_sha,
        },
        "latest_reading_at": series[-1]["reading"]["taken_at"],
        **result,
    }
    path = write_once(args.out_dir, record)
    summary = {
        "path": str(path),
        "series_days": round(result["series_days"], 2),
        "central_mib_per_day": round(result["growth"]["central_mib_per_day"], 1),
        "conservative_mib_per_day": round(result["growth"]["conservative_mib_per_day"], 1),
        "verdicts": {k: v["verdict"] for k, v in result["verdicts"].items()},
    }
    sys.stdout.write(json.dumps(summary) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
