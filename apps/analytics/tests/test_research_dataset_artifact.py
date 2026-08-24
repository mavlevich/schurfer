from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

import pytest
from schurfer_analytics.research_dataset_artifact import (
    ArtifactWriteOutcome,
    DatasetArtifactManifest,
    ResearchDatasetArtifactCorruptError,
    iter_artifact_fingerprints,
    read_dataset_artifact,
    write_dataset_artifact,
)

if TYPE_CHECKING:
    from pathlib import Path


def _rows(n: int = 3) -> list[dict[str, object]]:
    return [{"trade_id": i, "value": i * 1.5} for i in range(1, n + 1)]


def _write(
    tmp_path: Path, *, rows: list[dict[str, object]] | None = None, **overrides: Any
) -> tuple[ArtifactWriteOutcome, DatasetArtifactManifest | None]:
    kwargs: dict[str, Any] = {
        "dataset_name": "test_dataset",
        "dataset_version": "v1",
        "schema_version": "s1",
        "rows": _rows() if rows is None else rows,
        "row_id_field": "trade_id",
        "row_order": "trade_id ascending",
        "cohort": {"since": "2026-01-01", "until": "2026-02-01"},
        "code_revision": "abc123",
        "working_tree_dirty": False,
        "directory": tmp_path,
    }
    kwargs.update(overrides)
    return write_dataset_artifact(**kwargs)


def test_same_input_gives_same_fingerprint(tmp_path: Path) -> None:
    outcome1, manifest1 = _write(tmp_path)
    outcome2, manifest2 = _write(tmp_path)

    assert outcome1 == ArtifactWriteOutcome.CREATED
    assert outcome2 == ArtifactWriteOutcome.ALREADY_EXISTS
    assert manifest1 is not None
    assert manifest2 is not None
    assert manifest1.fingerprint == manifest2.fingerprint
    assert manifest1.data_sha256 == manifest2.data_sha256


def test_second_writer_reads_back_first_writers_manifest_not_its_own(tmp_path: Path) -> None:
    """Concurrent-write simulation: two writers with identical rows but
    different code_revision/generated_at must converge on ONE durable
    manifest (the first writer's), not two different "successful" results."""
    outcome1, manifest1 = _write(tmp_path, code_revision="revision-one")
    outcome2, manifest2 = _write(tmp_path, code_revision="revision-two")

    assert outcome1 == ArtifactWriteOutcome.CREATED
    assert outcome2 == ArtifactWriteOutcome.ALREADY_EXISTS
    assert manifest1 is not None
    assert manifest2 is not None
    assert manifest2.code_revision == "revision-one"

    read_manifest, _ = read_dataset_artifact(manifest1.fingerprint, directory=tmp_path)
    assert read_manifest.code_revision == "revision-one"


def test_changing_one_row_changes_the_fingerprint(tmp_path: Path) -> None:
    _, manifest1 = _write(tmp_path, rows=_rows())
    changed = _rows()
    changed[0]["value"] = 999.0
    _, manifest2 = _write(tmp_path, rows=changed)

    assert manifest1 is not None
    assert manifest2 is not None
    assert manifest1.fingerprint != manifest2.fingerprint
    assert manifest1.data_sha256 != manifest2.data_sha256


def test_empty_rows_rejected_by_default(tmp_path: Path) -> None:
    outcome, manifest = _write(tmp_path, rows=[])

    assert outcome == ArtifactWriteOutcome.REJECTED_EMPTY_OR_AMBIGUOUS
    assert manifest is None
    assert iter_artifact_fingerprints(directory=tmp_path) == []


def test_empty_rows_allowed_when_caller_opts_in(tmp_path: Path) -> None:
    outcome, manifest = _write(tmp_path, rows=[], allow_empty=True)

    assert outcome == ArtifactWriteOutcome.CREATED
    assert manifest is not None
    assert manifest.row_count == 0
    read_manifest, rows = read_dataset_artifact(manifest.fingerprint, directory=tmp_path)
    assert rows == []
    assert read_manifest.row_count == 0


@pytest.mark.parametrize(
    "bad_rows",
    [
        [{"trade_id": 1, "value": 1.0}, {"trade_id": 1, "value": 2.0}],  # duplicate id
        [{"trade_id": None, "value": 1.0}],  # missing id
    ],
)
def test_ambiguous_row_identity_rejected(tmp_path: Path, bad_rows: list[dict[str, object]]) -> None:
    outcome, manifest = _write(tmp_path, rows=bad_rows)

    assert outcome == ArtifactWriteOutcome.REJECTED_EMPTY_OR_AMBIGUOUS
    assert manifest is None
    assert iter_artifact_fingerprints(directory=tmp_path) == []


