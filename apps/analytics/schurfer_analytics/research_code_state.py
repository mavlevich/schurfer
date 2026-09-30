"""Code provenance shared by research artifacts."""

from __future__ import annotations

from typing import Any

from .abnormal_flow_formal_runner import RealGitState


def run_code_state() -> dict[str, Any]:
    git = RealGitState()
    return {"code_revision": git.get_revision(), "working_tree_dirty": git.is_dirty()}
