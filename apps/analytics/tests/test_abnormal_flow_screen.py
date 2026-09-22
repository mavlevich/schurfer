"""Pure tests for the abnormal-flow screen contract + frozen feature forms."""

from __future__ import annotations

import math

import pytest
from schurfer_analytics.abnormal_flow_screen import (
    AbnormalFlowContract,
    NotFrozenError,
    buy_pressure_ratio_60m,
    oi_growth_pct_60m,
    price_containment_max_bar_dev,
)

_INF = math.inf
_NAN = math.nan


def test_oi_growth_pct_is_within_instrument_percent() -> None:
    assert oi_growth_pct_60m(1000.0, 1200.0) == pytest.approx(20.0)
    assert oi_growth_pct_60m(1000.0, 800.0) == pytest.approx(-20.0)  # declines are real values


def test_oi_growth_pct_unavailable_on_bad_base_or_nonfinite() -> None:
    assert oi_growth_pct_60m(0.0, 500.0) is None  # zero base -> unavailable, not infinite
    assert oi_growth_pct_60m(-1.0, 500.0) is None
    assert oi_growth_pct_60m(None, 500.0) is None  # type: ignore[arg-type]
    assert oi_growth_pct_60m(1000.0, None) is None  # type: ignore[arg-type]
    assert oi_growth_pct_60m(_NAN, 500.0) is None
    assert oi_growth_pct_60m(1000.0, _INF) is None


def test_buy_pressure_ratio_is_usd_share() -> None:
    assert buy_pressure_ratio_60m(750.0, 250.0) == pytest.approx(0.75)


def test_buy_pressure_unavailable_on_zero_flow_or_nonfinite() -> None:
    assert buy_pressure_ratio_60m(0.0, 0.0) is None
    assert buy_pressure_ratio_60m(-1.0, 5.0) is None
    assert buy_pressure_ratio_60m(_NAN, 5.0) is None
    assert buy_pressure_ratio_60m(5.0, _INF) is None


def test_price_containment_uses_bar_extremes_not_closes() -> None:
    # Open 100; a bar wicks to high=130 then the bar closes back near 100. Closes-only
    # would call this restrained; bar extremes correctly report a 30% deviation.
    bars = [(100.0, 99.0), (130.0, 100.0), (101.0, 99.5)]  # (high, low) per minute
    assert price_containment_max_bar_dev(100.0, bars) == pytest.approx(0.30)
    # A downward wick to low=70 is equally uncontained.
    assert price_containment_max_bar_dev(100.0, [(101.0, 70.0)]) == pytest.approx(0.30)
    # Genuinely restrained: extremes stay within 1%.
    assert price_containment_max_bar_dev(100.0, [(100.5, 99.5), (101.0, 99.0)]) == pytest.approx(
        0.01
    )


def test_price_containment_unavailable_on_bad_inputs() -> None:
    assert price_containment_max_bar_dev(0.0, [(1.0, 1.0)]) is None
    assert price_containment_max_bar_dev(100.0, []) is None
    assert price_containment_max_bar_dev(100.0, [(100.0, _NAN)]) is None
    assert price_containment_max_bar_dev(_INF, [(100.0, 100.0)]) is None


def _frozen_contract(**overrides: object) -> AbnormalFlowContract:
    base = dict(
        min_oi_growth_pct=5.0,
        min_buy_pressure_ratio=0.6,
        max_price_containment=0.1,
        min_oi_notional_usd=250_000.0,
        inference_rule="student_t_df_weeks_minus_one_v1",
        oi_usd_conversion_rule="bybit_native_value_binance_amount_x_decision_price_v1",
        position_usd=300.0,
        max_participation_frac=0.01,
        entry_execution_window_minutes=5,
        oi_freshness_limit_seconds_bybit=120,
        oi_freshness_limit_seconds_binance=300,
        calibration_rule="fixed_percentiles_on_prestart_window_v1",
        calibration_window_days=14,
        scan_lag_minutes=2,
        entry_reference="next_bar_open_priced_proxy_v1",
        exit_reference="horizon_bar_close_priced_proxy_v1",
        matching_rule="same_venue_regime_liquidity_pricemove_band_v1",
        controls_per_episode=5,
        portfolio_bank_usd=300.0,
        portfolio_max_slots=3,
        taker_fee_bps=10.0,
        entry_slippage_bps=15.0,
        exit_slippage_bps=15.0,
        funding_bps_720m_binance=6.0,
        funding_bps_720m_bybit=6.0,
        min_distinct_assets=30,
        min_utc_weeks=4,
        max_episodes_per_asset_frac=0.35,
        max_episodes_per_week_frac=0.45,
        min_control_coverage_frac=0.80,
        min_resolved_episodes=100,
        max_missing_fraction=0.2,
        min_excess_over_control_pct=0.0,
        window_start_utc="2026-08-14T00:00:00+00:00",
        window_end_utc="2026-09-14T00:00:00+00:00",
        input_fingerprint="abnormal_flow_input_audit_v1:" + "a" * 64,
    )
    base.update(overrides)
    return AbnormalFlowContract(**base)  # type: ignore[arg-type]


