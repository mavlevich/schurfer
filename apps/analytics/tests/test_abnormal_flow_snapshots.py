import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from schurfer_analytics.abnormal_flow_replay import DecisionFeatures, Outcome
from schurfer_analytics.abnormal_flow_snapshots import SnapshotReader, SnapshotWriter

UTC = UTC


def test_snapshot_write_read_equivalence(tmp_path: Path) -> None:
    d = DecisionFeatures(
        "bybit",
        "linear",
        "BTCUSDT",
        "v1",
        "BTCUSDT",
        "BTC",
        datetime(2026, 1, 1, tzinfo=UTC),
        0.1,
        0.1,
        0.1,
        100,
        1000,
        50000,
        100000,
        "2026-W01",
        None,
    )

    writer = SnapshotWriter(tmp_path, "fingerprint123")
    writer.append_decisions([d])
    writer.append_episodes([d])
    writer.append_controls(d, [d])
    writer.append_outcome(
        d,
        "primary",
        Outcome(
            "bybit",
            "linear",
            "BTCUSDT",
            "v1",
            "BTCUSDT",
            datetime(2026, 1, 1, tzinfo=UTC),
            50000,
            50100,
        ),
    )
    writer.publish()

    reader = SnapshotReader(tmp_path, "fingerprint123")

    decs = list(reader.iter_decisions())
    assert len(decs) == 1
    assert decs[0] == d

    eps = reader.load_episodes()
    assert len(eps) == 1
    assert eps[0] == d

    ctrls = reader.load_controls()
    assert len(ctrls) == 1

    outs = reader.load_outcomes()
    assert len(outs) == 1


def test_schema_hash_mismatch(tmp_path: Path) -> None:
    writer = SnapshotWriter(tmp_path, "fingerprint123")
    writer.publish()

    manifest_path = tmp_path / "snapshot_manifest.json"
    data = json.loads(manifest_path.read_text())

    data["evaluation_fingerprint"] = "bad"
    manifest_path.write_text(json.dumps(data))

    with pytest.raises(RuntimeError, match="Snapshot fingerprint mismatch"):
        SnapshotReader(tmp_path, "fingerprint123")


def test_corrupt_artifact(tmp_path: Path) -> None:
    writer = SnapshotWriter(tmp_path, "fingerprint123")
    writer.publish()

    (tmp_path / "decisions_snapshot.parquet").write_bytes(b"bad")

    with pytest.raises(RuntimeError, match="Snapshot artifact hash mismatch"):
        SnapshotReader(tmp_path, "fingerprint123")


def test_duplicate_ids(tmp_path: Path) -> None:
    d = DecisionFeatures(
        "bybit",
        "linear",
        "BTCUSDT",
        "v1",
        "BTCUSDT",
        "BTC",
        datetime(2026, 1, 1, tzinfo=UTC),
        0.1,
        0.1,
        0.1,
        100,
        1000,
        50000,
        100000,
        "2026-W01",
        None,
    )
    writer = SnapshotWriter(tmp_path, "fp")

    writer.append_decisions([d, d])
    writer.publish()

    reader = SnapshotReader(tmp_path, "fp")
    decs = list(reader.iter_decisions())
    assert len(decs) == 2
