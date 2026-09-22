import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import duckdb
import pytest
from schurfer_analytics.abnormal_flow_formal_runner import FormalRunner, GitStateProvider


class MockGitState(GitStateProvider):
    def __init__(self, dirty: bool = False, rev: str = "deadbeef"):
        self.dirty = dirty
        self.rev = rev

    def get_revision(self) -> str:
        return self.rev

    def is_dirty(self) -> bool:
        return self.dirty


def test_import_does_not_enable_returns():
    import schurfer_analytics.abnormal_flow_replay as replay

    assert not replay.FORMAL_RETURNS_RUN_ENABLED


def test_runner_requires_formal_run_flag(tmp_path: Path):
    runner = FormalRunner(MockGitState(dirty=False))
    p = tmp_path / "f"
    p.touch()
    with (
        patch("sys.argv", ["dummy"]),
        pytest.raises(ValueError, match="--formal-run flag required"),
    ):
        runner.run(p, p, p, p, p, p, p, p, require_formal_run=True)


def test_runner_dirty_tree_rejected(tmp_path: Path):
    runner = FormalRunner(MockGitState(dirty=True))
    p = tmp_path / "f"
    p.touch()
    with pytest.raises(ValueError, match="Dirty tree detected"):
        runner.run(p, p, p, p, p, p, p, p, require_formal_run=False)


