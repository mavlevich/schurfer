"""Per-day cold-bar staging from prod Borg to the local machine, over SSH.

The Mac has no Borg repo access and we do NOT open a PostgreSQL tunnel. Instead, for
each UTC day, Borg runs ON PROD (secrets stay there) to stream that day's Parquet member
to the local machine over SSH; the day's manifest and provenance are read the same way.
The SHA-256 is then recomputed LOCALLY and required to equal the manifest sha, so a
corrupt or wrong transfer fails here rather than silently entering the scan. Each day's
archive + member + verified SHA are recorded in a staging artifact so the scan can
verify provenance without a local Borg (see ``make_staging_verifier``). The frozen files
are the only source; the live database is never used in their place.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from .cold_bar_export import sha256_file

if TYPE_CHECKING:
    from collections.abc import Callable

STAGING_VERSION = "abnormal_flow_staging_v1"


class RemoteTransport(Protocol):
    """How the staging step reaches prod. Injectable so the fetch logic is testable
    without SSH or Borg."""

    def read_text(self, remote_path: str) -> str: ...

    def extract_member(self, borg_repo: str, archive: str, member: str, dest: Path) -> None: ...


@dataclass(frozen=True)
class SshBorgTransport:
    """Runs `cat` and `borg extract --stdout` on the prod host over SSH. Borg secrets
    never leave prod: when ``env_file`` is set it is sourced ON PROD (repo, passphrase,
    RSH), so only bytes and small JSON come back. ``sudo`` wraps commands for root-owned
    files, and an empty ``borg_repo`` uses the repo from the sourced env (``::archive``)."""

    host: str
    sudo: bool = False
    env_file: str | None = None

    def _ssh(self) -> str:
        ssh = shutil.which("ssh")
        if ssh is None:  # pragma: no cover - ssh absence is environment-specific
            raise RuntimeError("ssh executable not found on PATH")
        return ssh

    def _remote(self, inner: str) -> str:
        # Wrap the remote command so it runs under one login shell with prod's env; the
        # inner command is a single shell-quoted token, avoiding SSH re-splitting.
        return (
            f"sudo bash -c {shlex.quote(inner)}" if self.sudo else f"bash -c {shlex.quote(inner)}"
        )

    def read_text(self, remote_path: str) -> str:
        proc = subprocess.run(  # noqa: S603 -- resolved executable, single remote token
            [self._ssh(), self.host, self._remote(f"cat -- {shlex.quote(remote_path)}")],
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout

    def extract_member(self, borg_repo: str, archive: str, member: str, dest: Path) -> None:
        repo_spec = f"{borg_repo}::{archive}" if borg_repo else f"::{archive}"
        cmd = f"borg extract --stdout {shlex.quote(repo_spec)} {shlex.quote(member)}"
        if self.env_file:
            cmd = f"set -a; . {shlex.quote(self.env_file)}; set +a; {cmd}"
        with dest.open("wb") as handle:
            subprocess.run(  # noqa: S603 -- resolved executable, single remote token
                [self._ssh(), self.host, self._remote(cmd)],
                stdout=handle,
                check=True,
            )


@dataclass(frozen=True)
class StagedDay:
    day: str
    archive: str
    member: str
    sha256: str
    file_bytes: int
    verified: bool


def _read_receipt(
    transport: RemoteTransport, remote_manifest_dir: str, base: str
) -> dict[str, Any]:
    """Per-day archive proof: prod's ``offsite-receipt.json`` (archive_name +
    parquet_path), falling back to an audit-style ``provenance.json``."""
    for suffix in ("offsite-receipt", "provenance"):
        try:
            return json.loads(transport.read_text(f"{remote_manifest_dir}/{base}.{suffix}.json"))  # type: ignore[no-any-return]
        except subprocess.CalledProcessError:
            continue
    raise ValueError(f"{base}: no offsite-receipt.json or provenance.json found")


def stage_days(
    transport: RemoteTransport,
    *,
    remote_manifest_dir: str,
    borg_repo: str,
    local_dir: Path,
    start: date,
    end: date,
) -> dict[str, Any]:
    """Fetch and locally SHA-verify every day in ``[start, end]``. Writes each day's
    Parquet + manifest into ``local_dir`` and returns (and writes) a staging artifact.
    A day whose recomputed SHA does not match its manifest raises -- the file is never
    left as a usable input."""
    local_dir.mkdir(parents=True, exist_ok=True)
    staged: list[StagedDay] = []
    day = start
    while day <= end:
        base = f"bars-{day.isoformat()}"
        manifest = json.loads(transport.read_text(f"{remote_manifest_dir}/{base}.manifest.json"))
        receipt = _read_receipt(transport, remote_manifest_dir, base)
        archive = receipt.get("archive_name") or receipt.get("borg_archive")
        member = (
            receipt.get("parquet_path")
            or receipt.get("archive_member_path")
            or f"runtime/cold-bars/{base}.parquet"
        )
        if not archive:
            raise ValueError(f"{day.isoformat()}: receipt has no archive name")
        dest = local_dir / f"{base}.parquet"
        transport.extract_member(borg_repo, str(archive), str(member), dest)
        actual_sha = sha256_file(dest)
        if actual_sha != manifest["sha256"]:
            raise ValueError(
                f"{day.isoformat()}: local SHA {actual_sha} != manifest {manifest['sha256']}"
            )
        (local_dir / f"{base}.manifest.json").write_text(json.dumps(manifest))
        (local_dir / f"{base}.offsite-receipt.json").write_text(json.dumps(receipt))
        staged.append(
            StagedDay(
                day=day.isoformat(),
                archive=str(archive),
                member=str(member),
                sha256=actual_sha,
                file_bytes=dest.stat().st_size,
                verified=True,
            )
        )
        day += timedelta(days=1)
    artifact = {
        "staging_version": STAGING_VERSION,
        "borg_repo": borg_repo,
        "remote_manifest_dir": remote_manifest_dir,
        "days": [vars(s) for s in staged],
    }
    (local_dir / "staging.json").write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    return artifact


def make_staging_verifier(staging: dict[str, Any]) -> Callable[..., Any]:
    """Archive verifier for the scan, backed by a staging artifact instead of a local
    Borg. A day is verified when it was fetched and SHA-verified at staging AND its
    recorded SHA still equals the manifest the scan reads locally."""
    by_day = {d["day"]: d for d in staging.get("days", [])}

    def verify(day: date, manifest: Any) -> tuple[bool, str | None]:
        record = by_day.get(day.isoformat())
        if record is None:
            return False, None
        ok = bool(record.get("verified")) and record.get("sha256") == manifest.sha256
        return ok, record.get("archive")

    return verify


def build_parser() -> Any:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="SSH host alias for prod (e.g. schurfer)")
    parser.add_argument(
        "--borg-repo", default="", help="Borg repo; empty = use env file's BORG_REPO"
    )
    parser.add_argument(
        "--env-file", default=None, help="Env file sourced ON PROD before borg (secrets stay there)"
    )
    parser.add_argument("--sudo", action="store_true", help="Run remote cat/borg under sudo")
    parser.add_argument(
        "--remote-manifest-dir",
        required=True,
        help="Prod dir with per-day manifest + offsite-receipt JSON",
    )
    parser.add_argument(
        "--local-dir", type=Path, required=True, help="Local staging dir on the Mac"
    )
    parser.add_argument("--start-day", type=date.fromisoformat, required=True)
    parser.add_argument("--end-day", type=date.fromisoformat, required=True)
    return parser


def main() -> None:
    import sys

    args: Any = build_parser().parse_args()
    transport = SshBorgTransport(host=args.host, sudo=args.sudo, env_file=args.env_file)
    artifact = stage_days(
        transport,
        remote_manifest_dir=args.remote_manifest_dir,
        borg_repo=args.borg_repo,
        local_dir=args.local_dir,
        start=args.start_day,
        end=args.end_day,
    )
    sys.stdout.write(f"staged {len(artifact['days'])} day(s) to {args.local_dir}\n")


if __name__ == "__main__":
    main()
