"""History archive engine: closed time-series chunks and point-in-time snapshots of
plain tables to Parquet, Borg and back (docs/runbooks/history-archive-design-v1.md,
docs/runbooks/hyp015-inputs-archive-design-v1.md).

Two real consumers use it: the LSR pilot (`lsr_history_archive`) and the HYP-015 reader
inputs (`hyp015_inputs_archive`). It never deletes production rows and never raises a
fence.

- **Export** reads the row count, data keys, content fingerprint and rows of a chunk (or
  of a snapshot's filtered table) from ONE `REPEATABLE READ READ ONLY` snapshot, streams
  the rows through `COPY ... TO STDOUT` into a gzip staging file and converts it with
  DuckDB to zstd Parquet. Timestamps stay UTC microseconds; every other column keeps its
  exact PostgreSQL text (DuckDB's postgres scanner would turn `NUMERIC` into a double);
  `NULL` and the empty string stay distinct. A contract pins its columns and types, and
  an export whose source differs fails. The file is renamed into place only when its
  row count and content fingerprint equal the snapshot's.
- **Archive** puts the exported files into one `<prefix><UTC>` Borg archive (a prefix no
  prune rule matches) and accepts it only if its listing is exactly what was given.
- **Verify** extracts each Parquet and its manifest from the recorded archive and
  rechecks SHA-256, content fingerprint and manifest before the catalog says
  `verified`; only then is the local Parquet removed.
- **Fetch** brings verified ranges back into a byte-capped, least-recently-used cache.

The catalog (`app.history_archive_datasets`, migrations 0058 and 0059) only moves
forward and keeps its content immutable; see the migrations for the database rules.
"""

from __future__ import annotations

