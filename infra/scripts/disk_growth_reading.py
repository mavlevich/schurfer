#!/usr/bin/env python3
"""One daily disk-growth reading for the storage budget (PR 5 correction follow-up).

Writes `reading-<UTC>.json` (and a `.sha256` sidecar) with what a week-long series needs
to tell data growth, the hot-bars release and PostgreSQL's reuse of space apart:

- the filesystem's free and used bytes;
- every plain table in `app` and `timeseries`: heap, TOAST, index and total bytes, and
  its insert, update, HOT-update, delete, live and dead counters and autovacuum history
  (rows inserted while the size stays flat is space being reused);
- every hypertable chunk with its range, compression and bytes (new chunks against
  dropped old bars);
- PostgreSQL temporary-file counters, Docker's disk summary and the sizes of the
  `runtime/` directories.

Read-only: catalog and statistics views, the filesystem's usage, `docker system df` and
a walk of `runtime/`. It reads no
row of any table. Standard library only, because it runs on the host from a systemd
timer (schurfer-disk-growth-reading.timer).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

VERSION = "disk_growth_reading_v1"
POSTGRES_CONTAINER = "schurfer-postgres"
RUNTIME_DIR = Path("/opt/schurfer/runtime")

# Statistics and catalog views only (asserted by the tests): no table row is read.
SQL = """
SELECT json_build_object(
  'measured_at', now(),
  'database_bytes', pg_database_size(current_database()),
  'temp', (
    SELECT json_build_object('temp_files', temp_files, 'temp_bytes', temp_bytes,
                             'stats_reset', stats_reset)
    FROM pg_stat_database WHERE datname = current_database()
  ),
  'postmaster_start', pg_postmaster_start_time(),
  'tables', (
    SELECT coalesce(json_agg(json_build_object(
      'table', s.schemaname || '.' || s.relname,
      'heap_bytes', pg_relation_size(s.relid),
      'toast_bytes', CASE WHEN c.reltoastrelid = 0 THEN 0
                          ELSE pg_relation_size(c.reltoastrelid) END,
      'index_bytes', pg_indexes_size(s.relid),
      'total_bytes', pg_total_relation_size(s.relid),
      'n_live_tup', s.n_live_tup, 'n_dead_tup', s.n_dead_tup,
      'n_tup_ins', s.n_tup_ins, 'n_tup_upd', s.n_tup_upd,
      'n_tup_hot_upd', s.n_tup_hot_upd, 'n_tup_del', s.n_tup_del,
      'last_autovacuum', s.last_autovacuum, 'autovacuum_count', s.autovacuum_count
    ) ORDER BY s.schemaname, s.relname), '[]'::json)
    FROM pg_stat_user_tables s JOIN pg_class c ON c.oid = s.relid
    WHERE s.schemaname IN ('app', 'timeseries')
  ),
  'hypertables', (
    SELECT coalesce(json_agg(json_build_object(
      'hypertable', h.hypertable_schema || '.' || h.hypertable_name,
      'chunks', (
        SELECT coalesce(json_agg(json_build_object(
          'chunk', c.chunk_name,
          'range_start', c.range_start,
          'range_end', c.range_end,
          'compressed', c.is_compressed,
          'bytes', pg_total_relation_size(format('%I.%I', c.chunk_schema, c.chunk_name))
            + coalesce((
              SELECT pg_total_relation_size(format('%I.%I', cc.schema_name, cc.table_name))
              FROM _timescaledb_catalog.chunk k
              JOIN _timescaledb_catalog.chunk cc ON cc.id = k.compressed_chunk_id
              WHERE k.schema_name = c.chunk_schema AND k.table_name = c.chunk_name), 0)
        ) ORDER BY c.range_start), '[]'::json)
        FROM timescaledb_information.chunks c
        WHERE c.hypertable_schema = h.hypertable_schema
          AND c.hypertable_name = h.hypertable_name
      )
    ) ORDER BY h.hypertable_schema, h.hypertable_name), '[]'::json)
    FROM timescaledb_information.hypertables h
  )
)
"""


class ReadingError(RuntimeError):
    pass


def _run(args: list[str], *, stdin: str | None = None) -> str:
    done = subprocess.run(  # noqa: S603 -- fixed argv
        args, input=stdin, capture_output=True, text=True, check=False, timeout=600
    )
    if done.returncode != 0:
        raise ReadingError(f"{args[0]} failed: {done.stderr.strip()[-300:]}")
    return done.stdout


def database_reading() -> dict[str, Any]:
    out = _run(
        [
            "docker",
            "exec",
            "-i",
            POSTGRES_CONTAINER,
            "psql",
            "-U",
            "schurfer",
            "-d",
            "schurfer",
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
        ],
        stdin=SQL,
    )
    payload: dict[str, Any] = json.loads(out)
    return payload


def docker_summary(text: str) -> list[dict[str, Any]]:
    """`docker system df --format '{{json .}}'`: one JSON object per line."""
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def filesystem(path: Path) -> dict[str, int]:
    usage = shutil.disk_usage(path)
    return {"total": usage.total, "used": usage.used, "free": usage.free}


def take_reading(runtime_dir: Path) -> dict[str, Any]:
    directories = sorted(str(p) for p in runtime_dir.iterdir() if p.is_dir())
    return {
        "version": VERSION,
        "taken_at": datetime.now(UTC).isoformat(),
        "filesystem": filesystem(runtime_dir),
        "database": database_reading(),
        "docker": docker_summary(_run(["docker", "system", "df", "--format", "{{json .}}"])),
        "runtime_dirs": _runtime(directories),
    }


def _runtime(directories: list[str]) -> dict[str, Any]:
    """Apparent size of each directory (the bytes of its files). A directory this user
    may not read (root-owned drill state) is reported as unreadable instead of failing
    the whole reading."""
    sizes: dict[str, int] = {}
    unreadable: set[str] = set()
    for directory in directories:
        total = 0

        def note(error: OSError) -> None:
            unreadable.add(str(error.filename))

        for root, _dirs, files in os.walk(directory, onerror=note):
            for name in files:
                try:
                    total += (Path(root) / name).lstat().st_size
                except OSError as error:
                    note(error)
        sizes[Path(directory).name] = total
    return {"sizes": sizes, "unreadable": sorted(unreadable)}


def write_reading(out_dir: Path, reading: dict[str, Any]) -> Path:
    """Prepare the reading and its SHA-256 under temporary names, publish the hash, then
    the reading last: a `reading-*.json` exists only when its hash is already beside it.
    Any failure removes what this call wrote."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(reading["taken_at"]).strftime("%Y%m%dT%H%M%SZ")
    target = out_dir / f"reading-{stamp}.json"
    body = (json.dumps(reading, indent=1, sort_keys=True, default=str) + "\n").encode()
    sidecar = target.with_name(target.name + ".sha256")
    partial_body = out_dir / f".{target.name}.partial"
    partial_hash = out_dir / f".{sidecar.name}.partial"
    try:
        partial_body.write_bytes(body)
        partial_hash.write_bytes((hashlib.sha256(body).hexdigest() + "\n").encode())
        partial_hash.replace(sidecar)
        partial_body.replace(target)
    except BaseException:
        for path in (partial_body, partial_hash, sidecar):
            path.unlink(missing_ok=True)
        raise
    return target


