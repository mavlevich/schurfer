"""Tests for the per-day Borg-over-SSH staging step (no real SSH/Borg).

A stub transport stands in for prod: it serves manifest/provenance JSON and "extracts"
a prebuilt Parquet by copying it. The tests pin the local SHA verification (match and
mismatch) and the staging-backed archive verifier used by the scan.
"""

from __future__ import annotations

import json
from datetime import date
from typing import TYPE_CHECKING

import pytest
from schurfer_analytics.abnormal_flow_stage import make_staging_verifier, stage_days
from schurfer_analytics.cold_bar_export import sha256_file

if TYPE_CHECKING:
    from pathlib import Path


class _StubTransport:
    """Serves JSON from a dict and 'extracts' a member by copying a local source file."""

    def __init__(self, texts: dict[str, str], member_source: Path) -> None:
        self._texts = texts
        self._member_source = member_source

    def read_text(self, remote_path: str) -> str:
        return self._texts[remote_path]

    def extract_member(self, borg_repo: str, archive: str, member: str, dest: Path) -> None:
        dest.write_bytes(self._member_source.read_bytes())


def _setup(tmp_path: Path, *, corrupt: bool = False) -> tuple[_StubTransport, Path]:
    day = date(2026, 9, 18)
    base = f"bars-{day.isoformat()}"
    source = tmp_path / "source.parquet"
    source.write_bytes(b"fake-parquet-bytes-for-the-day")
    sha = sha256_file(source)
    remote_dir = "/prod/cold-bars"
    manifest = {"file_name": f"{base}.parquet", "sha256": ("0" * 64 if corrupt else sha)}
    receipt = {
        "archive_name": "bars-2026-09-19T04:30:16",
        "parquet_path": f"runtime/cold-bars/{base}.parquet",
        "parquet_sha256": sha,
    }
    texts = {
        f"{remote_dir}/{base}.manifest.json": json.dumps(manifest),
        f"{remote_dir}/{base}.offsite-receipt.json": json.dumps(receipt),
    }
    return _StubTransport(texts, source), source


def test_stage_days_fetches_and_verifies_sha_locally(tmp_path: Path) -> None:
    transport, _ = _setup(tmp_path)
    local = tmp_path / "local"
    artifact = stage_days(
        transport,
        remote_manifest_dir="/prod/cold-bars",
        borg_repo="/prod/borg",
        local_dir=local,
        start=date(2026, 9, 18),
        end=date(2026, 9, 18),
    )
    assert (local / "bars-2026-09-18.parquet").exists()
    assert (local / "bars-2026-09-18.manifest.json").exists()
    assert (local / "staging.json").exists()
    assert len(artifact["days"]) == 1
    rec = artifact["days"][0]
    assert rec["verified"] is True
    assert rec["archive"] == "bars-2026-09-19T04:30:16"
    assert rec["file_bytes"] > 0


def test_stage_days_rejects_a_sha_mismatch(tmp_path: Path) -> None:
    transport, _ = _setup(tmp_path, corrupt=True)
    with pytest.raises(ValueError, match="SHA"):
        stage_days(
            transport,
            remote_manifest_dir="/prod/cold-bars",
            borg_repo="/prod/borg",
            local_dir=tmp_path / "local",
            start=date(2026, 9, 18),
            end=date(2026, 9, 18),
        )


def test_staging_verifier_matches_on_recorded_sha() -> None:
    staging = {
        "days": [
            {"day": "2026-09-18", "archive": "arch-1", "sha256": "abc", "verified": True},
        ]
    }
    verify = make_staging_verifier(staging)

    class _M:
        sha256 = "abc"

    class _Wrong:
        sha256 = "different"

    assert verify(date(2026, 9, 18), _M()) == (True, "arch-1")
    # Local manifest SHA drifted from what staging verified -> not verified.
    assert verify(date(2026, 9, 18), _Wrong()) == (False, "arch-1")
    # A day not in the staging artifact is unverified.
    assert verify(date(2026, 9, 19), _M()) == (False, None)
