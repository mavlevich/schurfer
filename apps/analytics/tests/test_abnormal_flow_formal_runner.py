from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import duckdb
import pytest
from schurfer_analytics import abnormal_flow_formal_runner as runner_module
from schurfer_analytics.abnormal_flow_formal_runner import (
    FormalRunner,
    GitStateProvider,
    _authorize_formal_run,
    dependency_bounds,
)
from schurfer_analytics.abnormal_flow_replay import (
    DecisionFeatures,
    EvaluationManifest,
    MinuteBar,
    Outcome,
)
from schurfer_analytics.abnormal_flow_screen import AbnormalFlowContract
from schurfer_analytics.cold_bar_export import (
    EXPORT_VERSION,
    SCHEMA_VERSION,
    SOURCE_TABLE,
    ExportManifest,
    sha256_file,
)


class MockGitState(GitStateProvider):
    def __init__(self, *, dirty: bool = False, revision: str = "deadbeef") -> None:
        self.dirty = dirty
        self.revision = revision

    def get_revision(self) -> str:
        return self.revision

    def is_dirty(self) -> bool:
        return self.dirty


def _sha(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _decision(
    symbol: str,
    *,
    oi_growth: float,
    decision_at: datetime = datetime(2026, 8, 20, 0, 0, tzinfo=UTC),
) -> DecisionFeatures:
    return DecisionFeatures(
        exchange="bybit",
        market_type="linear",
        native_market_id=symbol,
        capture_version="v1",
        symbol=symbol,
        canonical_asset=f"asset:{symbol}",
        decision_at=decision_at,
        oi_growth_pct=oi_growth,
        buy_pressure=0.8,
        containment=0.01,
        oi_native_amount=100.0,
        oi_native_value_usd=100_000.0,
        decision_price=100.0,
        pre_decision_turnover_usd=20_000.0,
        iso_week="2026-W34",
    )


def _bar(symbol: str) -> MinuteBar:
    moment = datetime(2026, 8, 19, 23, 0, tzinfo=UTC)
    return MinuteBar(
        exchange="bybit",
        market_type="linear",
        native_market_id=symbol,
        capture_version="v1",
        symbol=symbol,
        bucket_start=moment,
        created_at=moment,
        open_price=100.0,
        high_price=100.0,
        low_price=100.0,
        close_price=100.0,
        buy_notional_usd=10_000.0,
        sell_notional_usd=1_000.0,
        open_interest=100.0,
        open_interest_value=100_000.0,
        open_interest_observed_at=moment,
        last_trade_received_at=moment,
        price_complete=True,
        trades_complete=True,
        open_interest_complete=True,
    )


def _contract_dict() -> dict[str, Any]:
    return {
        "contract_version": "abnormal_flow_screen_v1",
        "lookback_minutes": 60,
        "outcome_horizon_minutes": 720,
        "cooldown_minutes": 720,
        "direction": "long",
        "min_oi_growth_pct": 10.0,
        "min_buy_pressure_ratio": 0.6,
        "max_price_containment": 0.05,
        "min_oi_notional_usd": 100.0,
        "oi_usd_conversion_rule": "bybit_native_value_binance_amount_x_decision_price_v1",
        "position_usd": 300.0,
        "max_participation_frac": 0.1,
        "entry_execution_window_minutes": 1,
        "oi_freshness_limit_seconds_bybit": 120,
        "oi_freshness_limit_seconds_binance": 300,
        "calibration_rule": "fixed_percentiles_on_prestart_window_v1",
        "calibration_window_days": 14,
        "scan_lag_minutes": 2,
        "entry_reference": "next_bar_open_priced_proxy_v1",
        "exit_reference": "horizon_bar_close_priced_proxy_v1",
        "matching_rule": "same_venue_regime_liquidity_pricemove_band_v1",
        "controls_per_episode": 1,
        "portfolio_bank_usd": 300.0,
        "portfolio_max_slots": 1,
        "inference_rule": "normal_1_96_v1",
        "window_start_utc": "2026-08-20T00:00:00+00:00",
        "window_end_utc": "2026-08-21T00:00:00+00:00",
        "input_fingerprint": "pending",
        "taker_fee_bps": 5.0,
        "entry_slippage_bps": 5.0,
        "exit_slippage_bps": 5.0,
        "funding_bps_720m_binance": 5.0,
        "funding_bps_720m_bybit": 5.0,
        "min_resolved_episodes": 1,
        "max_missing_fraction": 0.2,
        "min_excess_over_control_pct": 0.0,
        "min_distinct_assets": 1,
        "min_utc_weeks": 1,
        "max_episodes_per_asset_frac": 1.0,
        "max_episodes_per_week_frac": 1.0,
        "min_control_coverage_frac": 1.0,
    }


def _write_empty_day(bars_dir: Path, day: date) -> ExportManifest:
    parquet = bars_dir / f"bars-{day.isoformat()}.parquet"
    connection = duckdb.connect()
    connection.execute(
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
    connection.execute("COPY bars TO ? (FORMAT PARQUET)", [str(parquet)])
    connection.close()
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    fingerprint = f"cbfp_v1:{day.strftime('%Y%m%d').ljust(64, '0')}"
    manifest = ExportManifest(
        schema_version=SCHEMA_VERSION,
        export_version=EXPORT_VERSION,
        source_table=SOURCE_TABLE,
        day=day.isoformat(),
        bucket_start_from=start.isoformat(),
        bucket_start_until=(start + timedelta(days=1)).isoformat(),
        row_count=0,
        file_name=parquet.name,
        file_bytes=parquet.stat().st_size,
        sha256=sha256_file(parquet),
        data_keys=(),
        exported_at=start.isoformat(),
        source_fingerprint=fingerprint,
        file_fingerprint=fingerprint,
        fidelity_verified=True,
    )
    (bars_dir / f"bars-{day.isoformat()}.manifest.json").write_text(manifest.to_json())
    return manifest


@pytest.fixture
def run_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    identity = tmp_path / "identity.json"
    identity.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "exchange": "bybit",
                        "market_type": "linear",
                        "native_market_id": symbol,
                        "valid_from": "2026-08-19T00:00:00+00:00",
                        "valid_to": None,
                        "canonical_asset": f"asset:{symbol}",
                        "snapshot_captured_at": "2026-08-19T00:00:00+00:00",
                    }
                    for symbol in ("FIRE", "CONTROL")
                ]
            }
        )
    )
    funding = tmp_path / "funding.json"
    funding.write_text("{}")
    settlements = tmp_path / "settlements.json"
    settlements.write_text("{}")
    candidate = tmp_path / "candidate.json"
    candidate.write_text("{}")

    bars_dir = tmp_path / "bars"
    bars_dir.mkdir()
    scan_days = []
    for day in (date(2026, 8, 19), date(2026, 8, 20), date(2026, 8, 21)):
        manifest = _write_empty_day(bars_dir, day)
        scan_days.append(
            {
                "day": day.isoformat(),
                "sha256": manifest.sha256,
                "source_fingerprint": manifest.source_fingerprint,
            }
        )
    scan = tmp_path / "scan.json"
    scan.write_text(json.dumps({"days": scan_days}))

    evaluation = EvaluationManifest(
        input_audit_fingerprint=_sha(scan),
        identity_snapshot_hash=_sha(identity),
        candidate_table_version=_sha(candidate),
        funding_snapshot_hash=_sha(funding),
        funding_settlements_hash=_sha(settlements),
    )
    evaluation_path = tmp_path / "evaluation.json"
    evaluation_path.write_text(json.dumps(asdict(evaluation), sort_keys=True))

    contract_data = _contract_dict()
    contract_data["input_fingerprint"] = evaluation.compute_fingerprint()
    contract = AbnormalFlowContract(**contract_data)
    contract_data["contract_hash"] = contract.compute_hash()
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(contract_data, sort_keys=True))

    monkeypatch.setattr(runner_module, "REGISTERED_CONTRACT_FILE_HASH", _sha(contract_path))
    monkeypatch.setattr(
        runner_module,
        "REGISTERED_EVALUATION_MANIFEST_FILE_HASH",
        _sha(evaluation_path),
    )
    output = tmp_path / "output"
    return {
        "contract": contract_path,
        "evaluation": evaluation_path,
        "scan": scan,
        "identity": identity,
        "funding": funding,
        "settlements": settlements,
        "candidate": candidate,
        "bars": bars_dir,
        "output": output,
    }


