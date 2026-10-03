"""History archive pilot: `app.live_long_short_ratio` chunks to Parquet, Borg and back.

Design and audit: docs/runbooks/history-archive-design-v1.md. One closed hypertable chunk
is one archived range. This module exports, archives, verifies, fetches, reads and plans
deletion; it never deletes production rows and never raises a fence.

- **Export** reads the chunk's row count, data keys, content fingerprint and rows from ONE
  `REPEATABLE READ READ ONLY` snapshot, streams the rows through `COPY ... TO STDOUT` into
  a gzip staging file and converts it with DuckDB to zstd Parquet. `NUMERIC` stays its
  exact PostgreSQL text (DuckDB's postgres scanner would read it as a double); timestamps
  stay UTC microseconds; `NULL` and the empty string stay distinct. The file is renamed
  into place only when its row count and content fingerprint equal the snapshot's.
- **Archive** puts the exported files into one `history-lsr-<UTC>` Borg archive (a prefix
  no prune rule matches) and accepts it only if its listing is exactly what was given.
- **Verify** extracts each member from its recorded archive and rechecks SHA-256 and the
  content fingerprint before the catalog says `verified`; only then is the local Parquet
  removed.
- **Fetch** brings verified ranges back into a byte-capped, least-recently-used cache.
- **read_lsr** reads verified archive ranges below the fence from fetched Parquet and the
  rest from PostgreSQL.
- **Deletion dry-run** explains, per chunk, every reason it may not be dropped yet.

The catalog (`app.history_archive_datasets`, migration 0058) only moves forward and keeps
its content immutable; see the migration for the database-enforced rules.
"""

from __future__ import annotations

import argparse
import fcntl
import gzip
import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .cold_bar_fetch import FetchError, ensure_reserve, sha256_of, stream_member
from .cold_bar_gated_deletion_collectors import (
    borg_list_archives_args,
    borg_list_members_args,
    parse_env_file,
    parse_short_list,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

FINGERPRINT_VERSION = "hafp_v1"
ARCHIVE_PREFIX = "history-lsr-"
DEFAULT_RESERVE_BYTES = 10 * 1024**3
DEFAULT_MAX_CACHE_BYTES = 2 * 1024**3
EXPORT_GRACE = timedelta(days=1)
LOCK_FILE = ".history-archive.lock"
# Ties every catalog-writing run of one dataset together (archiver against archiver
# only; the live writer is held off by the database fence and lock, not by this key).
ARCHIVER_LOCK_NAMESPACE = 0x4A5C


class ArchiveError(RuntimeError):
    """A step refused to continue; nothing it was responsible for was half-done."""


@dataclass(frozen=True)
class Column:
    name: str
    kind: str  # timestamptz, text or numeric


@dataclass(frozen=True)
class Reader:
    name: str
    rule: str


@dataclass(frozen=True)
class DatasetContract:
    dataset: str
    contract_version: str
    schema_version: str
    export_version: str
    source_schema: str
    source_table: str
    time_column: str
    key: tuple[str, ...]
    columns: tuple[Column, ...]
    hot_days: int
    readers: tuple[Reader, ...]
    open_episode_lookback: timedelta
    protected_windows: tuple[tuple[datetime, datetime], ...] = ()

    @property
    def table(self) -> str:
        return f"{self.source_schema}.{self.source_table}"


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
        Column("ts", "timestamptz"),
        Column("base", "text"),
        Column("exchange", "text"),
        Column("ratio", "numeric"),
        Column("long_account", "numeric"),
        Column("short_account", "numeric"),
    ),
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

_DUCK_TYPES = {"timestamptz": "TIMESTAMPTZ", "text": "VARCHAR", "numeric": "VARCHAR"}


# ---------- canonical content fingerprint (same value from PostgreSQL and DuckDB) ----------


