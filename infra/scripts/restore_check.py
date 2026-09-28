#!/usr/bin/env python3
"""ENG-025: a database backup with a content receipt, and a narrow restore drill.

Standard library only: it runs on the production host as root, next to
offsite-backup.sh.

`backup` (called by offsite-backup.sh for the db archive)
    Opens one psql session in the production container, starts a
    REPEATABLE READ READ ONLY transaction and exports its snapshot. Inside that
    snapshot it resolves the critical table set (the seed tables plus every
    table they reference through foreign keys, transitively), and records for
    each table its row count and a hash over every full row in primary-key
    order. It also records the owned sequences and indexes of those tables and
    the schema revision. While the transaction is still open it creates the db
    archive with `pg_dump --snapshot`, through borg --content-from-command so a
    dying pg_dump never leaves a truncated archive. Only after that archive
    succeeds does it write the receipt, as its own small archive
    `dbreceipt-<same timestamp>`. So the receipt describes exactly the data in
    the dump.

`check` (the weekly drill)
    Takes the newest db archive that has a receipt, and starts only if the
    free disk space covers the estimated peak plus a fixed reserve. Then:
    1. start a throwaway container of the production image (no network, its
       own volume, no published port);
    2. stream the dump once to read its table of contents, and select the
       entries of the critical set only (schema, types, the tables, their
       data, constraints, owned sequences and indexes);
    3. stream it again into pg_restore with that list and --exit-on-error;
    4. recompute the same counts and row hashes and the schema revision, and
       compare them with the receipt.

    A disk watchdog aborts the drill if free space falls under the reserve.
    The container and its volume are always removed. The outcome is a JSON
    record and a stamp for the health check, and a Telegram alert on failure.
    It measures the restore time of the selected set and the age of the
    verified restore point. It is not the RTO/RPO of the whole system.
"""

from __future__ import annotations

# ruff: noqa: S608 -- the only values interpolated into SQL are table, index and
# sequence names taken from SEED_TABLES and from the database's own catalogue.
import argparse
import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

RECEIPT_VERSION = "restore_check_receipt_v1"
# Irreplaceable or formal-read-bearing tables. Their foreign-key parents are
# added automatically, so the restored set is closed under its constraints.
SEED_TABLES = (
    "app.alembic_version",
    "app.trades",
    "app.trade_close_fills",
    "app.trade_exit_liquidity_observations",
    "app.alerts",
    "app.early_momentum_episodes",
    "app.formal_read_claims",
    "app.hold12h_formal_read_claims",
    "app.research_cohort_registrations",
    "app.source_lead_captures",
    "app.source_lead_target_observations",
    "app.source_lead_qualifications",
    "app.source_lead_exit_observations",
    "app.source_lead_shadow_attempts",
    "app.pump_events",
    "app.pump_event_sources",
    "app.momentum_flow_paper_runs",
    "app.momentum_flow_paper_probes",
    "app.momentum_flow_watch_runs",
    "app.momentum_flow_watch_states",
    "app.hold12h_funding_coverage_runs",
    "app.hold12h_funding_settlements",
)
RESERVE_BYTES = 10 * 1024**3
# The newest receipted archive must be this fresh. Backups are nightly, so an older
# restore point means receipts stopped: the drill must fail rather than keep
# certifying an ever older copy.
MAX_RESTORE_POINT_AGE_HOURS = 48
PEAK_FACTOR = 2.0  # data + indexes + WAL + temporary files, over the table sizes
END = "__END__"


class CheckError(RuntimeError):
    pass


# --- psql session ----------------------------------------------------------------