import fcntl
import gzip
import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .cold_bar_fetch import FetchError, ensure_reserve, sha256_of, stream_member
from .cold_bar_gated_deletion_collectors import (
    borg_list_members_args,
    parse_env_file,
    parse_short_list,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

FINGERPRINT_VERSION = "hafp_v1"
TIMESTAMPTZ = "timestamp with time zone"
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
    kind: str  # the PostgreSQL type exactly as format_type() prints it


def columns_from_spec(spec: str) -> tuple[Column, ...]:
    """`name:type,name:type,...` (types as format_type() prints them) to columns."""
    out = []
    for item in spec.split(","):
        name, _, kind = item.strip().partition(":")
        out.append(Column(name, kind))
    return tuple(out)


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
    time_column: str | None  # None for a snapshot of a plain table
    key: tuple[str, ...]
    columns: tuple[Column, ...]
    archive_prefix: str
    unit: str = "chunk"  # chunk or snapshot
    data_key_column: str | None = "exchange"
    # A chunk dataset archives only chunks lying wholly inside this window, if set.
    window: tuple[datetime, datetime] | None = None
    # A snapshot dataset exports the rows this condition selects (constant SQL, no input).
    snapshot_filter: str = "TRUE"
    hot_days: int = 0
    readers: tuple[Reader, ...] = ()
    open_episode_lookback: timedelta = timedelta(0)
    protected_windows: tuple[tuple[datetime, datetime], ...] = ()

    @property
    def table(self) -> str:
        return f"{self.source_schema}.{self.source_table}"


def duck_type(column: Column) -> str:
    """Timestamps stay timestamps; every other type is kept as its exact text."""
    return "TIMESTAMPTZ" if column.kind == TIMESTAMPTZ else "VARCHAR"


# ---------- canonical content fingerprint (same value from PostgreSQL and DuckDB) ----------


def _pg_value(column: Column) -> str:
    if column.kind == TIMESTAMPTZ:
        return f"((extract(epoch FROM {column.name}) * 1000000)::bigint)::text"
    return f"{column.name}::text"


def _duck_value(column: Column) -> str:
    if column.kind == TIMESTAMPTZ:
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


def pg_fingerprint_where_sql(contract: DatasetContract, where: str, *, table: str = "") -> str:
    """Fingerprint of the rows `where` selects (empty input: the hash of ''). `where` is
    constant SQL from a contract or holds only %s placeholders."""
    return (
        "SELECT encode(sha256(convert_to(coalesce(string_agg(h, '' ORDER BY h), ''), "  # noqa: S608
        "'UTF8')), 'hex') "
        f"FROM (SELECT encode(sha256(convert_to({pg_row_text(contract)}, 'UTF8')), 'hex') AS h "
        f"FROM {table or contract.table} WHERE {where}) AS rows"
    )


def _time_where(contract: DatasetContract) -> str:
    return f"{contract.time_column} >= %s AND {contract.time_column} < %s"


def pg_fingerprint_sql(contract: DatasetContract) -> str:
    """Fingerprint of a chunk's rows; takes (range_start, range_end)."""
    return pg_fingerprint_where_sql(contract, _time_where(contract))


def parquet_fingerprint(contract: DatasetContract, path: Path) -> tuple[int, str]:
    """Row count and content fingerprint of an exported (or extracted) Parquet file."""
    import duckdb

    source = "read_parquet(" + _sql_str(str(path)) + ")"
    row = (
        duckdb.connect()
        .execute(
            "SELECT count(*), sha256(coalesce(string_agg(h, '' ORDER BY h), '')) FROM ("  # noqa: S608
            f"SELECT sha256({duck_row_text(contract)}) AS h FROM {source})"
        )
        .fetchone()
    )
    if row is None or row[1] is None:
        raise ArchiveError(f"{path}: cannot fingerprint")
    return int(row[0]), f"{FINGERPRINT_VERSION}:{row[1]}"


def check_columns(conn: Any, contract: DatasetContract) -> None:
    """The source must have exactly the pinned columns, in order, with the pinned types."""
    rows = conn.execute(
        "SELECT a.attname, format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
        "WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped "
        "ORDER BY a.attnum",
        (contract.table,),
    ).fetchall()
    live = tuple(Column(str(r[0]), str(r[1])) for r in rows)
    if live != contract.columns:
        pinned = {(c.name, c.kind) for c in contract.columns}
        drift = sorted({(c.name, c.kind) for c in live} ^ pinned)
        raise ArchiveError(
            f"{contract.table} columns differ from {contract.contract_version}: {drift}"
        )


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
    chunk_name: str | None
    range_start: str | None
    range_end: str | None
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
    unit: str = "chunk"
    snapshot_set: str | None = None
    snapshot_filter: str | None = None

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
    where = (
        f"{contract.time_column} >= '{start.isoformat()}' "
        f"AND {contract.time_column} < '{end.isoformat()}'"
    )
    _copy_where_gz(conn, contract, where, out)


def _copy_where_gz(conn: Any, contract: DatasetContract, where: str, out: Path) -> None:
    # Every column but a timestamp is selected as `::text`, the exact text the content
    # fingerprint hashes (COPY's own output differs for some types, e.g. boolean `t`
    # against `true`), so the file and the fingerprint agree by construction.
    columns = ", ".join(
        c.name if c.kind == TIMESTAMPTZ else f"{c.name}::text AS {c.name}" for c in contract.columns
    )
    query = (
        f"COPY (SELECT {columns} FROM {contract.table} WHERE {where}) "  # noqa: S608
        "TO STDOUT (FORMAT csv, FORCE_QUOTE *)"
    )
    with gzip.open(out, "wb", compresslevel=3) as sink, conn.cursor().copy(query) as copy:
        for block in copy:
            sink.write(bytes(block))


def _csv_to_parquet(contract: DatasetContract, csv_gz: Path, parquet: Path) -> None:
    import duckdb

    columns = "{" + ", ".join(f"'{c.name}': '{duck_type(c)}'" for c in contract.columns) + "}"
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
            check_columns(conn, contract)
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
            keys = _data_keys(
                conn, contract, _time_where(contract), (chunk.range_start, chunk.range_end)
            )
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
        data_keys=keys,
        file_name=parquet_name,
        file_bytes=target.stat().st_size,
        file_sha256=sha256_of(target),
        content_fingerprint=source_fp,
        snapshot_at=snapshot_at.astimezone(UTC).isoformat(),
        code_revision=code_revision,
    )
    (out_dir / manifest_name).write_text(manifest.to_json())
    return manifest


