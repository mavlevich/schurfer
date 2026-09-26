"""Every data file the analytics code loads at runtime must ship in the package.

Registry v4 was deployed with `evidence/source_lead/v4` missing from
`[tool.setuptools.package-data]`, so the installed worker could not verify
the registry and crashed on start (2026-09-26). The tests pass from the
source tree, so only this check catches that class of omission.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "schurfer_analytics"
PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_every_evidence_and_registry_file_is_package_data() -> None:
    patterns = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["setuptools"][
        "package-data"
    ]["schurfer_analytics"]
    covered = {path for pattern in patterns for path in PACKAGE_ROOT.glob(pattern)}
    data_files = {
        path
        for directory in ("evidence", "registry")
        for path in (PACKAGE_ROOT / directory).rglob("*")
        if path.is_file() and not path.name.startswith(".") and path.suffix != ".py"
    }
    missing = sorted(str(path.relative_to(PACKAGE_ROOT)) for path in data_files - covered)
    assert not missing, f"not shipped as package data: {missing[:10]}"