class Psql:
    """One interactive psql process. `query` sends SQL and returns its output lines,
    delimited by an echoed marker; ON_ERROR_STOP makes any error end the process."""

    def __init__(self, argv: list[str]) -> None:
        self.proc = subprocess.Popen(  # noqa: S603 -- fixed argv built by this script
            [*argv, "-qAtX", "-v", "ON_ERROR_STOP=1"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )

    def query(self, sql: str) -> list[str]:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(sql.rstrip().rstrip(";") + ";\n\\echo " + END + "\n")
        self.proc.stdin.flush()
        lines: list[str] = []
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            if line == END:
                return lines
            lines.append(line)
        raise CheckError(f"psql ended before completing: {sql.splitlines()[0][:80]}")

    def close(self) -> None:
        if self.proc.stdin is not None:
            with contextlib.suppress(BrokenPipeError):
                self.proc.stdin.close()
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def _array(values: list[str] | tuple[str, ...]) -> str:
    return "ARRAY[" + ",".join("'" + v.replace("'", "''") + "'" for v in values) + "]"


def critical_set(psql: Psql, seeds: tuple[str, ...]) -> list[str]:
    """Every seed table plus its foreign-key parents, transitively. A missing seed is
    refused: silently dropping it would let the receipt and the drill agree on a set
    that no longer contains a critical table."""
    missing = psql.query(
        f"SELECT s FROM unnest({_array(seeds)}) AS s WHERE to_regclass(s) IS NULL ORDER BY 1"
    )
    if missing:
        raise CheckError(f"critical tables missing from the database: {missing}")
    return psql.query(
        f"""
        WITH RECURSIVE seed AS (
            SELECT to_regclass(s) AS rel FROM unnest({_array(seeds)}) AS s
        ), closure(rel) AS (
            SELECT rel FROM seed WHERE rel IS NOT NULL
            UNION
            SELECT c.confrelid::regclass FROM pg_constraint c
            JOIN closure ON c.conrelid = closure.rel
            WHERE c.contype = 'f'
        )
        SELECT n.nspname || '.' || k.relname FROM closure
        JOIN pg_class k ON k.oid = closure.rel
        JOIN pg_namespace n ON n.oid = k.relnamespace
        ORDER BY 1
        """
    )


def fingerprint(psql: Psql, tables: list[str]) -> dict[str, dict[str, Any]]:
    """Row count and a hash over every full row, in primary-key order (every column
    in declaration order when a table has no primary key), per table."""
    out: dict[str, dict[str, Any]] = {}
    for table in tables:
        schema, name = table.split(".", 1)
        order = psql.query(
            f"""
            SELECT coalesce(
                (SELECT string_agg(quote_ident(a.attname), ',' ORDER BY k.ord)
                 FROM pg_index i
                 CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)
                 JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
                 WHERE i.indrelid = '{table}'::regclass AND i.indisprimary),
                (SELECT string_agg(quote_ident(attname), ',' ORDER BY attnum)
                 FROM pg_attribute
                 WHERE attrelid = '{table}'::regclass AND attnum > 0 AND NOT attisdropped))
            """
        )[0]
        ident = f'"{schema}"."{name}"'
        [line] = psql.query(
            f"SELECT count(*) || '|' || coalesce(md5(string_agg(md5(t::text), '' "
            f"ORDER BY {order})), '') FROM {ident} t"
        )
        count, digest = line.split("|", 1)
        out[table] = {"rows": int(count), "row_hash": digest}
    return out


def schema_objects(psql: Psql, tables: list[str]) -> dict[str, Any]:
    """Owned sequences, index names and total sizes of the set: the restore list and
    the disk estimate need them, and the dump's table of contents alone cannot map an
    index or a sequence to its table."""
    arr = _array(tables)
    sequences = psql.query(
        f"""
        SELECT DISTINCT sn.nspname || '.' || s.relname FROM pg_depend d
        JOIN pg_class s ON s.oid = d.objid AND s.relkind = 'S'
        JOIN pg_namespace sn ON sn.oid = s.relnamespace
        WHERE d.refobjid IN (SELECT to_regclass(x)::oid FROM unnest({arr}) x)
          AND d.deptype IN ('a', 'i')
        ORDER BY 1
        """
    )
    indexes = psql.query(
        f"""
        SELECT schemaname || '.' || indexname FROM pg_indexes
        WHERE schemaname || '.' || tablename = ANY({arr}) ORDER BY 1
        """
    )
    [size] = psql.query(
        f"SELECT coalesce(sum(pg_total_relation_size(to_regclass(x))), 0) FROM unnest({arr}) x"
    )
    [revision] = psql.query("SELECT string_agg(version_num, ',') FROM app.alembic_version")
    return {
        "owned_sequences": sequences,
        "indexes": indexes,
        "total_relation_bytes": int(size),
        "schema_revision": revision,
    }


# --- the restore list --------------------------------------------------------------

_TYPES = (
    "SEQUENCE OWNED BY",
    "SEQUENCE SET",
    "FK CONSTRAINT",
    "TABLE DATA",
    "CONSTRAINT",
    "SEQUENCE",
    "EXTENSION",
    "TRIGGER",
    "DEFAULT",
    "SCHEMA",
    "DOMAIN",
    "TABLE",
    "INDEX",
    "TYPE",
)


def parse_toc_line(line: str) -> tuple[str, list[str]] | None:
    """`215; 1259 16432 TABLE app trades owner` -> ("TABLE", ["app", "trades", "owner"])."""
    if not line.strip() or line.lstrip().startswith(";") or ";" not in line:
        return None
    rest = line.split(";", 1)[1].split()
    if len(rest) < 3:
        return None
    body = " ".join(rest[2:])
    for kind in _TYPES:
        if body.startswith(kind + " "):
            return kind, body[len(kind) + 1 :].split()
    return body.split()[0], body.split()[1:]


def restore_list(toc: list[str], receipt: dict[str, Any]) -> list[str]:
    """The table-of-contents lines to restore: the critical set closed under its
    dependencies, nothing else (no hypertable, no other table, no comment or ACL)."""
    tables = set(receipt["tables"])
    schemas = {t.split(".", 1)[0] for t in tables}
    sequences = set(receipt["owned_sequences"])
    indexes = set(receipt["indexes"])
    keep = []
    for line in toc:
        parsed = parse_toc_line(line)
        if parsed is None:
            continue
        kind, args = parsed
        qualified = f"{args[0]}.{args[1]}" if len(args) >= 2 else ""
        wanted = (
            (kind == "SCHEMA" and len(args) >= 2 and args[1] in schemas)
            or kind == "EXTENSION"
            or (kind in ("TYPE", "DOMAIN") and args[0] in schemas)
            or (
                kind in ("TABLE", "TABLE DATA", "CONSTRAINT", "FK CONSTRAINT", "DEFAULT", "TRIGGER")
                and qualified in tables
            )
            or (
                kind in ("SEQUENCE", "SEQUENCE OWNED BY", "SEQUENCE SET") and qualified in sequences
            )
            or (kind == "INDEX" and qualified in indexes)
        )
        if wanted:
            keep.append(line)
    missing = tables - {
        f"{p[1][0]}.{p[1][1]}"
        for p in map(parse_toc_line, keep)
        if p is not None and p[0] == "TABLE DATA"
    }
    if missing:
        raise CheckError(f"the dump has no data entry for {sorted(missing)}")
    return keep


# --- backup ------------------------------------------------------------------------


def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, text=True, check=False, **kw)  # noqa: S603 -- fixed argv