def _run(runner: FormalRunner, env: dict[str, Path]) -> dict[str, Any]:
    return runner.run(
        _authorize_formal_run(True),
        env["contract"],
        env["evaluation"],
        env["scan"],
        env["identity"],
        env["funding"],
        env["settlements"],
        env["candidate"],
        env["bars"],
        env["output"],
    )


def test_import_does_not_enable_returns() -> None:
    import schurfer_analytics.abnormal_flow_replay as replay

    assert not replay.FORMAL_RETURNS_RUN_ENABLED


def test_formal_capability_requires_explicit_flag() -> None:
    with pytest.raises(ValueError, match="--formal-run"):
        _authorize_formal_run(False)
    with pytest.raises(ValueError, match="formal capability"):
        runner_module.FormalCapability(object())


def test_runner_rejects_dirty_tree_before_artifacts(run_env: dict[str, Path]) -> None:
    with pytest.raises(ValueError, match="dirty tree"):
        _run(FormalRunner(MockGitState(dirty=True)), run_env)
    assert not run_env["output"].exists()


def test_dependency_bounds_cover_lookback_and_last_exit() -> None:
    contract_data = _contract_dict()
    contract_data["input_fingerprint"] = "a" * 64
    bounds = dependency_bounds(AbnormalFlowContract(**contract_data))
    assert bounds.feature_start == datetime(2026, 8, 19, 22, 57, tzinfo=UTC)
    assert bounds.outcome_end_exclusive == datetime(2026, 8, 21, 12, 1, tzinfo=UTC)
    assert bounds.first_day == date(2026, 8, 19)
    assert bounds.day_end_exclusive == date(2026, 8, 22)