def _pg_value(column: Column) -> str:
    if column.kind == "timestamptz":
        return f"((extract(epoch FROM {column.name}) * 1000000)::bigint)::text"
    if column.kind == "numeric":
        return f"{column.name}::text"
    return column.name


def _duck_value(column: Column) -> str:
    if column.kind == "timestamptz":
        return f"CAST(epoch_us({column.name}) AS VARCHAR)"
    return column.name


def pg_row_text(contract: DatasetContract) -> str:
    """Each column as `N` (NULL) or `V<bytes>:<text>`; the length prefix makes the
    concatenation unambiguous, so NULL, '' and every value stay distinct."""
    parts = [
        f"CASE WHEN {c.name} IS NULL THEN 'N' "
        f"ELSE 'V' || octet_length({_pg_value(c)}) || ':' || {_pg_value(c)} END"
        for c in contract.columns
    ]
    return " || ".join(parts)


def duck_row_text(contract: DatasetContract) -> str:
    parts = [
        f"CASE WHEN {c.name} IS NULL THEN 'N' "
        f"ELSE 'V' || CAST(strlen({_duck_value(c)}) AS VARCHAR) || ':' || {_duck_value(c)} END"
        for c in contract.columns
    ]
    return " || ".join(parts)


def pg_fingerprint_sql(contract: DatasetContract) -> str:
    return (
        "SELECT encode(sha256(convert_to(string_agg(h, '' ORDER BY h), 'UTF8')), 'hex') "  # noqa: S608
        f"FROM (SELECT encode(sha256(convert_to({pg_row_text(contract)}, 'UTF8')), 'hex') AS h "
        f"FROM {contract.table} WHERE {contract.time_column} >= %s "
        f"AND {contract.time_column} < %s) AS rows"
    )


def parquet_fingerprint(contract: DatasetContract, path: Path) -> tuple[int, str]:
    """Row count and content fingerprint of an exported (or extracted) Parquet file."""
    import duckdb

    source = "read_parquet(" + _sql_str(str(path)) + ")"
    row = (
        duckdb.connect()
        .execute(
            "SELECT count(*), sha256(string_agg(h, '' ORDER BY h)) FROM ("  # noqa: S608
            f"SELECT sha256({duck_row_text(contract)}) AS h FROM {source})"
        )
        .fetchone()
    )
    if row is None or row[1] is None:
        raise ArchiveError(f"{path}: no rows to fingerprint")
    return int(row[0]), f"{FINGERPRINT_VERSION}:{row[1]}"


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# ---------- chunks ----------


@dataclass(frozen=True)
class Chunk:
    name: str
    range_start: datetime
    range_end: datetime
    bytes: int

    @property
    def label(self) -> str:
        return self.range_start.strftime("%Y-%m-%d")


def list_chunks(conn: Any, contract: DatasetContract) -> list[Chunk]:
    rows = conn.execute(
        "SELECT chunk_schema || '.' || chunk_name, range_start, range_end, "
        "pg_total_relation_size(format('%%I.%%I', chunk_schema, chunk_name)::regclass) "
        "FROM timescaledb_information.chunks "
        "WHERE hypertable_schema = %s AND hypertable_name = %s ORDER BY range_start",
        (contract.source_schema, contract.source_table),
    ).fetchall()
    return [Chunk(r[0], r[1].astimezone(UTC), r[2].astimezone(UTC), int(r[3])) for r in rows]


# ---------- export ----------


@dataclass(frozen=True)
class Manifest:
    dataset: str
    contract_version: str
    schema_version: str
    export_version: str
    source_table: str
    chunk_name: str
    range_start: str
    range_end: str
    columns: tuple[tuple[str, str], ...]
    key: tuple[str, ...]
    row_count: int
    data_keys: tuple[dict[str, Any], ...]
    file_name: str
    file_bytes: int
    file_sha256: str
    content_fingerprint: str
    snapshot_at: str
    code_revision: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"