MAX_AGE = timedelta(hours=36)  # a daily timer; one missed run is already a failure


def check_latest(out_dir: Path, *, now: datetime, max_age: timedelta = MAX_AGE) -> list[str]:
    """Problems with the newest reading: none, unreadable, a hash that does not match,
    another format version, or older than `max_age`. An empty list means healthy."""
    readings = sorted(out_dir.glob("reading-*.json")) if out_dir.is_dir() else []
    if not readings:
        return [f"no reading in {out_dir}"]
    latest = readings[-1]
    sidecar = latest.with_name(latest.name + ".sha256")
    if not sidecar.exists():
        return [f"{latest.name} has no .sha256"]
    body = latest.read_bytes()
    if hashlib.sha256(body).hexdigest() != sidecar.read_text().strip():
        return [f"{latest.name} does not match its .sha256"]
    try:
        payload = json.loads(body)
        taken = datetime.fromisoformat(payload["taken_at"])
    except (ValueError, KeyError, TypeError):
        return [f"{latest.name} is not a reading"]
    problems = []
    if payload.get("version") != VERSION:
        problems.append(f"{latest.name} is {payload.get('version')!r}, not {VERSION!r}")
    if now - taken > max_age:
        problems.append(f"the newest reading is from {taken.isoformat()}, older than {max_age}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, default=RUNTIME_DIR)
    parser.add_argument(
        "--check", action="store_true", help="check the newest reading instead of taking one"
    )
    args = parser.parse_args(argv)
    if args.check:
        problems = check_latest(args.out_dir, now=datetime.now(UTC))
        for problem in problems:
            sys.stderr.write(f"disk growth readings unhealthy: {problem}\n")
        if not problems:
            sys.stdout.write("disk growth readings healthy\n")
        return 1 if problems else 0
    try:
        path = write_reading(args.out_dir, take_reading(args.runtime_dir))
    except (ReadingError, OSError, ValueError) as exc:
        sys.stderr.write(f"disk growth reading failed: {exc}\n")
        return 1
    sys.stdout.write(f"{path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
