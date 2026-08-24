from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from schurfer_analytics.exit_liquidity_calibration_dataset_artifact import (
    WrongDatasetArtifactError,
    freeze,
    read,
)
from schurfer_analytics.exit_liquidity_calibration_report import (
    EXIT_LIQUIDITY_COHORT_START,
    ExitLiquidityFilters,
    ExitLiquidityRow,
)
from schurfer_analytics.research_dataset_artifact import (
    ArtifactWriteOutcome,
    write_dataset_artifact,
)

if TYPE_CHECKING:
    from pathlib import Path


def _row(trade_id: int, *, observation: bool = True) -> ExitLiquidityRow:
    exit_at = EXIT_LIQUIDITY_COHORT_START + timedelta(hours=trade_id)
    return ExitLiquidityRow(
        trade_id=trade_id,
        symbol="COTI/USDT:USDT",
        exchange="binance",
        size_usd=50,
        entry_at=exit_at - timedelta(hours=3),
        exit_at=exit_at,
        exit_reason="max_hold",
        modeled_exit_bps=5.0,
        observation_id=trade_id if observation else None,
        observed_at=exit_at - timedelta(seconds=1) if observation else None,
        observation_exchange="binance" if observation else None,
        observation_symbol="COTI/USDT:USDT" if observation else None,
        observation_status="sampled" if observation else None,
        requested_notional_usd=50 if observation else None,
        filled_notional_usd=50 if observation else None,
        observed_spread_bps=4 if observation else None,
        observed_exit_bps=8 if observation else None,
        latency_ms=120 if observation else None,
        error=None,
    )


def _filters() -> ExitLiquidityFilters:
    return ExitLiquidityFilters(
        since=EXIT_LIQUIDITY_COHORT_START, until=EXIT_LIQUIDITY_COHORT_START + timedelta(days=1)
    )


def test_freeze_then_read_round_trips_rows_exactly(tmp_path: Path) -> None:
    rows = (_row(1), _row(2, observation=False))

    outcome, manifest = freeze(
        rows, _filters(), code_revision="abc123", working_tree_dirty=False, directory=str(tmp_path)
    )

    assert outcome == ArtifactWriteOutcome.CREATED
    assert manifest is not None
    assert manifest.row_count == 2
    assert manifest.dataset_name == "exit_liquidity_calibration"

    read_manifest, read_filters, read_rows = read(manifest.fingerprint, directory=str(tmp_path))
    assert read_rows == rows
    # datetimes specifically -- the field most at risk of a lossy round trip
    # through a string intermediate representation.
    assert read_rows[0].exit_at.tzinfo is not None
    assert read_rows[0].exit_at == rows[0].exit_at
    assert read_rows[1].observed_at is None
    assert read_manifest.fingerprint == manifest.fingerprint
    assert read_filters == _filters()


def test_freeze_is_idempotent_for_the_same_rows_and_cohort(tmp_path: Path) -> None:
    rows = (_row(1),)
    outcome1, manifest1 = freeze(
        rows, _filters(), code_revision="rev-a", working_tree_dirty=False, directory=str(tmp_path)
    )
    outcome2, manifest2 = freeze(
        rows, _filters(), code_revision="rev-b", working_tree_dirty=True, directory=str(tmp_path)
    )

    assert outcome1 == ArtifactWriteOutcome.CREATED
    assert outcome2 == ArtifactWriteOutcome.ALREADY_EXISTS
    assert manifest1 is not None
    assert manifest2 is not None
    assert manifest1.fingerprint == manifest2.fingerprint


def test_different_cohort_gives_different_fingerprint(tmp_path: Path) -> None:
    rows = (_row(1),)
    _, manifest1 = freeze(
        rows, _filters(), code_revision="abc123", working_tree_dirty=False, directory=str(tmp_path)
    )
    other_filters = ExitLiquidityFilters(
        since=EXIT_LIQUIDITY_COHORT_START, until=EXIT_LIQUIDITY_COHORT_START + timedelta(days=2)
    )
    _, manifest2 = freeze(
        rows,
        other_filters,
        code_revision="abc123",
        working_tree_dirty=False,
        directory=str(tmp_path),
    )

    assert manifest1 is not None
    assert manifest2 is not None
    assert manifest1.fingerprint != manifest2.fingerprint


def test_read_rejects_an_artifact_from_a_different_dataset(tmp_path: Path) -> None:
    """Regression: read() used to trust any well-formed artifact handed to
    it regardless of what dataset it actually claimed to be -- a fingerprint
    from a differently-shaped (or just differently-named) dataset with
    row dicts that happen to overlap enough to construct an ExitLiquidityRow
    would previously be silently accepted (colleague review, 2026-08-25)."""
    outcome, foreign_manifest = write_dataset_artifact(
        dataset_name="some_other_dataset",
        dataset_version="v1",
        schema_version="whatever",
        rows=[{"trade_id": 1, "symbol": "X"}],
        row_id_field="trade_id",
        row_order="trade_id ascending",
        cohort={"since": "2026-01-01", "until": "2026-02-01"},
        code_revision="abc123",
        working_tree_dirty=False,
        directory=str(tmp_path),
    )
    assert outcome == ArtifactWriteOutcome.CREATED
    assert foreign_manifest is not None

    with pytest.raises(WrongDatasetArtifactError, match="some_other_dataset"):
        read(foreign_manifest.fingerprint, directory=str(tmp_path))
