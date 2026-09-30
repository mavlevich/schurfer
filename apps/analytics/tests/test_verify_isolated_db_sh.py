"""The local verify runner reaps SIGKILL orphans without touching fresh runs."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path


def test_reaps_only_stale_disposable_containers(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    calls = tmp_path / "docker-calls"
    old = (datetime.now(UTC) - timedelta(hours=7)).isoformat()
    fresh = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$DOCKER_CALLS"\n'
        'case "$1" in\n'
        "  info) exit 0 ;;\n"
        '  ps) printf "old-container\\nfresh-container\\n" ;;\n'
        f'  inspect) case "$*" in *old-container) printf "%s\\n" "{old}" ;; '
        f'*fresh-container) printf "%s\\n" "{fresh}" ;; esac ;;\n'
        "  rm) exit 0 ;;\n"
        "  run) exit 17 ;;\n"
        "esac\n"
    )
    docker.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["DOCKER_CALLS"] = str(calls)
    result = subprocess.run(  # noqa: S603, RUF100 - fixed shell and generated Docker stub
        ["/bin/bash", "infra/scripts/verify_isolated_db.sh", "true"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 17, result.stdout + result.stderr
    recorded = calls.read_text().splitlines()
    assert "rm -f -v old-container" in recorded
    assert "rm -f -v fresh-container" not in recorded
