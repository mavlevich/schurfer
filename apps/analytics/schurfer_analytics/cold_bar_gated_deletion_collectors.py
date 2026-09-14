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
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from .cold_bar_export import (
    connect,
    day_bounds,
    file_fingerprint,
    source_day_range,
    source_fingerprint,
)
from .cold_bar_gated_deletion_job import RECEIPT_SUFFIX, ChunkCandidate, ExtractedOffsite

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .cold_bar_gated_deletion import DropReceipt

# The exporter names each day's files by this convention (see cold_bar_export).
PARQUET_NAME = "bars-{day}.parquet"
MANIFEST_NAME = "bars-{day}.manifest.json"


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

    # --- database ---

    def list_chunks(self) -> tuple[ChunkCandidate, ...]:
        """One 1-day ChunkCandidate per complete UTC day present in the source.

        Chunks are 1-day (migration 0024), so each present day maps to exactly one
        chunk spanning [day 00:00, next day 00:00). Today is excluded (still being
        written). Eligibility filtering happens in the runner."""
        span = source_day_range(self._connection)
        if span is None:
            return ()
        oldest, newest_complete = span
        out: list[ChunkCandidate] = []
        current = oldest
        while current <= newest_complete:
            start, until = day_bounds(current)
            out.append(ChunkCandidate(day=current.isoformat(), range_start=start, range_end=until))
            current += timedelta(days=1)
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
        return (self._dir / MANIFEST_NAME.format(day=day)).exists()

    # --- borg ---

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(args, env=self._env, capture_output=True, check=True)  # noqa: S603

    def archive_present(self, archive_name: str) -> bool:
        result = self._run(borg_list_archives_args(self._repo))
        return archive_name in parse_short_list(result.stdout.decode())

    def receipt_offsite_confirmed(self, day: str) -> bool:
        result = self._run(borg_list_members_args(self._repo, self._newest_bars_archive))
        members = parse_short_list(result.stdout.decode())
        receipt_name = f"bars-{day}{RECEIPT_SUFFIX}"
        return any(m.endswith(receipt_name) for m in members)

    def extract_offsite(self, receipt: DropReceipt) -> ExtractedOffsite | None:
        try:
            parquet_bytes = self._run(
                borg_extract_args(self._repo, receipt.archive_name, receipt.parquet_path)
            ).stdout
            manifest_member = MANIFEST_NAME.format(day=receipt.day)
            manifest_bytes = self._run(
                borg_extract_args(self._repo, receipt.archive_name, manifest_member)
            ).stdout
        except subprocess.CalledProcessError:
            return None
        with tempfile.TemporaryDirectory() as tmp:
            parquet_path = Path(tmp) / "extracted.parquet"
            parquet_path.write_bytes(parquet_bytes)
            file_fp = file_fingerprint(self._connection, parquet_path)
        return ExtractedOffsite(
            parquet_sha256=hashlib.sha256(parquet_bytes).hexdigest(),
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
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
    parser.add_argument("--cutoff-days", type=int, default=40)
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
        )
        sys.stdout.write(render_plan(result) + "\n")


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
