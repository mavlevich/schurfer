"""The deploy decides whether to back up by comparing the database's Alembic revision
with the newest migration file, read by a grep in `make prod-deploy`. A docstring line
starting with "revision" once fed that grep prose instead of a number, so every deploy
would back up whether or not it migrates. This runs the Makefile's own pipeline."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_the_deploy_reads_the_newest_migration_number() -> None:
    makefile = (ROOT / "Makefile").read_text()
    match = re.search(r"@head=\$\$\((grep -h .*? \| sort \| tail -1)\)", makefile)
    assert match is not None
    pipeline = match.group(1).replace("$$", "$")
    head = subprocess.run(  # noqa: S603 -- the repository's own Makefile pipeline
        ["/bin/bash", "-c", pipeline], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    revisions = [
        found.group(1)
        for path in (ROOT / "packages/journal/migrations/versions").glob("*.py")
        if (found := re.search(r'^revision: str = "(\d+)"', path.read_text(), re.MULTILINE))
    ]
    assert head == max(revisions)
