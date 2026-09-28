"""The Makefile's gated-deletion targets: the command each one builds.

The dry-run's arguments must never reach a real deletion. A shared target that always
added `--cutoff-days 25` once let `ARGS=--execute` delete with the dry-run's buffer; these
tests pin what each target hands to the job.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from schurfer_analytics import cold_bar_gated_deletion_collectors as c

ROOT = Path(__file__).resolve().parents[3]
DRY_RUN = "prod-cold-bar-gated-deletion-dry-run"
EXECUTE = "prod-cold-bar-gated-deletion-execute"

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="needs make")


def _job_command(*make_args: str) -> str:
    out = subprocess.run(  # noqa: S603 -- fixed argv
        ["make", "-s", "-n", "-C", str(ROOT), *make_args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    entry = "--entrypoint cold-bar-gated-deletion"
    [command] = [line for line in out.splitlines() if entry in line]
    return command


def _job_args(command: str) -> str:
    return command.split("--entrypoint cold-bar-gated-deletion analytics", 1)[1]


def test_the_dry_run_passes_no_cutoff_of_its_own() -> None:
    assert "--cutoff-days" not in _job_args(_job_command(DRY_RUN))
    planned = _job_args(_job_command(DRY_RUN, "ARGS=--cutoff-days 14 --max-eval-days 3"))
    assert re.findall(r"--cutoff-days \d+", planned) == ["--cutoff-days 14"]


def test_the_execute_target_names_only_its_own_buffer() -> None:
    args = _job_args(_job_command(EXECUTE, "COLD_BAR_CUTOFF_DAYS=14"))
    assert re.findall(r"--cutoff-days \d+", args) == ["--cutoff-days 14"]
    assert "--execute" in args and "--max-eval-days 3" in args
    # ARGS is not an input to a real deletion
    assert "--cutoff-days 25" not in _job_args(_job_command(EXECUTE, "ARGS=--cutoff-days 25"))


@pytest.mark.parametrize("args", ["--execute", "--cutoff-days 14 --execute", "--execute-x"])
def test_the_dry_run_target_refuses_execute_before_touching_anything(args: str) -> None:
    run = subprocess.run(  # noqa: S603 -- fixed argv
        ["make", "-s", "-C", str(ROOT), DRY_RUN, f"ARGS={args}"],  # noqa: S607
        capture_output=True,
        text=True,
    )
    assert run.returncode != 0
    assert "never deletes" in run.stdout + run.stderr


def test_an_abbreviated_execute_is_not_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--exec` would otherwise mean --execute to argparse and slip past the guard."""
    argv = ["cold-bar-gated-deletion", "--cold-bars-dir", "x", "--backup-env", "y", "--exec"]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc:
        c.main()
    assert exc.value.code == 2