def file_names(contract: DatasetContract, chunk: Chunk, revision: int) -> tuple[str, str]:
    stem = f"{contract.source_table}-{chunk.label}-r{revision}"
    return f"{stem}.parquet", f"{stem}.manifest.json"


@contextmanager
def _snapshot(dsn: str) -> Iterator[Any]:
    """One read-only REPEATABLE READ transaction: every read inside sees one snapshot."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conn.read_only = True
        with conn.transaction():
            conn.execute("SET LOCAL TimeZone = 'UTC'")
            conn.execute("SET LOCAL DateStyle = 'ISO, YMD'")
            yield conn


def _copy_rows_gz(
    conn: Any, contract: DatasetContract, start: datetime, end: datetime, out: Path
) -> None:
    columns = ", ".join(c.name for c in contract.columns)
    query = (
        f"COPY (SELECT {columns} FROM {contract.table} "  # noqa: S608
        f"WHERE {contract.time_column} >= '{start.isoformat()}' "
        f"AND {contract.time_column} < '{end.isoformat()}') "
        "TO STDOUT (FORMAT csv, FORCE_QUOTE *)"
    )
    with gzip.open(out, "wb", compresslevel=3) as sink, conn.cursor().copy(query) as copy:
        for block in copy:
            sink.write(bytes(block))


def _csv_to_parquet(contract: DatasetContract, csv_gz: Path, parquet: Path) -> None:
    import duckdb

    columns = (
        "{" + ", ".join(f"'{c.name}': '{_DUCK_TYPES[c.kind]}'" for c in contract.columns) + "}"
    )
    duckdb.connect().execute(
        f"COPY (SELECT * FROM read_csv({_sql_str(str(csv_gz))}, header = false, "  # noqa: S608
        "delim = ',', quote = '\"', escape = '\"', allow_quoted_nulls = false, "
        f"auto_detect = false, compression = 'gzip', columns = {columns})) "
        f"TO {_sql_str(str(parquet))} (FORMAT parquet, COMPRESSION zstd)"
    )


def export_chunk(
    dsn: str,
    contract: DatasetContract,
    chunk: Chunk,
    out_dir: Path,
    *,
    revision: int,
    code_revision: str,
    reserve_bytes: int = DEFAULT_RESERVE_BYTES,
) -> Manifest:
    """Write one chunk to Parquet from one snapshot and describe it in a manifest.

    The chunk's on-disk size bounds the staging space (gzip CSV plus Parquet are both
    smaller than the heap and indexes they come from), so the free-space reserve is
    checked against it before anything is written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    parquet_name, manifest_name = file_names(contract, chunk, revision)
    try:
        ensure_reserve(out_dir, chunk.bytes, reserve_bytes)
    except FetchError as exc:
        raise ArchiveError(f"{chunk.label}: {exc}") from exc
    staging_csv = out_dir / f".{parquet_name}.csv.gz.partial"
    staging_parquet = out_dir / f".{parquet_name}.partial"
    for leftover in (staging_csv, staging_parquet):
        leftover.unlink(missing_ok=True)
    try:
        with _snapshot(dsn) as conn:
            snapshot_at = conn.execute("SELECT now()").fetchone()[0]
            count_row = conn.execute(
                f"SELECT count(*) FROM {contract.table} "  # noqa: S608
                f"WHERE {contract.time_column} >= %s AND {contract.time_column} < %s",
                (chunk.range_start, chunk.range_end),
            ).fetchone()
            row_count = int(count_row[0])
            if row_count == 0:
                raise ArchiveError(f"{chunk.label}: the chunk has no rows; refusing to export")
            fp_row = conn.execute(
                pg_fingerprint_sql(contract), (chunk.range_start, chunk.range_end)
            ).fetchone()
            source_fp = f"{FINGERPRINT_VERSION}:{fp_row[0]}"
            keys = conn.execute(
                f"SELECT exchange, count(*), min({contract.time_column}), "  # noqa: S608
                f"max({contract.time_column}) FROM {contract.table} "
                f"WHERE {contract.time_column} >= %s AND {contract.time_column} < %s "
                "GROUP BY exchange ORDER BY exchange",
                (chunk.range_start, chunk.range_end),
            ).fetchall()
            _copy_rows_gz(conn, contract, chunk.range_start, chunk.range_end, staging_csv)
        _csv_to_parquet(contract, staging_csv, staging_parquet)
        written_rows, file_fp = parquet_fingerprint(contract, staging_parquet)
        if written_rows != row_count or file_fp != source_fp:
            raise ArchiveError(
                f"{chunk.label}: Parquet has {written_rows} rows / {file_fp}, the snapshot "
                f"had {row_count} / {source_fp}; nothing kept"
            )
        target = out_dir / parquet_name
        staging_parquet.replace(target)
    finally:
        staging_csv.unlink(missing_ok=True)
        staging_parquet.unlink(missing_ok=True)
    manifest = Manifest(
        dataset=contract.dataset,
        contract_version=contract.contract_version,
        schema_version=contract.schema_version,
        export_version=contract.export_version,
        source_table=contract.table,
        chunk_name=chunk.name,
        range_start=chunk.range_start.isoformat(),
        range_end=chunk.range_end.isoformat(),
        columns=tuple((c.name, c.kind) for c in contract.columns),
        key=contract.key,
        row_count=row_count,
        data_keys=tuple(
            {
                "exchange": k[0],
                "rows": int(k[1]),
                "first": k[2].isoformat(),
                "last": k[3].isoformat(),
            }
            for k in keys
        ),
        file_name=parquet_name,
        file_bytes=target.stat().st_size,
        file_sha256=sha256_of(target),
        content_fingerprint=source_fp,
        snapshot_at=snapshot_at.astimezone(UTC).isoformat(),
        code_revision=code_revision,
    )
    (out_dir / manifest_name).write_text(manifest.to_json())
    return manifest


