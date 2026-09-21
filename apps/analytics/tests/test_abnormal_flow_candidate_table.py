"""Tests for the candidate-table power-floor selection (pure)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import schurfer_analytics.abnormal_flow_candidate_table as candidate_table
from schurfer_analytics.abnormal_flow_candidate_table import (
    build_candidate_table,
    select_oi_percentile,
)
from schurfer_analytics.abnormal_flow_replay import DecisionFeatures
from schurfer_analytics.abnormal_flow_screen import AbnormalFlowContract


def _rows() -> list[dict[str, object]]:
    # Higher OI percentile -> fewer, more-selective episodes.
    return [
        {"oi_percentile": 0.90, "projected_evaluation_episodes": 900.0},
        {"oi_percentile": 0.95, "projected_evaluation_episodes": 300.0},
        {"oi_percentile": 0.975, "projected_evaluation_episodes": 160.0},
        {"oi_percentile": 0.99, "projected_evaluation_episodes": 80.0},
    ]


def test_selects_highest_percentile_clearing_the_power_floor() -> None:
    # P99 (80) fails 150; P97.5 (160) clears -> pick the most selective that still clears.
    assert select_oi_percentile(_rows(), 150.0) == 0.975


def test_stricter_floor_pushes_selection_lower() -> None:
    # Floor 350: only P90 (900) and... P95 is 300 < 350, so P90.
    assert select_oi_percentile(_rows(), 350.0) == 0.90


def test_returns_none_when_nothing_clears() -> None:
    assert select_oi_percentile(_rows(), 10_000.0) is None


def _decision(
    *, minute: int, oi_growth_pct: float, canonical_asset: str, exchange: str = "bybit"
) -> DecisionFeatures:
    return DecisionFeatures(
        exchange=exchange,
        market_type="linear",
        native_market_id=f"{canonical_asset}USDT",
        capture_version="v1",
        symbol=f"{canonical_asset}/USDT:USDT",
        canonical_asset=canonical_asset,
        decision_at=datetime(2026, 8, 16, 0, minute, tzinfo=UTC),
        oi_growth_pct=oi_growth_pct,
        buy_pressure=0.9,
        containment=0.001,
        oi_native_amount=1_000_000.0,
        oi_native_value_usd=1_000_000.0 if exchange == "bybit" else None,
        decision_price=1.0,
        pre_decision_turnover_usd=10_000.0,
        iso_week="2026-W33",
    )


def test_build_candidate_table_forms_episodes_from_real_decision_shape(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    decisions = [
        _decision(minute=0, oi_growth_pct=10.0, canonical_asset="A"),
        _decision(minute=1, oi_growth_pct=11.0, canonical_asset="A"),
        _decision(minute=2, oi_growth_pct=12.0, canonical_asset="B", exchange="binance"),
    ]
    monkeypatch.setattr(candidate_table, "_iter_decisions", lambda *args, **kwargs: iter(decisions))
    contract = AbnormalFlowContract(
        min_oi_growth_pct=5.0,
        min_buy_pressure_ratio=0.6,
        max_price_containment=0.1,
        min_oi_notional_usd=50_000.0,
        position_usd=300.0,
        max_participation_frac=0.1,
        entry_execution_window_minutes=5,
        oi_freshness_limit_seconds_bybit=120,
        oi_freshness_limit_seconds_binance=300,
        scan_lag_minutes=2,
    )

    progress: list[str] = []
    result = build_candidate_table(
        ["unused.parquet"],
        calib_start=datetime(2026, 8, 16, tzinfo=UTC),
        calib_end=datetime(2026, 8, 30, tzinfo=UTC),
        eval_scorable_days=19.5,
        contract=contract,
        resolver=lambda *args: "resolved",
        buy_pct=0.0,
        containment_pct=1.0,
        floor_pct=0.0,
        oi_pcts=(0.0,),
        min_projected=1.0,
        progress=progress.append,
    )

    row = result["rows"][0]
    assert row["calibration_fires"] >= 1
    assert row["calibration_episodes"] == 2
    assert row["distinct_identity_keys"] == 2
    assert row["calibration_fires_by_venue"] == {"binance": 1, "bybit": 2}
    assert row["calibration_episodes_by_venue"] == {"binance": 1, "bybit": 1}
    assert row["calibration_episodes_by_week"] == {"2026-W33": 2}
    assert row["identity_keys_by_venue"] == {"binance": 1, "bybit": 1}
    assert row["venues"] == ["binance", "bybit"]
    assert row["weeks"] == ["2026-W33"]
    assert progress == [
        "candidate-table pass 1/3 complete (OI-notional floor)",
        "candidate-table pass 2/3 complete (feature thresholds)",
        "candidate-table pass 3/3 complete (candidate fires)",
    ]


def test_build_candidate_table_refuses_empty_calibration(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(candidate_table, "_iter_decisions", lambda *args, **kwargs: iter(()))
    contract = AbnormalFlowContract(position_usd=300.0, max_participation_frac=0.1)

    with pytest.raises(ValueError, match="no available OI-notional"):
        build_candidate_table(
            ["unused.parquet"],
            calib_start=datetime(2026, 8, 16, tzinfo=UTC),
            calib_end=datetime(2026, 8, 30, tzinfo=UTC),
            eval_scorable_days=19.5,
            contract=contract,
            resolver=lambda *args: "resolved",
        )
