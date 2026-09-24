from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
from schurfer_analytics import abnormal_flow_snapshot_pipeline as pipeline_module
from schurfer_analytics.abnormal_flow_replay import (
    DecisionFeatures,
    assemble_decisions,
    iter_instrument_bars,
    oi_freshness_limit_for,
)
from schurfer_analytics.abnormal_flow_scan import load_identity_resolver
from schurfer_analytics.abnormal_flow_screen import AbnormalFlowContract
from schurfer_analytics.abnormal_flow_snapshot_pipeline import (
    build_or_reuse_snapshot,
    snapshot_bounds,
)
from schurfer_analytics.abnormal_flow_snapshots import SnapshotReader
from schurfer_analytics.cold_bar_export import (
    EXPORT_VERSION,
    SCHEMA_VERSION,
    SOURCE_TABLE,
    ExportManifest,
    sha256_file,
)

if TYPE_CHECKING:
    import pytest


def _contract() -> AbnormalFlowContract:
    return AbnormalFlowContract(
        contract_version="abnormal_flow_screen_v1",
        lookback_minutes=60,
        outcome_horizon_minutes=720,
        cooldown_minutes=720,
        direction="long",
        min_oi_growth_pct=10.0,
        min_buy_pressure_ratio=0.6,
        max_price_containment=0.05,
        min_oi_notional_usd=100.0,
        oi_usd_conversion_rule="bybit_native_value_binance_amount_x_decision_price_v1",
        position_usd=300.0,
        max_participation_frac=0.1,
        entry_execution_window_minutes=1,
        oi_freshness_limit_seconds_bybit=120,
        oi_freshness_limit_seconds_binance=300,
        calibration_rule="fixed_percentiles_on_prestart_window_v1",
        calibration_window_days=14,
        scan_lag_minutes=2,
        entry_reference="next_bar_open_priced_proxy_v1",
        exit_reference="horizon_bar_close_priced_proxy_v1",
        matching_rule="same_venue_regime_liquidity_pricemove_band_v1",
        controls_per_episode=1,
        portfolio_bank_usd=300.0,
        portfolio_max_slots=1,
        inference_rule="student_t_df_weeks_minus_one_v1",
        window_start_utc="2026-01-01T01:03:00+00:00",
        window_end_utc="2026-01-01T01:04:00+00:00",
        input_fingerprint="sha256:" + "a" * 64,
        taker_fee_bps=5.0,
        entry_slippage_bps=5.0,
        exit_slippage_bps=5.0,
        funding_bps_720m_binance=5.0,
        funding_bps_720m_bybit=5.0,
        min_resolved_episodes=1,
        max_missing_fraction=0.2,
        min_excess_over_control_pct=0.0,
        min_distinct_assets=1,
        min_utc_weeks=1,
        max_episodes_per_asset_frac=1.0,
        max_episodes_per_week_frac=1.0,
        min_control_coverage_frac=1.0,
    )


def _write_contract(path: Path, contract: AbnormalFlowContract) -> None:
    payload = asdict(contract)
    payload["contract_hash"] = contract.compute_hash()
    path.write_text(json.dumps(payload, sort_keys=True))


def _write_identity(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "exchange": "bybit",
                        "market_type": "linear",
                        "native_market_id": symbol,
                        "valid_from": "2025-12-31T00:00:00+00:00",
                        "valid_to": None,
                        "canonical_asset": f"asset:{symbol}",
                        "snapshot_captured_at": "2025-12-31T00:00:00+00:00",
                    }
                    for symbol in ("FIRE", "CONTROL")
                ]
            }
        )
    )


