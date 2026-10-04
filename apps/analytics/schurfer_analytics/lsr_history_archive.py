"""History archive pilot for `app.live_long_short_ratio`: the LSR contract, the reader
that joins archived and live rows, and the deletion dry-run.

Design and audit: docs/runbooks/history-archive-design-v1.md. The export, archive,
verify and fetch steps are the shared engine in `history_archive`. This module never
deletes production rows and never raises a fence.

- **read_lsr** reads verified archive ranges below the fence from fetched Parquet and the
  rest from PostgreSQL, from one snapshot, repeating the read if a prune moved the fence.
- **Deletion dry-run** explains, per chunk, every reason it may not be dropped yet.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .cold_bar_gated_deletion_collectors import borg_list_archives_args, parse_short_list
from .history_archive import (
    DEFAULT_MAX_CACHE_BYTES,
    DEFAULT_RESERVE_BYTES,
    FINGERPRINT_VERSION,
    TIMESTAMPTZ,
    ArchiveError,
    CatalogRow,
    Chunk,
    Column,
    DatasetContract,
    Reader,
    _copy_rows_gz,
    _csv_to_parquet,
    _run,
    _snapshot,
    _sql_str,
    borg_env,
    covering_ranges,
    duck_type,
    fence_of,
    fetch,
    list_chunks,
    live_rows,
    pg_fingerprint_sql,
    run_archive,
    run_export,
    run_verify,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

LSR_CONTRACT = DatasetContract(
    dataset="lsr_history",
    contract_version="lsr_history_v1",
    schema_version="lsr_parquet_v1",
    export_version="lsr_export_v1",
    source_schema="app",
    source_table="live_long_short_ratio",
    time_column="ts",
    key=("exchange", "base", "ts"),
    columns=(
        Column("ts", TIMESTAMPTZ),
        Column("base", "text"),
        Column("exchange", "text"),
        Column("ratio", "numeric"),
        Column("long_account", "numeric"),
        Column("short_account", "numeric"),
    ),
    archive_prefix="history-lsr-",
    hot_days=14,
    readers=(
        Reader(
            "api-gateway pumps signal (MAD score)",
            "4 h before the anchor of every open pump episode; protected by the open-episode "
            "gate, never read from the archive",
        ),
        Reader(
            "research",
            "only through read_lsr; no analytics module queries the table directly "
            "(enforced by test_no_direct_lsr_readers)",
        ),
    ),
    open_episode_lookback=timedelta(hours=4),
)


ARCHIVE_PREFIX = LSR_CONTRACT.archive_prefix


READ_ATTEMPTS = 3


def read_lsr(
    dsn: str,
    start: datetime,
    end: datetime,
    *,
    cache_dir: Path,
    fetcher: Callable[[datetime, datetime], list[Path]],
    contract: DatasetContract = LSR_CONTRACT,
) -> Any:
    """All rows of `[start, end)` as a DuckDB relation with the archive's column types.

    The fence, the catalog and the live rows come from ONE snapshot, so the split and
    the live half agree. Below the fence, the verified ranges must tile the interval
    exactly and every one of their files must be present. A prune commits its chunk
    drop and its fence move together, so if the fence read again after the snapshot
    differs, a prune may have removed live rows this read relied on: the whole read is
    repeated (at most `READ_ATTEMPTS` times) rather than returned short."""
    import duckdb
    import psycopg

    cache_dir.mkdir(parents=True, exist_ok=True)
    columns = ", ".join(f"{c.name} {duck_type(c)}" for c in contract.columns)
    for _ in range(READ_ATTEMPTS):
        # Both halves are copied into the connection, so a later cache eviction cannot
        # pull a file out from under the returned relation.
        connection = duckdb.connect()
        connection.execute(f"CREATE TABLE lsr ({columns})")
        with tempfile.TemporaryDirectory(dir=cache_dir) as tmp:
            live = Path(tmp) / "live.csv.gz"
            with _snapshot(dsn) as conn:
                fence = fence_of(conn, contract)
                split = start if fence is None else min(max(fence, start), end)
                catalog = live_rows(conn, contract)
                if end > split:
                    _copy_rows_gz(conn, contract, split, end, live)
            if split > start:
                ranges = covering_ranges(catalog, start, split)
                by_name = {p.name: p for p in fetcher(start, split)}
                missing = [r.file_name for r in ranges if r.file_name not in by_name]
                if missing:
                    raise ArchiveError(f"the fetch did not return {missing}")
                listing = ", ".join(_sql_str(str(by_name[r.file_name])) for r in ranges)
                connection.execute(
                    f"INSERT INTO lsr SELECT * FROM read_parquet([{listing}]) "  # noqa: S608
                    f"WHERE {contract.time_column} >= {_sql_str(start.isoformat())}::TIMESTAMPTZ "
                    f"AND {contract.time_column} < {_sql_str(split.isoformat())}::TIMESTAMPTZ"
                )
            if end > split:
                parquet = Path(tmp) / "live.parquet"
                _csv_to_parquet(contract, live, parquet)
                source = f"read_parquet({_sql_str(str(parquet))})"
                connection.execute(f"INSERT INTO lsr SELECT * FROM {source}")  # noqa: S608
        with psycopg.connect(dsn, autocommit=True) as conn:
            if fence_of(conn, contract) == fence:
                return connection.table("lsr")
        connection.close()
    raise ArchiveError(f"the fence kept moving during {READ_ATTEMPTS} reads; try again later")


# ---------- deletion dry-run ----------


@dataclass(frozen=True)
class DropVerdict:
    chunk: str
    range_start: str
    range_end: str
    eligible: bool
    blockers: tuple[str, ...]


def open_episode_floor(conn: Any, lookback: timedelta) -> datetime | None:
    """The earliest instant an open pump episode's API window reaches back to."""
    row = conn.execute(
        "SELECT min(coalesce(entry_qualified_at, first_seen_at)) FROM app.pump_events "
        "WHERE closed_at IS NULL"
    ).fetchone()
    if row is None or row[0] is None:
        return None
    anchor: datetime = row[0].astimezone(UTC)
    return anchor - lookback


