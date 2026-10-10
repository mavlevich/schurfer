from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from schurfer_analytics.source_digest import main, source_digest

if TYPE_CHECKING:
    from pathlib import Path


def _tree(root: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return root


def test_the_digest_covers_python_sources_by_path_and_bytes(tmp_path: Path) -> None:
    base = source_digest(_tree(tmp_path / "a", {"x.py": "1", "sub/y.py": "2"}))
    same = source_digest(_tree(tmp_path / "b", {"sub/y.py": "2", "x.py": "1"}))
    assert base == same
    assert source_digest(_tree(tmp_path / "c", {"x.py": "1", "sub/y.py": "3"})) != base
    assert source_digest(_tree(tmp_path / "d", {"x.py": "1", "y.py": "2"})) != base


def test_caches_and_other_files_do_not_count(tmp_path: Path) -> None:
    base = source_digest(_tree(tmp_path / "a", {"x.py": "1"}))
    noisy = _tree(tmp_path / "b", {"x.py": "1", "__pycache__/x.py": "z", "data.json": "{}"})
    assert source_digest(noisy) == base


def test_a_directory_without_sources_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no Python source"):
        source_digest(tmp_path)


def test_the_cli_prints_the_digest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _tree(tmp_path, {"x.py": "1"})
    assert main([str(root)]) == 0
    assert capsys.readouterr().out == source_digest(root) + "\n"
