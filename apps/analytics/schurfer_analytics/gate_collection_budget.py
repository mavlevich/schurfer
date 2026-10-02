"""Storage, load and recovery budget for collecting Gate trade archives (PR 5).

Measures, from public archive metadata and already downloaded files only:

- the size of Gate's monthly trade archive per base for June and July 2026, via HEAD
  requests whose window is checked by the PR 3 guard
  (`pre_move_source_probe.classify_request`), so nothing dated on or after
  2026-08-01 is touched;
- how a downloaded archive expands and converts (CSV size, Parquet zstd size and
  conversion time) on the run 3 files of PR 3;
- the resulting monthly, 3-month and 6-month budget for several universes, against
  the measured server headroom passed in by the caller, the growth of everything else
  on the disk (estimated from database metadata, see `other_growth`) and the space the
  hot bars retention is still due to release.

A request that neither returns a size nor a confirmed 404 stops the measurement:
an unmeasured archive is never counted as an absent one.

It computes no price, return or trade feature. The universe comes from PR 3's
committed run 3 artifact and the identity registry v4 evidence, not from a new
catalogue request.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from .pre_move_source_probe import (
    GATE_ARCHIVE,
    ProtocolViolationError,
    classify_request,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

VERSION = "gate_collection_budget_v1"
MONTHS: tuple[str, ...] = ("202606", "202607")
MAX_REQUESTS = 1_400
REQUEST_TIMEOUT_SECONDS = 30.0
RETRY_BACKOFF_SECONDS = (1.0, 4.0)
STOP_STATUSES = (418, 429)
GIB = 1024**3
MIB = 1024**2
DAYS_PER_MONTH = 30.44
HORIZONS_MONTHS: tuple[int, ...] = (3, 6)
REGISTRY_EVIDENCE_GLOB = "*-gate-bybit.json"
ABSENT_STATUS = 404
BARS_HYPERTABLE = "timeseries.bybit_momentum_bars_1m"


class BudgetStoppedError(RuntimeError):
    """A rate limit, the request budget or an unmeasured archive stopped the measurement."""


def archive_url(base: str, month: str) -> str:
    return f"{GATE_ARCHIVE}/futures_usdt/trades/{month}/{base}_USDT-{month}.csv.gz"


def measure_sizes(
    client: httpx.Client,
    bases: Sequence[str],
    *,
    months: Sequence[str] = MONTHS,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[dict[str, dict[str, int | None]], list[dict[str, Any]]]:
    """HEAD every (base, month) archive file. A size comes only from a 200 with a
    Content-Length; None only from a confirmed 404. Anything else, after the retries,
    stops the measurement. Returns the sizes and a log of every request sent."""
    sizes: dict[str, dict[str, int | None]] = {}
    log: list[dict[str, Any]] = []
    requests = 0
    for base in bases:
        sizes[base] = {}
        for month in months:
            url = archive_url(base, month)
            # Refuses anything past the boundary; an archive always has a window end.
            window_end = classify_request("HEAD", url, None).window_end
            attempt = 0
            while True:
                if requests >= MAX_REQUESTS:
                    raise BudgetStoppedError("request budget reached")
                requests += 1
                sent_at = now()
                try:
                    response = client.head(url, timeout=REQUEST_TIMEOUT_SECONDS)
                except httpx.TransportError as exc:
                    log.append(_log_row(base, month, url, window_end, sent_at, None, None, exc))
                    if attempt < len(RETRY_BACKOFF_SECONDS):
                        sleep(RETRY_BACKOFF_SECONDS[attempt])
                        attempt += 1
                        continue
                    raise BudgetStoppedError(f"transport error for {url}: {exc}") from exc
                length = response.headers.get("content-length")
                log.append(
                    _log_row(base, month, url, window_end, sent_at, response.status_code, length)
                )
                if response.status_code in STOP_STATUSES:
                    raise BudgetStoppedError(f"rate-limited: HTTP {response.status_code}")
                if response.status_code >= 500 and attempt < len(RETRY_BACKOFF_SECONDS):
                    sleep(RETRY_BACKOFF_SECONDS[attempt])
                    attempt += 1
                    continue
                break
            if response.status_code == ABSENT_STATUS:
                sizes[base][month] = None
            elif response.status_code == 200 and length is not None and length.isdigit():
                sizes[base][month] = int(length)
            else:
                raise BudgetStoppedError(
                    f"unmeasured archive {url}: HTTP {response.status_code}, "
                    f"content-length {length!r}"
                )
    return sizes, log


def _log_row(
    base: str,
    month: str,
    url: str,
    window_end: datetime | None,
    sent_at: datetime,
    status: int | None,
    length: str | None,
    error: Exception | None = None,
) -> dict[str, Any]:
    return {
        "base": base,
        "month": month,
        "url": url,
        "window_end": window_end.isoformat() if window_end else None,
        "sent_at": sent_at.isoformat(),
        "status": status,
        "content_length": length,
        "error": type(error).__name__ if error else None,
    }


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def conversion_profile(files: Sequence[Path]) -> dict[str, Any]:
    """Expansion and Parquet conversion of Gate trade archives (`timestamp, dealid,
    price, size`). Parquet is written with zstd by DuckDB, reading the gzip directly."""
    import duckdb

    rows: list[dict[str, Any]] = []
    for path in files:
        gz_bytes = path.stat().st_size
        csv_bytes = len(gzip.decompress(path.read_bytes()))
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.parquet"
            started = time.perf_counter()
            columns = (
                "{'timestamp': 'DOUBLE', 'dealid': 'BIGINT', 'price': 'VARCHAR', 'size': 'VARCHAR'}"
            )
            source = _sql_literal(str(path))
            sink = _sql_literal(str(target))
            # Both paths are local files of this measurement, quoted by _sql_literal.
            query = f"COPY (SELECT * FROM read_csv({source}, header=false, compression='gzip', columns={columns})) TO {sink} (FORMAT parquet, COMPRESSION zstd)"  # noqa: E501, S608
            duckdb.execute(query)
            seconds = time.perf_counter() - started
            parquet_bytes = target.stat().st_size
        rows.append(
            {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "gz_bytes": gz_bytes,
                "csv_bytes": csv_bytes,
                "parquet_bytes": parquet_bytes,
                "seconds": round(seconds, 3),
            }
        )
    total_gz = sum(r["gz_bytes"] for r in rows)
    return {
        "files": rows,
        "csv_per_gz": sum(r["csv_bytes"] for r in rows) / total_gz,
        "parquet_per_gz": sum(r["parquet_bytes"] for r in rows) / total_gz,
        "seconds_per_gib_gz": sum(r["seconds"] for r in rows) / (total_gz / GIB),
    }


def _monthly(sizes: dict[str, dict[str, int | None]], bases: Sequence[str]) -> dict[str, Any]:
    """Every measured file counts, including a base with only one month (a listing
    that started mid-period). A base missing from `sizes` was never measured."""
    unmeasured = sorted(b for b in bases if any(m not in sizes.get(b, {}) for m in MONTHS))
    measured = [b for b in bases if b not in unmeasured]
    files: dict[str, list[int]] = {
        b: [n for n in (sizes[b][m] for m in MONTHS) if n is not None] for b in measured
    }
    complete = [b for b in measured if len(files[b]) == len(MONTHS)]
    partial = sorted(b for b in measured if 0 < len(files[b]) < len(MONTHS))
    per_month = {m: sum(sizes[b][m] or 0 for b in measured) for m in MONTHS}
    per_base = sorted(max(f) for f in files.values() if f)
    if not bases or unmeasured:
        status = "insufficient_data"
    elif not per_base:
        status = "no_archives"
    else:
        status = "measured"
    return {
        "status": status,
        "bases_requested": len(bases),
        "bases_unmeasured": unmeasured,
        "bases_with_both_months": len(complete),
        "bases_with_one_month": partial,
        "bases_absent": len(measured) - len(complete) - len(partial),
        "gz_bytes_by_month": per_month,
        "gz_bytes_month_mean": statistics.fmean(per_month.values()),
        # The budget plans on the larger month: June and July differ by up to 2x.
        "gz_bytes_month_peak": max(per_month.values()),
        "largest_base_gz_month": per_base[-1] if per_base else 0,
        "median_base_gz_month": statistics.median(per_base) if per_base else 0,
    }


def budget(
    sizes: dict[str, dict[str, int | None]],
    universes: Mapping[str, Sequence[str]],
    profile: dict[str, Any],
) -> dict[str, Any]:
    """Monthly and cumulative bytes per universe at the peak measured month. Raw
    archives are kept (they are the native payload); Parquet is the processed copy.
    Both go to the offsite backup."""
    out: dict[str, Any] = {}
    for name, bases in universes.items():
        monthly = _monthly(sizes, bases)
        gz = monthly["gz_bytes_month_peak"]
        parquet = gz * profile["parquet_per_gz"]
        out[name] = {
            **monthly,
            "monthly_gib": {
                "raw_gz": gz / GIB,
                "parquet": parquet / GIB,
                "kept_total": (gz + parquet) / GIB,
            },
            "cumulative_gib": {f"{h}m": (gz + parquet) * h / GIB for h in HORIZONS_MONTHS},
            # Converting one file at a time from its gzip needs no CSV on disk; a plain
            # gunzip first would need the largest file's CSV as scratch space.
            "scratch_gib": {
                "streaming_conversion": monthly["largest_base_gz_month"]
                * profile["parquet_per_gz"]
                / GIB,
                "gunzip_first": monthly["largest_base_gz_month"] * profile["csv_per_gz"] / GIB,
            },
            # Elapsed seconds on the measuring machine, not CPU time on the server.
            "conversion_elapsed_minutes_per_month": gz / GIB * profile["seconds_per_gib_gz"] / 60,
            "download_gib_per_month": gz / GIB,
        }
    return out


def headroom(
    budget_rows: dict[str, Any],
    *,
    free_gib: float,
    reserve_gib: float,
    other_growth_gib_per_day: float,
    release_gib: float = 0.0,
    horizon_months: int = 6,
) -> dict[str, Any]:
    """Whether each universe fits over the horizon while the reserve stays free.

    The reserve is never spent: the growth of everything else on the disk and the
    collection both come out of the space above it. `release_gib` is space already
    due to be freed (old hot bars past the retention cutoff). A universe without a
    complete measurement gets no verdict, and none fits when the disk is already
    below the reserve."""
    usable = free_gib + release_gib - reserve_gib
    days = horizon_months * DAYS_PER_MONTH
    out: dict[str, Any] = {}
    for name, row in budget_rows.items():
        if row["status"] == "insufficient_data":
            out[name] = {"status": "insufficient_data", "fits": None}
            continue
        if usable <= 0:
            out[name] = {"status": "below_reserve", "usable_gib": usable, "fits": False}
            continue
        collection = row["monthly_gib"]["kept_total"] / DAYS_PER_MONTH
        need = (
            row["cumulative_gib"][f"{horizon_months}m"] + row["scratch_gib"]["streaming_conversion"]
        )
        daily = collection + other_growth_gib_per_day
        out[name] = {
            "status": "measured",
            "usable_gib": usable,
            "horizon_months": horizon_months,
            "need_gib": need + other_growth_gib_per_day * days,
            "fits": need + other_growth_gib_per_day * days <= usable,
            "days_until_reserve": usable / daily if daily > 0 else None,
            # The largest growth of everything else that still leaves the horizon fitting.
            "max_other_growth_mib_per_day": max(0.0, (usable - need) / days) * GIB / MIB,
        }
    return out


def other_growth(inputs: dict[str, Any]) -> dict[str, Any]:
    """Growth of the database outside the hot bars, from metadata (growth-inputs.sql).

    Plain tables: mean bytes per row times the rows created per day in the window.
    Hypertables: bytes per day of the chunks lying wholly in the window, except one
    whose retention already drops its oldest chunks (steady state, no net growth).
    Bars are excluded: the gated deletion holds them at the retention cutoff."""
    start = datetime.fromisoformat(inputs["window"]["start"]).replace(tzinfo=UTC)
    end = datetime.fromisoformat(inputs["window"]["end"]).replace(tzinfo=UTC)
    window_days = (end - start).total_seconds() / 86400
    measured_at = datetime.fromisoformat(inputs["measured_at"])
    rates: dict[str, float] = {}
    for table in inputs["plain_tables"]:
        per_row = table["bytes"] / table["rows"] if table["rows"] else 0.0
        rates[table["table"]] = per_row * table["rows_in_window"] / window_days
    for table in inputs["hypertables"]:
        name = table["hypertable"]
        if name == BARS_HYPERTABLE:
            continue
        drop_after = _days(table["retention_drop_after"])
        oldest = datetime.fromisoformat(table["oldest_chunk_start"])
        if drop_after is not None and oldest <= measured_at - timedelta(days=drop_after):
            rates[name] = 0.0
            continue
        inside = [
            c
            for c in table["chunks"]
            if datetime.fromisoformat(c["start"]) >= start
            and datetime.fromisoformat(c["end"]) <= end
        ]
        covered = sum(
            (datetime.fromisoformat(c["end"]) - datetime.fromisoformat(c["start"])).total_seconds()
            for c in inside
        )
        rates[name] = sum(c["bytes"] for c in inside) / (covered / 86400) if covered else 0.0
    return {
        "mib_per_day": {k: v / MIB for k, v in sorted(rates.items())},
        "total_gib_per_day": sum(rates.values()) / GIB,
    }


def bars_release_gib(inputs: dict[str, Any], cutoff_days: int) -> float:
    """Bytes of hot bar chunks already past the retention cutoff, which the gated
    deletion drops (a few days a night) once each day is verified offsite."""
    measured_at = datetime.fromisoformat(inputs["measured_at"])
    midnight = measured_at.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = midnight - timedelta(days=cutoff_days)
    (bars,) = (t for t in inputs["hypertables"] if t["hypertable"] == BARS_HYPERTABLE)
    past: int = sum(
        c["bytes"] for c in bars["chunks"] if datetime.fromisoformat(c["end"]) <= cutoff
    )
    return past / GIB


def _days(interval: str | None) -> int | None:
    if interval is None:
        return None
    number, unit = interval.split()
    if unit not in ("day", "days"):
        raise ValueError(f"unexpected retention interval {interval!r}")
    return int(number)


def registry_bases(evidence_dir: Path) -> list[str]:
    bases = sorted(
        {json.loads(p.read_text())["base"] for p in evidence_dir.glob(REGISTRY_EVIDENCE_GLOB)}
    )
    if not bases:
        raise ValueError(f"no {REGISTRY_EVIDENCE_GLOB} evidence under {evidence_dir}")
    return bases


def universes_from(pr3_artifact: dict[str, Any], registry: Sequence[str]) -> dict[str, list[str]]:
    universe: list[str] = list(pr3_artifact["catalogue"]["universe"])
    if not universe:
        raise ValueError("the PR 3 artifact has an empty universe")
    return {"registry_44": sorted(set(registry)), "universe_592": universe}


def largest(sizes: dict[str, dict[str, int | None]], bases: Sequence[str], k: int) -> list[str]:
    """The k bases with the largest archive in their larger measured month."""
    present = [b for b in bases if any(sizes.get(b, {}).get(m) for m in MONTHS)]
    return sorted(present, key=lambda b: (-max(sizes[b].get(m) or 0 for m in MONTHS), b))[:k]


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pr3-artifact", type=Path, required=True)
    parser.add_argument("--registry-evidence", type=Path, required=True)
    parser.add_argument("--raw-dir", type=Path, required=True, help="PR 3 run 3 raw responses")
    parser.add_argument("--free-gib", type=float, required=True)
    parser.add_argument("--reserve-gib", type=float, required=True)
    parser.add_argument("--growth-inputs", type=Path, required=True, help="growth-inputs.sql")
    parser.add_argument("--bars-cutoff-days", type=int, required=True)
    parser.add_argument("--code-revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    pr3 = json.loads(args.pr3_artifact.read_bytes())
    registry = registry_bases(args.registry_evidence)
    universes = universes_from(pr3, registry)
    every = sorted(set().union(*universes.values()))
    files = [args.raw_dir / v["sha256"] for v in pr3["gate"]["g2"].values()]
    profile = conversion_profile(files)
    growth_inputs = json.loads(args.growth_inputs.read_bytes())
    growth = other_growth(growth_inputs)
    release = bars_release_gib(growth_inputs, args.bars_cutoff_days)
    with httpx.Client(headers={"User-Agent": "schurfer-research-probe/1"}) as client:
        sizes, request_log = measure_sizes(client, every)
    top10 = largest(sizes, universes["universe_592"], 10)
    universes["universe_without_top10"] = [b for b in universes["universe_592"] if b not in top10]
    rows = budget(sizes, universes, profile)
    scenarios = {
        f"{state}_{rate}": headroom(
            rows,
            free_gib=args.free_gib,
            reserve_gib=args.reserve_gib,
            other_growth_gib_per_day=growth["total_gib_per_day"] if rate == "table_growth" else 0.0,
            release_gib=release if state == "after_bar_release" else 0.0,
        )
        for state in ("now", "after_bar_release")
        for rate in ("table_growth", "no_other_growth")
    }
    result = {
        "version": VERSION,
        "code_revision": args.code_revision,
        "pr3_artifact_sha256": hashlib.sha256(args.pr3_artifact.read_bytes()).hexdigest(),
        "months": list(MONTHS),
        "universes": {k: len(v) for k, v in universes.items()},
        "top10_by_size": top10,
        "sizes": sizes,
        "request_log": request_log,
        "conversion_profile": profile,
        "budget": rows,
        "growth_inputs_sha256": hashlib.sha256(args.growth_inputs.read_bytes()).hexdigest(),
        "other_growth": growth,
        "bars_cutoff_days": args.bars_cutoff_days,
        "bars_release_gib": release,
        "headroom": scenarios,
        "free_gib": args.free_gib,
        "reserve_gib": args.reserve_gib,
    }
    body = json.dumps(result, indent=2, sort_keys=True, default=str).encode() + b"\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(body)
    args.output.with_name(args.output.name + ".sha256").write_text(
        hashlib.sha256(body).hexdigest() + "\n"
    )
    sys.stdout.write(
        json.dumps({"bases": len(every), "sha256": hashlib.sha256(body).hexdigest()}) + "\n"
    )


__all__ = ["BudgetStoppedError", "ProtocolViolationError"]

if __name__ == "__main__":
    main()