def test_frozen_artifact_hash_anchors_and_dependency_days() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    contract_path = repo_root / "docs/research/evidence/abnormal-flow-v1/formal/contract.json"
    evaluation_path = (
        repo_root / "docs/research/evidence/abnormal-flow-v1/formal/evaluation_manifest.json"
    )
    assert _sha(contract_path) == runner_module.REGISTERED_CONTRACT_FILE_HASH
    assert _sha(evaluation_path) == runner_module.REGISTERED_EVALUATION_MANIFEST_FILE_HASH

    raw = json.loads(contract_path.read_text())
    raw.pop("contract_hash")
    bounds = dependency_bounds(AbnormalFlowContract(**raw))
    assert bounds.first_day == date(2026, 8, 29)
    assert bounds.outcome_end_exclusive == datetime(2026, 9, 18, 23, 59, tzinfo=UTC)
    assert bounds.day_end_exclusive == date(2026, 9, 19)


def test_registered_input_mismatch_prevents_claim(run_env: dict[str, Path]) -> None:
    run_env["identity"].write_text('{"records": []}')
    with pytest.raises(ValueError, match="registered input hash mismatch"):
        _run(FormalRunner(MockGitState()), run_env)
    assert not run_env["output"].exists()


def test_runner_nonempty_path_uses_shared_economics(run_env: dict[str, Path]) -> None:
    fire = _decision("FIRE", oi_growth=20.0)
    control = _decision("CONTROL", oi_growth=0.0)
    outcomes = {
        fire.route_key(): Outcome(
            exchange="bybit",
            market_type="linear",
            native_market_id="FIRE",
            capture_version="v1",
            symbol="FIRE",
            decision_at=fire.decision_at,
            entry_price=100.0,
            exit_price=99.0,
        ),
        control.route_key(): Outcome(
            exchange="bybit",
            market_type="linear",
            native_market_id="CONTROL",
            capture_version="v1",
            symbol="CONTROL",
            decision_at=control.decision_at,
            entry_price=100.0,
            exit_price=100.0,
        ),
    }
    calls = 0

    def chunks(*args: Any, **kwargs: Any) -> Any:
        return iter([[_bar("FIRE")], [_bar("CONTROL")]])

    def decisions(bars: list[MinuteBar], **kwargs: Any) -> list[DecisionFeatures]:
        return [fire] if bars[0].symbol == "FIRE" else [control]

    def reader(*args: Any, **kwargs: Any) -> Any:
        def read(requested: list[DecisionFeatures]) -> dict[Any, Outcome]:
            nonlocal calls
            calls += 1
            return {item.route_key(): outcomes[item.route_key()] for item in requested}

        return read

    with (
        patch.object(runner_module, "iter_instrument_bars", side_effect=chunks),
        patch.object(runner_module, "assemble_decisions", side_effect=decisions),
        patch.object(runner_module, "parquet_outcome_reader", side_effect=reader),
    ):
        report = _run(FormalRunner(MockGitState()), run_env)

    assert calls == 1
    assert report["replay"]["resolved_episodes"] == 1
    assert report["replay"]["resolved_controls"] == 1
    assert report["funnel"]["primary_episodes"] == 1
    assert report["verdict"] == "FAIL"
    run_dir = next(run_env["output"].iterdir())
    assert json.loads((run_dir / "formal_run_report.json").read_text()) == report


def test_exception_is_terminal_and_duplicate_is_rejected(run_env: dict[str, Path]) -> None:
    with (
        patch.object(runner_module, "iter_instrument_bars", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError, match="boom"),
    ):
        _run(FormalRunner(MockGitState()), run_env)
    run_dir = next(run_env["output"].iterdir())
    failure = json.loads((run_dir / "formal_run_failed.json").read_text())
    assert failure["error_type"] == "RuntimeError"
    with pytest.raises(ValueError, match="terminal failed formal run"):
        _run(FormalRunner(MockGitState()), run_env)


def test_outcome_flag_resets_after_reader_failure(run_env: dict[str, Path]) -> None:
    import schurfer_analytics.abnormal_flow_replay as replay

    fire = _decision("FIRE", oi_growth=20.0)

    def chunks(*args: Any, **kwargs: Any) -> Any:
        return iter([[_bar("FIRE")]])

    with (
        patch.object(runner_module, "iter_instrument_bars", side_effect=chunks),
        patch.object(runner_module, "assemble_decisions", return_value=[fire]),
        patch.object(
            runner_module,
            "parquet_outcome_reader",
            return_value=lambda _: (_ for _ in ()).throw(RuntimeError("reader failed")),
        ),
        pytest.raises(RuntimeError, match="reader failed"),
    ):
        _run(FormalRunner(MockGitState()), run_env)
    assert replay.FORMAL_RETURNS_RUN_ENABLED is False