def _write_cold_bars(directory: Path) -> Path:
    directory.mkdir()
    day = date(2026, 1, 1)
    parquet = directory / f"bars-{day.isoformat()}.parquet"
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    for symbol in ("FIRE", "CONTROL"):
        for minute in range(61):
            bucket = start + timedelta(minutes=minute)
            oi = 100.0 + (20.0 * minute / 60.0 if symbol == "FIRE" else 0.0)
            buy = 9_000.0 if symbol == "FIRE" else 5_000.0
            sell = 1_000.0 if symbol == "FIRE" else 5_000.0
            rows.append(
                (
                    "bybit",
                    "linear",
                    symbol,
                    "v1",
                    bucket,
                    bucket + timedelta(seconds=30),
                    100.0,
                    100.1,
                    99.9,
                    100.0,
                    buy,
                    sell,
                    oi,
                    oi * 100.0,
                    bucket,
                    bucket + timedelta(seconds=30),
                    True,
                    True,
                    True,
                )
            )

    with duckdb.connect() as db:
        db.execute(
            """
            CREATE TABLE bars (
                exchange VARCHAR, market_type VARCHAR, symbol VARCHAR,
                capture_version VARCHAR, bucket_start TIMESTAMPTZ,
                created_at TIMESTAMPTZ, open_price DOUBLE, high_price DOUBLE,
                low_price DOUBLE, close_price DOUBLE,
                buy_total_notional_usd DOUBLE, sell_total_notional_usd DOUBLE,
                open_interest DOUBLE, open_interest_value DOUBLE,
                open_interest_observed_at TIMESTAMPTZ,
                last_trade_received_at TIMESTAMPTZ, price_complete BOOLEAN,
                trades_complete BOOLEAN, open_interest_complete BOOLEAN
            )
            """
        )
        db.executemany(
            "INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        db.execute("COPY bars TO ? (FORMAT PARQUET)", [str(parquet)])

    fingerprint = "cbfp_v1:" + "a" * 64
    manifest = ExportManifest(
        schema_version=SCHEMA_VERSION,
        export_version=EXPORT_VERSION,
        source_table=SOURCE_TABLE,
        day=day.isoformat(),
        bucket_start_from=start.isoformat(),
        bucket_start_until=(start + timedelta(days=1)).isoformat(),
        row_count=len(rows),
        file_name=parquet.name,
        file_bytes=parquet.stat().st_size,
        sha256=sha256_file(parquet),
        data_keys=(),
        exported_at=start.isoformat(),
        source_fingerprint=fingerprint,
        file_fingerprint=fingerprint,
        fidelity_verified=True,
    )
    (directory / f"bars-{day.isoformat()}.manifest.json").write_text(manifest.to_json())
    return parquet


def _legacy_decisions(
    parquet: Path,
    identity_path: Path,
    contract: AbnormalFlowContract,
) -> list[DecisionFeatures]:
    resolver, _ = load_identity_resolver(identity_path)
    bounds = snapshot_bounds(contract)
    decisions: list[DecisionFeatures] = []
    for bars in iter_instrument_bars(
        [str(parquet)],
        window_start=bounds.feature_start,
        window_end=bounds.evaluation_end,
    ):
        first = bars[0]
        freshness = oi_freshness_limit_for(contract, first.exchange)
        assert freshness is not None

        def resolve(
            at: datetime,
            exchange: str = first.exchange,
            market_type: str = first.market_type,
            native_market_id: str = first.native_market_id,
            capture_version: str = first.capture_version,
        ) -> str | None:
            return resolver.identity_key(
                exchange,
                market_type,
                native_market_id,
                capture_version,
                at,
            )

        decisions.extend(
            assemble_decisions(
                bars,
                scan_lag_minutes=contract.scan_lag_minutes or 0,
                entry_execution_window_minutes=contract.entry_execution_window_minutes or 0,
                oi_freshness_limit_seconds=freshness,
                resolve_canonical=resolve,
            )
        )
    return sorted(decisions, key=lambda item: item.native_market_id)


def test_real_parquet_pipeline_is_equivalent_and_cache_hit_skips_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = _contract()
    contract_path = tmp_path / "contract.json"
    identity_path = tmp_path / "identity.json"
    artifact_root = tmp_path / "artifacts"
    cold_bars = tmp_path / "cold-bars"
    _write_contract(contract_path, contract)
    _write_identity(identity_path)
    parquet = _write_cold_bars(cold_bars)

    expected = _legacy_decisions(parquet, identity_path, contract)
    first = build_or_reuse_snapshot(
        contract_path=contract_path,
        identity_snapshot_path=identity_path,
        cold_bars_dir=cold_bars,
        artifact_root=artifact_root,
        code_revision="deadbeef",
        working_tree_dirty=False,
    )
    assert first.cache_hit is False
    reader = SnapshotReader(Path(first.snapshot_dir), first.fingerprint)
    assert sorted(reader.iter_decisions(), key=lambda item: item.native_market_id) == expected
    assert [item.native_market_id for item in reader.load_episodes()] == ["FIRE"]
    controls = reader.load_controls()
    assert [[item.native_market_id for item in items] for items in controls.values()] == [
        ["CONTROL"]
    ]
    assert reader.manifest.artifacts["outcomes"].row_count == 0

    def fail_if_scanned(*args: object, **kwargs: object) -> object:
        raise AssertionError("cache hit must not scan Parquet rows")

    monkeypatch.setattr(pipeline_module, "iter_instrument_bars", fail_if_scanned)
    second = build_or_reuse_snapshot(
        contract_path=contract_path,
        identity_snapshot_path=identity_path,
        cold_bars_dir=cold_bars,
        artifact_root=artifact_root,
        code_revision="other",
        working_tree_dirty=True,
    )
    assert second.cache_hit is True
    assert second.scan_seconds == 0.0
    assert second.fingerprint == first.fingerprint