@pytest.fixture
def run_env(tmp_path: Path) -> dict[str, Any]:
    env = {}

    contract = {
        "lookback_minutes": 60,
        "outcome_horizon_minutes": 720,
        "cooldown_minutes": 720,
        "direction": "long",
        "scan_lag_minutes": 5,
        "entry_execution_window_minutes": 1,
        "oi_freshness_limit_seconds_bybit": 60,
        "oi_freshness_limit_seconds_binance": 60,
        "min_oi_growth_pct": 10.0,
        "min_buy_pressure_ratio": 0.6,
        "max_price_containment": 5.0,
        "min_oi_notional_usd": 100.0,
        "oi_usd_conversion_rule": "bybit_native_value_binance_amount_x_decision_price_v1",
        "position_usd": 1000.0,
        "max_participation_frac": 0.1,
        "calibration_rule": "fixed_percentiles_on_prestart_window_v1",
        "calibration_window_days": 10,
        "entry_reference": "next_bar_open_priced_proxy_v1",
        "exit_reference": "horizon_bar_close_priced_proxy_v1",
        "matching_rule": "same_venue_regime_liquidity_pricemove_band_v1",
        "inference_rule": "normal_1_96_v1",
        "controls_per_episode": 2,
        "portfolio_bank_usd": 10000.0,
        "portfolio_max_slots": 10,
        "window_start_utc": "2026-08-20T00:00:00Z",
        "window_end_utc": "2026-08-21T00:00:00Z",
        "input_fingerprint": "fake",
        "taker_fee_bps": 5.0,
        "entry_slippage_bps": 5.0,
        "exit_slippage_bps": 5.0,
        "funding_bps_720m_binance": 5.0,
        "funding_bps_720m_bybit": 5.0,
        "min_resolved_episodes": 2,
        "max_missing_fraction": 0.1,
        "min_excess_over_control_pct": 0.1,
        "min_distinct_assets": 1,
        "min_utc_weeks": 1,
        "max_episodes_per_asset_frac": 1.0,
        "max_episodes_per_week_frac": 1.0,
        "min_control_coverage_frac": 0.1,
    }

    c_path = tmp_path / "contract.json"
    c_path.write_text(json.dumps(contract))
    env["contract"] = c_path

    ident_path = tmp_path / "ident.json"
    ident_path.write_text('{"snapshot_at": "2026-08-20T00:00:00Z", "records": []}')
    env["ident"] = ident_path

    fund_path = tmp_path / "fund.json"
    fund_path.write_text("{}")
    env["fund"] = fund_path

    set_path = tmp_path / "set.json"
    set_path.write_text("{}")
    env["set"] = set_path

    bars_dir = tmp_path / "bars"
    bars_dir.mkdir()
    env["bars_dir"] = bars_dir

    days = ["2026-08-19", "2026-08-20", "2026-08-21"]
    scan_days = []

    import hashlib

    def _shasum(p: Path) -> str:
        return f"sha256:{hashlib.sha256(p.read_bytes()).hexdigest()}"

    for d in days:
        p = bars_dir / f"bars-{d}.parquet"
        conn = duckdb.connect()
        conn.execute(f"""
            CREATE TABLE bars AS SELECT
                'bybit' as exchange, 'linear' as market_type,
                'FOO' as native_market_id, 'v1' as capture_version, 'FOO' as symbol,
                CAST('{d}T00:00:00Z' AS TIMESTAMP) as bucket_start,
                100.0 as last_bid_price, 100.1 as last_ask_price,
                100.05 as last_trade_price, 1.0 as last_trade_size,
                0.0 as buy_total_notional_usd, 0.0 as sell_total_notional_usd,
                1000.0 as open_interest, 100000.0 as open_interest_value,
                CAST('{d}T00:00:00Z' AS TIMESTAMP) as open_interest_observed_at,
                CAST('{d}T00:00:00Z' AS TIMESTAMP) as last_trade_received_at,
                true as price_complete, true as trades_complete, true as open_interest_complete,
                CAST('{d}T00:00:00Z' AS TIMESTAMP) as created_at,
                CAST('{d}T00:00:00Z' AS TIMESTAMP) as open_interest_event_at
        """)
        conn.execute(f"COPY bars TO '{p}' (FORMAT PARQUET)")
        conn.close()

        m = {
            "day": d,
            "file_name": p.name,
            "row_count": 1,
            "sha256": "fake",
            "source_fingerprint": "fake",
            "bucket_start_from": f"{d}T00:00:00+00:00",
            "bucket_start_until": f"{(datetime.fromisoformat(d) + timedelta(days=1)).date()}T00:00:00+00:00",  # noqa: E501
            "source_table": "raw_bars",
            "export_version": "v1",
            "manifest_version": "cold_bars_v1",
            "fidelity_verified": True,
        }
        (bars_dir / f"bars-{d}.manifest.json").write_text(json.dumps(m))
        scan_days.append({"day": d, "sha256": "fake", "source_fingerprint": "fake"})

    scan_path = tmp_path / "scan.json"
    scan_path.write_text(json.dumps({"days": scan_days}))
    env["scan"] = scan_path

    eval_m = {
        "identity_snapshot_hash": _shasum(ident_path),
        "funding_snapshot_hash": _shasum(fund_path),
        "funding_settlements_hash": _shasum(set_path),
        "input_audit_fingerprint": _shasum(scan_path),
    }

    eval_path = tmp_path / "eval.json"
    eval_path.write_text(json.dumps(eval_m))
    env["eval"] = eval_path

    contract["input_fingerprint"] = eval_m["input_audit_fingerprint"]
    c_path.write_text(json.dumps(contract))

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    env["out"] = out_dir

    return env


def test_runner_deterministic_underpowered(run_env: dict[str, Any]):
    runner = FormalRunner(MockGitState(dirty=False))

    class MockManifest:
        sha256 = "fake"
        source_fingerprint = "fake"

    def _verified(d, day):
        return d / f"bars-{day.isoformat()}.parquet", MockManifest()

    with (
        patch("schurfer_analytics.abnormal_flow_formal_runner.parquet_outcome_reader") as spy,
        patch("schurfer_analytics.abnormal_flow_formal_runner.verified_input") as mock_verified,
    ):
        mock_verified.side_effect = _verified
        runner.run(
            run_env["contract"],
            run_env["eval"],
            run_env["scan"],
            run_env["ident"],
            run_env["fund"],
            run_env["set"],
            run_env["bars_dir"],
            run_env["out"],
            require_formal_run=False,
        )
        assert spy.call_count == 1

    dirs = list(run_env["out"].iterdir())
    assert len(dirs) == 1
    report = json.loads((dirs[0] / "formal_run_report.json").read_text())
    assert report["economics"]["verdict"] == "INSUFFICIENT_EVIDENCE"
