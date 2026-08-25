from datetime import UTC, datetime, timedelta
from pathlib import Path

from schurfer_analytics.early_momentum_net_evidence import EpisodeRow, RawDataset
from schurfer_analytics.early_momentum_net_evidence_dataset_artifact import freeze, read
from schurfer_analytics.research_dataset_artifact import ArtifactWriteOutcome


def test_round_trip_preserves_complete_raw_dataset_and_registration(tmp_path: Path) -> None:
    start = datetime(2026, 8, 25, tzinfo=UTC)
    episode = EpisodeRow(
        episode_id="episode-1",
        strategy_id=1,
        contract_sha256=b"a" * 32,
        exchange="bybit",
        native_market_id="BTCUSDT",
        execution_symbol="BTC/USDT:USDT",
        source_exchange="binance",
        source_native_id="BTCUSDT",
        execution_identity_key="exec",
        source_identity_key="source",
        cluster_key="btc",
        armed_at=start + timedelta(minutes=1),
        expires_at=start + timedelta(hours=1),
        status="expired",
        terminal_reason="expired",
        claimed_at=None,
        claim_expires_at=None,
        claim_attempts=0,
    )
    dataset = RawDataset(
        cohort_start=start,
        cohort_end=start + timedelta(days=1),
        db_snapshot_at=start + timedelta(days=2),
        episodes=(episode,),
        trades=(),
        exit_liquidity=(),
    )
    registration = {
        "cohort_key": "early_momentum_v4_prospective_v1",
        "strategy_name": "early_momentum",
        "strategy_version": "4",
        "contract_sha256": "aa",
        "runtime_policy_sha256": "bb",
        "cohort_started_at": start.isoformat(),
    }

    outcome, manifest = freeze(
        dataset,
        (),
        registration=registration,
        code_revision="abc123",
        working_tree_dirty=False,
        directory=str(tmp_path),
    )

    assert outcome is ArtifactWriteOutcome.CREATED
    assert manifest is not None
    read_manifest, restored, legacy, restored_registration = read(
        manifest.fingerprint, directory=str(tmp_path)
    )
    assert read_manifest.fingerprint == manifest.fingerprint
    assert restored == dataset
    assert legacy == ()
    assert restored_registration == registration


def test_empty_dataset_is_a_valid_explicit_prospective_checkpoint(tmp_path: Path) -> None:
    start = datetime(2026, 8, 25, tzinfo=UTC)
    dataset = RawDataset(start, start + timedelta(hours=1), start + timedelta(hours=7), (), (), ())
    outcome, manifest = freeze(
        dataset,
        (),
        registration={"cohort_started_at": start.isoformat()},
        code_revision="abc123",
        working_tree_dirty=False,
        directory=str(tmp_path),
    )
    assert outcome is ArtifactWriteOutcome.CREATED
    assert manifest is not None
    _, restored, _, _ = read(manifest.fingerprint, directory=str(tmp_path))
    assert restored == dataset


def test_registration_is_part_of_fingerprint_even_for_empty_dataset(tmp_path: Path) -> None:
    start = datetime(2026, 8, 25, tzinfo=UTC)
    dataset = RawDataset(start, start + timedelta(hours=1), start + timedelta(hours=7), (), (), ())
    registration_a = {
        "cohort_started_at": start.isoformat(),
        "runtime_policy_sha256": "aa" * 32,
    }
    registration_b = {**registration_a, "runtime_policy_sha256": "bb" * 32}

    _, manifest_a = freeze(
        dataset,
        (),
        registration=registration_a,
        code_revision="abc123",
        working_tree_dirty=False,
        directory=str(tmp_path),
    )
    _, manifest_b = freeze(
        dataset,
        (),
        registration=registration_b,
        code_revision="abc123",
        working_tree_dirty=False,
        directory=str(tmp_path),
    )

    assert manifest_a is not None
    assert manifest_b is not None
    assert manifest_a.fingerprint != manifest_b.fingerprint