def drop_verdicts(
    *,
    contract: DatasetContract,
    chunks: Sequence[Chunk],
    catalog: Mapping[datetime, CatalogRow],
    live_fingerprint: Callable[[Chunk], str | None],
    archives: frozenset[str] | None,
    fence: datetime | None,
    episode_floor: datetime | None,
    now: datetime,
) -> list[DropVerdict]:
    """Every reason each chunk may not be dropped yet. Pure: evidence is passed in."""
    verdicts: list[DropVerdict] = []
    contiguous = True
    for chunk in chunks:
        blockers: list[str] = []
        if chunk.range_end > now - timedelta(days=contract.hot_days):
            blockers.append(f"inside the {contract.hot_days}-day hot window")
        row = catalog.get(chunk.range_start)
        if row is None:
            blockers.append("not exported")
        elif row.range_end != chunk.range_end or row.chunk_name != chunk.name:
            blockers.append("catalog range does not match the chunk")
        elif row.state != "verified":
            blockers.append(f"catalog state is {row.state}, not verified")
        if row is not None and row.state == "verified":
            current = live_fingerprint(chunk)
            if current != row.content_fingerprint:
                blockers.append("source changed since export; re-export")
            if archives is None:
                blockers.append("Borg archive list unavailable")
            elif row.borg_archive not in archives:
                blockers.append(f"archive {row.borg_archive} is missing")
        if not contiguous:
            blockers.append("history below it is not contiguously verified")
        if row is None or row.state != "verified":
            contiguous = False
        if fence is None or fence < chunk.range_end:
            blockers.append("fence not raised to the chunk end (a separate approved step)")
        if episode_floor is not None and chunk.range_end > episode_floor:
            blockers.append(f"an open pump episode's API window starts {episode_floor.isoformat()}")
        for p_start, p_end in contract.protected_windows:
            if chunk.range_start < p_end and chunk.range_end > p_start:
                blockers.append(
                    f"overlaps protected window {p_start.isoformat()}..{p_end.isoformat()}"
                )
        verdicts.append(
            DropVerdict(
                chunk=chunk.name,
                range_start=chunk.range_start.isoformat(),
                range_end=chunk.range_end.isoformat(),
                eligible=not blockers,
                blockers=tuple(blockers),
            )
        )
    return verdicts


