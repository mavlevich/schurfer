"""Regression coverage for the bar-coverage part of offsite-backup-health.sh.

Same approach as test_offsite_backup_sh.py: run the REAL script against a fake
state directory rather than reimplementing its logic in Python, where the two
could drift.

The check exists because minute bars are the one dataset here that cannot be
regenerated. Timescale drops them after 35 days, no exchange sells them back,
and until now nothing watched whether the nightly export still ran. A broken
exporter starts a silent countdown: the days it skips stay in the database and
stay recoverable right up to the moment retention deletes them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "infra" / "scripts" / "offsite-backup-health.sh"
_BASH = shutil.which("bash") or "/bin/bash"


def _has_gnu_date() -> bool:
    """The script does date arithmetic with `date -d`, which BSD date lacks."""
    date_binary = shutil.which("date")
    if date_binary is None:
        return False
    probe = subprocess.run(  # noqa: S603
        [date_binary, "-u", "-d", "1 day ago", "+%Y-%m-%d"],
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(
    not _has_gnu_date(),
    reason="offsite-backup-health.sh needs GNU date; on macOS: brew install coreutils",
)


def _day(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).date().isoformat()


def _state(tmp_path: Path, *, exported: list[str], bars_stamp_age_days: int = 0) -> Path:
    state = tmp_path / "runtime"
    cold = state / "cold-bars"
    cold.mkdir(parents=True)
    for name in ("db", "research", "bars"):
        (state / f"offsite-backup-{name}.stamp").write_text("stamped\n")
    if bars_stamp_age_days:
        stamp = state / "offsite-backup-bars.stamp"
        old = (datetime.now(UTC) - timedelta(days=bars_stamp_age_days)).timestamp()
        os.utime(stamp, (old, old))
    for day in exported:
        (cold / f"bars-{day}.manifest.json").write_text("{}")
    return state


def _run(state: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [_BASH, str(_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "STATE_DIR": str(state),
            "COLD_BARS_DIR": str(state / "cold-bars"),
            # Disk runway is a separate signal with its own test surface; take it
            # out of the way so these assertions are about bar coverage.
            "DISK_PATH": "/",
            "MIN_FREE_GB": "0",
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_CHAT_ID": "",
        },
    )


def test_contiguous_coverage_is_healthy(tmp_path: Path) -> None:
    state = _state(tmp_path, exported=[_day(index) for index in range(2, 20)])
    result = _run(state)
    assert result.returncode == 0, result.stderr
    assert "18 bar days exported" in result.stdout


def test_a_gap_in_the_middle_is_reported(tmp_path: Path) -> None:
    """The shape a partial failure leaves: one night's run died, every night
    since worked, and the hole is invisible in any 'last run succeeded' check."""
    days = [_day(index) for index in range(2, 20) if index != 8]
    result = _run(_state(tmp_path, exported=days))
    assert result.returncode == 1
    assert "1 cold bar day" in result.stderr
    assert _day(8) in result.stderr


def test_a_stalled_exporter_names_every_missing_day(tmp_path: Path) -> None:
    result = _run(_state(tmp_path, exported=[_day(index) for index in range(5, 20)]))
    assert result.returncode == 1
    assert "3 cold bar day" in result.stderr
    for days_ago in (2, 3, 4):
        assert _day(days_ago) in result.stderr


def test_nothing_ever_exported_is_reported(tmp_path: Path) -> None:
    result = _run(_state(tmp_path, exported=[]))
    assert result.returncode == 1
    assert "no cold bar day has ever been exported" in result.stderr


def test_a_stale_bars_stamp_is_reported_even_when_coverage_looks_complete(
    tmp_path: Path,
) -> None:
    """Coverage and archival are different failures. The manifests can be
    complete while the Storage Box has been unreachable for days, and that is the
    case where the only copy of a reclaimed day is neither local nor offsite."""
    state = _state(
        tmp_path, exported=[_day(index) for index in range(2, 20)], bars_stamp_age_days=3
    )
    result = _run(state)
    assert result.returncode == 1
    assert "cold bars archive is 72h old" in result.stderr


def test_history_before_the_first_export_is_not_a_gap(tmp_path: Path) -> None:
    """Capture began at some point. Days before the oldest manifest are absent by
    history rather than by failure, and alerting on them hourly forever is how
    an alert becomes something everyone filters out."""
    result = _run(_state(tmp_path, exported=[_day(index) for index in range(2, 6)]))
    assert result.returncode == 0, result.stderr


def test_a_day_that_is_merely_not_exported_yet_is_tolerated(tmp_path: Path) -> None:
    """The exporter runs at 03:30 UTC for the previous day. Yesterday having no
    file at 03:00 is the schedule working, not a fault."""
    result = _run(_state(tmp_path, exported=[_day(index) for index in range(2, 20)]))
    assert result.returncode == 0, result.stderr


def test_a_manifest_alone_counts_as_covered(tmp_path: Path) -> None:
    """The design point this check depends on: the backup reclaims each .parquet
    once it is confirmed inside a bars-* archive and leaves the manifest behind.
    A check that looked for Parquet files would report every archived day as
    missing, which is the opposite of the truth."""
    state = _state(tmp_path, exported=[_day(index) for index in range(2, 20)])
    assert not list((state / "cold-bars").glob("*.parquet"))
    assert _run(state).returncode == 0
