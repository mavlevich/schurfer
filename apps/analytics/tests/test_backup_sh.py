"""Regression coverage for infra/scripts/backup.sh
(chore/disk-safety-backup-and-prune-hardening-v1).

Colleague review, 2026-09-03: the success/low-space/gzip-failure behavior
this branch's own commit message claimed was never actually exercised by
an automated test -- only manually, once, in the session that wrote it.
These tests run the REAL script (`bash infra/scripts/backup.sh`) against a
fake `docker`/`gzip`/`psql` on PATH and a real, isolated tmp directory for
BACKUP_DIR (so `df`/`du` see real, if tiny, filesystem state rather than
also needing to be faked) -- black-box, the same way a human running this
in a terminal would exercise it, not a reimplementation of its logic in
Python that could drift from what the shipped script actually does.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BACKUP_SH = _REPO_ROOT / "infra" / "scripts" / "backup.sh"
_BASH = shutil.which("bash") or "/bin/bash"


def _fake_bin(tmp_path: Path, *, pg_dump_sleep_seconds: float = 0.0) -> Path:
    """A fake `docker` (handles `exec <container> pg_dump ...` and
    `exec <container> psql ... pg_database_size ...`) and a real-behaving
    `gzip` passthrough, on their own PATH-prepended directory. Controlled
    entirely via env vars the test sets, not arguments, since backup.sh
    itself decides `docker`'s argv."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    docker_script = bin_dir / "docker"
    docker_script.write_text(
        textwrap.dedent(f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            # docker exec <container> pg_dump -U ... -d ... -Fc
            # docker exec <container> psql -U ... -d ... -tAc "SELECT pg_database_size(...)"
            if [[ "$*" == *pg_dump* ]]; then
                sleep {pg_dump_sleep_seconds}
                head -c "${{FAKE_PG_DUMP_BYTES:-4096}}" /dev/zero
                exit 0
            fi
            if [[ "$*" == *pg_database_size* ]]; then
                echo "${{FAKE_DB_SIZE_BYTES:-1048576}}"
                exit 0
            fi
            echo "unhandled fake docker invocation: $*" >&2
            exit 1
            """)
    )
    docker_script.chmod(0o755)
    return bin_dir


def _run_backup(
    *,
    backup_dir: Path,
    bin_dir: Path,
    env_overrides: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["BACKUP_DIR"] = str(backup_dir)
    env["POSTGRES_CONTAINER"] = "fake-postgres"
    env.pop("TELEGRAM_BOT_TOKEN", None)
    env.pop("TELEGRAM_CHAT_ID", None)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(  # noqa: S603 - fixed executable and reviewed targets
        [_BASH, str(_BACKUP_SH)],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_successful_run_saves_one_compressed_backup_and_no_leftovers(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    bin_dir = _fake_bin(tmp_path)

    result = _run_backup(backup_dir=backup_dir, bin_dir=bin_dir)

    assert result.returncode == 0, result.stderr
    saved = list(backup_dir.glob("schurfer_*.dump.gz"))
    assert len(saved) == 1
    # No raw .dump left behind, no stray .tmp/.build artifacts.
    assert list(backup_dir.glob("schurfer_*.dump")) == []
    assert "Saved:" in result.stdout


def test_low_headroom_skips_pg_dump_and_leaves_no_file(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    bin_dir = _fake_bin(tmp_path)

    # An impossibly large floor forces the pre-flight check to fail
    # regardless of this test machine's real free space -- deterministic
    # without needing to fake `df` itself.
    result = _run_backup(
        backup_dir=backup_dir,
        bin_dir=bin_dir,
        env_overrides={"MIN_FREE_FLOOR_KB": "999999999999"},
    )

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert "Not starting pg_dump" in result.stderr
    assert list(backup_dir.glob("schurfer_*")) == []


def test_required_space_scales_with_live_db_size_not_just_the_floor(tmp_path: Path) -> None:
    """A live DB size that, multiplied by MIN_FREE_MULTIPLIER, exceeds a
    LOW floor must still be what gates the check -- proves the floor is a
    minimum, not the effective requirement, once a real size signal exists."""
    backup_dir = tmp_path / "backups"
    bin_dir = _fake_bin(tmp_path)

    result = _run_backup(
        backup_dir=backup_dir,
        bin_dir=bin_dir,
        env_overrides={
            "MIN_FREE_FLOOR_KB": "1",  # trivially satisfied on its own
            "FAKE_DB_SIZE_BYTES": str(999_999_999_999),  # ~931 GiB
            "MIN_FREE_MULTIPLIER": "3",
        },
    )

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert list(backup_dir.glob("schurfer_*")) == []


def test_gzip_failure_before_any_output_leaves_the_backups_dir_empty(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    bin_dir = _fake_bin(tmp_path)
    (bin_dir / "gzip").write_text(
        "#!/usr/bin/env bash\necho 'fake gzip: simulated disk-full failure' >&2\nexit 1\n"
    )
    (bin_dir / "gzip").chmod(0o755)

    result = _run_backup(backup_dir=backup_dir, bin_dir=bin_dir)

    assert result.returncode != 0
    # The exact reproduction of the 2026-09-03 incident: no leftover raw
    # .dump from a run whose gzip step failed partway.
    assert list(backup_dir.glob("schurfer_*")) == []


def test_gzip_failure_after_partial_output_leaves_no_partial_gz_behind(tmp_path: Path) -> None:
    """Colleague review, 2026-09-03, third round: the first version of
    this fix (`gzip "$FILE"`, in place, trap only covering $RAW_FILE) left
    a PARTIAL .dump.gz sitting under the real backup filename when gzip
    failed after already writing some output -- the exact same class of
    disk-pressure leftover the original incident was, just one pipeline
    stage later. This fake gzip reproduces that shape of failure (writes
    real bytes to stdout, exactly like `gzip -c` would mid-write, THEN
    fails) rather than failing before writing anything at all."""
    backup_dir = tmp_path / "backups"
    bin_dir = _fake_bin(tmp_path)
    (bin_dir / "gzip").write_text(
        "#!/usr/bin/env bash\n"
        "head -c 1000 /dev/zero\n"
        "echo 'fake gzip: simulated disk-full failure mid-write' >&2\n"
        "exit 1\n"
    )
    (bin_dir / "gzip").chmod(0o755)

    result = _run_backup(backup_dir=backup_dir, bin_dir=bin_dir)

    assert result.returncode != 0
    # Nothing at all under the real backup filename -- raw dump, partial
    # .gz.partial, AND a partial schurfer_*.dump.gz must all be absent.
    assert list(backup_dir.glob("schurfer_*")) == []


def test_concurrent_runs_serialize_instead_of_racing_pg_dump(tmp_path: Path) -> None:
    """Two runs launched back-to-back: the first holds the lock through a
    slow (simulated) pg_dump; the second, given a short LOCK_WAIT_SECONDS,
    must fail to acquire the lock rather than starting its own pg_dump
    concurrently -- the exact race the 2026-09-03 review flagged as still
    possible between a cron run and a prod-deploy-triggered run."""
    backup_dir = tmp_path / "backups"
    bin_dir = _fake_bin(tmp_path, pg_dump_sleep_seconds=3.0)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["BACKUP_DIR"] = str(backup_dir)
    env["POSTGRES_CONTAINER"] = "fake-postgres"
    env.pop("TELEGRAM_BOT_TOKEN", None)
    env.pop("TELEGRAM_CHAT_ID", None)

    first = subprocess.Popen(  # noqa: S603 - fixed executable and reviewed targets
        [_BASH, str(_BACKUP_SH)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Give the first run time to acquire the lock and enter pg_dump before
    # starting the second.
    time.sleep(0.5)

    second_env = dict(env)
    second_env["LOCK_WAIT_SECONDS"] = "1"
    second = subprocess.run(  # noqa: S603 - fixed executable and reviewed targets
        [_BASH, str(_BACKUP_SH)],
        env=second_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    _first_stdout, first_stderr = first.communicate(timeout=30)

    assert first.returncode == 0, first_stderr
    assert second.returncode == 1
    assert "could not acquire" in second.stderr

    # Exactly one backup was produced -- the second run never reached
    # pg_dump at all.
    saved = list(backup_dir.glob("schurfer_*.dump.gz"))
    assert len(saved) == 1


@pytest.fixture(autouse=True)
def _require_backup_script() -> None:
    if not _BACKUP_SH.exists():
        pytest.skip(f"{_BACKUP_SH} not found")