def test_unfilled_contract_is_not_frozen_and_a_formal_run_fails_closed() -> None:
    c = AbnormalFlowContract(min_oi_growth_pct=None, calibration_rule=None, matching_rule=None)
    assert not c.is_frozen()
    with pytest.raises(NotFrozenError) as exc:
        c.require_frozen()
    msg = str(exc.value)
    assert "min_oi_growth_pct is not set" in msg
    assert "calibration_rule is not set" in msg
    assert "matching_rule is not set" in msg


def test_partially_frozen_contract_still_fails_closed() -> None:
    c = AbnormalFlowContract(min_buy_pressure_ratio=None)  # one unset
    assert not c.is_frozen()
    with pytest.raises(NotFrozenError):
        c.require_frozen()


def test_fully_frozen_contract_permits_a_formal_run() -> None:
    c = _frozen_contract()
    assert c.is_frozen()
    c.require_frozen()  # does not raise
    assert c.lookback_minutes == 60
    assert c.outcome_horizon_minutes == 720
    assert c.cooldown_minutes == 720
    assert c.direction == "long"


def test_require_frozen_rejects_out_of_range_and_nonfinite_values() -> None:
    # The colleague's counterexample: all fields set but semantically invalid.
    bad = _frozen_contract(
        direction="short",
        lookback_minutes=1,
        min_oi_notional_usd=-1.0,
        max_missing_fraction=2.0,
        min_buy_pressure_ratio=_NAN,
    )
    assert not bad.is_frozen()
    problems = bad.problems()
    joined = "; ".join(problems)
    assert "direction must be 'long'" in joined
    assert "lookback_minutes must be 60" in joined
    assert "min_oi_notional_usd must be > 0.0" in joined
    assert "max_missing_fraction must be <= 1.0" in joined
    assert "min_buy_pressure_ratio must be a finite number" in joined
    with pytest.raises(NotFrozenError):
        bad.require_frozen()


def test_buy_pressure_threshold_must_indicate_dominance() -> None:
    # "buy dominating sell" means > 0.5; a 0.5 or lower threshold is rejected.
    assert not _frozen_contract(min_buy_pressure_ratio=0.5).is_frozen()
    assert _frozen_contract(min_buy_pressure_ratio=0.55).is_frozen()


def test_rule_fields_must_be_registered_executable_rules_not_free_text() -> None:
    # A plausible-sounding but unregistered label is not an executable rule: the code
    # path it names does not exist, so the run would silently do something else.
    for field in (
        "oi_usd_conversion_rule",
        "calibration_rule",
        "entry_reference",
        "exit_reference",
        "matching_rule",
    ):
        bad = _frozen_contract(**{field: "sounds_official_v9"})
        assert not bad.is_frozen()
        joined = "; ".join(bad.problems())
        assert f"{field} must be a registered executable rule" in joined
        with pytest.raises(NotFrozenError):
            bad.require_frozen()


def test_window_boundaries_must_be_ordered_explicit_utc() -> None:
    # Naive (no timezone) instant is rejected: "UTC" must be literal, not assumed.
    naive = _frozen_contract(window_start_utc="2026-08-14T00:00:00")
    assert not naive.is_frozen()
    assert "window_start_utc must be an explicit UTC instant" in "; ".join(naive.problems())

    # A non-UTC offset is not a UTC boundary.
    offset = _frozen_contract(window_start_utc="2026-08-14T00:00:00+02:00")
    assert "window_start_utc must be an explicit UTC instant" in "; ".join(offset.problems())

    # end <= start is rejected.
    reversed_window = _frozen_contract(
        window_start_utc="2026-09-14T00:00:00+00:00",
        window_end_utc="2026-08-14T00:00:00+00:00",
    )
    assert "window_start_utc must be strictly before window_end_utc" in "; ".join(
        reversed_window.problems()
    )


