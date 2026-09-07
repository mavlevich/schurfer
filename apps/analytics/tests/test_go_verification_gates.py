"""Regression coverage for infra/scripts/go_lint.sh, go_deadcode.sh and the
shared go.work module list.

September audit, H-1/H-2: the pre-commit golangci-lint hook was an inline
one-liner whose `while` loop returned the exit status of its LAST iteration,
so a failing module was masked by any later module that passed, and whose
`grep '^use '` parser silently produced an empty module list against the
valid `use (...)` block form. Both reported a clean tree that was not clean.

These run the REAL scripts against a fake `golangci-lint` on PATH and a
throwaway repo root, the same way the hook invokes them, rather than
reimplementing their logic in Python.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS = _REPO_ROOT / "infra" / "scripts"
_BASH = shutil.which("bash") or "/bin/bash"

# go.work is parsed by `go work edit -json`, go's own parser, which is the
# whole point of go_workspace_modules.sh -- so these need a real toolchain.
pytestmark = pytest.mark.skipif(shutil.which("go") is None, reason="Go toolchain not installed")

_MODULES = ("first", "middle", "last")


def _fake_repo(tmp_path: Path, *, go_work: str) -> Path:
    """A throwaway repo root holding real Go modules, a go.work in the
    requested syntax, and copies of both scripts at their real paths (the
    scripts locate the root relative to their own file)."""
    root = tmp_path / "repo"
    (root / "infra" / "scripts").mkdir(parents=True)
    for name in ("go_lint.sh", "go_deadcode.sh", "go_workspace_modules.sh"):
        shutil.copy(_SCRIPTS / name, root / "infra" / "scripts" / name)
        (root / "infra" / "scripts" / name).chmod(0o755)
    for module in _MODULES:
        module_dir = root / "apps" / module
        module_dir.mkdir(parents=True)
        (module_dir / "go.mod").write_text(f"module example.com/{module}\n\ngo 1.24\n")
        (module_dir / "main.go").write_text("package main\n\nfunc main() {}\n")
    (root / "go.work").write_text(go_work)
    (root / ".golangci.yml").write_text("version: '2'\n")
    return root


def _single_line_go_work() -> str:
    return "go 1.24\n\n" + "".join(f"use ./apps/{m}\n" for m in _MODULES)


def _block_go_work() -> str:
    listed = "".join(f"\t./apps/{m}\n" for m in _MODULES)
    return f"go 1.24\n\nuse (\n{listed})\n"


def _fake_golangci_lint(root: Path, *, failing_module: str | None) -> dict[str, str]:
    """A fake golangci-lint that records every module it was run in and
    fails only in the named one. `config verify` always succeeds here; the
    schema-rejection path is exercised separately below."""
    bin_dir = root / "fakebin" / "bin"
    bin_dir.mkdir(parents=True)
    fail = f'"apps/{failing_module}"' if failing_module else '"__none__"'
    (bin_dir / "golangci-lint").write_text(
        textwrap.dedent(f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            if [[ "${{1:-}}" == "config" ]]; then
                if [[ -n "${{FAKE_LINT_CONFIG_INVALID:-}}" ]]; then
                    echo "the configuration contains invalid elements" >&2
                    exit 1
                fi
                exit 0
            fi
            echo "$PWD" >> "{root}/visited.txt"
            if [[ "$PWD" == *{fail} ]]; then
                echo "fake finding in $PWD"
                exit 1
            fi
            exit 0
            """)
    )
    (bin_dir / "golangci-lint").chmod(0o755)
    env = dict(os.environ)
    env["GOBIN"] = str(bin_dir)
    env["GOFLAGS"] = ""
    return env


