"""Regression coverage for infra/scripts/offsite-backup.sh.

Same approach as test_backup_sh.py: run the REAL script against a fake `borg`
and `docker` on PATH, rather than reimplementing its logic in Python where the
two could drift.

Every test here corresponds to a defect a colleague reproduced on the first
version of this script (review of 0ed26fb, 2026-09-08):

  * a research file appearing between the file walk and the archive aborted the
    whole job, including the database dump that had not started yet;
  * `borg list | grep -c ... || true` recorded a verified archive when
    `borg list` printed plausible output and then failed;
  * a failure in one archive family cancelled the other.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "infra" / "scripts" / "offsite-backup.sh"
_BASH = shutil.which("bash") or "/bin/bash"

pytestmark = pytest.mark.skipif(
    shutil.which("flock") is None,
    reason="flock (util-linux) is required by the script; brew install flock on macOS",
)


def _write(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body))
    path.chmod(0o755)


def _fake_env(tmp_path: Path, *, borg_body: str) -> tuple[Path, dict[str, str]]:
    """A fake repo tree, a fake `borg` whose behaviour the test controls, and a
    `docker` that produces a few bytes for pg_dump."""
    repo = tmp_path / "repo"
    for relative in (
        "runtime/market-path-cache",
        "runtime/research-dataset-artifacts",
        "backups/reports",
    ):
        (repo / relative).mkdir(parents=True)
    (repo / "runtime/cold-bars").mkdir(parents=True)
    (repo / "runtime/cold-bars/bars-2026-08-20.parquet").write_text("parquet")
    (repo / "runtime/cold-bars/bars-2026-08-20.manifest.json").write_text("{}")
    (repo / "runtime/market-path-cache/a.json").write_text("a")
    (repo / "runtime/research-dataset-artifacts/b.json").write_text("b")
    (repo / "backups/reports/c.md").write_text("c")

    state = tmp_path / "state"
    state.mkdir()
    env_file = state / "backup.env"
    env_file.write_text('BORG_REPO="/does/not/matter"\n')

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _write(bin_dir / "borg", borg_body)
    _write(
        bin_dir / "docker",
        """\
        #!/usr/bin/env bash
        printf 'dump'
        """,
    )
    _write(bin_dir / "curl", "#!/usr/bin/env bash\nexit 0\n")

    # The fake tools shadow the real ones, but the rest of PATH stays: the
    # script needs real `find`, `sort`, `flock` and friends.
    env = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "OFFSITE_BACKUP_ENV": str(env_file),
        "REPO_ROOT": str(repo),
        "STATE_DIR": str(state),
        "BORG_TRACE": str(tmp_path / "borg-calls.log"),
    }
    return state, env


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- running the shipped script is the test
        [_BASH, str(_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


# A fake borg that succeeds and reports back exactly the paths it was handed on
# stdin, which is what a correct `--paths-from-stdin` archive contains.
_HONEST_BORG = """\
    #!/usr/bin/env bash
    echo "$*" >> "$BORG_TRACE"
    case "$1" in
      create)
        if [[ "$*" == *--paths-from-stdin* ]]; then
          cat > "${BORG_TRACE}.paths"
        else
          cat > /dev/null
        fi
        ;;
      list) [[ -f "${BORG_TRACE}.paths" ]] && cat "${BORG_TRACE}.paths" ;;
      *) : ;;
    esac
    exit 0
    """


def test_successful_run_stamps_both_families(tmp_path: Path) -> None:
    state, env = _fake_env(tmp_path, borg_body=_HONEST_BORG)
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert (state / "offsite-backup-db.stamp").exists()
    assert (state / "offsite-backup-research.stamp").exists()


def test_new_research_file_mid_run_does_not_fail_the_job(tmp_path: Path) -> None:
    """The defect this replaces: the script counted files, then let borg walk
    the same directories, so an ordinary new cache entry written by a running
    container made a good archive look wrong. It deleted the archive and
    aborted before the database dump had even started."""
    borg = """\
        #!/usr/bin/env bash
        echo "$*" >> "$BORG_TRACE"
        case "$1" in
          create)
            if [[ "$*" == *--paths-from-stdin* ]]; then
              cat > "${BORG_TRACE}.paths"
              # A container writes one more file while the archive is running.
              echo extra > "${REPO_ROOT}/runtime/market-path-cache/late.json"
            else
              cat > /dev/null
            fi
            ;;
          list) [[ -f "${BORG_TRACE}.paths" ]] && cat "${BORG_TRACE}.paths" ;;
          *) : ;;
        esac
        exit 0
        """
    state, env = _fake_env(tmp_path, borg_body=borg)
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert (state / "offsite-backup-research.stamp").exists()
    assert (state / "offsite-backup-db.stamp").exists()


def test_borg_list_failure_is_not_recorded_as_a_verified_archive(tmp_path: Path) -> None:
    """`borg list` printing the right paths and then exiting non-zero must not
    count as verification. The first version piped it into a counter and
    swallowed the status with `|| true`."""
    borg = """\
        #!/usr/bin/env bash
        echo "$*" >> "$BORG_TRACE"
        case "$1" in
          create)
            if [[ "$*" == *--paths-from-stdin* ]]; then
          cat > "${BORG_TRACE}.paths"
        else
          cat > /dev/null
        fi
            ;;
          list)
            [[ -f "${BORG_TRACE}.paths" ]] && cat "${BORG_TRACE}.paths"
            exit 2
            ;;
          *) : ;;
        esac
        exit 0
        """
    state, env = _fake_env(tmp_path, borg_body=borg)
    result = _run(env)
    assert result.returncode != 0
    assert not (state / "offsite-backup-research.stamp").exists()
    assert "delete" in (tmp_path / "borg-calls.log").read_text()


def test_research_failure_still_leaves_a_database_archive(tmp_path: Path) -> None:
    """The database dump is the more valuable artifact and must not be
    cancelled by anything that happens to the research archive."""
    borg = """\
        #!/usr/bin/env bash
        echo "$*" >> "$BORG_TRACE"
        if [[ "$1" == create && "$*" == *--paths-from-stdin* ]]; then
          cat > /dev/null
          exit 2
        fi
        [[ "$1" == create ]] && cat > /dev/null
        exit 0
        """
    state, env = _fake_env(tmp_path, borg_body=borg)
    result = _run(env)
    assert result.returncode != 0, "the job as a whole must still report failure"
    assert (state / "offsite-backup-db.stamp").exists()
    assert not (state / "offsite-backup-research.stamp").exists()


def test_database_failure_is_reported_and_leaves_no_stamp(tmp_path: Path) -> None:
    borg = """\
        #!/usr/bin/env bash
        echo "$*" >> "$BORG_TRACE"
        if [[ "$1" == create && "$*" == *--content-from-command* ]]; then
          exit 2
        fi
        case "$1" in
          create)
            if [[ "$*" == *--paths-from-stdin* ]]; then
          cat > "${BORG_TRACE}.paths"
        else
          cat > /dev/null
        fi
            ;;
          list) [[ -f "${BORG_TRACE}.paths" ]] && cat "${BORG_TRACE}.paths" ;;
          *) : ;;
        esac
        exit 0
        """
    state, env = _fake_env(tmp_path, borg_body=borg)
    result = _run(env)
    assert result.returncode != 0
    assert not (state / "offsite-backup-db.stamp").exists()
    assert (state / "offsite-backup-research.stamp").exists()


def test_archive_missing_a_requested_file_is_deleted(tmp_path: Path) -> None:
    """Completeness is checked by comparing the set of paths, not their count:
    an archive holding the right number of wrong files must not pass."""
    borg = """\
        #!/usr/bin/env bash
        echo "$*" >> "$BORG_TRACE"
        case "$1" in
          create)
            if [[ "$*" == *--paths-from-stdin* ]]; then
              cat > "${BORG_TRACE}.paths"
              sed 's#market-path-cache/a.json#market-path-cache/impostor.json#' \
                "${BORG_TRACE}.paths" > "${BORG_TRACE}.reported"
            else
              cat > /dev/null
            fi
            ;;
          list) [[ -f "${BORG_TRACE}.reported" ]] && cat "${BORG_TRACE}.reported" ;;
          *) : ;;
        esac
        exit 0
        """
    state, env = _fake_env(tmp_path, borg_body=borg)
    result = _run(env)
    assert result.returncode != 0
    assert not (state / "offsite-backup-research.stamp").exists()


@pytest.mark.parametrize("missing", ["runtime/market-path-cache", "backups/reports"])
def test_missing_research_path_does_not_silently_shrink_the_archive(
    tmp_path: Path, missing: str
) -> None:
    """A path disappearing must be a failure, not a smaller successful backup."""
    state, env = _fake_env(tmp_path, borg_body=_HONEST_BORG)
    shutil.rmtree(Path(env["REPO_ROOT"]) / missing)
    result = _run(env)
    assert result.returncode != 0
    assert not (state / "offsite-backup-research.stamp").exists()


def test_archived_cold_bars_are_reclaimed_but_their_manifests_are_kept(tmp_path: Path) -> None:
    """324 MB a day fills this disk in a season, so the Parquet has to go once
    it is safely archived. The manifest is kilobytes and is what tells the
    exporter which days are already done, so it stays."""
    state, env = _fake_env(tmp_path, borg_body=_HONEST_BORG)
    repo = Path(env["REPO_ROOT"])
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert not (repo / "runtime/cold-bars/bars-2026-08-20.parquet").exists()
    assert (repo / "runtime/cold-bars/bars-2026-08-20.manifest.json").exists()
    assert (state / "offsite-backup-bars.stamp").exists()


def test_cold_bars_survive_an_unverified_archive(tmp_path: Path) -> None:
    """The whole point of deleting only what was verified: if borg cannot
    confirm the archive holds what was asked for, the local copy is the only
    one left and must not be touched."""
    borg = """\
        #!/usr/bin/env bash
        echo "$*" >> "$BORG_TRACE"
        case "$1" in
          create)
            if [[ "$*" == *--paths-from-stdin* ]]; then
              cat > "${BORG_TRACE}.paths"
            else
              cat > /dev/null
            fi
            ;;
          list)
            # Reports one file fewer than it was handed.
            [[ -f "${BORG_TRACE}.paths" ]] && tail -n +2 "${BORG_TRACE}.paths"
            ;;
          *) : ;;
        esac
        exit 0
        """
    state, env = _fake_env(tmp_path, borg_body=borg)
    repo = Path(env["REPO_ROOT"])
    result = _run(env)
    assert result.returncode != 0
    assert (repo / "runtime/cold-bars/bars-2026-08-20.parquet").exists()
    assert not (state / "offsite-backup-bars.stamp").exists()


def test_bars_are_never_pruned(tmp_path: Path) -> None:
    """A day exists only in the archives that already held it, because the local
    file is gone. Pruning this family by age would delete data, not a redundant
    copy of it."""
    _, env = _fake_env(tmp_path, borg_body=_HONEST_BORG)
    _run(env)
    calls = (tmp_path / "borg-calls.log").read_text()
    prunes = [line for line in calls.splitlines() if line.startswith("prune")]
    assert prunes, "expected the other families to still be pruned"
    assert not any("bars-*" in line for line in prunes), prunes


def test_exporter_staging_files_are_not_archived(tmp_path: Path) -> None:
    """The exporter writes each day under a dot-prefixed staging name and
    renames it into place only once the row count matches, so a staging file is
    an unfinished export by definition -- and it can disappear mid-archive when
    that rename happens. That failed a deploy on 2026-09-08 while a backfill was
    still running."""
    state, env = _fake_env(tmp_path, borg_body=_HONEST_BORG)
    repo = Path(env["REPO_ROOT"])
    staging = repo / "runtime/cold-bars/.bars-2026-08-21.parquet.partial"
    staging.write_text("half a day")

    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert (state / "offsite-backup-bars.stamp").exists()
    archived = (tmp_path / "borg-calls.log.paths").read_text()
    assert ".partial" not in archived
    # And the unfinished file is left alone rather than reclaimed.
    assert staging.exists()