def _data_keys(
    conn: Any, contract: DatasetContract, where: str, params: Sequence[Any]
) -> tuple[dict[str, Any], ...]:
    """Rows per data-key value (and the time span, for a chunk), for the manifest."""
    key = contract.data_key_column
    if key is None:
        return ()
    span = (
        f", min({contract.time_column}), max({contract.time_column})"
        if contract.time_column
        else ""
    )
    rows = conn.execute(
        f"SELECT {key}, count(*){span} FROM {contract.table} WHERE {where} "  # noqa: S608
        f"GROUP BY {key} ORDER BY {key}",
        params,
    ).fetchall()
    out = []
    for r in rows:
        item: dict[str, Any] = {key: r[0], "rows": int(r[1])}
        if span:
            item |= {"first": r[2].isoformat(), "last": r[3].isoformat()}
        out.append(item)
    return tuple(out)


def snapshot_file_names(contract: DatasetContract, set_id: str, revision: int) -> tuple[str, str]:
    stem = f"{contract.source_table}-{set_id}-r{revision}"
    return f"{stem}.parquet", f"{stem}.manifest.json"


def export_snapshot_member(
    conn: Any,
    contract: DatasetContract,
    out_dir: Path,
    *,
    set_id: str,
    revision: int,
    snapshot_at: datetime,
    code_revision: str,
) -> Manifest:
    """Write the rows `contract.snapshot_filter` selects, as the caller's open REPEATABLE
    READ snapshot sees them, to Parquet with a manifest. All members of one snapshot set
    are exported inside the same transaction, so they describe one instant. A member may
    hold zero rows."""
    if contract.unit != "snapshot":
        raise ArchiveError(f"{contract.dataset} is not a snapshot dataset")
    out_dir.mkdir(parents=True, exist_ok=True)
    check_columns(conn, contract)
    where = contract.snapshot_filter
    parquet_name, manifest_name = snapshot_file_names(contract, set_id, revision)
    count_row = conn.execute(
        f"SELECT count(*) FROM {contract.table} WHERE {where}"  # noqa: S608
    ).fetchone()
    row_count = int(count_row[0])
    fp_row = conn.execute(pg_fingerprint_where_sql(contract, where)).fetchone()
    source_fp = f"{FINGERPRINT_VERSION}:{fp_row[0]}"
    keys = _data_keys(conn, contract, where, ())
    staging_csv = out_dir / f".{parquet_name}.csv.gz.partial"
    staging_parquet = out_dir / f".{parquet_name}.partial"
    try:
        _copy_where_gz(conn, contract, where, staging_csv)
        _csv_to_parquet(contract, staging_csv, staging_parquet)
        written_rows, file_fp = parquet_fingerprint(contract, staging_parquet)
        if written_rows != row_count or file_fp != source_fp:
            raise ArchiveError(
                f"{contract.dataset}: Parquet has {written_rows} rows / {file_fp}, the "
                f"snapshot had {row_count} / {source_fp}; nothing kept"
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
        chunk_name=None,
        range_start=None,
        range_end=None,
        columns=tuple((c.name, c.kind) for c in contract.columns),
        key=contract.key,
        row_count=row_count,
        data_keys=keys,
        file_name=parquet_name,
        file_bytes=target.stat().st_size,
        file_sha256=sha256_of(target),
        content_fingerprint=source_fp,
        snapshot_at=snapshot_at.astimezone(UTC).isoformat(),
        code_revision=code_revision,
        unit="snapshot",
        snapshot_set=set_id,
        snapshot_filter=where,
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
        "WHERE dataset = %s AND unit = 'chunk' AND state <> 'superseded' ORDER BY range_start",
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
        "file_sha256, content_fingerprint, manifest_sha256, snapshot_at, code_revision, unit, "
        "snapshot_set) VALUES (%s, %s, %s, %s, %s, %s, %s, 'exported', %s, %s, %s, %s, %s, %s, "
        "%s, %s, %s, %s) RETURNING id",
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
            manifest.unit,
            manifest.snapshot_set,
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
    """One catalog-writing run per archive family (its prefix): a file lock on the staging
    directory (against another run on this host) and a session advisory lock (against a
    run elsewhere)."""
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
                (ARCHIVER_LOCK_NAMESPACE, contract.archive_prefix),
            ).fetchone()
            if not got or not got[0]:
                raise ArchiveError(
                    f"another history-archive run holds the {contract.archive_prefix} lock"
                )
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
            if contract.window is not None and not (
                contract.window[0] <= chunk.range_start and chunk.range_end <= contract.window[1]
            ):
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