# ---------- catalog ----------


@dataclass(frozen=True)
class CatalogRow:
    id: int
    state: str
    chunk_name: str
    range_start: datetime
    range_end: datetime
    revision: int
    row_count: int
    file_name: str
    file_bytes: int
    file_sha256: str
    content_fingerprint: str
    borg_archive: str | None
    manifest_sha256: str = ""


_CATALOG_COLUMNS = (
    "id, state, chunk_name, range_start, range_end, revision, row_count, file_name, "
    "file_bytes, file_sha256, content_fingerprint, borg_archive, manifest_sha256"
)


def _row(values: Sequence[Any]) -> CatalogRow:
    return CatalogRow(
        int(values[0]),
        str(values[1]),
        str(values[2]),
        values[3].astimezone(UTC),
        values[4].astimezone(UTC),
        int(values[5]),
        int(values[6]),
        str(values[7]),
        int(values[8]),
        str(values[9]),
        str(values[10]),
        values[11],
        str(values[12]),
    )


def live_rows(conn: Any, contract: DatasetContract) -> dict[datetime, CatalogRow]:
    """The live (not superseded) catalog row of each range, keyed by range start."""
    rows = conn.execute(
        f"SELECT {_CATALOG_COLUMNS} FROM app.history_archive_datasets "  # noqa: S608
        "WHERE dataset = %s AND state <> 'superseded' ORDER BY range_start",
        (contract.dataset,),
    ).fetchall()
    return {r.range_start: r for r in map(_row, rows)}


def next_revision(conn: Any, contract: DatasetContract, chunk: Chunk) -> int:
    row = conn.execute(
        "SELECT coalesce(max(revision), 0) + 1 FROM app.history_archive_datasets "
        "WHERE dataset = %s AND contract_version = %s AND range_start = %s AND range_end = %s",
        (contract.dataset, contract.contract_version, chunk.range_start, chunk.range_end),
    ).fetchone()
    return int(row[0])


