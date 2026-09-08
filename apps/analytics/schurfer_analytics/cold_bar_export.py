"""Export a day of minute bars to Parquet before Timescale retention drops it.

`timeseries.bybit_momentum_bars_1m` is dropped after 35 days. That is the only
copy: the PostgreSQL dump contains whatever is still in the database when it
runs, so a bar older than 35 days is gone from the backups too. Every day we do
not export, we lose a day of history that no amount of backup restores.

This module exports; it does not delete. Deleting only what has been confirmed
written is a separate change, and the order matters: an export that has never
been verified is not a licence to drop the source.

What the manifest carries is deliberate. The full data key -- exchange,
market_type, capture_version, universe_version -- is written down because two
files covering the same day are not interchangeable if they were captured under
different versions, and a reader that cannot tell will silently mix them.

Measured on production, 2026-09-08: one day is about 1.5 million rows, 324 MB of
zstd Parquet, and 107 seconds. That is roughly 118 GB per year, not the "tens of
gigabytes" an earlier estimate guessed.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

# Bumped when the exported column set or file layout changes in a way that makes
# older files unreadable by the current reader. A reader must refuse a file whose
# schema version it does not know rather than guessing.
SCHEMA_VERSION = "cold_bars_v1"
EXPORT_VERSION = "cold_bar_export_v1"

SOURCE_TABLE = "timeseries.bybit_momentum_bars_1m"

# The columns that together identify what a row is a measurement of. Recorded in
# the manifest for every exported day.
DATA_KEY_COLUMNS = ("exchange", "market_type", "capture_version", "universe_version")


@dataclass(frozen=True)
class ExportManifest:
    """What was exported, precisely enough to verify it later without the source.

    Written beside the Parquet file. `row_count` and `sha256` are what make a
    later "is this file complete" answerable at all: once the source chunk is
    dropped there is nothing left to compare against.
    """

    schema_version: str
    export_version: str
    source_table: str
    day: str
    bucket_start_from: str
    bucket_start_until: str
    row_count: int
    file_name: str
    file_bytes: int
    sha256: str
    data_keys: tuple[dict[str, str], ...]
    exported_at: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """The half-open UTC day this export covers.

    Calendar UTC, not "the last 24 hours": a file's contents must not depend on
    what time the timer happened to fire, or two runs of the same day would
    disagree and neither would be wrong.
    """
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


def sha256_file(path: Path, *, chunk_bytes: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk_bytes):
            digest.update(block)
    return digest.hexdigest()


def _quote(value: str) -> str:
    return value.replace("'", "''")


def connect(dsn: str) -> Any:
    """A DuckDB connection with the source database attached read-only."""
    import duckdb

    connection = duckdb.connect()
    connection.execute("INSTALL postgres")
    connection.execute("LOAD postgres")
    # READ_ONLY is not a formality: this process must not be able to modify the
    # database it exists to preserve, however a query is later mistyped.
    connection.execute(f"ATTACH '{_quote(dsn)}' AS pg (TYPE POSTGRES, READ_ONLY)")
    return connection


def _where(start: datetime, until: datetime) -> str:
    return (
        f"bucket_start >= TIMESTAMPTZ '{start.isoformat()}' "
        f"AND bucket_start < TIMESTAMPTZ '{until.isoformat()}'"
    )


def count_rows(connection: Any, start: datetime, until: datetime) -> int:
    row = connection.execute(
        f"SELECT count(*) FROM pg.{SOURCE_TABLE} WHERE {_where(start, until)}"  # noqa: S608
    ).fetchone()
    return 0 if row is None else int(row[0])


def data_keys(connection: Any, start: datetime, until: datetime) -> tuple[dict[str, str], ...]:
    columns = ", ".join(DATA_KEY_COLUMNS)
    rows = connection.execute(
        f"SELECT DISTINCT {columns} FROM pg.{SOURCE_TABLE} "  # noqa: S608
        f"WHERE {_where(start, until)} ORDER BY {columns}"
    ).fetchall()
    return tuple(dict(zip(DATA_KEY_COLUMNS, map(str, row), strict=True)) for row in rows)


def export_day(
    connection: Any,
    day: date,
    out_dir: Path,
    *,
    exported_at: datetime | None = None,
) -> ExportManifest:
    """Write one UTC day to Parquet and describe it in a manifest.

    Raises when the day holds no rows. An empty file is indistinguishable from a
    day nobody exported, and the difference matters enormously once deletion is
    driven by these manifests.
    """
    start, until = day_bounds(day)
    row_count = count_rows(connection, start, until)
    if row_count == 0:
        raise ValueError(f"{day.isoformat()} has no rows in {SOURCE_TABLE}; refusing to export")

    out_dir.mkdir(parents=True, exist_ok=True)
    file_name = f"bars-{day.isoformat()}.parquet"
    target = out_dir / file_name
    # Written under a temporary name and renamed on success, so an interrupted
    # run can never leave a short file sitting under the real name where the
    # next run would take it for a finished export.
    staging = out_dir / f".{file_name}.partial"
    staging.unlink(missing_ok=True)
    try:
        connection.execute(
            f"COPY (SELECT * FROM pg.{SOURCE_TABLE} WHERE {_where(start, until)}) "  # noqa: S608
            f"TO '{_quote(str(staging))}' (FORMAT PARQUET, COMPRESSION zstd)"
        )
        written = connection.execute(
            f"SELECT count(*) FROM read_parquet('{_quote(str(staging))}')"  # noqa: S608
        ).fetchone()
        written_rows = 0 if written is None else int(written[0])
        if written_rows != row_count:
            raise ValueError(
                f"{day.isoformat()}: exported {written_rows} rows, source had {row_count}"
            )
        staging.replace(target)
    finally:
        staging.unlink(missing_ok=True)

    manifest = ExportManifest(
        schema_version=SCHEMA_VERSION,
        export_version=EXPORT_VERSION,
        source_table=SOURCE_TABLE,
        day=day.isoformat(),
        bucket_start_from=start.isoformat(),
        bucket_start_until=until.isoformat(),
        row_count=row_count,
        file_name=file_name,
        file_bytes=target.stat().st_size,
        sha256=sha256_file(target),
        data_keys=data_keys(connection, start, until),
        exported_at=(exported_at or datetime.now(UTC)).isoformat(),
    )
    (out_dir / f"bars-{day.isoformat()}.manifest.json").write_text(manifest.to_json())
    return manifest


def verify_local(out_dir: Path, day: date) -> ExportManifest:
    """Re-read a manifest and check the file beside it still matches.

    Separate from export on purpose: verifying with the numbers the same run
    just computed proves nothing. This reads the manifest back from disk the way
    a later run, or a restore, would.
    """
    manifest_path = out_dir / f"bars-{day.isoformat()}.manifest.json"
    payload = json.loads(manifest_path.read_text())
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"{manifest_path}: schema version {payload.get('schema_version')!r} "
            f"is not {SCHEMA_VERSION!r}; refusing to read it as this format"
        )
    manifest = ExportManifest(
        **{**payload, "data_keys": tuple(payload["data_keys"])},
    )
    target = out_dir / manifest.file_name
    if not target.exists():
        raise ValueError(f"{target} is missing but its manifest is not")
    actual_bytes = target.stat().st_size
    if actual_bytes != manifest.file_bytes:
        raise ValueError(f"{target}: {actual_bytes} bytes, manifest says {manifest.file_bytes}")
    actual_sha = sha256_file(target)
    if actual_sha != manifest.sha256:
        raise ValueError(f"{target}: sha256 {actual_sha} does not match the manifest")
    return manifest


def days_to_export(oldest: date, newest: date, already: Sequence[str]) -> tuple[date, ...]:
    """Days in the range that have no manifest yet, oldest first.

    Oldest first because the oldest day is the one retention deletes next. A run
    that is cut short should have saved the days closest to being lost, not the
    ones with the most time left.
    """
    done = set(already)
    out = []
    current = oldest
    while current <= newest:
        if current.isoformat() not in done:
            out.append(current)
        current += timedelta(days=1)
    return tuple(out)


def existing_days(out_dir: Path) -> tuple[str, ...]:
    if not out_dir.exists():
        return ()
    return tuple(
        sorted(
            path.name[len("bars-") : -len(".manifest.json")]
            for path in out_dir.glob("bars-*.manifest.json")
        )
    )


def source_day_range(connection: Any) -> tuple[date, date] | None:
    """The oldest and newest complete UTC day still present in the source.

    Today is excluded: it is still being written, and a file for a day that is
    not over yet would be complete only by accident.
    """
    row = connection.execute(
        f"SELECT min(bucket_start), max(bucket_start) FROM pg.{SOURCE_TABLE}"  # noqa: S608
    ).fetchone()
    if row is None or row[0] is None:
        return None
    oldest = row[0].astimezone(UTC).date()
    newest_complete = datetime.now(UTC).date() - timedelta(days=1)
    if oldest > newest_complete:
        return None
    return oldest, newest_complete


def main() -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Export cold minute bars to Parquet")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument(
        "--day",
        type=date.fromisoformat,
        help="export one specific UTC day instead of everything missing",
    )
    parser.add_argument(
        "--max-days",
        type=int,
        default=0,
        help="stop after this many days (0 means no limit)",
    )
    args = parser.parse_args()

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise ValueError("DATABASE_URL is required for cold-bar-export")

    connection = connect(dsn)
    if args.day is not None:
        targets: tuple[date, ...] = (args.day,)
    else:
        span = source_day_range(connection)
        if span is None:
            sys.stdout.write("no complete day available to export\n")
            return
        targets = days_to_export(span[0], span[1], existing_days(args.out_dir))
        if args.max_days > 0:
            targets = targets[: args.max_days]

    for day in targets:
        manifest = export_day(connection, day, args.out_dir)
        verified = verify_local(args.out_dir, day)
        sys.stdout.write(
            f"{verified.day}: {verified.row_count} rows, "
            f"{verified.file_bytes / 1e6:.1f} MB, sha256 {verified.sha256[:16]}\n"
        )
        del manifest
    if not targets:
        sys.stdout.write("nothing to export\n")