def backup(args: argparse.Namespace) -> int:
    """Create the db archive from an exported snapshot, then its receipt archive."""
    psql = Psql(
        ["docker", "exec", "-i", args.container, "psql", "-U", args.db_user, "-d", args.db_name]
    )
    try:
        psql.query("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        [snapshot] = psql.query("SELECT pg_export_snapshot()")
        taken_at = datetime.now(UTC).isoformat()
        receipt: dict[str, Any] | None = None
        receipt_error = ""
        try:
            tables = critical_set(psql, SEED_TABLES)
            receipt = {
                "version": RECEIPT_VERSION,
                "db_archive": args.db_archive,
                "snapshot_taken_at": taken_at,
                "tables": tables,
                "fingerprints": fingerprint(psql, tables),
                **schema_objects(psql, tables),
            }
        except CheckError as exc:
            # The dump is still a backup and is still taken; only the receipt is
            # refused, and the drill will not certify this archive.
            receipt_error = str(exc)
        dump = run(
            [
                "borg",
                "create",
                "--stats",
                "--compression",
                "zstd,3",
                "--content-from-command",
                "--stdin-name",
                "schurfer.dump",
                f"::{args.db_archive}",
                "--",
                "docker",
                "exec",
                args.container,
                "pg_dump",
                "-U",
                args.db_user,
                "-d",
                args.db_name,
                "-Fc",
                "-Z0",
                f"--snapshot={snapshot}",
            ]
        )
        if dump.returncode != 0:
            sys.stderr.write(f"borg create failed for {args.db_archive}\n")
            return 2
        psql.query("COMMIT")
    finally:
        psql.close()
    if receipt is None:
        sys.stderr.write(f"db archive kept, but no receipt: {receipt_error}\n")
        return 3
    receipt_archive = args.db_archive.replace("db-", "dbreceipt-", 1)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(receipt, handle, indent=1, sort_keys=True)
        receipt_path = handle.name
    try:
        made = run(
            [
                "borg",
                "create",
                "--content-from-command",
                "--stdin-name",
                "receipt.json",
                f"::{receipt_archive}",
                "--",
                "cat",
                receipt_path,
            ]
        )
    finally:
        Path(receipt_path).unlink()
    if made.returncode != 0:
        run(["borg", "delete", f"::{receipt_archive}"], capture_output=True)
        sys.stderr.write(f"db archive kept, but its receipt {receipt_archive} failed\n")
        return 3
    return 0


# --- check -------------------------------------------------------------------------


@dataclass
class Drill:
    image: str
    container: str
    volume: str
    reserve: int
    state_dir: Path
    disk_path: str


def newest_verified_archive() -> tuple[str, dict[str, Any]]:
    listed = run(["borg", "list", "--short"], capture_output=True)
    if listed.returncode != 0:
        raise CheckError("borg list failed")
    names = set(listed.stdout.split())
    candidates = sorted(
        n for n in names if n.startswith("db-") and n.replace("db-", "dbreceipt-", 1) in names
    )
    if not candidates:
        raise CheckError("no db archive has a receipt")
    archive = candidates[-1]
    got = run(
        ["borg", "extract", "--stdout", f"::{archive.replace('db-', 'dbreceipt-', 1)}"],
        capture_output=True,
    )
    if got.returncode != 0:
        raise CheckError(f"could not read the receipt of {archive}")
    receipt = json.loads(got.stdout)
    if receipt.get("version") != RECEIPT_VERSION or receipt.get("db_archive") != archive:
        raise CheckError(f"the receipt does not belong to {archive}")
    return archive, receipt


def stream(archive: str, consumer: list[str]) -> subprocess.CompletedProcess[str]:
    """borg extract --stdout into a consumer. A consumer that stops reading early makes
    borg fail with a broken pipe, so both exit codes are returned to the caller."""
    # borg's stderr is discarded: pg_restore stops reading once it has what it needs,
    # and borg then prints a BrokenPipeError traceback that is expected and alarming.
    # pg_restore's own result decides; a truly failed extract shows up there as a
    # truncated or unreadable archive.
    producer = subprocess.Popen(  # noqa: S603 -- fixed argv
        ["borg", "extract", "--stdout", f"::{archive}"],  # noqa: S607 -- borg from PATH
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert producer.stdout is not None
    result = subprocess.run(  # noqa: S603 -- fixed argv
        consumer, stdin=producer.stdout, capture_output=True, text=True, check=False
    )
    producer.stdout.close()
    producer.wait()
    result.producer_returncode = producer.returncode  # type: ignore[attr-defined]
    return result


def watchdog(drill: Drill, stop: threading.Event, tripped: threading.Event) -> None:
    while not stop.wait(5):
        if shutil.disk_usage(drill.disk_path).free < drill.reserve:
            tripped.set()
            run(["docker", "rm", "-f", drill.container], capture_output=True)
            return


def remove_drill(drill: Drill) -> str | None:
    """Remove the drill container and volume, then confirm both are gone. Returns what
    is left, or None. The removal commands' own exit codes are not trusted: removing a
    container that never started fails too, while a real leftover is what matters."""
    run(["docker", "rm", "-f", drill.container], capture_output=True)
    run(["docker", "volume", "rm", "-f", drill.volume], capture_output=True)
    left = []
    if (
        run(["docker", "container", "inspect", drill.container], capture_output=True).returncode
        == 0
    ):
        left.append(f"container {drill.container}")
    if run(["docker", "volume", "inspect", drill.volume], capture_output=True).returncode == 0:
        left.append(f"volume {drill.volume}")
    return ", ".join(left) or None


def check(args: argparse.Namespace) -> int:
    drill = Drill(
        image=args.image,
        container=args.drill_container,
        volume=args.drill_container + "-data",
        reserve=args.reserve_bytes,
        state_dir=Path(args.state_dir),
        disk_path=args.disk_path,
    )
    started = time.monotonic()
    record: dict[str, Any] = {"started_at": datetime.now(UTC).isoformat(), "ok": False}
    stop, tripped = threading.Event(), threading.Event()
    try:
        archive, receipt = newest_verified_archive()
        record["archive"] = archive
        record["verified_restore_point"] = receipt["snapshot_taken_at"]
        age = datetime.now(UTC) - datetime.fromisoformat(receipt["snapshot_taken_at"])
        record["restore_point_age_hours"] = round(age.total_seconds() / 3600, 1)
        if age.total_seconds() > args.max_restore_point_age_hours * 3600:
            raise CheckError(
                f"the newest receipted archive is {record['restore_point_age_hours']}h old "
                f"(limit {args.max_restore_point_age_hours}h): receipts have stopped"
            )
        estimate = int(receipt["total_relation_bytes"] * PEAK_FACTOR)
        free = shutil.disk_usage(drill.disk_path).free
        record["estimated_peak_bytes"] = estimate
        if free < estimate + drill.reserve:
            raise CheckError(f"free {free} < estimated peak {estimate} + reserve {drill.reserve}")
        # A leftover volume from an earlier run would hand this drill an old database.
        leftover = remove_drill(drill)
        if leftover:
            raise CheckError(f"a previous drill could not be cleaned up: {leftover}")
        started_box = run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                drill.container,
                "--network",
                "none",
                "-e",
                "POSTGRES_PASSWORD=restore-check",
                "-e",
                "POSTGRES_USER=schurfer",
                "-e",
                "POSTGRES_DB=restore_check",
                "-v",
                f"{drill.volume}:/var/lib/postgresql/data",
                drill.image,
            ],
            capture_output=True,
        )
        if started_box.returncode != 0:
            raise CheckError(f"could not start the drill container: {started_box.stderr[-300:]}")
        guard = threading.Thread(target=watchdog, args=(drill, stop, tripped), daemon=True)
        guard.start()
        for _ in range(120):
            # Over TCP on purpose: the image's entrypoint first runs a temporary
            # server on the unix socket only (for its init scripts), stops it, and
            # then starts the real one. Checking the socket raced that restart.
            ready = run(
                [
                    "docker",
                    "exec",
                    drill.container,
                    "pg_isready",
                    "-h",
                    "127.0.0.1",
                    "-U",
                    "schurfer",
                    "-d",
                    "restore_check",
                ],
                capture_output=True,
            )
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise CheckError("the drill database never became ready")
        toc = stream(archive, ["docker", "exec", "-i", drill.container, "pg_restore", "-l"])
        if toc.returncode != 0:
            raise CheckError(f"pg_restore -l failed: {toc.stderr[-300:]}")
        selected = restore_list(toc.stdout.splitlines(), receipt)
        record["restore_list_entries"] = len(selected)
        put = run(
            # A path inside the throwaway drill container, not on the host.
            ["docker", "exec", "-i", drill.container, "sh", "-c", "cat > /tmp/restore.list"],
            input="\n".join(selected) + "\n",
            capture_output=True,
        )
        if put.returncode != 0:
            raise CheckError("could not write the restore list into the drill container")
        restored = stream(
            archive,
            [
                "docker",
                "exec",
                "-i",
                drill.container,
                "pg_restore",
                "-U",
                "schurfer",
                "-d",
                "restore_check",
                "--no-owner",
                "--no-privileges",
                "--exit-on-error",
                "-L",
                "/tmp/restore.list",  # noqa: S108 -- inside the throwaway drill container
            ],
        )
        if tripped.is_set():
            raise CheckError("aborted: free disk space fell under the reserve")
        if restored.returncode != 0:
            raise CheckError(f"pg_restore failed: {restored.stderr[-500:]}")
        psql = Psql(
            [
                "docker",
                "exec",
                "-i",
                drill.container,
                "psql",
                "-U",
                "schurfer",
                "-d",
                "restore_check",
            ]
        )
        try:
            got = fingerprint(psql, receipt["tables"])
            [revision] = psql.query("SELECT string_agg(version_num, ',') FROM app.alembic_version")
        finally:
            psql.close()
        mismatches = {
            t: {"expected": receipt["fingerprints"][t], "restored": got.get(t)}
            for t in receipt["tables"]
            if got.get(t) != receipt["fingerprints"][t]
        }
        record["tables"] = len(receipt["tables"])
        record["rows"] = sum(v["rows"] for v in receipt["fingerprints"].values())
        record["schema_revision_ok"] = revision == receipt["schema_revision"]
        record["mismatches"] = mismatches
        record["ok"] = not mismatches and record["schema_revision_ok"]
        if not record["ok"]:
            raise CheckError(f"restored data differs from the receipt: {sorted(mismatches)[:5]}")
    except CheckError as exc:
        record["error"] = str(exc)
    finally:
        stop.set()
        cleanup_error = remove_drill(drill)
        if cleanup_error:
            record["cleanup_error"] = cleanup_error
            record["ok"] = False
            record.setdefault("error", cleanup_error)
        record["restore_seconds_selected_set"] = round(time.monotonic() - started, 1)
        record["finished_at"] = datetime.now(UTC).isoformat()
        write_record(drill.state_dir, record)
    if not record["ok"]:
        sys.stderr.write(f"restore check FAILED: {record.get('error')}\n")
        return 1
    summary = {k: record[k] for k in ("archive", "tables", "rows", "restore_seconds_selected_set")}
    sys.stdout.write(json.dumps(summary) + "\n")
    return 0


