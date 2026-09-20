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
        oi_usd_conversion_rule="native_oi_amount_x_point_in_time_mark_v1",
        position_usd=300.0,
        max_participation_frac=0.01,
        participation_turnover_window_minutes=60,
        calibration_rule="fixed_percentiles_on_prestart_window_v1",
        calibration_window_days=14,
        scan_lag_minutes=2,
        entry_reference="next_bar_open_priced_proxy",
        exit_reference="horizon_bar_close_priced_proxy",
        matching_rule="same_venue_regime_liquidity_pricemove_band_v1",
        portfolio_bank_usd=300.0,
        portfolio_max_slots=3,
        entry_cost_bps=5.0,
        slippage_bps=10.0,
        fee_bps=5.0,
        funding_model="conservative_8h_v1",
        min_resolved_episodes=100,
        max_missing_fraction=0.2,
        min_excess_over_control_pct=0.0,
    )
    base.update(overrides)
    return AbnormalFlowContract(**base)  # type: ignore[arg-type]


def test_default_contract_is_not_frozen_and_a_formal_run_fails_closed() -> None:
    c = AbnormalFlowContract()
    assert not c.is_frozen()
    with pytest.raises(NotFrozenError) as exc:
        c.require_frozen()
    msg = str(exc.value)
    assert "min_oi_growth_pct is not set" in msg
    assert "calibration_rule is not set" in msg
    assert "matching_rule is not set" in msg


def test_partially_frozen_contract_still_fails_closed() -> None:
    c = AbnormalFlowContract(min_oi_growth_pct=5.0)  # one set, the rest unset
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