def _run(
    root: Path, env: dict[str, str], script: str = "go_lint.sh"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed executable and reviewed targets
        [_BASH, str(root / "infra" / "scripts" / script)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _visited(root: Path) -> list[str]:
    path = root / "visited.txt"
    return path.read_text().split() if path.exists() else []


@pytest.mark.parametrize("failing", _MODULES)
def test_failure_in_any_module_fails_the_gate(tmp_path: Path, failing: str) -> None:
    """The masked case: a failure in the first or middle module used to be
    overwritten by the last module's success."""
    root = _fake_repo(tmp_path, go_work=_single_line_go_work())
    env = _fake_golangci_lint(root, failing_module=failing)

    result = _run(root, env)

    assert result.returncode != 0, result.stdout
    assert f"apps/{failing}" in result.stderr
    # Every module is still linted, so one run reports the whole tree.
    assert len(_visited(root)) == len(_MODULES)


def test_clean_workspace_passes_and_visits_every_module(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path, go_work=_single_line_go_work())
    env = _fake_golangci_lint(root, failing_module=None)

    result = _run(root, env)

    assert result.returncode == 0, result.stderr
    assert sorted(Path(p).name for p in _visited(root)) == sorted(_MODULES)


def test_block_form_go_work_lints_every_module(tmp_path: Path) -> None:
    """`use (...)` is valid go.work syntax that the old `grep '^use '`
    parser turned into an empty list, which then lint nothing and passed."""
    root = _fake_repo(tmp_path, go_work=_block_go_work())
    env = _fake_golangci_lint(root, failing_module="middle")

    result = _run(root, env)

    assert result.returncode != 0, result.stdout
    assert len(_visited(root)) == len(_MODULES)


def test_workspace_without_modules_fails_instead_of_linting_nothing(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path, go_work="go 1.24\n")
    env = _fake_golangci_lint(root, failing_module=None)

    result = _run(root, env)

    assert result.returncode != 0
    assert _visited(root) == []


def test_invalid_config_fails_before_any_module_is_linted(tmp_path: Path) -> None:
    """`golangci-lint run` ignores unknown config keys silently, so the
    schema check is what turns a setting written at a location this version
    does not understand into a failure."""
    root = _fake_repo(tmp_path, go_work=_single_line_go_work())
    env = _fake_golangci_lint(root, failing_module=None)
    env["FAKE_LINT_CONFIG_INVALID"] = "1"

    result = _run(root, env)

    assert result.returncode != 0
    assert _visited(root) == []


def _fake_deadcode(root: Path, *, failing_module: str | None) -> dict[str, str]:
    """A fake deadcode that records the modules it ran in and fails only in
    the named one."""
    bin_dir = root / "fakebin" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    fail = f'"apps/{failing_module}"' if failing_module else '"__none__"'
    (bin_dir / "deadcode").write_text(
        textwrap.dedent(f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            echo "$PWD" >> "{root}/visited.txt"
            if [[ "$PWD" == *{fail} ]]; then
                echo "fake dead code in $PWD"
                exit 1
            fi
            exit 0
            """)
    )
    (bin_dir / "deadcode").chmod(0o755)
    env = dict(os.environ)
    env["GOBIN"] = str(bin_dir)
    env["GOFLAGS"] = ""
    return env


@pytest.mark.parametrize("failing", _MODULES)
def test_deadcode_failure_in_any_module_fails_the_gate(tmp_path: Path, failing: str) -> None:
    """`make deadcode` (and therefore `make verify`) ran this loop inline,
    where the last module's success overwrote an earlier module's failure."""
    root = _fake_repo(tmp_path, go_work=_single_line_go_work())
    env = _fake_deadcode(root, failing_module=failing)

    result = _run(root, env, script="go_deadcode.sh")

    assert result.returncode != 0, result.stdout
    assert f"apps/{failing}" in result.stderr
    assert len(_visited(root)) == len(_MODULES)


def test_deadcode_clean_workspace_passes(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path, go_work=_single_line_go_work())
    env = _fake_deadcode(root, failing_module=None)

    result = _run(root, env, script="go_deadcode.sh")

    assert result.returncode == 0, result.stderr
    assert sorted(Path(p).name for p in _visited(root)) == sorted(_MODULES)


def test_deadcode_workspace_without_modules_fails(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path, go_work="go 1.24\n")
    env = _fake_deadcode(root, failing_module=None)

    result = _run(root, env, script="go_deadcode.sh")

    assert result.returncode != 0
    assert _visited(root) == []


def test_module_list_matches_across_both_go_work_forms(tmp_path: Path) -> None:
    single = _fake_repo(tmp_path / "single", go_work=_single_line_go_work())
    block = _fake_repo(tmp_path / "block", go_work=_block_go_work())
    env = dict(os.environ)

    single_out = _run(single, env, script="go_workspace_modules.sh")
    block_out = _run(block, env, script="go_workspace_modules.sh")

    assert single_out.returncode == 0, single_out.stderr
    assert block_out.returncode == 0, block_out.stderr
    assert single_out.stdout.split() == [f"./apps/{m}" for m in _MODULES]
    assert single_out.stdout == block_out.stdout
