from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from schurfer_analytics.abnormal_flow_replay import DecisionFeatures
from schurfer_analytics.abnormal_flow_snapshots import (
    ColdBarInput,
    SnapshotCorruptError,
    SnapshotIdentity,
    SnapshotInputError,
    SnapshotPublishOutcome,
    SnapshotReader,
    SnapshotWriter,
    decision_id,
)

if TYPE_CHECKING:
    from pathlib import Path


def _decision(symbol: str = "BTCUSDT") -> DecisionFeatures:
    return DecisionFeatures(
        exchange="bybit",
        market_type="linear",
        native_market_id=symbol,
        capture_version="v1",
        symbol=symbol,
        canonical_asset=f"asset:{symbol}",
        decision_at=datetime(2026, 1, 1, tzinfo=UTC),
        oi_growth_pct=0.1,
        buy_pressure=0.7,
        containment=0.01,
        oi_native_amount=100.0,
        oi_native_value_usd=1_000.0,
        decision_price=10.0,
        pre_decision_turnover_usd=5_000.0,
        iso_week="2026-W01",
    )


def _identity(*, contract_hash: str = "sha256:contract") -> SnapshotIdentity:
    return SnapshotIdentity(
        pipeline_version="pipeline-v1",
        contract_hash=contract_hash,
        contract_version="contract-v1",
        identity_snapshot_hash="sha256:identity",
        dependency_start="2026-01-01T00:00:00+00:00",
        evaluation_start="2026-01-01T01:00:00+00:00",
        evaluation_end_exclusive="2026-01-02T00:00:00+00:00",
        cold_bars=(
            ColdBarInput(
                day="2026-01-01",
                file_name="bars-2026-01-01.parquet",
                row_count=1,
                sha256="abc",
                source_fingerprint="source",
            ),
        ),
    )


def _publish(root: Path, identity: SnapshotIdentity | None = None) -> tuple[Path, str]:
    selected_identity = identity or _identity()
    decision = _decision()
    with SnapshotWriter(
        root,
        selected_identity,
        code_revision="deadbeef",
        working_tree_dirty=False,
    ) as writer:
        writer.append_decisions([decision])
        writer.append_episodes([decision])
        result = writer.publish()
    assert result.outcome is SnapshotPublishOutcome.CREATED
    return result.directory, selected_identity.fingerprint()


def test_snapshot_write_read_equivalence(tmp_path: Path) -> None:
    directory, fingerprint = _publish(tmp_path)
    reader = SnapshotReader(directory, fingerprint)
    assert list(reader.iter_decisions()) == [_decision()]
    assert reader.load_episodes() == [_decision()]
    assert reader.load_controls() == {}
    assert reader.load_outcomes() == {}
    assert reader.manifest.identity["contract_hash"] == _identity().contract_hash


def test_fingerprint_changes_with_any_registered_input() -> None:
    assert _identity().fingerprint() != _identity(contract_hash="sha256:other").fingerprint()


def test_reader_rejects_wrong_fingerprint(tmp_path: Path) -> None:
    directory, _fingerprint = _publish(tmp_path)
    with pytest.raises(SnapshotCorruptError, match="fingerprint mismatch"):
        SnapshotReader(directory, "b" * 64)


def test_reader_rejects_corrupt_artifact(tmp_path: Path) -> None:
    directory, fingerprint = _publish(tmp_path)
    (directory / "decisions_snapshot.parquet").write_bytes(b"corrupt")
    with pytest.raises(SnapshotCorruptError, match="artifact hash mismatch"):
        SnapshotReader(directory, fingerprint)


def test_duplicate_decision_ids_fail_closed(tmp_path: Path) -> None:
    identity = _identity()
    decision = _decision()
    with (
        SnapshotWriter(
            tmp_path,
            identity,
            code_revision="deadbeef",
            working_tree_dirty=False,
        ) as writer,
        pytest.raises(SnapshotInputError, match="duplicate identity in decisions"),
    ):
        writer.append_decisions([decision, decision])
        writer.publish()


def test_control_requires_published_primary_episode(tmp_path: Path) -> None:
    identity = _identity()
    primary = _decision("BTCUSDT")
    control = _decision("ETHUSDT")
    with (
        SnapshotWriter(
            tmp_path,
            identity,
            code_revision="deadbeef",
            working_tree_dirty=False,
        ) as writer,
        pytest.raises(SnapshotInputError, match="control without primary episode"),
    ):
        writer.append_decisions([primary, control])
        writer.append_controls(primary, [control])
        writer.publish()


def test_first_writer_wins_and_second_reads_winner(tmp_path: Path) -> None:
    identity = _identity()
    first = _decision("BTCUSDT")
    with SnapshotWriter(
        tmp_path,
        identity,
        code_revision="first",
        working_tree_dirty=False,
    ) as writer:
        writer.append_decisions([first])
        writer.append_episodes([first])
        created = writer.publish()

    with SnapshotWriter(
        tmp_path,
        identity,
        code_revision="second",
        working_tree_dirty=False,
    ) as writer:
        writer.append_decisions([first])
        writer.append_episodes([first])
        loser = writer.publish()

    assert created.outcome is SnapshotPublishOutcome.CREATED
    assert loser.outcome is SnapshotPublishOutcome.ALREADY_EXISTS
    assert loser.manifest.code_revision == "first"
    reader = SnapshotReader(loser.directory, identity.fingerprint())
    assert {decision_id(item) for item in reader.load_episodes()} == {decision_id(first)}


def test_abandoned_staging_does_not_block_retry(tmp_path: Path) -> None:
    identity = _identity()
    decision = _decision()
    with SnapshotWriter(
        tmp_path,
        identity,
        code_revision="crashed",
        working_tree_dirty=False,
    ) as writer:
        writer.append_decisions([decision])

    with SnapshotWriter(
        tmp_path,
        identity,
        code_revision="retry",
        working_tree_dirty=False,
    ) as writer:
        writer.append_decisions([decision])
        writer.append_episodes([decision])
        result = writer.publish()

    assert result.outcome is SnapshotPublishOutcome.CREATED
    assert SnapshotReader(result.directory, identity.fingerprint()).load_episodes() == [decision]
