"""Storage, load and recovery budget for collecting Gate trade archives (PR 5).

Measures, from public archive metadata and already downloaded files only:

- the size of Gate's monthly trade archive per base for June and July 2026, via HEAD
  requests whose window is checked by the PR 3 guard
  (`pre_move_source_probe.classify_request`), so nothing dated on or after
  2026-08-01 is touched;
- how a downloaded archive expands and converts (CSV size, Parquet zstd size and
  conversion time) on the run 3 files of PR 3;
- the resulting monthly, 3-month and 6-month budget for several universes, against
  the measured server headroom passed in by the caller.

It computes no price, return or trade feature. The universe comes from PR 3's
committed run 3 artifact and the identity registry v4 evidence, not from a new
catalogue request.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import statistics
import sys
import tempfile
import time
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
DAYS_PER_MONTH = 30.44
HORIZONS_MONTHS: tuple[int, ...] = (3, 6)
REGISTRY_EVIDENCE_GLOB = "*-gate-bybit.json"


class BudgetStoppedError(RuntimeError):
    """A rate limit or the request budget stopped the measurement."""


def archive_url(base: str, month: str) -> str:
    return f"{GATE_ARCHIVE}/futures_usdt/trades/{month}/{base}_USDT-{month}.csv.gz"


def measure_sizes(
    client: httpx.Client,
    bases: Sequence[str],
    *,
    months: Sequence[str] = MONTHS,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, dict[str, int | None]]:
    """HEAD every (base, month) archive file; None when the file does not exist."""
    sizes: dict[str, dict[str, int | None]] = {}
    requests = 0
    for base in bases:
        sizes[base] = {}
        for month in months:
            url = archive_url(base, month)
            classify_request("HEAD", url, None)  # refuses anything past the boundary
            attempt = 0
            while True:
                if requests >= MAX_REQUESTS:
                    raise BudgetStoppedError("request budget reached")
                requests += 1
                try:
                    response = client.head(url, timeout=REQUEST_TIMEOUT_SECONDS)
                except httpx.TransportError:
                    if attempt < len(RETRY_BACKOFF_SECONDS):
                        sleep(RETRY_BACKOFF_SECONDS[attempt])
                        attempt += 1
                        continue
                    raise
                if response.status_code in STOP_STATUSES:
                    raise BudgetStoppedError(f"rate-limited: HTTP {response.status_code}")
                if response.status_code >= 500 and attempt < len(RETRY_BACKOFF_SECONDS):
                    sleep(RETRY_BACKOFF_SECONDS[attempt])
                    attempt += 1
                    continue
                break
            length = response.headers.get("content-length")
            sizes[base][month] = int(length) if response.status_code == 200 and length else None
    return sizes


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
    present = [b for b in bases if all(sizes.get(b, {}).get(m) for m in MONTHS)]
    per_month = {m: sum(sizes[b][m] or 0 for b in present) for m in MONTHS}
    per_base = sorted(statistics.fmean(sizes[b][m] or 0 for m in MONTHS) for b in present)
    return {
        "bases_requested": len(bases),
        "bases_present_both_months": len(present),
        "gz_bytes_by_month": per_month,
        "gz_bytes_month_mean": statistics.fmean(per_month.values()) if present else 0.0,
        "largest_base_gz_month_mean": per_base[-1] if per_base else 0,
        "median_base_gz_month_mean": statistics.median(per_base) if per_base else 0,
    }


def budget(
    sizes: dict[str, dict[str, int | None]],
    universes: Mapping[str, Sequence[str]],
    profile: dict[str, Any],
) -> dict[str, Any]:
    """Monthly and cumulative bytes per universe. Raw archives are kept (they are the
    native payload); Parquet is the processed copy. Both go to the offsite backup."""
    out: dict[str, Any] = {}
    for name, bases in universes.items():
        monthly = _monthly(sizes, bases)
        gz = monthly["gz_bytes_month_mean"]
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
                "streaming_conversion": monthly["largest_base_gz_month_mean"]
                * profile["parquet_per_gz"]
                / GIB,
                "gunzip_first": monthly["largest_base_gz_month_mean"] * profile["csv_per_gz"] / GIB,
            },
            "conversion_cpu_minutes_per_month": gz / GIB * profile["seconds_per_gib_gz"] / 60,
            "download_gib_per_month": gz / GIB,
        }
    return out


def headroom(budget_rows: dict[str, Any], *, free_gib: float, reserve_gib: float) -> dict[str, Any]:
    """Months each universe fits into the free disk above a fixed reserve."""
    usable = max(0.0, free_gib - reserve_gib)
    return {
        name: {
            "usable_gib": usable,
            "months_until_full": (
                usable / row["monthly_gib"]["kept_total"]
                if row["monthly_gib"]["kept_total"] > 0
                else math.inf
            ),
            "fits_6m": row["cumulative_gib"]["6m"] + row["scratch_gib"]["streaming_conversion"]
            <= usable,
        }
        for name, row in budget_rows.items()
    }


def registry_bases(evidence_dir: Path) -> list[str]:
    return sorted(
        {json.loads(p.read_text())["base"] for p in evidence_dir.glob(REGISTRY_EVIDENCE_GLOB)}
    )


def universes_from(pr3_artifact: dict[str, Any], registry: Sequence[str]) -> dict[str, list[str]]:
    universe: list[str] = list(pr3_artifact["catalogue"]["universe"])
    return {"registry_44": sorted(set(registry)), "universe_592": universe}


def largest(sizes: dict[str, dict[str, int | None]], bases: Sequence[str], k: int) -> list[str]:
    """The k bases with the largest mean archive over the measured months."""
    present = [b for b in bases if all(sizes.get(b, {}).get(m) for m in MONTHS)]
    return sorted(present, key=lambda b: (-statistics.fmean(sizes[b][m] or 0 for m in MONTHS), b))[
        :k
    ]


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pr3-artifact", type=Path, required=True)
    parser.add_argument("--registry-evidence", type=Path, required=True)
    parser.add_argument("--raw-dir", type=Path, required=True, help="PR 3 run 3 raw responses")
    parser.add_argument("--free-gib", type=float, required=True)
    parser.add_argument("--reserve-gib", type=float, required=True)
    parser.add_argument("--code-revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    pr3 = json.loads(args.pr3_artifact.read_bytes())
    registry = registry_bases(args.registry_evidence)
    universes = universes_from(pr3, registry)
    every = sorted(set().union(*universes.values()))
    files = [args.raw_dir / v["sha256"] for v in pr3["gate"]["g2"].values()]
    profile = conversion_profile(files)
    with httpx.Client(headers={"User-Agent": "schurfer-research-probe/1"}) as client:
        sizes = measure_sizes(client, every)
    top10 = largest(sizes, universes["universe_592"], 10)
    universes["universe_without_top10"] = [b for b in universes["universe_592"] if b not in top10]
    rows = budget(sizes, universes, profile)
    result = {
        "version": VERSION,
        "code_revision": args.code_revision,
        "pr3_artifact_sha256": hashlib.sha256(args.pr3_artifact.read_bytes()).hexdigest(),
        "months": list(MONTHS),
        "universes": {k: len(v) for k, v in universes.items()},
        "top10_by_size": top10,
        "sizes": sizes,
        "conversion_profile": profile,
        "budget": rows,
        "headroom": headroom(rows, free_gib=args.free_gib, reserve_gib=args.reserve_gib),
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
