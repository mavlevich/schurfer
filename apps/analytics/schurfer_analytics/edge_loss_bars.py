"""Reduce verified cold-bar days to the columns the edge-loss study reads.

docs/research/edge-loss-decomposition-v1.md, amendment 1: every day of the window
(2026-08-13..09-28) is read from a verified Borg fetch, so all days share one provenance.
A full day is about 320 MB, and the production host keeps a 10 GiB reserve, so the days
go through in small batches: each one is fetched and verified against its receipt
(`cold_bar_fetch.fetch_day`), reduced to the study's columns, and the full file this
run fetched is then deleted. A file that was already present is never deleted.

The reduced file has its own manifest (source sha256 and rows, reduced sha256 and rows),
so the analysis can check the chain from the receipt to what it reads. Only linear v1
bars of Bybit and Binance are kept. No price is printed: the output is counts only.

Read-only towards the database and the repository.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

from .cold_bar_fetch import (
    DEFAULT_RESERVE_BYTES,
    FetchError,
    days_between,
    ensure_reserve,
    fetch_day,
    sha256_of,
)
from .cold_bar_gated_deletion_collectors import parse_env_file

REDUCTION_VERSION = "edge_loss_bars_v1"
WINDOW_FIRST = date(2026, 8, 13)
WINDOW_LAST = date(2026, 9, 28)
MAX_DAYS = 3
# The reduction's own disk and memory bounds. DuckDB spills into a temporary directory
# capped at TEMP_CAP_BYTES; the output is a subset of the source, so it is bounded by the
# source's size. Both are checked against the reserve before the work starts, and the
# reserve is checked again after it.
TEMP_CAP_BYTES = 2 * 1024**3
DUCKDB_MEMORY_LIMIT = "1GB"
DUCKDB_THREADS = 2
COLUMNS = (
    "exchange",
    "symbol",
    "bucket_start",
    "open_price",
    "high_price",
    "low_price",
    "close_price",
    "buy_total_notional_usd",
    "sell_total_notional_usd",
    "trade_count",
    "trades_complete",
    "price_complete",
    "complete",
)
_FILTER = "market_type = 'linear' AND capture_version = 'v1' AND exchange IN ('bybit', 'binance')"


class ReduceError(RuntimeError):
    pass


def reduced_name(day: str) -> str:
    return f"edge-loss-bars-{day}.parquet"


def reduced_state(out_dir: Path, day: str) -> str:
    """`complete` only when the reduced file and its manifest both exist and agree.

    A file without a manifest is the leftover of an interrupted run (the file is linked
    before the manifest): it is removed and the day is reduced again. A manifest
    without its file, or a file that differs from its manifest, is refused.
    """
    dest = out_dir / reduced_name(day)
    manifest_path = dest.with_suffix(".manifest.json")
    if manifest_path.exists():
        if not dest.exists():
            raise ReduceError(f"{day}: a manifest without its reduced file")
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("day") != day or sha256_of(dest) != manifest.get("reduced_sha256"):
            raise ReduceError(f"{day}: {dest.name} does not match its manifest")
        return "complete"
    if dest.exists():
        dest.unlink()
    return "absent"


def reduce_day(
    source: Path,
    day: str,
    source_sha: str,
    source_rows: int,
    out_dir: Path,
    reserve_bytes: int = DEFAULT_RESERVE_BYTES,
) -> dict[str, Any]:
    """Write the reduced Parquet and its manifest within the disk reserve."""
    import duckdb

    dest = out_dir / reduced_name(day)
    manifest_path = dest.with_suffix(".manifest.json")
    if dest.exists() or manifest_path.exists():
        raise ReduceError(f"{day}: {dest.name} or its manifest already exists")
    try:
        ensure_reserve(out_dir, source.stat().st_size + TEMP_CAP_BYTES, reserve_bytes)
    except FetchError as exc:
        raise ReduceError(f"{day}: {exc}") from exc
    partial = dest.with_name(f".{dest.name}.{os.getpid()}.partial")
    spill = out_dir / f".duckdb-tmp-{os.getpid()}"
    con = duckdb.connect()
    try:
        con.execute(f"SET memory_limit = '{DUCKDB_MEMORY_LIMIT}'")
        con.execute(f"SET threads = {DUCKDB_THREADS}")
        con.execute(f"SET temp_directory = '{spill}'")
        con.execute(f"SET max_temp_directory_size = '{TEMP_CAP_BYTES}B'")
        con.execute(
            f"COPY (SELECT {', '.join(COLUMNS)} FROM read_parquet(?) WHERE {_FILTER}"  # noqa: S608 -- fixed columns
            " ORDER BY exchange, symbol, bucket_start)"
            f" TO '{partial}' (FORMAT parquet, COMPRESSION zstd)",
            [str(source)],
        )
        row = con.execute(
            "SELECT count(*), min(bucket_start), max(bucket_start) FROM read_parquet(?)",
            [str(partial)],
        ).fetchone()
        assert row is not None
        rows, first, last = int(row[0]), row[1], row[2]
        if rows and (first.date().isoformat() != day or last.date().isoformat() != day):
            raise ReduceError(f"{day}: reduced bars fall outside the day")
        by_exchange = dict(
            con.execute(
                "SELECT exchange, count(*) FROM read_parquet(?) GROUP BY 1 ORDER BY 1",
                [str(partial)],
            ).fetchall()
        )
        try:
            ensure_reserve(out_dir, 0, reserve_bytes)
        except FetchError as exc:
            raise ReduceError(f"{day}: below the reserve after reducing: {exc}") from exc
        os.link(partial, dest)
    finally:
        partial.unlink(missing_ok=True)
        con.close()
        if spill.exists():
            for leftover in spill.iterdir():
                leftover.unlink()
            spill.rmdir()
    manifest: dict[str, Any] = {
        "reduction_version": REDUCTION_VERSION,
        "day": day,
        "columns": list(COLUMNS),
        "filter": _FILTER,
        "source_sha256": source_sha,
        "source_rows": source_rows,
        "reduced_sha256": sha256_of(dest),
        "reduced_rows": rows,
        "rows_by_exchange": by_exchange,
    }
    tmp = manifest_path.with_name(f".{manifest_path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    os.link(tmp, manifest_path)
    tmp.unlink()
    return manifest


def check_window(days: list[str]) -> None:
    for day in days:
        d = date.fromisoformat(day)
        if not WINDOW_FIRST <= d <= WINDOW_LAST:
            raise SystemExit(
                f"{day} is outside the registered window {WINDOW_FIRST}..{WINDOW_LAST}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cold-bars-dir", type=Path, required=True)
    parser.add_argument("--backup-env", type=Path, required=True, help="backup.env (BORG_*)")
    parser.add_argument("--fetch-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--from", dest="first", type=date.fromisoformat, required=True)
    parser.add_argument("--to", dest="last", type=date.fromisoformat, required=True)
    args = parser.parse_args(argv)
    days = days_between(args.first, args.last)
    if len(days) > MAX_DAYS:
        raise SystemExit(f"{len(days)} days requested, more than {MAX_DAYS} per run")
    check_window(days)
    env = parse_env_file(args.backup_env.read_text())
    repo = env.get("BORG_REPO")
    if not repo:
        raise SystemExit(f"BORG_REPO not found in {args.backup_env}")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    done: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for day in days:
        try:
            if reduced_state(args.out_dir, day) == "complete":
                done.append({"day": day, "status": "already_reduced"})
                continue
            got = fetch_day(
                day,
                cold_bars_dir=args.cold_bars_dir,
                out_dir=args.fetch_dir,
                repo=repo,
                env=env,
                reserve_bytes=DEFAULT_RESERVE_BYTES,
            )
            try:
                manifest = reduce_day(got.path, day, got.sha256, got.rows, args.out_dir)
            finally:
                if not got.already_present:
                    got.path.unlink(missing_ok=True)
            done.append(
                {
                    "day": day,
                    "status": "reduced",
                    "rows_by_exchange": manifest["rows_by_exchange"],
                    "source_deleted": not got.already_present,
                }
            )
        except (FetchError, ReduceError) as exc:
            failed.append({"day": day, "error": str(exc)})
    sys.stdout.write(json.dumps({"done": done, "failed": failed}, indent=1) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