def record_export(
    conn: Any, contract: DatasetContract, manifest: Manifest, manifest_sha: str, revision: int
) -> int:
    row = conn.execute(
        "INSERT INTO app.history_archive_datasets (dataset, contract_version, source_table, "
        "chunk_name, range_start, range_end, revision, state, row_count, file_name, file_bytes, "
        "file_sha256, content_fingerprint, manifest_sha256, snapshot_at, code_revision) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'exported', %s, %s, %s, %s, %s, %s, %s, %s) "
        "RETURNING id",
        (
            contract.dataset,
            contract.contract_version,
            contract.table,
            manifest.chunk_name,
            manifest.range_start,
            manifest.range_end,
            revision,
            manifest.row_count,
            manifest.file_name,
            manifest.file_bytes,
            manifest.file_sha256,
            manifest.content_fingerprint,
            manifest_sha,
            manifest.snapshot_at,
            manifest.code_revision,
        ),
    ).fetchone()
    return int(row[0])


def supersede(conn: Any, row_id: int, reason: str) -> None:
    conn.execute(
        "UPDATE app.history_archive_datasets SET state = 'superseded', superseded_reason = %s "
        "WHERE id = %s",
        (reason, row_id),
    )


@contextmanager
def archiver_session(dsn: str, contract: DatasetContract, out_dir: Path) -> Iterator[Any]:
    """One catalog-writing run per dataset: a file lock on the staging directory (against
    another run on this host) and a session advisory lock (against a run elsewhere)."""
    import psycopg

    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / LOCK_FILE).open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ArchiveError(f"another history-archive run holds {out_dir / LOCK_FILE}") from exc
        with psycopg.connect(dsn, autocommit=True) as conn:
            got = conn.execute(
                "SELECT pg_try_advisory_lock(%s, hashtext(%s))",
                (ARCHIVER_LOCK_NAMESPACE, contract.dataset),
            ).fetchone()
            if not got or not got[0]:
                raise ArchiveError(f"another history-archive run holds the {contract.dataset} lock")
            yield conn


# ---------- step: export closed chunks ----------


@dataclass
class StepReport:
    done: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=1) + "\n"


def run_export(
    dsn: str,
    contract: DatasetContract,
    out_dir: Path,
    *,
    code_revision: str,
    now: datetime,
    max_chunks: int,
    reserve_bytes: int = DEFAULT_RESERVE_BYTES,
) -> StepReport:
    """Export every closed chunk without a live catalog row, oldest first.

    A chunk is closed once its end is `EXPORT_GRACE` in the past. A live `exported` row
    whose local file is gone (lost before it reached Borg) is superseded and exported
    again; `archived` and `verified` rows are already offsite and are left alone."""
    report = StepReport()
    with archiver_session(dsn, contract, out_dir) as conn:
        live = live_rows(conn, contract)
        for chunk in list_chunks(conn, contract):
            if len(report.done) >= max_chunks:
                break
            if chunk.range_end > now - EXPORT_GRACE:
                continue
            row = live.get(chunk.range_start)
            if row is not None:
                if row.state == "exported" and not (out_dir / row.file_name).exists():
                    supersede(conn, row.id, "local export lost before it was archived")
                else:
                    report.skipped.append(f"{chunk.label}: already {row.state}")
                    continue
            revision = next_revision(conn, contract, chunk)
            try:
                manifest = export_chunk(
                    dsn,
                    contract,
                    chunk,
                    out_dir,
                    revision=revision,
                    code_revision=code_revision,
                    reserve_bytes=reserve_bytes,
                )
            except ArchiveError as exc:
                report.failed.append(str(exc))
                break
            manifest_path = out_dir / file_names(contract, chunk, revision)[1]
            record_export(conn, contract, manifest, sha256_of(manifest_path), revision)
            report.done.append(f"{chunk.label}: {manifest.row_count} rows, r{revision}")
    return report


# ---------- step: archive into Borg ----------


