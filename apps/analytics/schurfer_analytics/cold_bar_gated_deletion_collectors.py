"""Concrete Collectors for cold-bar gated deletion: the impure wrappers around
Borg, the manifest/receipt files, and the source database that the dry-run runner
(``cold_bar_gated_deletion_job``) drives. The safety decisions live in the pure
gate; this file only gathers evidence.

The subprocess/DB calls here cannot be unit-tested without a real Borg repo and
Postgres, so the parts that CAN be tested -- command construction, Borg-output
parsing, and the targeted-drop transaction (``drop_one_chunk_under_lock``) -- are
pulled out as pure/injectable helpers with their own tests, and the thin wrappers are
covered by the integration tests named in the runbook. ``drop_chunk`` performs a real,
targeted, lock-guarded drop; it runs only when the gated-deletion job is invoked with
``--execute`` (dry-run is the default and deletes nothing).
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
from datetime import UTC, date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .cold_bar_export import (
    connect,
    day_bounds,
    file_fingerprint,
    source_fingerprint,
)
from .cold_bar_gated_deletion_job import (
    ARCHIVE_MEMBER_PREFIX,
    RECEIPT_SUFFIX,
    ChunkCandidate,
    ExtractedOffsite,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import datetime

    from .cold_bar_gated_deletion import DropReceipt

# Advisory-lock namespace for cold-bar day mutation. The gated drop holds
# pg_advisory_xact_lock(COLD_BAR_MUTATION_LOCK_KEY) across "re-verify source unchanged" and
# "drop the chunk", so those two steps are atomic with respect to any OTHER holder of the
# same key. The live collector (apps/collector/internal/momentumcapture/writer.go) is EXEMPT:
# it appends only current-minute buckets and its INSERT is a no-op ON CONFLICT (rows are
# immutable), so it can neither mutate nor insert into a chunk old enough to be a deletion
# candidate. There is no historical backfill/repair path for these bars today; if one is ever
# added it MUST take this same lock before writing a day at/older than the deletion cutoff, or
# the TOCTOU guarantee is void.
COLD_BAR_MUTATION_LOCK_KEY = 0x0C01DBA25


class ColdBarSourceChangedError(RuntimeError):
    """The live source fingerprint no longer matches the receipt at drop time (a change
    slipped in since export); the drop is refused and rolled back."""


class ColdBarDropSetError(RuntimeError):
    """A targeted drop_chunks would have removed a number of chunks other than exactly one;
    the transaction is rolled back and nothing is deleted."""


# Real deletion (--execute) is only ever allowed at or beyond the 40-day retention buffer, so
# a day that failed a check waits for repair rather than racing a deadline. A reconciliation
# dry-run may use a smaller cutoff (it deletes nothing), but execute below this is refused.
MIN_EXECUTE_CUTOFF_DAYS = 40


def validate_execute_cutoff(*, execute: bool, cutoff_days: int) -> None:
    """Guard: refuse ``--execute`` at a cutoff below the 40-day buffer. Dry-run is unbounded
    (it deletes nothing), so a reconciliation run may use a smaller cutoff, but real deletion
    must keep the full margin."""
    if execute and cutoff_days < MIN_EXECUTE_CUTOFF_DAYS:
        raise ValueError(
            f"--execute requires --cutoff-days >= {MIN_EXECUTE_CUTOFF_DAYS} "
            f"(got {cutoff_days}); the 40-day buffer must hold for real deletion. Use a dry-run "
            "for reconciliation at a smaller cutoff."
        )


# The drop never waits longer than this for any lock (advisory or chunk).
DROP_LOCK_TIMEOUT = "5s"
_LOCK_TIMEOUT_RE = re.compile(r"[1-9][0-9]{0,4}(ms|s)")


def drop_one_chunk_under_lock(
    pg_conn: Any,
    *,
    hypertable: str,
    range_start: datetime,
    range_end: datetime,
    verify_unchanged: Callable[[], bool],
    lock_key: int = COLD_BAR_MUTATION_LOCK_KEY,
    lock_timeout: str = DROP_LOCK_TIMEOUT,
) -> str:
    """Drop EXACTLY the one chunk ``[range_start, range_end)`` of ``hypertable``, atomically
    and fail-closed. In a single transaction: take ``pg_advisory_xact_lock(lock_key)``
    (serializing against any writer/repair that honours the same key), re-check the source is
    unchanged via ``verify_unchanged`` WHILE HOLDING THE LOCK (closing the TOCTOU between the
    gate's check and the drop), then issue a TARGETED ``drop_chunks`` bounded to this one chunk
    (``older_than => range_end`` and ``newer_than => range_start``) -- never a cutoff-wide call.

    If the source changed, or the targeted call would remove anything other than exactly one
    chunk, the transaction is ROLLED BACK and it raises; nothing is deleted. Returns the
    dropped chunk's name. ``verify_unchanged`` may read the live source over a separate session
    because the lock blocks concurrent lock-takers (writers/repair), not readers."""
    if not _LOCK_TIMEOUT_RE.fullmatch(lock_timeout):
        raise ValueError(f"lock_timeout {lock_timeout!r} must look like '5s' or '750ms'")
    with pg_conn.transaction():
        # Fail fast instead of queueing: a pending AccessExclusive request on the chunk
        # blocks every later reader of the hypertable, so waiting behind a slow or stuck
        # reader would stall production reads (2026-09-26 canary). Timing out rolls the
        # transaction back; nothing is dropped and the next run retries.
        pg_conn.execute(f"SET LOCAL lock_timeout = '{lock_timeout}'")
        pg_conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
        if not verify_unchanged():
            raise ColdBarSourceChangedError(
                f"source for chunk [{range_start.isoformat()}, {range_end.isoformat()}) "
                "changed since export; refusing to drop"
            )
        rows = pg_conn.execute(
            "SELECT drop_chunks(%s, older_than => %s, newer_than => %s)",
            (hypertable, range_end, range_start),
        ).fetchall()
        if len(rows) != 1:
            raise ColdBarDropSetError(
                f"targeted drop_chunks for [{range_start.isoformat()}, "
                f"{range_end.isoformat()}) would remove {len(rows)} chunk(s), expected "
                "exactly 1; rolled back"
            )
        return str(rows[0][0])


# Archive members carry the backed-up directory prefix (ARCHIVE_MEMBER_PREFIX, defined in
# the job module and shared with the receipt writer), NOT the bare filename.
PARQUET_MEMBER = ARCHIVE_MEMBER_PREFIX + "bars-{day}.parquet"
MANIFEST_MEMBER = ARCHIVE_MEMBER_PREFIX + "bars-{day}.manifest.json"
# Local files (in cold_bars_dir) are the bare filename, no prefix.
LOCAL_MANIFEST_NAME = "bars-{day}.manifest.json"


# ---------- pure helpers (unit-tested) ----------


def borg_extract_args(repo: str, archive: str, member: str) -> list[str]:
    """`borg extract --stdout <repo>::<archive> <member>` argv."""
    return ["borg", "extract", "--stdout", f"{repo}::{archive}", member]


def borg_list_archives_args(repo: str) -> list[str]:
    """`borg list --short <repo>` argv (one archive name per line)."""
    return ["borg", "list", "--short", repo]


def borg_list_members_args(repo: str, archive: str) -> list[str]:
    """`borg list --short <repo>::<archive>` argv (one member path per line)."""
    return ["borg", "list", "--short", f"{repo}::{archive}"]


def parse_short_list(output: str) -> frozenset[str]:
    """Parse `borg list --short` output into a set of names (blank lines dropped)."""
    return frozenset(line.strip() for line in output.splitlines() if line.strip())


def newest_bars_archive(names: frozenset[str]) -> str | None:
    """The most recent `bars-*` archive by name (names are `bars-<ISO timestamp>`,
    so lexical max is chronological max), or None if there is none."""
    bars = sorted(n for n in names if n.startswith("bars-"))
    return bars[-1] if bars else None


def parse_env_file(text: str) -> dict[str, str]:
    """Parse a shell-style env file (KEY=value, optional `export`, # comments)."""
    env: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


# ---------- concrete collectors ----------


def fresh_source_fingerprint(dsn: str, day: str) -> str | None:
    """The day's source fingerprint over a DuckDB connection that is opened and closed
    here, so no Postgres transaction (and no chunk lock) outlives the call."""
    start, until = day_bounds(date.fromisoformat(day))
    connection = connect(dsn)
    try:
        return source_fingerprint(connection, start, until)
    except ValueError:
        return None
    finally:
        connection.close()


class BorgDbCollectors:
    """Gathers gated-deletion evidence from Borg + the manifest/receipt dir + the source
    database (read-only), and performs the real targeted ``drop_chunk`` (write) when the run
    is not dry-run."""

    def __init__(
        self,
        *,
        dsn: str,
        cold_bars_dir: Path,
        borg_repo: str,
        borg_env: Mapping[str, str],
        newest_bars_archive: str,
    ) -> None:
        self._dsn = dsn
        self._dir = cold_bars_dir
        self._repo = borg_repo
        self._env = dict(borg_env)
        # The archive whose file list is checked to confirm a receipt is offsite.
        # A receipt written today is archived by the NEXT backup, so this is the
        # most recent bars archive at run time (resolved by the CLI).
        self._newest_bars_archive = newest_bars_archive
        self._connection_handle: Any = None  # DuckDB attached to Postgres READ_ONLY, lazy
        self._archive_names_cache: frozenset[str] | None = None
        self._newest_members_cache: frozenset[str] | None = None

    # --- database ---

    @property
    def _connection(self) -> Any:
        if self._connection_handle is None:
            self._connection_handle = connect(self._dsn)
        return self._connection_handle

    def _close_connection(self) -> None:
        """Closing the DuckDB connection closes its Postgres session, which ends the
        transaction the postgres extension left open after the last read."""
        if self._connection_handle is not None:
            self._connection_handle.close()
            self._connection_handle = None

    def list_chunks(self) -> tuple[ChunkCandidate, ...]:
        """The ACTUAL Timescale chunks of the source hypertable, from
        ``timescaledb_information.chunks`` (not an inferred min..max day range), each
        with its real range_start/range_end. Chunks are 1-day (migration 0024); the
        integration test confirms no drift. The runner filters by eligibility, and a
        chunk whose day cannot be verified is blocked by the gate."""
        rows = self._connection.execute(
            "SELECT range_start, range_end FROM postgres_query('pg', "
            "'SELECT range_start, range_end FROM timescaledb_information.chunks "
            "WHERE hypertable_schema = ''timeseries'' "
            "AND hypertable_name = ''bybit_momentum_bars_1m'' ORDER BY range_start')"
        ).fetchall()
        out: list[ChunkCandidate] = []
        for range_start, range_end in rows:
            start = range_start.astimezone(UTC)
            out.append(
                ChunkCandidate(
                    day=start.date().isoformat(),
                    range_start=start,
                    range_end=range_end.astimezone(UTC),
                )
            )
        return tuple(out)

    def recompute_source_fingerprint(self, day: str) -> str | None:
        start, until = day_bounds(date.fromisoformat(day))
        try:
            return source_fingerprint(self._connection, start, until)
        except ValueError:
            return None  # no rows -> cannot prove; the gate blocks

    def drop_chunk(self, candidate: ChunkCandidate, *, expected_source_fingerprint: str) -> None:
        """Targeted, per-chunk drop under the mutation lock, with a final source re-check.

        Opens a dedicated Postgres WRITE connection (the DuckDB one is READ_ONLY), then defers
        to ``drop_one_chunk_under_lock``: it re-derives the live source fingerprint WHILE
        holding the advisory lock and drops only if it still equals the receipt's, so a late
        repair cannot slip a change in between the gate's check and the drop. Called only for a
        cleared day when the run is not dry-run."""
        import psycopg

        from .cold_bar_export import SOURCE_TABLE

        # The DuckDB postgres attachment keeps its Postgres transaction open after a
        # read, holding AccessShare on the chunk; a drop on another session would then
        # wait on this very process forever (2026-09-26 canary). So the long-lived
        # connection is closed first, and the under-lock re-check uses a fresh one that is
        # closed before drop_chunks runs.
        self._close_connection()
        with psycopg.connect(self._dsn, autocommit=True) as conn:
            drop_one_chunk_under_lock(
                conn,
                hypertable=SOURCE_TABLE,
                range_start=candidate.range_start,
                range_end=candidate.range_end,
                verify_unchanged=lambda: (
                    fresh_source_fingerprint(self._dsn, candidate.day)
                    == expected_source_fingerprint
                ),
            )

    # --- files ---

    def manifest_present(self, day: str) -> bool:
        return (self._dir / LOCAL_MANIFEST_NAME.format(day=day)).exists()

    # --- borg ---

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(args, env=self._env, capture_output=True, check=True)  # noqa: S603

    def _archive_names(self) -> frozenset[str]:
        # Cached: the archive list does not change during one run, and every day
        # would otherwise re-list the whole repo.
        if self._archive_names_cache is None:
            result = self._run(borg_list_archives_args(self._repo))
            self._archive_names_cache = parse_short_list(result.stdout.decode())
        return self._archive_names_cache

    def _newest_bars_members(self) -> frozenset[str]:
        if self._newest_members_cache is None:
            result = self._run(borg_list_members_args(self._repo, self._newest_bars_archive))
            self._newest_members_cache = parse_short_list(result.stdout.decode())
        return self._newest_members_cache

    def archive_present(self, archive_name: str) -> bool:
        return archive_name in self._archive_names()

    def receipt_offsite_confirmed(self, day: str) -> bool:
        receipt_name = f"bars-{day}{RECEIPT_SUFFIX}"
        return any(m.endswith(receipt_name) for m in self._newest_bars_members())

    def _extract_to(self, archive: str, member: str, dest: Path) -> str:
        """Stream one archive member to a file (no full-file buffering in memory) and
        return its SHA-256, computed incrementally as it is written."""
        digest = hashlib.sha256()
        with dest.open("wb") as out:
            proc = subprocess.Popen(  # noqa: S603
                borg_extract_args(self._repo, archive, member),
                env=self._env,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            stdout = proc.stdout
            assert stdout is not None
            for block in iter(lambda: stdout.read(1024 * 1024), b""):
                out.write(block)
                digest.update(block)
            if proc.wait() != 0:
                raise subprocess.CalledProcessError(proc.returncode, "borg extract")
        return digest.hexdigest()

    def extract_offsite(self, receipt: DropReceipt) -> ExtractedOffsite | None:
        # A 350MB parquet is streamed to a temp file, never buffered whole in memory.
        # Member paths carry the archived directory prefix (runtime/cold-bars/...).
        with tempfile.TemporaryDirectory() as tmp:
            parquet_dest = Path(tmp) / "extracted.parquet"
            manifest_dest = Path(tmp) / "extracted.manifest.json"
            try:
                parquet_sha = self._extract_to(
                    receipt.archive_name, PARQUET_MEMBER.format(day=receipt.day), parquet_dest
                )
                manifest_sha = self._extract_to(
                    receipt.archive_name, MANIFEST_MEMBER.format(day=receipt.day), manifest_dest
                )
            except subprocess.CalledProcessError:
                return None
            file_fp = file_fingerprint(self._connection, parquet_dest)
        return ExtractedOffsite(
            parquet_sha256=parquet_sha,
            manifest_sha256=manifest_sha,
            file_fingerprint=file_fp,
        )


def main() -> None:
    """CLI entrypoint. Dry-run by default (computes and prints the drop plan, deletes
    nothing); ``--execute`` performs the real targeted drops and is refused below the 40-day
    cutoff. A file-lock keeps a single run at a time; each real drop additionally takes the
    Postgres mutation advisory lock."""
    import argparse
    import fcntl
    import os
    import sys

    from .cold_bar_gated_deletion_job import render_plan, run_gated_deletion

    parser = argparse.ArgumentParser(description="Cold-bar gated deletion (dry-run)")
    parser.add_argument("--cold-bars-dir", type=Path, required=True)
    parser.add_argument(
        "--backup-env", type=Path, required=True, help="path to backup.env (BORG_*)"
    )
    # PR 1 dry-run uses a RECONCILIATION cutoff below the 35-day Timescale retention
    # so there are real eligible chunks to validate against; PR 2 raises it to 40 once
    # the automatic retention is removed. The buffer must stay a positive integer.
    parser.add_argument("--cutoff-days", type=int, default=25)
    parser.add_argument(
        "--max-eval-days",
        type=int,
        default=None,
        help="bound the reconciliation work to this many oldest days (borg extracts)",
    )
    parser.add_argument(
        "--fail-if-empty",
        action="store_true",
        help="exit non-zero if there are no eligible candidates (commissioning check)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="ACTUALLY drop cleared chunks (default: dry-run, deletes nothing). Each drop "
        "re-verifies the source fingerprint under the mutation lock and is targeted to one "
        "chunk. Enable only after a dry-run has been reconciled on prod.",
    )
    args = parser.parse_args()
    validate_execute_cutoff(execute=args.execute, cutoff_days=args.cutoff_days)

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise ValueError("DATABASE_URL is required")

    borg_env = parse_env_file(args.backup_env.read_text())
    repo = borg_env.get("BORG_REPO")
    if not repo:
        raise ValueError(f"BORG_REPO not found in {args.backup_env}")

    lock_path = args.cold_bars_dir / ".gated-deletion.lock"
    args.cold_bars_dir.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.stdout.write("another gated-deletion run holds the lock; exiting\n")
            return

        archives = parse_short_list(
            subprocess.run(  # noqa: S603
                borg_list_archives_args(repo), env=borg_env, capture_output=True, check=True
            ).stdout.decode()
        )
        newest = newest_bars_archive(archives)
        if newest is None:
            sys.stdout.write(
                "no bars-* archive present; cannot confirm receipts offsite; exiting\n"
            )
            return

        from datetime import UTC, datetime

        collectors = BorgDbCollectors(
            dsn=dsn,
            cold_bars_dir=args.cold_bars_dir,
            borg_repo=repo,
            borg_env=borg_env,
            newest_bars_archive=newest,
        )
        if args.execute:
            sys.stdout.write("EXECUTE mode: cleared chunks WILL be dropped (not a dry-run)\n")
        result = run_gated_deletion(
            now=datetime.now(UTC),
            cutoff_days=args.cutoff_days,
            receipts_dir=args.cold_bars_dir,
            collectors=collectors,
            dry_run=not args.execute,  # default dry-run; --execute deletes
            max_eval_days=args.max_eval_days,
        )
        sys.stdout.write(render_plan(result) + "\n")
        if args.fail_if_empty and result.n_eligible == 0:
            sys.stdout.write("FAIL: no eligible candidates (commissioning check)\n")
            sys.exit(1)


__all__ = [
    "COLD_BAR_MUTATION_LOCK_KEY",
    "MIN_EXECUTE_CUTOFF_DAYS",
    "BorgDbCollectors",
    "ColdBarDropSetError",
    "ColdBarSourceChangedError",
    "borg_extract_args",
    "borg_list_archives_args",
    "borg_list_members_args",
    "drop_one_chunk_under_lock",
    "main",
    "newest_bars_archive",
    "parse_env_file",
    "parse_short_list",
    "validate_execute_cutoff",
]
