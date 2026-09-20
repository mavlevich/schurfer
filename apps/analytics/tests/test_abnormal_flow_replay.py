"""Tests for the abnormal-flow replay engine.

The load-bearing test is that no forward return can be read until the contract is
frozen; the rest pin the pure decision logic (OI->USD per venue, participation,
eligibility, primary/ablation cells, episode cooldown, control matching, priced-proxy
economics) and the outcome-blind feature assembly, all with synthetic rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.abnormal_flow_replay import (
    DecisionFeatures,
    FormalReplay,
    MinuteBar,
    Outcome,
    ablation_cell_fires,
    assemble_decisions,
    control_band_key,
    form_episodes,
    is_eligible,
    match_controls,
    oi_notional_usd,
    participation_frac,
    primary_cell_fires,
    proxy_net_return,
)
from schurfer_analytics.abnormal_flow_screen import AbnormalFlowContract, NotFrozenError

_T0 = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)


def _frozen_contract(**overrides: object) -> AbnormalFlowContract:
    base = dict(
        min_oi_growth_pct=5.0,
        min_buy_pressure_ratio=0.6,
        max_price_containment=0.1,
        min_oi_notional_usd=50_000.0,
        oi_usd_conversion_rule="bybit_native_value_binance_amount_x_decision_price_v1",
        position_usd=300.0,
        max_participation_frac=0.1,
        entry_execution_window_minutes=5,
        calibration_rule="fixed_percentiles_on_prestart_window_v1",
        calibration_window_days=14,
        scan_lag_minutes=2,
        entry_reference="next_bar_open_priced_proxy_v1",
        exit_reference="horizon_bar_close_priced_proxy_v1",
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
        window_start_utc="2026-08-14T00:00:00+00:00",
        window_end_utc="2026-09-14T00:00:00+00:00",
        input_fingerprint="a" * 64,
    )
    base.update(overrides)
    return AbnormalFlowContract(**base)  # type: ignore[arg-type]


def _decision(**overrides: object) -> DecisionFeatures:
    base = dict(
        exchange="bybit",
        symbol="FOOUSDT",
        canonical_asset="FOO",
        decision_at=_T0,
        oi_growth_pct=20.0,
        buy_pressure=0.7,
        containment=0.01,
        oi_native_amount=1200.0,
        oi_native_value_usd=120_000.0,
        decision_price=100.0,
        pre_decision_turnover_usd=5_000.0,
        iso_week="2026-W34",
        unavailable_reason=None,
    )
    base.update(overrides)
    return DecisionFeatures(**base)  # type: ignore[arg-type]


# --- The load-bearing invariant ----------------------------------------------------


def test_formal_run_refuses_to_read_returns_before_freeze() -> None:
    called = False

    def reader(_eps: object) -> dict[tuple[str, datetime], Outcome]:
        nonlocal called
        called = True  # pragma: no cover - must never run
        return {}

    replay = FormalReplay(AbnormalFlowContract())  # default: not frozen
    with pytest.raises(NotFrozenError):
        replay.run([_decision()], reader)
    assert called is False, "returns were read against an unfrozen contract"


def test_frozen_run_reads_returns_and_scores_excess() -> None:
    ep = _decision()
    control = _decision(
        symbol="BARUSDT", canonical_asset="BAR", buy_pressure=0.4
    )  # eligible non-fire

    def reader(_eps: object) -> dict[tuple[str, datetime], Outcome]:
        return {
            ("FOOUSDT", _T0): Outcome("bybit", "FOOUSDT", _T0, entry_price=100.0, exit_price=110.0),
            ("BARUSDT", _T0): Outcome("bybit", "BARUSDT", _T0, entry_price=100.0, exit_price=101.0),
        }

    result = FormalReplay(_frozen_contract()).run([ep, control], reader)
    assert result.resolved_episodes == 1
    assert result.mean_net_return is not None and result.mean_net_return > 0
    assert result.mean_excess_over_control is not None
    # FOO gained 10% gross, BAR (matched control) 1%; excess is clearly positive.
    assert result.mean_excess_over_control > 0.05


# --- OI -> USD per venue -----------------------------------------------------------


def test_oi_notional_usd_uses_native_value_on_bybit() -> None:
    assert oi_notional_usd("bybit", 1200.0, 120_000.0, 100.0) == pytest.approx(120_000.0)
    # Bybit without its native USD value is unavailable, not amount x price.
    assert oi_notional_usd("bybit", 1200.0, None, 100.0) is None


def test_oi_notional_usd_uses_amount_times_price_on_binance() -> None:
    assert oi_notional_usd("binance", 1200.0, None, 100.0) == pytest.approx(120_000.0)
    # Binance without a decision price cannot be converted.
    assert oi_notional_usd("binance", 1200.0, None, None) is None
    # An unknown venue is never guessed.
    assert oi_notional_usd("okx", 1200.0, 1.0, 100.0) is None


def test_participation_and_eligibility() -> None:
    assert participation_frac(300.0, 5_000.0) == pytest.approx(0.06)
    assert participation_frac(300.0, 0.0) is None
    c = _frozen_contract()
    assert is_eligible(c, 120_000.0, 0.06) is True
    assert is_eligible(c, 10.0, 0.06) is False  # below OI floor
    assert is_eligible(c, 120_000.0, 0.5) is False  # over participation cap
    assert is_eligible(c, None, 0.06) is False


# --- Cells ------------------------------------------------------------------------


def test_primary_and_ablation_cells() -> None:
    c = _frozen_contract()
    fires = _decision()
    assert primary_cell_fires(c, fires) is True
    assert ablation_cell_fires(c, fires) is True
    # Low OI growth fails the primary but the ablation (OI-growth removed) still fires.
    low_oi = _decision(oi_growth_pct=1.0)
    assert primary_cell_fires(c, low_oi) is False
    assert ablation_cell_fires(c, low_oi) is True
    # An unavailable feature never counts as a pass.
    assert primary_cell_fires(c, _decision(buy_pressure=None)) is False


def test_form_episodes_enforces_cooldown_per_asset() -> None:
    a0 = _decision(decision_at=_T0)
    a1 = _decision(decision_at=_T0 + timedelta(minutes=60))  # within 720m cooldown -> dropped
    a2 = _decision(decision_at=_T0 + timedelta(minutes=800))  # after cooldown -> kept
    other = _decision(
        symbol="BARUSDT", canonical_asset="BAR", decision_at=_T0 + timedelta(minutes=5)
    )
    kept = form_episodes([a1, a0, a2, other], cooldown_minutes=720)
    kept_keys = {(k.canonical_asset, k.decision_at) for k in kept}
    assert kept_keys == {
        ("FOO", _T0),
        ("FOO", _T0 + timedelta(minutes=800)),
        ("BAR", other.decision_at),
    }


# --- Control matching -------------------------------------------------------------


def test_control_band_key_and_matching() -> None:
    fired = _decision()
    same = _decision(
        symbol="BAZUSDT", canonical_asset="BAZ", decision_at=_T0 + timedelta(minutes=3)
    )
    other_week = _decision(symbol="QUXUSDT", iso_week="2026-W40")
    assert control_band_key(fired) == control_band_key(same)
    assert control_band_key(fired) != control_band_key(other_week)
    controls = match_controls(fired, [same, other_week], max_controls=3)
    assert [c.symbol for c in controls] == ["BAZUSDT"]  # only same-band, other week excluded


# --- Priced-proxy economics --------------------------------------------------------


def test_proxy_net_return_charges_costs_and_flags_unresolved() -> None:
    c = _frozen_contract()
    # 10% gross long, minus (5 + 10 + 2*5 + funding) bps of cost.
    r = proxy_net_return(c, 100.0, 110.0)
    assert r is not None and r == pytest.approx(0.10 - (5 + 10 + 10 + 3 * 2) / 10_000.0)
    assert proxy_net_return(c, None, 110.0) is None  # unresolved outcome, not filled in
    assert proxy_net_return(c, 100.0, 0.0) is None


# --- Outcome-blind feature assembly ------------------------------------------------


def _series() -> list[MinuteBar]:
    bars: list[MinuteBar] = []
    span = 61
    for i in range(span):
        bucket = _T0 + timedelta(minutes=i)
        oi = 1000.0 + (200.0 * i / (span - 1))  # 1000 -> 1200 across the hour
        bars.append(
            MinuteBar(
                exchange="bybit",
                symbol="FOOUSDT",
                canonical_asset="FOO",
                bucket_start=bucket,
                created_at=bucket + timedelta(seconds=30),
                open_price=100.0,
                high_price=100.5,
                low_price=99.5,
                close_price=100.0,
                buy_notional_usd=700.0,
                sell_notional_usd=300.0,
                open_interest=oi,
                open_interest_value=oi * 100.0,
                open_interest_observed_at=bucket,
                price_complete=True,
                trades_complete=True,
                open_interest_complete=True,
            )
        )
    return bars


def test_assemble_decisions_computes_frozen_feature_forms() -> None:
    decisions = assemble_decisions(_series(), scan_lag_minutes=2, entry_execution_window_minutes=5)
    assert len(decisions) == 1
    d = decisions[0]
    assert d.unavailable_reason is None
    assert d.oi_growth_pct == pytest.approx(20.0)
    assert d.buy_pressure == pytest.approx(0.7)
    assert d.containment == pytest.approx(0.005)
    assert d.decision_price == pytest.approx(100.0)
    assert d.pre_decision_turnover_usd == pytest.approx(5_000.0)  # 5 bars x 1000 turnover
    # Decision is timed after the last feature bar finalizes plus scan lag.
    assert d.decision_at == _T0 + timedelta(minutes=61 + 2)


def test_assemble_decisions_marks_unavailable_windows() -> None:
    # A gap in the lookback.
    gapped = _series()
    gapped[30] = MinuteBar(
        **{**gapped[30].__dict__, "bucket_start": gapped[30].bucket_start + timedelta(minutes=5)}
    )
    assert assemble_decisions(gapped, scan_lag_minutes=2, entry_execution_window_minutes=5)[
        0
    ].unavailable_reason in {
        "lookback_gap",
    }

    # An incomplete bar.
    incomplete = _series()
    incomplete[40] = MinuteBar(**{**incomplete[40].__dict__, "open_interest_complete": False})
    assert (
        assemble_decisions(incomplete, scan_lag_minutes=2, entry_execution_window_minutes=5)[
            0
        ].unavailable_reason
        == "incomplete_lookback"
    )

    # OI observed after the decision (stale).
    stale = _series()
    stale[0] = MinuteBar(
        **{**stale[0].__dict__, "open_interest_observed_at": _T0 + timedelta(hours=5)}
    )
    assert (
        assemble_decisions(stale, scan_lag_minutes=2, entry_execution_window_minutes=5)[
            0
        ].unavailable_reason
        == "stale_oi"
    )