def run_dry_run(
    dsn: str, contract: DatasetContract, *, repo: str | None, env: Mapping[str, str], now: datetime
) -> list[DropVerdict]:
    import psycopg

    archives: frozenset[str] | None = None
    if repo is not None:
        try:
            archives = parse_short_list(_run(borg_list_archives_args(repo), env))
        except ArchiveError:
            archives = None
    with psycopg.connect(dsn, autocommit=True) as conn:
        chunks = list_chunks(conn, contract)
        catalog = live_rows(conn, contract)
        fence = fence_of(conn, contract)
        floor = open_episode_floor(conn, contract.open_episode_lookback)

    def live_fp(chunk: Chunk) -> str | None:
        with _snapshot(dsn) as snap:
            value = snap.execute(
                pg_fingerprint_sql(contract), (chunk.range_start, chunk.range_end)
            ).fetchone()
        return None if value is None or value[0] is None else f"{FINGERPRINT_VERSION}:{value[0]}"

    return drop_verdicts(
        contract=contract,
        chunks=chunks,
        catalog=catalog,
        live_fingerprint=live_fp,
        archives=archives,
        fence=fence,
        episode_floor=floor,
        now=now,
    )


# ---------- CLI ----------


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError(f"{value} needs a UTC offset")
    return parsed.astimezone(UTC)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="step", required=True)
    for name in ("export", "archive", "verify", "fetch", "deletion-dry-run"):
        step = sub.add_parser(name)
        step.add_argument("--out-dir", type=Path, required=True)
        step.add_argument("--reserve-bytes", type=int, default=DEFAULT_RESERVE_BYTES)
        if name != "export":
            step.add_argument("--backup-env", type=Path, required=True)
    sub.choices["export"].add_argument("--code-revision", required=True)
    sub.choices["export"].add_argument("--max-chunks", type=int, default=1)
    sub.choices["fetch"].add_argument("--from", dest="start", type=_utc, required=True)
    sub.choices["fetch"].add_argument("--to", dest="end", type=_utc, required=True)
    sub.choices["fetch"].add_argument(
        "--max-cache-bytes", type=int, default=DEFAULT_MAX_CACHE_BYTES
    )
    args = parser.parse_args(argv)
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL is required")
    now = datetime.now(UTC)
    contract = LSR_CONTRACT
    if args.step == "export":
        report = run_export(
            dsn,
            contract,
            args.out_dir,
            code_revision=args.code_revision,
            now=now,
            max_chunks=args.max_chunks,
            reserve_bytes=args.reserve_bytes,
        )
        sys.stdout.write(report.to_json())
        return 1 if report.failed else 0
    repo, env = borg_env(args.backup_env)
    if args.step == "archive":
        report = run_archive(dsn, contract, args.out_dir, repo=repo, env=env, now=now)
    elif args.step == "verify":
        report = run_verify(
            dsn,
            contract,
            args.out_dir,
            repo=repo,
            env=env,
            now=now,
            reserve_bytes=args.reserve_bytes,
        )
    elif args.step == "fetch":
        paths = fetch(
            dsn,
            contract,
            args.start,
            args.end,
            args.out_dir,
            repo=repo,
            env=env,
            max_cache_bytes=args.max_cache_bytes,
            reserve_bytes=args.reserve_bytes,
        )
        sys.stdout.write(json.dumps({"fetched": [str(p) for p in paths]}, indent=1) + "\n")
        return 0
    else:
        verdicts = run_dry_run(dsn, contract, repo=repo, env=env, now=now)
        sys.stdout.write(json.dumps([asdict(v) for v in verdicts], indent=1) + "\n")
        return 0
    sys.stdout.write(report.to_json())
    return 1 if report.failed else 0


__all__ = [
    "ARCHIVE_PREFIX",
    "FINGERPRINT_VERSION",
    "LSR_CONTRACT",
    "ArchiveError",
    "CatalogRow",
    "Chunk",
    "DatasetContract",
    "DropVerdict",
    "_copy_rows_gz",
    "_snapshot",
    "covering_ranges",
    "drop_verdicts",
    "fetch",
    "list_chunks",
    "live_rows",
    "open_episode_floor",
    "pg_fingerprint_sql",
    "read_lsr",
    "run_archive",
    "run_dry_run",
    "run_export",
    "run_verify",
]

if __name__ == "__main__":
    sys.exit(main())
