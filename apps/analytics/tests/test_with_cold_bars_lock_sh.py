"""Regression coverage for infra/scripts/with-cold-bars-lock.sh.

The wrapper serializes cold-bar exports and the fingerprint backfill against the
offsite backup, which archives and reclaims the same Parquet files. Run the REAL
script (as with test_offsite_backup_sh.py) rather than reimplementing its logic.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "infra" / "scripts" / "with-cold-bars-lock.sh"
_BASH = shutil.which("bash") or "/bin/bash"

pytestmark = pytest.mark.skipif(
    shutil.which("flock") is None,
    reason="flock (util-linux) is required by the script; brew install flock on macOS",
)


def _run(lock_file: Path, *command: str, wait_seconds: int = 1) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "COLD_BARS_LOCK_FILE": str(lock_file),
        "COLD_BARS_LOCK_WAIT_SECONDS": str(wait_seconds),
    }
    return subprocess.run(  # noqa: S603 -- running the shipped script is the test
        [_BASH, str(_SCRIPT), *command],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_runs_the_command_when_the_lock_is_free(tmp_path: Path) -> None:
    lock = tmp_path / ".cold-bars.lock"
    marker = tmp_path / "ran"
    result = _run(lock, "touch", str(marker))
    assert result.returncode == 0, result.stderr
    assert marker.exists()
    # The lock file is created 0666 so root and deploy can both take it.
    assert (lock.stat().st_mode & 0o666) == 0o666


def test_does_not_run_the_command_while_the_lock_is_held(tmp_path: Path) -> None:
    lock = tmp_path / ".cold-bars.lock"
    lock.touch()
    marker = tmp_path / "ran"
    holder = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX)
        result = _run(lock, "touch", str(marker), wait_seconds=1)
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)
    assert result.returncode == 75, result.stderr
    assert not marker.exists(), "the command must not run while the backup holds the lock"


def test_passes_through_the_command_exit_status(tmp_path: Path) -> None:
    lock = tmp_path / ".cold-bars.lock"
    result = _run(lock, "sh", "-c", "exit 7")
    assert result.returncode == 7


def test_refuses_when_no_command_is_given(tmp_path: Path) -> None:
    lock = tmp_path / ".cold-bars.lock"
    result = _run(lock)
    assert result.returncode == 64
