"""Concrete Collectors for cold-bar gated deletion: the impure wrappers around
Borg, the manifest/receipt files, and the source database that the dry-run runner
(``cold_bar_gated_deletion_job``) drives. The safety decisions live in the pure
gate; this file only gathers evidence.

The subprocess/DB calls here cannot be unit-tested without a real Borg repo and
Postgres, so the parts that CAN be tested -- command construction and Borg-output
parsing -- are pulled out as pure helpers with their own tests, and the thin
wrappers are covered by the integration test named in the runbook. ``drop_chunk``
deliberately raises: PR 1 is dry-run only, and actually removing a chunk is wired
in PR 2 together with removing the automatic Timescale retention.
"""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
from datetime import UTC, date
from pathlib import Path
from typing import TYPE_CHECKING

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
    from collections.abc import Mapping

    from .cold_bar_gated_deletion import DropReceipt

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


class BorgDbCollectors:
    """Gathers gated-deletion evidence from Borg + the manifest/receipt dir + the
    source database. Read-only everywhere; ``drop_chunk`` is not implemented in PR 1."""

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
        self._connection = connect(dsn)  # DuckDB attached to Postgres READ_ONLY
        self._archive_names_cache: frozenset[str] | None = None
        self._newest_members_cache: frozenset[str] | None = None

    # --- database ---

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

    def drop_chunk(self, candidate: ChunkCandidate) -> None:
        raise NotImplementedError(
            "drop_chunk is not enabled in PR 1 (dry-run only); the write path and the "
            "removal of the automatic Timescale retention land together in PR 2"
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
    """CLI entrypoint. PR 1: DRY-RUN ONLY -- it computes and prints the drop plan and
    deletes nothing (the concrete drop path is added in PR 2). A file-lock keeps a
    single run at a time; the Postgres advisory lock lands with the write path."""
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
    args = parser.parse_args()

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
        result = run_gated_deletion(
            now=datetime.now(UTC),
            cutoff_days=args.cutoff_days,
            receipts_dir=args.cold_bars_dir,
            collectors=collectors,
            dry_run=True,  # PR 1: never deletes
            max_eval_days=args.max_eval_days,
        )
        sys.stdout.write(render_plan(result) + "\n")
        if args.fail_if_empty and result.n_eligible == 0:
            sys.stdout.write("FAIL: no eligible candidates (commissioning check)\n")
            sys.exit(1)


__all__ = [
    "BorgDbCollectors",
    "borg_extract_args",
    "borg_list_archives_args",
    "borg_list_members_args",
    "main",
    "newest_bars_archive",
    "parse_env_file",
    "parse_short_list",
]