def test_round_trip_preserves_row_values_and_order(tmp_path: Path) -> None:
    rows = [
        {
            "trade_id": 3,
            "symbol": "COTI/USDT:USDT",
            "modeled_exit_bps": 4.25,
            "flag": True,
            "n": None,
        },
        {
            "trade_id": 1,
            "symbol": "AKE/USDT:USDT",
            "modeled_exit_bps": 7.5,
            "flag": False,
            "n": None,
        },
    ]
    _, manifest = _write(tmp_path, rows=rows, row_order="deliberately NOT id-sorted")

    assert manifest is not None
    _, read_rows = read_dataset_artifact(manifest.fingerprint, directory=tmp_path)
    assert read_rows == rows  # exact values AND the caller's own row order preserved


def test_corrupt_data_file_is_rejected_not_silently_treated_as_miss(tmp_path: Path) -> None:
    _, manifest = _write(tmp_path)
    assert manifest is not None
    data_path = tmp_path / manifest.fingerprint[:2] / manifest.fingerprint / "data.json"
    data_path.write_text(json.dumps([{"trade_id": 1, "value": "tampered"}]))

    with pytest.raises(ResearchDatasetArtifactCorruptError):
        read_dataset_artifact(manifest.fingerprint, directory=tmp_path)


def test_corrupt_manifest_hash_is_rejected(tmp_path: Path) -> None:
    _, manifest = _write(tmp_path)
    assert manifest is not None
    manifest_hash_path = (
        tmp_path / manifest.fingerprint[:2] / manifest.fingerprint / "manifest.sha256"
    )
    manifest_hash_path.write_text("0" * 64)

    with pytest.raises(ResearchDatasetArtifactCorruptError):
        read_dataset_artifact(manifest.fingerprint, directory=tmp_path)


def test_truncated_data_file_row_count_mismatch_is_rejected(tmp_path: Path) -> None:
    """A data.json truncated/replaced with fewer rows than the manifest
    records, but whose own data.sha256 sidecar was updated to match (e.g. a
    manual, half-finished edit) -- must still be caught by the manifest's
    OWN recorded data_sha256, which nothing legitimate ever updates after
    publish."""
    _, manifest = _write(tmp_path)
    assert manifest is not None
    base = tmp_path / manifest.fingerprint[:2] / manifest.fingerprint
    truncated = json.dumps(_rows(1))
    (base / "data.json").write_text(truncated)
    (base / "data.sha256").write_text(hashlib.sha256(truncated.encode()).hexdigest())

    with pytest.raises(ResearchDatasetArtifactCorruptError):
        read_dataset_artifact(manifest.fingerprint, directory=tmp_path)


def test_missing_file_is_corrupt_not_a_miss(tmp_path: Path) -> None:
    _, manifest = _write(tmp_path)
    assert manifest is not None
    data_path = tmp_path / manifest.fingerprint[:2] / manifest.fingerprint / "data.json"
    data_path.unlink()

    with pytest.raises(ResearchDatasetArtifactCorruptError):
        read_dataset_artifact(manifest.fingerprint, directory=tmp_path)


def test_unknown_fingerprint_is_also_corrupt_not_a_silent_miss(tmp_path: Path) -> None:
    with pytest.raises(ResearchDatasetArtifactCorruptError):
        read_dataset_artifact("0" * 64, directory=tmp_path)


def test_manifest_records_required_contract_fields(tmp_path: Path) -> None:
    _, manifest = _write(
        tmp_path,
        cost_model_version="cost_v1",
        exclusion_reasons={"missing_observation": 2},
        extra={"threshold_bps": 50.0},
    )

    assert manifest is not None
    assert manifest.dataset_name == "test_dataset"
    assert manifest.dataset_version == "v1"
    assert manifest.schema_version == "s1"
    assert manifest.cost_model_version == "cost_v1"
    assert manifest.cohort == {"since": "2026-01-01", "until": "2026-02-01"}
    assert manifest.code_revision == "abc123"
    assert manifest.working_tree_dirty is False
    assert manifest.row_count == 3
    assert manifest.row_id_field == "trade_id"
    assert manifest.exclusion_reasons == {"missing_observation": 2}
    assert manifest.extra == {"threshold_bps": 50.0}
    assert len(manifest.data_sha256) == 64
    assert len(manifest.fingerprint) == 64


def test_iter_artifact_fingerprints_lists_every_published_artifact(tmp_path: Path) -> None:
    assert iter_artifact_fingerprints(directory=tmp_path) == []
    _, manifest1 = _write(tmp_path, rows=_rows(1))
    _, manifest2 = _write(tmp_path, rows=_rows(2))

    assert manifest1 is not None
    assert manifest2 is not None
    found = iter_artifact_fingerprints(directory=tmp_path)
    assert sorted(found) == sorted([manifest1.fingerprint, manifest2.fingerprint])