def write_record(state_dir: Path, record: dict[str, Any]) -> None:
    target = state_dir / "restore-check"
    target.mkdir(parents=True, exist_ok=True)
    name = record["started_at"].replace(":", "").replace("+0000", "Z")
    (target / f"{name}.json").write_text(json.dumps(record, indent=1, sort_keys=True))
    if record["ok"]:
        (state_dir / "restore-check.stamp").write_text(record["finished_at"] + "\n")


def main(argv: list[str] | None = None, out: IO[str] = sys.stdout) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    b = sub.add_parser("backup")
    b.add_argument("--container", required=True)
    b.add_argument("--db-user", required=True)
    b.add_argument("--db-name", required=True)
    b.add_argument("--db-archive", required=True)
    c = sub.add_parser("check")
    c.add_argument("--image", required=True, help="the production postgres image, by digest")
    c.add_argument("--drill-container", default="schurfer-restore-check")
    c.add_argument("--state-dir", required=True)
    c.add_argument("--disk-path", default="/")
    c.add_argument("--reserve-bytes", type=int, default=RESERVE_BYTES)
    c.add_argument("--max-restore-point-age-hours", type=int, default=MAX_RESTORE_POINT_AGE_HOURS)
    args = parser.parse_args(argv)
    return backup(args) if args.mode == "backup" else check(args)


if __name__ == "__main__":
    sys.exit(main())