def test_input_fingerprint_must_be_present_and_a_sha256() -> None:
    missing = _frozen_contract(input_fingerprint=None)
    assert "input_fingerprint is not set" in "; ".join(missing.problems())
    with pytest.raises(NotFrozenError):
        missing.require_frozen()

    malformed = _frozen_contract(input_fingerprint="not-a-hash")
    assert "input_fingerprint must be a sha256 hex" in "; ".join(malformed.problems())

    # A bare 64-hex SHA-256 (no namespace prefix) is accepted.
    assert _frozen_contract(input_fingerprint="b" * 64).is_frozen()


def test_participation_window_must_be_a_short_pre_decision_period() -> None:
    # The whole 60m lookback is NOT a realistic fill window; participation must use a
    # short pre-decision window strictly shorter than the lookback.
    whole_hour = _frozen_contract(entry_execution_window_minutes=60)
    assert not whole_hour.is_frozen()
    assert "entry_execution_window_minutes must be < lookback_minutes" in "; ".join(
        whole_hour.problems()
    )
    assert _frozen_contract(entry_execution_window_minutes=5).is_frozen()


def test_frozen_artifact_loads_and_hashes_correctly() -> None:
    import hashlib
    import json
    from pathlib import Path

    from schurfer_analytics.abnormal_flow_screen import AbnormalFlowContract

    path = (
        Path(__file__).resolve().parent.parent.parent.parent
        / "docs/research/evidence/abnormal-flow-v1/formal/contract.json"
    )
    content = path.read_bytes()
    file_sha = hashlib.sha256(content).hexdigest()

    # Must be valid json
    d = json.loads(content)

    # Check that it loads
    contract = AbnormalFlowContract.from_json(content.decode("utf-8"))

    # Check internal hash matches what's registered
    assert contract.compute_hash() == d["contract_hash"]

    # File SHA must match what's reported (for safety, though it will change if formatted)
    assert file_sha == "36502eeb4ffb63cb2d0eead5e81c97947c97130a85ac046e6d2759459e492256"
    contract.require_frozen()


def test_evaluation_manifest_hash_matches_contract() -> None:
    import json
    from pathlib import Path

    from schurfer_analytics.abnormal_flow_replay import EvaluationManifest

    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    manifest_path = (
        repo_root / "docs/research/evidence/abnormal-flow-v1/formal/evaluation_manifest.json"
    )
    contract_path = repo_root / "docs/research/evidence/abnormal-flow-v1/formal/contract.json"

    manifest_content = manifest_path.read_text(encoding="utf-8")
    contract_content = contract_path.read_text(encoding="utf-8")

    manifest_d = json.loads(manifest_content)
    contract_d = json.loads(contract_content)

    # Instantiate manifest
    manifest = EvaluationManifest(
        input_audit_fingerprint=manifest_d["input_audit_fingerprint"],
        identity_snapshot_hash=manifest_d["identity_snapshot_hash"],
        candidate_table_version=manifest_d["candidate_table_version"],
        funding_snapshot_hash=manifest_d["funding_snapshot_hash"],
        funding_settlements_hash=manifest_d["funding_settlements_hash"],
    )

    assert manifest.compute_fingerprint() == contract_d["input_fingerprint"]


def test_evaluation_manifest_hashes_match_actual_files() -> None:
    import hashlib
    import json
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    manifest_path = (
        repo_root / "docs/research/evidence/abnormal-flow-v1/formal/evaluation_manifest.json"
    )

    snapshot_path = (
        repo_root / "docs/research/evidence/abnormal-flow-v1/funding/funding_snapshot.json"
    )
    settlements_path = (
        repo_root / "docs/research/evidence/abnormal-flow-v1/funding/funding_settlements.json.gz"
    )

    manifest_d = json.loads(manifest_path.read_text(encoding="utf-8"))

    snapshot_hash = "sha256:" + hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    settlements_hash = "sha256:" + hashlib.sha256(settlements_path.read_bytes()).hexdigest()

    assert snapshot_hash == manifest_d["funding_snapshot_hash"]
    assert settlements_hash == manifest_d["funding_settlements_hash"]
