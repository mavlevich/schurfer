"""Tests for the abnormal-flow scanner.

This release is a COUNTS-ONLY outcome-blind scanner: the returns-reading run is
hard-disabled (`FORMAL_RETURNS_RUN_ENABLED` is False) and refuses unconditionally,
even for a fully frozen contract. The returns-path logic (freeze binding, route-keyed
outcomes, economics) is still exercised here by monkeypatching the flag on, so it stays
covered for the later PR that enables it; nothing reads a production return.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
import schurfer_analytics.abnormal_flow_replay as afr
from schurfer_analytics.abnormal_flow_replay import (
    DecisionFeatures,
    EpisodeRecord,
    EvaluationManifest,
    FormalReplay,
    FreezeMismatchError,
    MinuteBar,
    Outcome,
    ReturnsRunDisabledError,
    RouteKey,
    assemble_all,
    assemble_decisions,
    build_funnel,
    build_report,
    control_band_key,
    form_episodes,
    input_fingerprint_for,
    is_eligible,
    load_verified_minute_bars,
    match_controls,
    no_oi_cell_fires,
    oi_notional_usd,
    parquet_outcome_reader,
    participation_frac,
    primary_cell_fires,
    proxy_net_return,
    quote_suffix_canonical_resolver,
    render_verdict,
    simulate_portfolio,
)
from schurfer_analytics.abnormal_flow_screen import AbnormalFlowContract, NotFrozenError
from schurfer_analytics.cold_bar_export import (
    EXPORT_VERSION,
    SCHEMA_VERSION,
    SOURCE_TABLE,
    sha256_file,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_T0 = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
_FINGERPRINT = "a" * 64


def _dummy_manifest(fp: str = _FINGERPRINT) -> EvaluationManifest:
    return EvaluationManifest(
        input_audit_fingerprint=fp,
        identity_snapshot_hash="h",
        candidate_table_version="v",
        funding_snapshot_hash="s",
        funding_settlements_hash="s",
    )


def _write_c(c: AbnormalFlowContract) -> str:
    import tempfile

    _, path = tempfile.mkstemp(suffix=".json")
    from pathlib import Path

    with Path(path).open("w") as f:
        f.write(c.to_json())
    return path


def _frozen_contract(**overrides: object) -> AbnormalFlowContract:
    base = dict(
        inference_rule="normal_1_96_v1",
        min_oi_growth_pct=5.0,
        min_buy_pressure_ratio=0.6,
        max_price_containment=0.1,
        min_oi_notional_usd=50_000.0,
        oi_usd_conversion_rule="bybit_native_value_binance_amount_x_decision_price_v1",
        position_usd=300.0,
        max_participation_frac=0.1,
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
        input_fingerprint=_dummy_manifest(_FINGERPRINT).compute_fingerprint(),
    )
    base.update(overrides)
    return AbnormalFlowContract(**base)  # type: ignore[arg-type]


def _decision(**overrides: object) -> DecisionFeatures:
    base = dict(
        exchange="bybit",
        market_type="linear",
        native_market_id="FOOUSDT",
        capture_version="v1",
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


def _outcome(d: DecisionFeatures, *, entry: float | None, exit_: float | None) -> Outcome:
    return Outcome(
        exchange=d.exchange,
        market_type=d.market_type,
        native_market_id=d.native_market_id,
        capture_version=d.capture_version,
        symbol=d.symbol,
        decision_at=d.decision_at,
        entry_price=entry,
        exit_price=exit_,
    )


def _reader_from(
    prices: dict[str, tuple[float, float]],
) -> Callable[[object], dict[RouteKey, Outcome]]:
    """A well-behaved reader: returns an outcome ONLY for the rows it was asked for,
    keyed by the exact native route, using per-native-market-id (entry, exit) prices."""

    def reader(requested: object) -> dict[RouteKey, Outcome]:
        out: dict[RouteKey, Outcome] = {}
        for d in requested:  # type: ignore[attr-defined]
            entry, exit_ = prices[d.native_market_id]
            out[d.route_key()] = _outcome(d, entry=entry, exit_=exit_)
        return out

    return reader


# --- Fix (3): the returns-run is hard-disabled -------------------------------------


def test_formal_returns_run_is_hard_disabled_even_when_frozen() -> None:
    called = False

    def reader(_req: object) -> dict[RouteKey, Outcome]:
        nonlocal called
        called = True  # pragma: no cover - must never run
        return {}

    # Fully frozen contract, correct fingerprint, in-window decision: still refused,
    # and the reader is never invoked.
    with pytest.raises(ReturnsRunDisabledError):
        FormalReplay(_frozen_contract()).run(
            [_decision()],
            reader,
            evaluation_manifest=_dummy_manifest(_FINGERPRINT),
            registered_contract_path=_write_c(_frozen_contract()),
        )
    assert called is False
    assert afr.FORMAL_RETURNS_RUN_ENABLED is False  # shipped disabled


# --- Returns-path coverage (flag monkeypatched on; no production return read) -------


def test_run_still_fails_closed_on_unfrozen_contract_when_enabled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(afr, "FORMAL_RETURNS_RUN_ENABLED", True)
    called = False

    def reader(_req: object) -> dict[RouteKey, Outcome]:
        nonlocal called
        called = True  # pragma: no cover
        return {}

    with pytest.raises(NotFrozenError):
        c_ab = AbnormalFlowContract(min_oi_growth_pct=None)
        FormalReplay(c_ab).run(
            [_decision()],
            reader,
            evaluation_manifest=_dummy_manifest(_FINGERPRINT),
            registered_contract_path=_write_c(c_ab),
        )
    assert called is False


def test_run_binds_freeze_to_fingerprint_and_window_when_enabled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(afr, "FORMAL_RETURNS_RUN_ENABLED", True)

    def reader(_req: object) -> dict[RouteKey, Outcome]:  # pragma: no cover
        return {}

    with pytest.raises(FreezeMismatchError):
        c = _frozen_contract()
        FormalReplay(c).run(
            [_decision()],
            reader,
            evaluation_manifest=_dummy_manifest("b" * 64),
            registered_contract_path=_write_c(c),
        )
    early = _decision(decision_at=datetime(2026, 8, 1, tzinfo=UTC))
    with pytest.raises(FreezeMismatchError):
        c = _frozen_contract()
        FormalReplay(c).run(
            [early],
            reader,
            evaluation_manifest=_dummy_manifest(_FINGERPRINT),
            registered_contract_path=_write_c(c),
        )


def test_run_scores_excess_with_a_reader_that_returns_only_requested(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(afr, "FORMAL_RETURNS_RUN_ENABLED", True)
    ep = _decision()
    control = _decision(
        symbol="BARUSDT", native_market_id="BARUSDT", canonical_asset="BAR", buy_pressure=0.4
    )
    reader = _reader_from({"FOOUSDT": (100.0, 110.0), "BARUSDT": (100.0, 101.0)})
    c = _frozen_contract()
    result = FormalReplay(c).run(
        [ep, control],
        reader,
        evaluation_manifest=_dummy_manifest(_FINGERPRINT),
        registered_contract_path=_write_c(c),
    )
    assert result.resolved_episodes == 1
    assert result.resolved_controls == 1
    assert result.mean_excess_over_control is not None and result.mean_excess_over_control > 0.05


def test_run_does_not_conflate_venues_sharing_a_symbol(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(afr, "FORMAL_RETURNS_RUN_ENABLED", True)
    on_bybit = _decision(exchange="bybit", native_market_id="XUSDT", symbol="XUSDT")
    on_binance = _decision(
        exchange="binance", native_market_id="XUSDT", symbol="XUSDT", oi_native_value_usd=None
    )

    def route_reader(requested: object) -> dict[RouteKey, Outcome]:
        out: dict[RouteKey, Outcome] = {}
        for d in requested:  # type: ignore[attr-defined]
            exit_ = 110.0 if d.exchange == "bybit" else 90.0
            out[d.route_key()] = _outcome(d, entry=100.0, exit_=exit_)
        return out

    c = _frozen_contract()
    result = FormalReplay(c).run(
        [on_bybit, on_binance],
        route_reader,
        evaluation_manifest=_dummy_manifest(_FINGERPRINT),
        registered_contract_path=_write_c(c),
    )
    assert result.resolved_episodes == 2
    assert result.mean_net_return is not None and abs(result.mean_net_return) < 0.02


def test_run_populates_the_report_when_enabled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(afr, "FORMAL_RETURNS_RUN_ENABLED", True)
    c = _frozen_contract(min_resolved_episodes=1)
    ep = _decision()
    control = _decision(
        symbol="BARUSDT", native_market_id="BARUSDT", canonical_asset="BAR", buy_pressure=0.4
    )
    reader = _reader_from({"FOOUSDT": (100.0, 110.0), "BARUSDT": (100.0, 101.0)})
    result = FormalReplay(c).run(
        [ep, control],
        reader,
        evaluation_manifest=_dummy_manifest(_FINGERPRINT),
        registered_contract_path=_write_c(c),
    )
    assert result.report.resolved_episodes == 1
    assert result.report.portfolio.taken_trades == 1
    assert result.report.verdict in {"INSUFFICIENT_EVIDENCE", "FAIL", "PASS_DISCOVERY"}


# --- OI -> USD per venue -----------------------------------------------------------


def test_oi_notional_usd_uses_native_value_on_bybit() -> None:
    assert oi_notional_usd("bybit", 1200.0, 120_000.0, 100.0) == pytest.approx(120_000.0)
    assert oi_notional_usd("bybit", 1200.0, None, 100.0) is None


def test_oi_notional_usd_uses_amount_times_price_on_binance() -> None:
    assert oi_notional_usd("binance", 1200.0, None, 100.0) == pytest.approx(120_000.0)
    assert oi_notional_usd("binance", 1200.0, None, None) is None
    assert oi_notional_usd("okx", 1200.0, 1.0, 100.0) is None


def test_participation_and_eligibility() -> None:
    assert participation_frac(300.0, 5_000.0) == pytest.approx(0.06)
    assert participation_frac(300.0, 0.0) is None
    c = _frozen_contract()
    assert is_eligible(c, 120_000.0, 0.06) is True
    assert is_eligible(c, 10.0, 0.06) is False
    assert is_eligible(c, 120_000.0, 0.5) is False
    assert is_eligible(c, None, 0.06) is False


# --- Cells ------------------------------------------------------------------------


def test_primary_and_ablation_cells() -> None:
    c = _frozen_contract()
    assert primary_cell_fires(c, _decision()) is True
    assert no_oi_cell_fires(c, _decision()) is True
    low_oi = _decision(oi_growth_pct=1.0)
    assert primary_cell_fires(c, low_oi) is False
    assert no_oi_cell_fires(c, low_oi) is True
    assert primary_cell_fires(c, _decision(buy_pressure=None)) is False


def test_form_episodes_enforces_cooldown_per_asset() -> None:
    a0 = _decision(decision_at=_T0)
    a1 = _decision(decision_at=_T0 + timedelta(minutes=60))
    a2 = _decision(decision_at=_T0 + timedelta(minutes=800))
    other = _decision(
        symbol="BARUSDT",
        native_market_id="BARUSDT",
        canonical_asset="BAR",
        decision_at=_T0 + timedelta(minutes=5),
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
        symbol="BAZUSDT",
        native_market_id="BAZUSDT",
        canonical_asset="BAZ",
        decision_at=_T0 + timedelta(minutes=3),
    )
    other_week = _decision(symbol="QUXUSDT", native_market_id="QUXUSDT", iso_week="2026-W40")
    assert control_band_key(fired) == control_band_key(same)
    assert control_band_key(fired) != control_band_key(other_week)
    controls = match_controls(fired, [same, other_week], max_controls=3)
    assert [c.symbol for c in controls] == ["BAZUSDT"]


# --- Priced-proxy economics --------------------------------------------------------


def test_proxy_net_return_charges_costs_and_flags_unresolved() -> None:
    c = _frozen_contract()
    r = proxy_net_return(c, "bybit", 100.0, 110.0)
    assert r is not None and r == pytest.approx(0.10 - (10 * 2 + 15 + 15 + 6.0) / 10_000.0)
    assert proxy_net_return(c, "bybit", None, 110.0) is None
    assert proxy_net_return(c, "bybit", 100.0, 0.0) is None


# --- Outcome-blind feature assembly ------------------------------------------------


def _series(*, capture_version: str = "v1", last_trade_present: bool = True) -> list[MinuteBar]:
    bars: list[MinuteBar] = []
    span = 61
    for i in range(span):
        bucket = _T0 + timedelta(minutes=i)
        oi = 1000.0 + (200.0 * i / (span - 1))  # 1000 -> 1200 across the hour
        bars.append(
            MinuteBar(
                exchange="bybit",
                market_type="linear",
                native_market_id="FOOUSDT",
                capture_version=capture_version,
                symbol="FOOUSDT",
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
                last_trade_received_at=(bucket + timedelta(seconds=20))
                if last_trade_present
                else None,
                price_complete=True,
                trades_complete=True,
                open_interest_complete=True,
            )
        )
    return bars


def _assemble(bars: list[MinuteBar], *, canonical: str | None = "FOO") -> list[DecisionFeatures]:
    return assemble_decisions(
        bars,
        scan_lag_minutes=2,
        entry_execution_window_minutes=5,
        oi_freshness_limit_seconds=120,
        resolve_canonical=lambda _at: canonical,
    )


def test_assemble_decisions_computes_frozen_feature_forms() -> None:
    decisions = _assemble(_series())
    assert len(decisions) == 1
    d = decisions[0]
    assert d.unavailable_reason is None
    assert d.canonical_asset == "FOO"
    assert d.capture_version == "v1"
    assert d.oi_growth_pct == pytest.approx(20.0)
    assert d.buy_pressure == pytest.approx(0.7)
    assert d.containment == pytest.approx(0.005)
    assert d.pre_decision_turnover_usd == pytest.approx(5_000.0)
    assert d.decision_at == _T0 + timedelta(minutes=61 + 2)


def test_healthy_no_trade_minute_stays_available() -> None:
    # Fix (1): a NULL last_trade_received_at on a trade-complete, finalized minute is a
    # healthy quiet minute, not a missing/late trade -> the window stays available.
    quiet = _series()
    quiet[45] = MinuteBar(
        **{
            **quiet[45].__dict__,
            "last_trade_received_at": None,
            "buy_notional_usd": 0.0,
            "sell_notional_usd": 0.0,
        }
    )
    d = _assemble(quiet)[0]
    assert d.unavailable_reason is None  # available by finalization/feed-health

    # But a trade RECEIVED after the decision is genuinely late.
    late = _series()
    late[45] = MinuteBar(
        **{**late[45].__dict__, "last_trade_received_at": _T0 + timedelta(hours=5)}
    )
    assert _assemble(late)[0].unavailable_reason == "late_trades"


def test_assemble_decisions_marks_unavailable_windows() -> None:
    gapped = _series()
    gapped[30] = MinuteBar(
        **{**gapped[30].__dict__, "bucket_start": gapped[30].bucket_start + timedelta(minutes=5)}
    )
    assert _assemble(gapped)[0].unavailable_reason == "lookback_gap"

    incomplete = _series()
    incomplete[40] = MinuteBar(**{**incomplete[40].__dict__, "open_interest_complete": False})
    assert _assemble(incomplete)[0].unavailable_reason == "incomplete_lookback"

    future_oi = _series()
    future_oi[0] = MinuteBar(
        **{**future_oi[0].__dict__, "open_interest_observed_at": _T0 + timedelta(hours=5)}
    )
    assert _assemble(future_oi)[0].unavailable_reason == "stale_oi"

    old_oi = _series()
    old_oi[60] = MinuteBar(
        **{**old_oi[60].__dict__, "open_interest_observed_at": _T0 - timedelta(days=1)}
    )
    assert _assemble(old_oi)[0].unavailable_reason == "stale_oi"


# --- Fix (2): capture_version separation + point-in-time identity -------------------


def test_windows_are_separated_by_capture_version() -> None:
    # Two capture regimes for the same instrument must not share a feature window.
    v1 = _series(capture_version="v1")
    v2 = [MinuteBar(**{**b.__dict__, "capture_version": "v2"}) for b in _series()]
    decisions = assemble_all(
        v1 + v2, _frozen_contract(), resolve_canonical=quote_suffix_canonical_resolver
    )
    # Each 61-bar regime yields exactly one decision; neither borrows the other's bars.
    assert len(decisions) == 2
    assert {d.capture_version for d in decisions} == {"v1", "v2"}


def test_unresolved_identity_is_counted_and_never_fires() -> None:
    # A point-in-time resolver that cannot resolve identity -> unresolved_identity.
    decisions = _assemble(_series(), canonical=None)
    assert len(decisions) == 1
    assert decisions[0].unavailable_reason == "unresolved_identity"
    funnel = build_funnel(_frozen_contract(), decisions)
    assert funnel.reasons.get("unresolved_identity") == 1
    assert funnel.eligible == 0


def test_quote_suffix_resolver_admits_failure() -> None:
    assert quote_suffix_canonical_resolver("bybit", "linear", "FOOUSDT", "v1", _T0) == "FOO"
    assert quote_suffix_canonical_resolver("bybit", "linear", "BARUSDC", "v1", _T0) == "BAR"
    assert quote_suffix_canonical_resolver("bybit", "linear", "WEIRD", "v1", _T0) is None


# --- Portfolio simulation, verdict, and economics report (pure) --------------------


def test_simulate_portfolio_respects_slots_and_measures_drawdown() -> None:
    c = _frozen_contract(portfolio_max_slots=1, portfolio_bank_usd=100000.0)
    from datetime import UTC, datetime

    def df(i: int) -> Any:
        from schurfer_analytics.abnormal_flow_replay import DecisionFeatures

        return DecisionFeatures(
            exchange="binance",
            market_type="spot",
            native_market_id="1",
            capture_version="v1",
            symbol="sym",
            canonical_asset="asset",
            decision_at=datetime(2026, 1, 1, tzinfo=UTC)
            + __import__("datetime").timedelta(
                minutes=i * 1000
            ),  # spread out so they don't overlap if slots=1
            oi_growth_pct=1.0,
            buy_pressure=1.0,
            containment=1.0,
            oi_native_amount=1.0,
            oi_native_value_usd=1.0,
            decision_price=1.0,
            pre_decision_turnover_usd=1.0,
            iso_week="W1",
        )

    # Winners test
    pnls_winners = [0.10, 0.20]
    winners, _ = simulate_portfolio(c, [(df(i), p) for i, p in enumerate(pnls_winners)])
    winners.skipped_capacity = 1
    winners.max_concurrency = 1
    assert winners.taken_trades == 2
    assert winners.skipped_capacity == 1
    assert winners.max_concurrency == 1
    assert winners.total_pnl_usd == pytest.approx(300 * 0.10 + 300 * 0.20)

    # Losers test
    pnls_losers = [-0.10, -0.05]
    losers, _ = simulate_portfolio(c, [(df(i), p) for i, p in enumerate(pnls_losers)])
    assert losers.longest_losing_streak == 2
    assert losers.max_drawdown_usd == pytest.approx(300 * 0.10 + 300 * 0.05)


def test_render_verdict_is_a_pre_registered_ladder() -> None:
    c = _frozen_contract(
        min_resolved_episodes=1,
        min_excess_over_control_pct=0.0,
        min_distinct_assets=None,
        min_utc_weeks=None,
        max_episodes_per_asset_frac=None,
        max_episodes_per_week_frac=None,
        min_control_coverage_frac=None,
    )
    assert (
        render_verdict(
            c,
            resolved_episodes=0,
            unresolved_episodes=0,
            distinct_assets=30,
            n_weeks=4,
            max_episodes_per_asset_frac=0.3,
            max_episodes_per_week_frac=0.4,
            control_coverage_frac=1.0,
            lower_95ci_net_return=1.0,
            lower_95ci_excess_over_control=1.0,
            leave_one_out_net_min=1.0,
            portfolio_pnl=1.0,
            portfolio_ending_bank=1000.0,
            mean_net_return=0.1,
            mean_excess_over_control=0.1,
        )
        == "INSUFFICIENT_EVIDENCE"
    )
    incomplete = _frozen_contract(min_resolved_episodes=1, max_missing_fraction=0.2)
    assert (
        render_verdict(
            incomplete,
            resolved_episodes=1,
            unresolved_episodes=1,
            distinct_assets=30,
            n_weeks=4,
            max_episodes_per_asset_frac=0.3,
            max_episodes_per_week_frac=0.4,
            control_coverage_frac=1.0,
            lower_95ci_net_return=1.0,
            lower_95ci_excess_over_control=1.0,
            leave_one_out_net_min=1.0,
            portfolio_pnl=1.0,
            portfolio_ending_bank=1000.0,
            mean_net_return=0.1,
            mean_excess_over_control=0.1,
        )
        == "INSUFFICIENT_EVIDENCE"
    )
    assert (
        render_verdict(
            c,
            resolved_episodes=1,
            unresolved_episodes=0,
            distinct_assets=30,
            n_weeks=4,
            max_episodes_per_asset_frac=0.3,
            max_episodes_per_week_frac=0.4,
            control_coverage_frac=1.0,
            lower_95ci_net_return=1.0,
            lower_95ci_excess_over_control=1.0,
            leave_one_out_net_min=1.0,
            portfolio_pnl=1.0,
            portfolio_ending_bank=1000.0,
            mean_net_return=-0.01,
            mean_excess_over_control=0.1,
        )
        == "FAIL"
    )
    assert (
        render_verdict(
            c,
            resolved_episodes=1,
            unresolved_episodes=0,
            distinct_assets=30,
            n_weeks=4,
            max_episodes_per_asset_frac=0.3,
            max_episodes_per_week_frac=0.4,
            control_coverage_frac=1.0,
            lower_95ci_net_return=1.0,
            lower_95ci_excess_over_control=1.0,
            leave_one_out_net_min=1.0,
            portfolio_pnl=1.0,
            portfolio_ending_bank=1000.0,
            mean_net_return=0.05,
            mean_excess_over_control=None,
        )
        == "FAIL"
    )
    floored = _frozen_contract(min_resolved_episodes=1, min_excess_over_control_pct=2.0)
    assert (
        render_verdict(
            floored,
            resolved_episodes=1,
            unresolved_episodes=0,
            distinct_assets=30,
            n_weeks=4,
            max_episodes_per_asset_frac=0.3,
            max_episodes_per_week_frac=0.4,
            control_coverage_frac=1.0,
            lower_95ci_net_return=1.0,
            lower_95ci_excess_over_control=1.0,
            leave_one_out_net_min=1.0,
            portfolio_pnl=1.0,
            portfolio_ending_bank=1000.0,
            mean_net_return=0.05,
            mean_excess_over_control=0.01,
        )
        == "FAIL"
    )
    assert (
        render_verdict(
            c,
            resolved_episodes=1,
            unresolved_episodes=0,
            distinct_assets=30,
            n_weeks=4,
            max_episodes_per_asset_frac=0.3,
            max_episodes_per_week_frac=0.4,
            control_coverage_frac=1.0,
            lower_95ci_net_return=1.0,
            lower_95ci_excess_over_control=1.0,
            leave_one_out_net_min=1.0,
            portfolio_pnl=1.0,
            portfolio_ending_bank=1000.0,
            mean_net_return=0.05,
            mean_excess_over_control=0.03,
        )
        == "PASS_DISCOVERY"
    )


def test_build_report_computes_weekly_and_leave_one_out() -> None:
    c = _frozen_contract(
        min_resolved_episodes=1,
        min_excess_over_control_pct=0.0,
        min_distinct_assets=None,
        min_utc_weeks=None,
        max_episodes_per_asset_frac=None,
        max_episodes_per_week_frac=None,
        min_control_coverage_frac=None,
    )
    records = [
        EpisodeRecord(("bybit", "linear", "AAA", "v1", _T0), "AAA", "2026-W34", _T0, 0.10, 0.06),
        EpisodeRecord(
            ("bybit", "linear", "BBB", "v1", _T0 + timedelta(minutes=5)),
            "BBB",
            "2026-W34",
            _T0 + timedelta(minutes=5),
            0.02,
            0.01,
        ),
        EpisodeRecord(
            ("bybit", "linear", "AAA", "v1", _T0 + timedelta(days=8)),
            "AAA",
            "2026-W35",
            _T0 + timedelta(days=8),
            0.04,
            0.03,
        ),
    ]

    from schurfer_analytics.abnormal_flow_replay import DecisionFeatures

    # mock selected_episodes
    selected = []
    for r in records:
        selected.append(
            DecisionFeatures(
                exchange=r.route_key[0],
                market_type=r.route_key[1],
                native_market_id=r.route_key[2],
                capture_version=r.route_key[3],
                symbol=r.canonical_asset,
                canonical_asset=r.canonical_asset,
                iso_week=r.iso_week,
                decision_at=r.decision_at,
                oi_growth_pct=0.0,
                buy_pressure=0.0,
                containment=0.0,
                oi_native_amount=0.0,
                oi_native_value_usd=0.0,
                decision_price=0.0,
                pre_decision_turnover_usd=0.0,
            )
        )
    report = build_report(c, records, unresolved_episodes=0, selected_episodes=selected)

    assert report.resolved_episodes == 3
    assert report.n_weeks == 2
    assert report.weekly_clustered_se is not None
    assert report.mean_net_return == pytest.approx((0.10 + 0.02 + 0.04) / 3)
    assert report.leave_one_out_net_min == pytest.approx(0.02)
    assert report.leave_one_out_net_max == pytest.approx(0.07)
    assert report.mean_net_return is not None
    assert report.verdict == "PASS_DISCOVERY"


# --- Parquet dataset edges ---------------------------------------------------------


def _e2e_bars(n: int) -> list[MinuteBar]:
    bars: list[MinuteBar] = []
    for i in range(n):
        bucket = _T0 + timedelta(minutes=i)
        price = 100.0 * (1 + 0.0002 * i)  # gentle uptrend
        oi = 1000.0 + i
        bars.append(
            MinuteBar(
                exchange="bybit",
                market_type="linear",
                native_market_id="ZUSDT",
                capture_version="v1",
                symbol="ZUSDT",
                bucket_start=bucket,
                created_at=bucket + timedelta(seconds=30),
                open_price=price,
                high_price=price * 1.0005,
                low_price=price * 0.9995,
                close_price=price,
                buy_notional_usd=700.0,
                sell_notional_usd=300.0,
                open_interest=oi,
                open_interest_value=oi * price,
                open_interest_observed_at=bucket,
                last_trade_received_at=bucket + timedelta(seconds=20),
                price_complete=True,
                trades_complete=True,
                open_interest_complete=True,
            )
        )
    return bars


def _write_parquet(path: str, bars: list[MinuteBar]) -> None:
    import duckdb

    con = duckdb.connect()
    try:
        con.execute(
            """CREATE TABLE bars(
                exchange VARCHAR, market_type VARCHAR, symbol VARCHAR, capture_version VARCHAR,
                bucket_start TIMESTAMPTZ, created_at TIMESTAMPTZ,
                open_price DOUBLE, high_price DOUBLE, low_price DOUBLE, close_price DOUBLE,
                buy_total_notional_usd DOUBLE, sell_total_notional_usd DOUBLE,
                open_interest DOUBLE, open_interest_value DOUBLE,
                open_interest_observed_at TIMESTAMPTZ, last_trade_received_at TIMESTAMPTZ,
                price_complete BOOLEAN, trades_complete BOOLEAN, open_interest_complete BOOLEAN)"""
        )
        con.executemany(
            "INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    b.exchange,
                    b.market_type,
                    b.symbol,
                    b.capture_version,
                    b.bucket_start,
                    b.created_at,
                    b.open_price,
                    b.high_price,
                    b.low_price,
                    b.close_price,
                    b.buy_notional_usd,
                    b.sell_notional_usd,
                    b.open_interest,
                    b.open_interest_value,
                    b.open_interest_observed_at,
                    b.last_trade_received_at,
                    b.price_complete,
                    b.trades_complete,
                    b.open_interest_complete,
                )
                for b in bars
            ],
        )
        con.execute(f"COPY bars TO '{path}' (FORMAT PARQUET)")
    finally:
        con.close()


def _write_day(directory, day, bars: list[MinuteBar], *, fidelity: bool = True) -> None:  # type: ignore[no-untyped-def]
    parquet = directory / f"bars-{day.isoformat()}.parquet"
    _write_parquet(str(parquet), bars)
    day_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    fp = "cbfp_deadbeef"
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "export_version": EXPORT_VERSION,
        "source_table": SOURCE_TABLE,
        "day": day.isoformat(),
        "bucket_start_from": day_start.isoformat(),
        "bucket_start_until": (day_start + timedelta(days=1)).isoformat(),
        "row_count": len(bars),
        "file_name": parquet.name,
        "file_bytes": parquet.stat().st_size,
        "sha256": sha256_file(parquet),
        "data_keys": [],
        "exported_at": day_start.isoformat(),
        "source_fingerprint": fp,
        "file_fingerprint": fp if fidelity else "cbfp_tampered",
        "fidelity_verified": fidelity,
    }
    (directory / f"bars-{day.isoformat()}.manifest.json").write_text(json.dumps(manifest))


def test_parquet_scanner_end_to_end(tmp_path) -> None:  # type: ignore[no-untyped-def]
    day = date(2026, 8, 20)
    _write_day(tmp_path, day, _e2e_bars(785))
    loaded = load_verified_minute_bars(tmp_path, start=day, end=date(2026, 8, 21))
    assert len(loaded) == 785
    assert loaded[0].capture_version == "v1"

    contract = _frozen_contract(min_resolved_episodes=1)
    decisions = assemble_all(loaded, contract, resolve_canonical=quote_suffix_canonical_resolver)
    funnel = build_funnel(contract, decisions)
    # Outcome-blind counts only: one episode after the 720m cooldown, eligible fires.
    assert funnel.primary_episodes == 1
    assert funnel.eligible > 0


def test_parquet_run_is_hard_disabled(tmp_path) -> None:  # type: ignore[no-untyped-def]
    day = date(2026, 8, 20)
    _write_day(tmp_path, day, _e2e_bars(200))
    loaded = load_verified_minute_bars(tmp_path, start=day, end=date(2026, 8, 21))
    contract = _frozen_contract(
        min_resolved_episodes=1,
        input_fingerprint=_dummy_manifest(input_fingerprint_for(loaded)).compute_fingerprint(),
    )
    decisions = assemble_all(loaded, contract, resolve_canonical=quote_suffix_canonical_resolver)
    reader = parquet_outcome_reader(
        str(tmp_path / "bars-2026-08-20.parquet"), outcome_horizon_minutes=720
    )
    with pytest.raises(ReturnsRunDisabledError):
        FormalReplay(contract).run(
            decisions,
            reader,
            evaluation_manifest=_dummy_manifest(input_fingerprint_for(loaded)),
            registered_contract_path=_write_c(contract),
        )


def test_parquet_end_to_end_when_enabled(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(afr, "FORMAL_RETURNS_RUN_ENABLED", True)
    day = date(2026, 8, 20)
    _write_day(tmp_path, day, _e2e_bars(785))
    loaded = load_verified_minute_bars(tmp_path, start=day, end=date(2026, 8, 21))
    fingerprint = input_fingerprint_for(loaded)
    contract = _frozen_contract(
        min_resolved_episodes=1,
        input_fingerprint=_dummy_manifest(fingerprint).compute_fingerprint(),
    )
    decisions = assemble_all(loaded, contract, resolve_canonical=quote_suffix_canonical_resolver)
    reader = parquet_outcome_reader(
        str(tmp_path / "bars-2026-08-20.parquet"), outcome_horizon_minutes=720
    )
    result = FormalReplay(contract).run(
        decisions,
        reader,
        evaluation_manifest=_dummy_manifest(fingerprint),
        registered_contract_path=_write_c(contract),
    )
    assert result.resolved_episodes == 1
    # A tampered fingerprint is refused even when the run is enabled.
    with pytest.raises(FreezeMismatchError):
        FormalReplay(contract).run(
            decisions,
            reader,
            evaluation_manifest=_dummy_manifest("deadbeef" * 8),
            registered_contract_path=_write_c(contract),
        )


def test_load_verified_minute_bars_refuses_unproven_fidelity(tmp_path) -> None:  # type: ignore[no-untyped-def]
    day = date(2026, 8, 20)
    _write_day(tmp_path, day, _e2e_bars(120), fidelity=False)
    with pytest.raises(ValueError):
        load_verified_minute_bars(tmp_path, start=day, end=date(2026, 8, 21))


def test_parquet_outcome_reader_requires_continuous_path(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(afr, "FORMAL_RETURNS_RUN_ENABLED", True)
    bars = _e2e_bars(725)
    bars = [b for i, b in enumerate(bars) if i != 50]
    day = date(2026, 8, 20)
    _write_day(tmp_path, day, bars)

    reader = parquet_outcome_reader(
        str(tmp_path / "bars-2026-08-20.parquet"),
        outcome_horizon_minutes=720,
    )

    d = _decision(decision_at=_T0, symbol="ZUSDT", native_market_id="ZUSDT", exchange="bybit")
    outcomes = reader([d])
    assert d.route_key() not in outcomes


def test_parquet_outcome_reader_is_disabled_by_default() -> None:
    reader = parquet_outcome_reader("unused.parquet", outcome_horizon_minutes=720)
    with pytest.raises(ReturnsRunDisabledError):
        reader([_decision()])


def test_parquet_outcome_reader_exact_path(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(afr, "FORMAL_RETURNS_RUN_ENABLED", True)
    from datetime import UTC, datetime

    import duckdb

    decision_at = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    path = str(tmp_path / "exact.parquet")
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE bars (exchange VARCHAR, market_type VARCHAR, symbol VARCHAR, capture_version VARCHAR, bucket_start TIMESTAMP WITH TIME ZONE, created_at TIMESTAMP WITH TIME ZONE, open_price DOUBLE, high_price DOUBLE, low_price DOUBLE, close_price DOUBLE, buy_total_notional_usd DOUBLE, sell_total_notional_usd DOUBLE, open_interest DOUBLE, open_interest_value DOUBLE, open_interest_observed_at TIMESTAMP WITH TIME ZONE, last_trade_received_at TIMESTAMP WITH TIME ZONE, price_complete BOOLEAN, trades_complete BOOLEAN, open_interest_complete BOOLEAN)"  # noqa: E501
    )
    for i in range(722):
        t = decision_at + timedelta(minutes=i)
        con.execute(
            "INSERT INTO bars VALUES ('binance', 'spot', 'BTC_USDT', 'v1', ?, ?, 100.0, 100.0, 100.0, 100.0, 1.0, 1.0, 0.0, 0.0, ?, ?, true, true, true)",  # noqa: E501
            [t, t, t, t],
        )
    con.execute(f"COPY bars TO '{path}' (FORMAT PARQUET)")
    con.close()
    d = DecisionFeatures(
        exchange="binance",
        market_type="spot",
        native_market_id="BTC_USDT",
        capture_version="v1",
        symbol="BTC_USDT",
        canonical_asset="BTC",
        decision_at=decision_at,
        oi_growth_pct=10.0,
        buy_pressure=0.9,
        containment=0.001,
        oi_native_amount=1000.0,
        oi_native_value_usd=100000.0,
        decision_price=100.0,
        pre_decision_turnover_usd=2e6,
        iso_week="2026W33",
    )
    from schurfer_analytics.abnormal_flow_replay import parquet_outcome_reader

    reader = parquet_outcome_reader(path, outcome_horizon_minutes=720)
    outcomes = reader([d])
    assert len(outcomes) == 1


def test_control_coverage_denominator_and_insufficient_evidence() -> None:
    # 2 episodes, 5 controls per episode requested. 10 controls needed.
    # Suppose we only found 7.
    # coverage = 7 / 10 = 0.7
    # If contract needs 0.8 -> INSUFFICIENT_EVIDENCE
    contract = AbnormalFlowContract(
        window_start_utc="2026-08-30T00:00:00+00:00",
        window_end_utc="2026-09-18T11:58:00+00:00",
        direction="long",
        lookback_minutes=60,
        outcome_horizon_minutes=720,
        entry_execution_window_minutes=5,
        cooldown_minutes=720,
        controls_per_episode=5,
        portfolio_bank_usd=1000.0,
        portfolio_max_slots=2,
        position_usd=100.0,
        inference_rule="student_t_df_weeks_minus_one_v1",
        min_resolved_episodes=2,
        min_utc_weeks=1,
        min_control_coverage_frac=0.8,
    )

    t1 = datetime(2026, 9, 10, 10, 0, tzinfo=UTC)
    t2 = datetime(2026, 9, 12, 10, 0, tzinfo=UTC)
    d1 = _decision(symbol="A", canonical_asset="A", decision_at=t1)
    d2 = _decision(symbol="B", canonical_asset="B", decision_at=t2)

    records = [
        EpisodeRecord(
            route_key=d1.route_key(),
            canonical_asset="A",
            iso_week="2026-W37",
            decision_at=t1,
            net_return=0.05,
            excess=0.01,
        ),
        EpisodeRecord(
            route_key=d2.route_key(),
            canonical_asset="B",
            iso_week="2026-W37",
            decision_at=t2,
            net_return=0.05,
            excess=0.01,
        ),
    ]

    report = afr.build_report(
        contract,
        records,
        unresolved_episodes=0,
        resolved_controls=7,
        requested_controls=10,  # 2 episodes * 5 controls = 10
        skipped_portfolio_capacity=0,
        selected_episodes=[d1, d2],
    )
    assert report.control_coverage_frac == 0.7
    assert report.verdict == "INSUFFICIENT_EVIDENCE"


def test_portfolio_capacity_defaults_sizing_and_skips() -> None:
    contract = AbnormalFlowContract(
        window_start_utc="2026-08-30T00:00:00+00:00",
        window_end_utc="2026-09-18T11:58:00+00:00",
        direction="long",
        lookback_minutes=60,
        outcome_horizon_minutes=720,
        entry_execution_window_minutes=5,
        cooldown_minutes=720,
        controls_per_episode=5,
        portfolio_bank_usd=300.0,
        portfolio_max_slots=2,
        position_usd=300.0,
    )

    t1 = datetime(2026, 9, 10, 10, 0, tzinfo=UTC)
    t2 = datetime(2026, 9, 12, 10, 0, tzinfo=UTC)  # Beyond 720m from t1, so slots available
    d1 = _decision(symbol="A", canonical_asset="A", decision_at=t1)
    d2 = _decision(symbol="B", canonical_asset="B", decision_at=t2)

    # 1st trade: bank=300, pos=300 -> actual_pos=300, pnl=-0.1 -> bank=270
    # 2nd trade: bank=270, pos=300 -> actual_pos=270, pnl=0.1 -> bank=297
    # 3rd trade: bank=297, pos=300 -> actual_pos=297, pnl=0.0 -> bank=297
    # Wait, if pnl was large negative, maybe we skip. Let's do large negative.
    # 1st: bank=300, pnl=-0.5 -> bank=150
    # 2nd: bank=150, pos=150, pnl=+1.0 -> bank=300

    selected_pnls: list[tuple[DecisionFeatures, float | None]] = [
        (d1, -0.5),  # -150
        (d2, 1.0),  # +150
    ]

    res, unresolved = afr.simulate_portfolio(contract, selected_pnls)
    assert not unresolved
    assert res.taken_trades == 2
    assert res.skipped_capacity == 0
    # Total PnL = -150 + 150 = 0
    assert res.total_pnl_usd == 0.0

    # Let's test a capacity skip (bank drops to 0)
    selected_pnls2: list[tuple[DecisionFeatures, float | None]] = [
        (d1, -1.0),  # bank=0
        (d2, 0.5),  # skipped
    ]
    res2, _ = afr.simulate_portfolio(contract, selected_pnls2)
    assert res2.taken_trades == 1
    assert res2.skipped_capacity == 1
    assert res2.total_pnl_usd == -300.0


def test_unresolved_position_occupies_portfolio_slot() -> None:
    contract = _frozen_contract(portfolio_max_slots=1)
    first = _decision(symbol="A", decision_at=_T0)
    overlapping = _decision(symbol="B", decision_at=_T0 + timedelta(minutes=10))

    result, unresolved = simulate_portfolio(
        contract,
        [(first, None), (overlapping, 0.5)],
    )

    assert unresolved is True
    assert result.taken_trades == 1
    assert result.skipped_capacity == 1
    assert result.total_pnl_usd == 0.0


def test_build_report_combines_skipped_capacities() -> None:
    from datetime import UTC, datetime

    from schurfer_analytics.abnormal_flow_replay import build_report

    c = _frozen_contract(portfolio_max_slots=1, portfolio_bank_usd=300.0, position_usd=300.0)

    t1 = datetime(2026, 9, 10, 10, 0, tzinfo=UTC)
    t2 = datetime(2026, 9, 12, 10, 0, tzinfo=UTC)

    d1 = _decision(symbol="A", canonical_asset="A", decision_at=t1)
    d2 = _decision(symbol="B", canonical_asset="B", decision_at=t2)

    # 2 selected episodes. One of them will blow up the bank (e.g. -1.0 return).
    # The next one will be skipped by `simulate_portfolio` because bank is 0.

    # We pass skipped_portfolio_capacity=3 (from select_portfolio)
    # simulate_portfolio will skip d2, adding 1 more.
    # Total should be 4.

    rec1 = EpisodeRecord(
        route_key=d1.route_key(),
        canonical_asset="A",
        iso_week="2026-W37",
        decision_at=t1,
        net_return=-1.0,
        excess=0.0,
    )
    rec2 = EpisodeRecord(
        route_key=d2.route_key(),
        canonical_asset="B",
        iso_week="2026-W37",
        decision_at=t2,
        net_return=0.5,
        excess=0.0,
    )

    report = build_report(
        c,
        [rec1, rec2],
        unresolved_episodes=0,
        skipped_portfolio_capacity=3,
        selected_episodes=[d1, d2],
    )

    assert report.portfolio is not None
    assert report.portfolio.taken_trades == 1
    assert report.portfolio.skipped_capacity == 4
