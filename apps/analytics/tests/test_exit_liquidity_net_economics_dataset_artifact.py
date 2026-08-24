from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from schurfer_analytics.exit_liquidity_calibration_report import (
    EXIT_LIQUIDITY_COHORT_START,
    ExitLiquidityFilters,
)
from schurfer_analytics.exit_liquidity_net_economics import NetEconomicsRow
from schurfer_analytics.exit_liquidity_net_economics_dataset_artifact import (
    WrongDatasetArtifactError,
    freeze,
    read,
)
from schurfer_analytics.research_dataset_artifact import (
    ArtifactWriteOutcome,
    write_dataset_artifact,
)
from schurfer_performance import PAPER_ACCOUNTING_VERSION

if TYPE_CHECKING:
    from pathlib import Path


def _row(trade_id: int) -> NetEconomicsRow:
    exit_at = EXIT_LIQUIDITY_COHORT_START + timedelta(hours=trade_id)
    return NetEconomicsRow(
        trade_id=trade_id,
        episode_id=None,
        strategy_name="pump_short",
        strategy_version="1",
        symbol="COTI/USDT:USDT",
        exchange="binance",
        side="short",
        entry_at=exit_at - timedelta(hours=1),
        exit_at=exit_at,
        exit_reason="max_hold age=60min",
        size_usd=50.0,
        leverage=5.0,
        entry_price=1.0,
        exit_price=0.99,
        recorded_gross_pnl_usd=0.5,
        recorded_net_pnl_usd=0.20,
        fees_usd=0.05,
        funding_usd=0.01,
        entry_slippage_bps=2.0,
        modeled_exit_bps=5.0,
        accounting_version=PAPER_ACCOUNTING_VERSION,
        accounting_status="complete",
        accounting_error=None,
        observation_id=trade_id,
        observed_at=exit_at - timedelta(seconds=1),
        observation_exchange="binance",
        observation_symbol="COTI/USDT:USDT",
        observation_status="sampled",
        requested_notional_usd=50.0,
        filled_notional_usd=50.0,
        observed_mid=0.995,
        observed_spread_bps=4.0,
        observed_exit_bps=6.0,
        observed_ask_vwap=0.996,
        latency_ms=100,
        error=None,
    )


def _filters() -> ExitLiquidityFilters:
    return ExitLiquidityFilters(
        since=EXIT_LIQUIDITY_COHORT_START, until=EXIT_LIQUIDITY_COHORT_START + timedelta(days=1)
    )


def test_freeze_then_read_round_trips_rows_exactly(tmp_path: Path) -> None:
    rows = (_row(1), _row(2))

    outcome, manifest = freeze(
        rows, _filters(), code_revision="abc123", working_tree_dirty=False, directory=str(tmp_path)
    )

    assert outcome == ArtifactWriteOutcome.CREATED
    assert manifest is not None
    assert manifest.row_count == 2
    assert manifest.dataset_name == "exit_liquidity_net_economics"

    read_manifest, read_filters, read_rows = read(manifest.fingerprint, directory=str(tmp_path))
    assert read_rows == rows
    assert read_rows[0].exit_at == rows[0].exit_at
    assert read_manifest.fingerprint == manifest.fingerprint
    assert read_filters == _filters()


def test_freeze_is_idempotent(tmp_path: Path) -> None:
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


def test_read_rejects_an_artifact_from_a_different_dataset(tmp_path: Path) -> None:
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


def test_different_dataset_artifact_never_collides_with_exit_liquidity_calibration(
    tmp_path: Path,
) -> None:
    """Regression-ish sanity check: this is a genuinely different dataset
    from exit_liquidity_calibration_dataset_artifact.py's own
    `exit_liquidity_calibration` -- same cohort window, same underlying
    trades, but different dataset_name/schema must never fingerprint-
    collide."""
    from schurfer_analytics.exit_liquidity_calibration_dataset_artifact import (
        freeze as freeze_calibration,
    )
    from schurfer_analytics.exit_liquidity_calibration_report import ExitLiquidityRow

    calibration_row = ExitLiquidityRow(
        trade_id=1,
        symbol="COTI/USDT:USDT",
        exchange="binance",
        size_usd=50,
        entry_at=EXIT_LIQUIDITY_COHORT_START,
        exit_at=EXIT_LIQUIDITY_COHORT_START + timedelta(hours=1),
        exit_reason="max_hold",
        modeled_exit_bps=5.0,
        observation_id=1,
        observed_at=EXIT_LIQUIDITY_COHORT_START + timedelta(minutes=59),
        observation_exchange="binance",
        observation_symbol="COTI/USDT:USDT",
        observation_status="sampled",
        requested_notional_usd=50,
        filled_notional_usd=50,
        observed_spread_bps=4,
        observed_exit_bps=8,
        latency_ms=120,
        error=None,
    )
    _, calibration_manifest = freeze_calibration(
        (calibration_row,),
        _filters(),
        code_revision="abc123",
        working_tree_dirty=False,
        directory=str(tmp_path),
    )
    _, net_economics_manifest = freeze(
        (_row(1),),
        _filters(),
        code_revision="abc123",
        working_tree_dirty=False,
        directory=str(tmp_path),
    )
    assert calibration_manifest is not None
    assert net_economics_manifest is not None
    assert calibration_manifest.fingerprint != net_economics_manifest.fingerprint