def borg_env(backup_env: Path) -> tuple[str, dict[str, str]]:
    env = parse_env_file(backup_env.read_text())
    repo = env.get("BORG_REPO")
    if not repo:
        raise ArchiveError(f"BORG_REPO not found in {backup_env}")
    return repo, {**os.environ, **env}


def _run(args: list[str], env: Mapping[str, str], *, cwd: Path | None = None) -> str:
    done = subprocess.run(  # noqa: S603 -- fixed argv
        args, env=dict(env), cwd=cwd, capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        raise ArchiveError(f"{' '.join(args[:2])} failed: {done.stderr.strip()[-300:]}")
    return done.stdout


def run_archive(
    dsn: str,
    contract: DatasetContract,
    out_dir: Path,
    *,
    repo: str,
    env: Mapping[str, str],
    now: datetime,
) -> StepReport:
    """Archive every `exported` range's Parquet and manifest into one new archive."""
    report = StepReport()
    with archiver_session(dsn, contract, out_dir) as conn:
        pending = [r for r in live_rows(conn, contract).values() if r.state == "exported"]
        members: list[str] = []
        for row in pending:
            parquet = out_dir / row.file_name
            if not parquet.exists() or sha256_of(parquet) != row.file_sha256:
                report.failed.append(f"{row.file_name}: missing or changed since export")
                continue
            members += [row.file_name, manifest_member(row.file_name)]
        if not members:
            if not report.failed:
                report.skipped.append("nothing exported to archive")
            return report
        archive = ARCHIVE_PREFIX + now.strftime("%Y-%m-%dT%H:%M:%S")
        _run(
            ["borg", "create", "--compression", "zstd,3", f"{repo}::{archive}", *members],
            env,
            cwd=out_dir,
        )
        listed = parse_short_list(_run(borg_list_members_args(repo, archive), env))
        if listed != frozenset(members):
            subprocess.run(["borg", "delete", f"{repo}::{archive}"], env=dict(env), check=False)  # noqa: S603, S607
            raise ArchiveError(
                f"{archive} lists {sorted(listed)}, expected {sorted(members)}; deleted"
            )
        for row in pending:
            if row.file_name in listed:
                conn.execute(
                    "UPDATE app.history_archive_datasets SET state = 'archived', "
                    "borg_archive = %s, archived_at = %s WHERE id = %s AND state = 'exported'",
                    (archive, now, row.id),
                )
                report.done.append(f"{row.file_name} -> {archive}")
    return report


# ---------- step: verify by extraction ----------


def run_verify(
    dsn: str,
    contract: DatasetContract,
    out_dir: Path,
    *,
    repo: str,
    env: Mapping[str, str],
    now: datetime,
    reserve_bytes: int = DEFAULT_RESERVE_BYTES,
) -> StepReport:
    """Extract each `archived` range from its own archive and accept it only when the
    extracted bytes and content equal the catalog; then drop the local Parquet."""
    report = StepReport()
    with archiver_session(dsn, contract, out_dir) as conn:
        for row in live_rows(conn, contract).values():
            if row.state != "archived" or row.borg_archive is None:
                continue
            try:
                ensure_reserve(out_dir, row.file_bytes, reserve_bytes)
                with tempfile.TemporaryDirectory(dir=out_dir) as tmp:
                    extracted = Path(tmp) / row.file_name
                    sha = stream_member(
                        repo, row.borg_archive, row.file_name, extracted, dict(env), row.file_bytes
                    )
                    if sha != row.file_sha256:
                        raise ArchiveError(f"extracted sha256 {sha} != catalog {row.file_sha256}")
                    rows, fingerprint = parquet_fingerprint(contract, extracted)
                    if (rows, fingerprint) != (row.row_count, row.content_fingerprint):
                        raise ArchiveError(
                            f"extracted content {rows} rows / {fingerprint} != catalog "
                            f"{row.row_count} / {row.content_fingerprint}"
                        )
                    _verify_archived_manifest(contract, row, repo, env, Path(tmp))
            except (ArchiveError, FetchError) as exc:
                report.failed.append(f"{row.file_name} in {row.borg_archive}: {exc}")
                continue
            conn.execute(
                "UPDATE app.history_archive_datasets SET state = 'verified', "
                "verified_at = %s, verified_sha256 = %s, verified_fingerprint = %s "
                "WHERE id = %s AND state = 'archived'",
                (now, sha, fingerprint, row.id),
            )
            (out_dir / row.file_name).unlink(missing_ok=True)
            report.done.append(f"{row.file_name}: verified from {row.borg_archive}")
    return report


MAX_MANIFEST_BYTES = 1024 * 1024


def manifest_member(file_name: str) -> str:
    return file_name.removesuffix(".parquet") + ".manifest.json"


def _verify_archived_manifest(
    contract: DatasetContract, row: CatalogRow, repo: str, env: Mapping[str, str], tmp: Path
) -> None:
    """The manifest in the same archive must be the one the catalog recorded (SHA-256)
    and must describe this exact range, file and content."""
    member = manifest_member(row.file_name)
    extracted = tmp / member
    sha = stream_member(
        repo, str(row.borg_archive), member, extracted, dict(env), MAX_MANIFEST_BYTES
    )
    if sha != row.manifest_sha256:
        raise ArchiveError(f"extracted manifest sha256 {sha} != catalog {row.manifest_sha256}")
    try:
        payload = json.loads(extracted.read_text())
    except ValueError as exc:
        raise ArchiveError(f"extracted manifest {member} is not JSON") from exc
    expected = {
        "dataset": contract.dataset,
        "contract_version": contract.contract_version,
        "schema_version": contract.schema_version,
        "source_table": contract.table,
        "chunk_name": row.chunk_name,
        "range_start": row.range_start.isoformat(),
        "range_end": row.range_end.isoformat(),
        "row_count": row.row_count,
        "file_name": row.file_name,
        "file_bytes": row.file_bytes,
        "file_sha256": row.file_sha256,
        "content_fingerprint": row.content_fingerprint,
    }
    wrong = sorted(k for k, v in expected.items() if payload.get(k) != v)
    if wrong:
        raise ArchiveError(f"extracted manifest {member} disagrees with the catalog on {wrong}")


# ---------- research access: fetch into a bounded cache, read ----------


def _cache_files(cache_dir: Path) -> list[Path]:
    return sorted(
        (p for p in cache_dir.glob("*.parquet") if p.is_file()), key=lambda p: p.stat().st_atime
    )


def _make_room(cache_dir: Path, needed: int, max_cache_bytes: int, keep: set[Path]) -> None:
    if needed > max_cache_bytes:
        raise ArchiveError(f"one file of {needed} bytes exceeds the {max_cache_bytes}-byte cache")
    files = _cache_files(cache_dir)
    total = sum(p.stat().st_size for p in files)
    for victim in files:
        if total + needed <= max_cache_bytes:
            break
        if victim in keep:
            continue
        total -= victim.stat().st_size
        victim.unlink()
    if total + needed > max_cache_bytes:
        raise ArchiveError("the files this read needs do not fit in the cache together")


def fetch(
    dsn: str,
    contract: DatasetContract,
    start: datetime,
    end: datetime,
    cache_dir: Path,
    *,
    repo: str,
    env: Mapping[str, str],
    max_cache_bytes: int = DEFAULT_MAX_CACHE_BYTES,
    reserve_bytes: int = DEFAULT_RESERVE_BYTES,
) -> list[Path]:
    """Bring every verified range overlapping `[start, end)` into the cache, verified."""
    import psycopg

    cache_dir.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = [
            r
            for r in live_rows(conn, contract).values()
            if r.state == "verified" and r.range_start < end and r.range_end > start
        ]
    paths: list[Path] = []
    for row in rows:
        dest = cache_dir / row.file_name
        if dest.exists() and sha256_of(dest) == row.file_sha256:
            os.utime(dest)  # most recently used
            paths.append(dest)
            continue
        dest.unlink(missing_ok=True)
        _make_room(cache_dir, row.file_bytes, max_cache_bytes, set(paths))
        try:
            ensure_reserve(cache_dir, row.file_bytes, reserve_bytes)
        except FetchError as exc:
            raise ArchiveError(str(exc)) from exc
        partial = cache_dir / f".{row.file_name}.partial"
        try:
            sha = stream_member(
                repo, str(row.borg_archive), row.file_name, partial, dict(env), row.file_bytes
            )
            if sha != row.file_sha256:
                raise ArchiveError(f"{row.file_name}: fetched sha256 {sha} != catalog")
            partial.replace(dest)
        finally:
            partial.unlink(missing_ok=True)
        paths.append(dest)
    return paths


def fence_of(conn: Any, contract: DatasetContract) -> datetime | None:
    """The instant below which the source refuses rows, or None while no fence is
    raised (the row sits at -infinity, which psycopg cannot represent)."""
    row = conn.execute(
        "SELECT CASE WHEN closed_before = '-infinity' THEN NULL ELSE closed_before END "
        "FROM app.history_archive_fences WHERE dataset = %s",
        (contract.dataset,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    fence: datetime = row[0].astimezone(UTC)
    return fence


READ_ATTEMPTS = 3


def covering_ranges(
    catalog: Mapping[datetime, CatalogRow], start: datetime, split: datetime
) -> list[CatalogRow]:
    """The verified ranges that tile `[start, split)` exactly, or an error naming the
    first gap or overlap. A week without a verified range is a gap even if it held no
    rows: emptiness needs its own evidence, which the pilot does not record."""
    verified = sorted(
        (
            r
            for r in catalog.values()
            if r.state == "verified" and r.range_start < split and r.range_end > start
        ),
        key=lambda r: r.range_start,
    )
    cursor = start
    for index, row in enumerate(verified):
        if row.range_start > cursor:
            raise ArchiveError(f"no verified archive covers [{cursor}, {row.range_start})")
        if index > 0 and row.range_start < cursor:
            raise ArchiveError(f"verified archives overlap at {row.range_start}")
        cursor = row.range_end
    if cursor < split:
        raise ArchiveError(f"no verified archive covers [{cursor}, {split})")
    return verified


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
    columns = ", ".join(f"{c.name} {_DUCK_TYPES[c.kind]}" for c in contract.columns)
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


# ---------- restore check ----------


def restore_into(conn: Any, contract: DatasetContract, parquet: Path, target: str) -> int:
    """Load an archive file into `target` (created with the source's column types) and
    return the number of rows written. Values pass as text, so nothing is rounded."""
    import duckdb

    types = {"timestamptz": "TIMESTAMPTZ", "text": "TEXT", "numeric": "NUMERIC"}
    columns = ", ".join(f"{c.name} {types[c.kind]}" for c in contract.columns)
    conn.execute(f"CREATE TABLE {target} ({columns})")
    select = ", ".join(
        f"CAST(epoch_us({c.name}) AS VARCHAR)" if c.kind == "timestamptz" else c.name
        for c in contract.columns
    )
    reader = duckdb.connect().execute(
        f"SELECT {select} FROM read_parquet({_sql_str(str(parquet))})"  # noqa: S608
    )
    written = 0
    names = ", ".join(c.name for c in contract.columns)
    with conn.cursor().copy(f"COPY {target} ({names}) FROM STDIN") as copy:
        while batch := reader.fetchmany(50_000):
            for values in batch:
                copy.write_row(
                    [
                        _from_epoch_us(v) if c.kind == "timestamptz" and v is not None else v
                        for c, v in zip(contract.columns, values, strict=True)
                    ]
                )
                written += 1
    return written


def _from_epoch_us(value: str) -> datetime:
    micros = int(value)
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=micros)


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


if __name__ == "__main__":
    sys.exit(main())