@dataclass(frozen=True)
class SnapshotRow:
    """A live catalog row of a snapshot (no range: the rows of a table at one instant)."""

    id: int
    state: str
    dataset: str
    snapshot_set: str
    revision: int
    row_count: int
    file_name: str
    file_bytes: int
    file_sha256: str
    content_fingerprint: str
    borg_archive: str | None
    manifest_sha256: str


ArchivedRow = CatalogRow | SnapshotRow


def snapshot_rows(conn: Any, contract: DatasetContract) -> list[SnapshotRow]:
    rows = conn.execute(
        "SELECT id, state, dataset, snapshot_set, revision, row_count, file_name, file_bytes, "
        "file_sha256, content_fingerprint, borg_archive, manifest_sha256 "
        "FROM app.history_archive_datasets WHERE dataset = %s AND unit = 'snapshot' "
        "AND state <> 'superseded' ORDER BY id",
        (contract.dataset,),
    ).fetchall()
    return [
        SnapshotRow(
            int(r[0]),
            str(r[1]),
            str(r[2]),
            str(r[3]),
            int(r[4]),
            int(r[5]),
            str(r[6]),
            int(r[7]),
            str(r[8]),
            str(r[9]),
            r[10],
            str(r[11]),
        )
        for r in rows
    ]


def _live(conn: Any, contract: DatasetContract) -> list[ArchivedRow]:
    if contract.unit == "snapshot":
        return list(snapshot_rows(conn, contract))
    return list(live_rows(conn, contract).values())


def _family(contracts: DatasetContract | Sequence[DatasetContract]) -> list[DatasetContract]:
    group = [contracts] if isinstance(contracts, DatasetContract) else list(contracts)
    if not group or len({c.archive_prefix for c in group}) != 1:
        raise ArchiveError("one archive run takes datasets of one archive prefix")
    return group


def run_archive(
    dsn: str,
    contracts: DatasetContract | Sequence[DatasetContract],
    out_dir: Path,
    *,
    repo: str,
    env: Mapping[str, str],
    now: datetime,
) -> StepReport:
    """Archive every `exported` row's Parquet and manifest, across the given datasets of
    one archive prefix, into one new archive."""
    group = _family(contracts)
    report = StepReport()
    with archiver_session(dsn, group[0], out_dir) as conn:
        pending = [r for c in group for r in _live(conn, c) if r.state == "exported"]
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
        archive = group[0].archive_prefix + now.strftime("%Y-%m-%dT%H:%M:%S")
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
    contracts: DatasetContract | Sequence[DatasetContract],
    out_dir: Path,
    *,
    repo: str,
    env: Mapping[str, str],
    now: datetime,
    reserve_bytes: int = DEFAULT_RESERVE_BYTES,
) -> StepReport:
    """Extract each `archived` row from its own archive and accept it only when the
    extracted bytes, content and manifest equal the catalog; then drop the local
    Parquet."""
    group = _family(contracts)
    report = StepReport()
    with archiver_session(dsn, group[0], out_dir) as conn:
        for contract in group:
            for row in _live(conn, contract):
                if row.state != "archived" or row.borg_archive is None:
                    continue
                try:
                    sha, fingerprint = verify_extracted(
                        contract, row, out_dir, repo=repo, env=env, reserve_bytes=reserve_bytes
                    )
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