def test_different_row_order_label_gives_different_fingerprint(tmp_path: Path) -> None:
    """Regression: row_order used to be excluded from the fingerprint --
    two writers claiming a different order for byte-identical row content
    would silently collide on one fingerprint (colleague review,
    2026-08-25)."""
    _, manifest1 = _write(tmp_path, row_order="trade_id ascending")
    _, manifest2 = _write(tmp_path, row_order="trade_id descending")

    assert manifest1 is not None
    assert manifest2 is not None
    assert manifest1.fingerprint != manifest2.fingerprint


def test_publish_is_all_or_nothing_no_partial_artifact_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a crash/failure partway through used to leave data.json
    durable while manifest.json was still missing -- a concurrent (or
    later) reader would fail closed as CORRUPT on a fingerprint that was
    simply never fully published. Now the whole artifact is staged and
    published with a single directory rename, so a failure partway through
    must leave NOTHING at the final path (colleague review, 2026-08-25)."""
    import schurfer_analytics.research_dataset_artifact as artifact_module

    real_write_file = artifact_module._write_file_durable
    calls = {"n": 0}

    def _flaky_write_file(path: Path, content: str) -> None:
        calls["n"] += 1
        if calls["n"] == 3:  # fail while writing the 3rd of 4 staged files
            raise OSError("simulated disk failure mid-publish")
        real_write_file(path, content)

    monkeypatch.setattr(artifact_module, "_write_file_durable", _flaky_write_file)
    outcome, manifest = _write(tmp_path)

    assert outcome == ArtifactWriteOutcome.WRITE_FAILED
    assert manifest is None
    # Nothing at all published -- no half-written fingerprint directory,
    # and no leftover staging directory either.
    assert iter_artifact_fingerprints(directory=tmp_path) == []
    shard_dirs = list(tmp_path.iterdir()) if tmp_path.is_dir() else []
    for shard in shard_dirs:
        leftovers = list(shard.iterdir())
        assert leftovers == [], f"unexpected leftovers in {shard}: {leftovers}"


def test_coordinated_tamper_leaving_fingerprint_field_unchanged_is_still_caught(
    tmp_path: Path,
) -> None:
    """Regression: the previous version only checked that
    manifest.fingerprint equals the requested fingerprint (a self-reported
    field) and that each file's own sidecar hash matched -- it never
    re-derived the fingerprint from the manifest's own identity fields. An
    attacker (or corruption) that consistently updates data.json, both
    sidecars, and manifest.data_sha256 -- while leaving `fingerprint`
    unchanged -- passed every check. Colleague review, 2026-08-25,
    reproduced exactly this against the previous version."""
    _, manifest = _write(tmp_path)
    assert manifest is not None
    base = tmp_path / manifest.fingerprint[:2] / manifest.fingerprint

    tampered_rows = [{"trade_id": 1, "value": "tampered"}]
    tampered_data_json = json.dumps(tampered_rows, sort_keys=True, separators=(",", ":"))
    tampered_data_sha256 = hashlib.sha256(tampered_data_json.encode()).hexdigest()
    (base / "data.json").write_text(tampered_data_json)
    (base / "data.sha256").write_text(tampered_data_sha256)

    manifest_payload = json.loads((base / "manifest.json").read_text())
    manifest_payload["data_sha256"] = tampered_data_sha256
    manifest_payload["row_count"] = len(tampered_rows)
    # fingerprint field deliberately left unchanged -- the attacker's whole
    # point is to keep the directory name and this field self-consistent.
    tampered_manifest_json = json.dumps(manifest_payload, sort_keys=True, separators=(",", ":"))
    tampered_manifest_sha256 = hashlib.sha256(tampered_manifest_json.encode()).hexdigest()
    (base / "manifest.json").write_text(tampered_manifest_json)
    (base / "manifest.sha256").write_text(tampered_manifest_sha256)

    with pytest.raises(ResearchDatasetArtifactCorruptError, match="does not match its own"):
        read_dataset_artifact(manifest.fingerprint, directory=tmp_path)


@pytest.mark.parametrize(
    "bad_fingerprint",
    [
        "../../etc/passwd",
        "not-hex-at-all-but-64-characters-long-so-it-would-pass-a-length-only-check",
        "A" * 64,  # uppercase not accepted
        "0" * 63,  # too short
        "",
    ],
)
def test_malformed_fingerprint_is_rejected_before_touching_the_filesystem(
    tmp_path: Path, bad_fingerprint: str
) -> None:
    with pytest.raises(ValueError, match="invalid fingerprint"):
        read_dataset_artifact(bad_fingerprint, directory=tmp_path)


def test_nan_row_value_is_rejected_not_silently_written(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Out of range float"):
        _write(tmp_path, rows=[{"trade_id": 1, "value": float("nan")}])


def test_non_json_native_row_value_is_rejected_not_silently_stringified(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    with pytest.raises(TypeError):
        _write(tmp_path, rows=[{"trade_id": 1, "value": datetime.now(UTC)}])
