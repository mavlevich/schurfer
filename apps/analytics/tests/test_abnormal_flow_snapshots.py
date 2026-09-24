from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import duckdb
import pytest
from schurfer_analytics.abnormal_flow_replay import DecisionFeatures, Outcome
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


def _outcome(decision: DecisionFeatures) -> Outcome:
    return Outcome(
        exchange=decision.exchange,
        market_type=decision.market_type,
        native_market_id=decision.native_market_id,
        capture_version=decision.capture_version,
        symbol=decision.symbol,
        decision_at=decision.decision_at,
        entry_price=10.0,
        exit_price=11.0,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def test_bulk_decision_stage_preserves_null_and_empty_string(tmp_path: Path) -> None:
    identity = _identity()
    missing = replace(_decision("MISSING"), oi_native_value_usd=None, unavailable_reason=None)
    empty = replace(_decision("EMPTY"), unavailable_reason="")
    with SnapshotWriter(
        tmp_path,
        identity,
        code_revision="deadbeef",
        working_tree_dirty=False,
    ) as writer:
        writer.append_decisions([missing, empty])
        assert list(writer.iter_decisions()) == [empty, missing]
        with pytest.raises(SnapshotInputError, match="after the decision stage is sealed"):
            writer.append_decisions([_decision("LATE")])
        result = writer.publish()

    assert list(SnapshotReader(result.directory, identity.fingerprint()).iter_decisions()) == [
        empty,
        missing,
    ]


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


def test_episode_payload_must_match_decision(tmp_path: Path) -> None:
    identity = _identity()
    decision = _decision()
    with (
        SnapshotWriter(
            tmp_path,
            identity,
            code_revision="deadbeef",
            working_tree_dirty=False,
        ) as writer,
        pytest.raises(SnapshotInputError, match="episode payload differs"),
    ):
        writer.append_decisions([decision])
        writer.append_episodes([replace(decision, canonical_asset="asset:WRONG")])
        writer.publish()


def test_control_payload_must_match_decision(tmp_path: Path) -> None:
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
        pytest.raises(SnapshotInputError, match="control payload differs"),
    ):
        writer.append_decisions([primary, control])
        writer.append_episodes([primary])
        writer.append_controls(primary, [replace(control, symbol="WRONG")])
        writer.publish()


def test_outcome_payload_and_role_must_match_requested_decision(tmp_path: Path) -> None:
    identity = _identity()
    decision = _decision()
    mismatched = replace(decision, symbol="WRONG")
    with (
        SnapshotWriter(
            tmp_path / "payload",
            identity,
            code_revision="deadbeef",
            working_tree_dirty=False,
        ) as writer,
        pytest.raises(SnapshotInputError, match="outcome payload differs"),
    ):
        writer.append_decisions([decision])
        writer.append_episodes([decision])
        writer.append_outcome(mismatched, "primary", _outcome(mismatched), None)
        writer.publish()

    with (
        SnapshotWriter(
            tmp_path / "role",
            identity,
            code_revision="deadbeef",
            working_tree_dirty=False,
        ) as writer,
        pytest.raises(SnapshotInputError, match="outcome role does not match"),
    ):
        writer.append_decisions([decision])
        writer.append_episodes([decision])
        writer.append_outcome(decision, "control", _outcome(decision), None)
        writer.publish()


def test_outcome_cannot_have_exit_without_entry(tmp_path: Path) -> None:
    identity = _identity()
    decision = _decision()
    with (
        SnapshotWriter(
            tmp_path,
            identity,
            code_revision="deadbeef",
            working_tree_dirty=False,
        ) as writer,
        pytest.raises(SnapshotInputError, match="ambiguous outcome completeness"),
    ):
        writer.append_decisions([decision])
        writer.append_episodes([decision])
        writer.append_outcome(
            decision,
            "primary",
            replace(_outcome(decision), entry_price=None),
            "missing_entry",
        )
        writer.publish()


def test_reader_rejects_rehashed_episode_with_mismatched_payload(tmp_path: Path) -> None:
    directory, fingerprint = _publish(tmp_path)
    episodes_path = directory / "episodes_snapshot.parquet"
    replacement = directory / "episodes-replacement.parquet"
    with duckdb.connect() as db:
        db.execute("CREATE TABLE episodes AS SELECT * FROM read_parquet(?)", [str(episodes_path)])
        db.execute("UPDATE episodes SET canonical_asset = 'asset:WRONG'")
        db.execute("COPY episodes TO ? (FORMAT PARQUET, COMPRESSION ZSTD)", [str(replacement)])
    replacement.replace(episodes_path)

    manifest_path = directory / "snapshot_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["artifacts"]["episodes"]["sha256"] = _sha256(episodes_path)
    manifest_json = json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False)
    manifest_path.write_text(manifest_json)
    (directory / "snapshot_manifest.sha256").write_text(
        hashlib.sha256(manifest_json.encode()).hexdigest()
    )

    with pytest.raises(SnapshotCorruptError, match="episode payload differs"):
        SnapshotReader(directory, fingerprint)


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
