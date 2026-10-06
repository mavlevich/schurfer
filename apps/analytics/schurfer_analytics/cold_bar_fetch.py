"""Fetch archived cold minute bars back from Borg for a range of days, verified.

Once a day's bars are dropped from PostgreSQL (cold_bar_gated_deletion), the only copy is
the Parquet file inside a `bars-*` Borg archive. Research that needs older bars reads that
Parquet (for example with DuckDB `read_parquet`). This tool is the verified path back:
for each requested day it reads the day's offsite receipt beside its manifest (which names
the archive), streams the Parquet member out of that archive, and accepts it only if its
sha256 and its row count equal the receipt's. A file already present is kept only if it
passes the same two checks; anything else is refused, never overwritten. Publication is a
hard link of the verified temporary file, so two concurrent runs cannot replace each
other's file: the loser verifies the winner's instead.

It runs on the production host, whose disk holds the live database, so a run is bounded:
at most `--max-days` days, and each day is fetched only if the free space left after it
(sized from the day's manifest, checked against the receipt) stays at or above
`--reserve-bytes`; the stream is cut off if it grows past that size. Large extractions
belong on a separate machine with its own Borg access.

Read-only towards the database and the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .cold_bar_gated_deletion_collectors import (
    PARQUET_MEMBER,
    borg_extract_args,
    parse_env_file,
)
from .cold_bar_gated_deletion_job import RECEIPT_SUFFIX

DEFAULT_MAX_DAYS = 3
DEFAULT_RESERVE_BYTES = 10 * 1024**3  # the same floor the restore drill keeps
READABLE_MODE = 0o644  # the fetch runs as root; the deploy user reads the bars


class FetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class Fetched:
    day: str
    path: Path
    rows: int
    sha256: str
    already_present: bool


def days_between(first: date, last: date) -> list[str]:
    if last < first:
        raise ValueError("--to is before --from")
    return [(first + timedelta(days=k)).isoformat() for k in range((last - first).days + 1)]


def read_receipt(cold_bars_dir: Path, day: str) -> dict[str, Any]:
    path = cold_bars_dir / f"bars-{day}{RECEIPT_SUFFIX}"
    if not path.exists():
        raise FetchError(f"{day}: no offsite receipt ({path.name}); the day is not archived")
    receipt: dict[str, Any] = json.loads(path.read_text())
    if receipt.get("day") != day:
        raise FetchError(f"{day}: the receipt names another day")
    return receipt


def expected_bytes(cold_bars_dir: Path, day: str, receipt: dict[str, Any]) -> int:
    """The day's Parquet size from its manifest, which is kept after the Parquet is
    reclaimed. The manifest is trusted only if its sha256 is the one the receipt pinned;
    without a trusted size the disk cost is unknown, so the day is refused."""
    path = cold_bars_dir / f"bars-{day}.manifest.json"
    if not path.exists():
        raise FetchError(f"{day}: no manifest ({path.name}); cannot size the fetch")
    if sha256_of(path) != receipt.get("manifest_sha256"):
        raise FetchError(f"{day}: {path.name} does not match the receipt's manifest_sha256")
    return int(json.loads(path.read_text())["file_bytes"])


def ensure_reserve(out_dir: Path, needed: int, reserve: int) -> None:
    free = shutil.disk_usage(out_dir).free
    if free - needed < reserve:
        raise FetchError(
            f"fetching {needed} bytes would leave {free - needed} free, below the"
            f" {reserve}-byte reserve; extract on a separate machine"
        )


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parquet_rows(path: Path) -> int:
    import duckdb

    row = duckdb.connect().execute("SELECT count(*) FROM read_parquet(?)", [str(path)]).fetchone()
    return int(row[0]) if row else 0


def stream_member(
    repo: str, archive: str, member: str, dest: Path, env: dict[str, str], max_bytes: int
) -> str:
    """Stream one archive member into `dest` and return its sha256.

    The stream is cut off, and Borg killed, as soon as it exceeds `max_bytes` (the size the
    reserve check allowed for), so a wrong member cannot fill the disk before the final
    sha256 check. Borg's stderr goes to a file, not a pipe: a pipe nobody reads until the
    end could fill and stall Borg, and with it this read, forever.
    """
    digest = hashlib.sha256()
    written = 0
    with dest.open("wb") as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(  # noqa: S603 -- fixed argv
            borg_extract_args(repo, archive, member),
            env=env,
            stdout=subprocess.PIPE,
            stderr=err,
        )
        assert proc.stdout is not None
        try:
            for block in iter(lambda: proc.stdout.read(1024 * 1024), b""):  # type: ignore[union-attr]
                written += len(block)
                if written > max_bytes:
                    raise FetchError(
                        f"{archive}:{member} is larger than the expected {max_bytes} bytes"
                    )
                out.write(block)
                digest.update(block)
        finally:
            if proc.poll() is None and written > max_bytes:
                proc.kill()
            proc.stdout.close()
            code = proc.wait()
        if code != 0:
            err.seek(0)
            detail = err.read().decode(errors="replace")[-300:]
            raise FetchError(f"borg extract failed for {archive}:{member}: {detail}")
    return digest.hexdigest()


def verify(path: Path, day: str, sha: str, rows: int) -> None:
    got_sha = sha256_of(path)
    if got_sha != sha:
        raise FetchError(f"{day}: {path.name} sha256 {got_sha} != receipt {sha}")
    got_rows = parquet_rows(path)
    if got_rows != rows:
        raise FetchError(f"{day}: {path.name} has {got_rows} rows != receipt {rows}")


def fetch_day(
    day: str,
    *,
    cold_bars_dir: Path,
    out_dir: Path,
    repo: str,
    env: dict[str, str],
    reserve_bytes: int = DEFAULT_RESERVE_BYTES,
) -> Fetched:
    receipt = read_receipt(cold_bars_dir, day)
    expected_sha = str(receipt["parquet_sha256"])
    expected_rows = int(receipt["row_count"])
    dest = out_dir / f"bars-{day}.parquet"
    if dest.exists():
        verify(dest, day, expected_sha, expected_rows)
        dest.chmod(READABLE_MODE)  # an earlier run may have left it 0600
        return Fetched(day, dest, expected_rows, expected_sha, already_present=True)
    size = expected_bytes(cold_bars_dir, day, receipt)
    ensure_reserve(out_dir, size, reserve_bytes)
    with tempfile.NamedTemporaryFile(dir=out_dir, suffix=".partial", delete=False) as handle:
        partial = Path(handle.name)
    try:
        got_sha = stream_member(
            repo, str(receipt["archive_name"]), PARQUET_MEMBER.format(day=day), partial, env, size
        )
        if got_sha != expected_sha:
            raise FetchError(f"{day}: extracted sha256 {got_sha} != receipt {expected_sha}")
        rows = parquet_rows(partial)
        if rows != expected_rows:
            raise FetchError(f"{day}: {rows} rows != receipt {expected_rows}")
        partial.chmod(READABLE_MODE)  # the temp file is 0600
        try:
            os.link(partial, dest)  # never replaces: a concurrent winner keeps its file
        except FileExistsError:
            verify(dest, day, expected_sha, expected_rows)
            dest.chmod(READABLE_MODE)  # the winner may be an older 0600 run
            return Fetched(day, dest, expected_rows, expected_sha, already_present=True)
    finally:
        partial.unlink(missing_ok=True)
    return Fetched(day, dest, rows, got_sha, already_present=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cold-bars-dir", type=Path, required=True)
    parser.add_argument("--backup-env", type=Path, required=True, help="backup.env (BORG_*)")
    parser.add_argument("--from", dest="first", type=date.fromisoformat, required=True)
    parser.add_argument("--to", dest="last", type=date.fromisoformat, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--max-days", type=int, default=DEFAULT_MAX_DAYS)
    parser.add_argument("--reserve-bytes", type=int, default=DEFAULT_RESERVE_BYTES)
    args = parser.parse_args(argv)
    days = days_between(args.first, args.last)
    if len(days) > args.max_days:
        raise SystemExit(
            f"{len(days)} days requested, more than --max-days {args.max_days}; fetch in"
            " smaller ranges, or extract on a separate machine for large research"
        )
    env = parse_env_file(args.backup_env.read_text())
    repo = env.get("BORG_REPO")
    if not repo:
        raise SystemExit(f"BORG_REPO not found in {args.backup_env}")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    fetched, failed = [], []
    for day in days:
        try:
            got = fetch_day(
                day,
                cold_bars_dir=args.cold_bars_dir,
                out_dir=args.out_dir,
                repo=repo,
                env=env,
                reserve_bytes=args.reserve_bytes,
            )
            fetched.append(
                {"day": got.day, "rows": got.rows, "already_present": got.already_present}
            )
        except FetchError as exc:
            failed.append({"day": day, "error": str(exc)})
    sys.stdout.write(json.dumps({"fetched": fetched, "failed": failed}, indent=1) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