def verify_extracted(
    contract: DatasetContract,
    row: ArchivedRow,
    work_dir: Path,
    *,
    repo: str,
    env: Mapping[str, str],
    reserve_bytes: int,
    keep: Path | None = None,
) -> tuple[str, str]:
    """Extract one row's Parquet and manifest from its archive and check both against
    the catalog. Returns (sha256, fingerprint); with `keep`, the Parquet is moved there."""
    ensure_reserve(work_dir, row.file_bytes, reserve_bytes)
    with tempfile.TemporaryDirectory(dir=work_dir) as tmp:
        extracted = Path(tmp) / row.file_name
        sha = stream_member(
            repo, str(row.borg_archive), row.file_name, extracted, dict(env), row.file_bytes
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
        if keep is not None:
            extracted.replace(keep)
    return sha, fingerprint


MAX_MANIFEST_BYTES = 1024 * 1024


def manifest_member(file_name: str) -> str:
    return file_name.removesuffix(".parquet") + ".manifest.json"


def _verify_archived_manifest(
    contract: DatasetContract, row: ArchivedRow, repo: str, env: Mapping[str, str], tmp: Path
) -> None:
    """The manifest in the same archive must be the one the catalog recorded (SHA-256)
    and must describe this exact range or snapshot, file and content."""
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
    expected: dict[str, Any] = {
        "dataset": contract.dataset,
        "contract_version": contract.contract_version,
        "schema_version": contract.schema_version,
        "source_table": contract.table,
        "row_count": row.row_count,
        "file_name": row.file_name,
        "file_bytes": row.file_bytes,
        "file_sha256": row.file_sha256,
        "content_fingerprint": row.content_fingerprint,
    }
    if isinstance(row, SnapshotRow):
        expected |= {"unit": "snapshot", "snapshot_set": row.snapshot_set}
    else:
        expected |= {
            "unit": "chunk",
            "chunk_name": row.chunk_name,
            "range_start": row.range_start.isoformat(),
            "range_end": row.range_end.isoformat(),
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


# ---------- restore check ----------


def restore_into(
    conn: Any,
    contract: DatasetContract,
    parquet: Path,
    target: str,
    *,
    where: str = "TRUE",
    create: bool = True,
) -> int:
    """Load the rows of an archive file that `where` (constant DuckDB SQL) selects into
    `target`, created with the pinned column types unless `create` is False. Returns the
    number of rows written. Values pass as text, so nothing is rounded."""
    import duckdb

    if create:
        columns = ", ".join(f"{c.name} {c.kind}" for c in contract.columns)
        conn.execute(f"CREATE TABLE {target} ({columns})")
    select = ", ".join(
        f"CAST(epoch_us({c.name}) AS VARCHAR)" if c.kind == TIMESTAMPTZ else c.name
        for c in contract.columns
    )
    reader = duckdb.connect().execute(
        f"SELECT {select} FROM read_parquet({_sql_str(str(parquet))}) WHERE {where}"  # noqa: S608
    )
    written = 0
    names = ", ".join(c.name for c in contract.columns)
    with conn.cursor().copy(f"COPY {target} ({names}) FROM STDIN") as copy:
        while batch := reader.fetchmany(50_000):
            for values in batch:
                copy.write_row(
                    [
                        _from_epoch_us(v) if c.kind == TIMESTAMPTZ and v is not None else v
                        for c, v in zip(contract.columns, values, strict=True)
                    ]
                )
                written += 1
    return written


def _from_epoch_us(value: str) -> datetime:
    micros = int(value)
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=micros)
