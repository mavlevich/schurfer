"""Tests for the abnormal-flow replay engine.

The load-bearing tests are that no forward return can be read until the contract is
frozen AND the scored data matches the freeze (fingerprint + registered window); the
rest pin the pure decision logic (OI->USD per venue, participation, eligibility,
primary/ablation cells, episode cooldown, control matching, priced-proxy economics,
route-keyed outcomes) and the outcome-blind feature assembly, all with synthetic rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.abnormal_flow_replay import (
    DecisionFeatures,
    EpisodeRecord,
    FormalReplay,
    FreezeMismatchError,
    MinuteBar,
    Outcome,
    RouteKey,
    ablation_cell_fires,
    assemble_all,
    assemble_decisions,
    build_report,
    control_band_key,
    form_episodes,
    input_fingerprint_for,
    is_eligible,
    load_minute_bars_from_parquet,
    match_controls,
    oi_notional_usd,
    parquet_outcome_reader,
    participation_frac,
    primary_cell_fires,
    proxy_net_return,
    render_verdict,
    simulate_portfolio,
)
from schurfer_analytics.abnormal_flow_screen import AbnormalFlowContract, NotFrozenError

_T0 = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
_FINGERPRINT = "a" * 64


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
        entry_cost_bps=5.0,
        slippage_bps=10.0,
        fee_bps=5.0,
        funding_model="conservative_8h_v1",
        min_resolved_episodes=100,
        max_missing_fraction=0.2,
        min_excess_over_control_pct=0.0,
        window_start_utc="2026-08-14T00:00:00+00:00",
        window_end_utc="2026-09-14T00:00:00+00:00",
        input_fingerprint=_FINGERPRINT,
    )
    base.update(overrides)
    return AbnormalFlowContract(**base)  # type: ignore[arg-type]


def _decision(**overrides: object) -> DecisionFeatures:
    base = dict(
        exchange="bybit",
        market_type="linear",
        native_market_id="FOOUSDT",
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
        symbol=d.symbol,
        decision_at=d.decision_at,
        entry_price=entry,
        exit_price=exit_,
    )


def _reader_from(prices: dict[str, tuple[float, float]]):
    """A well-behaved reader: it returns an outcome ONLY for the rows it was asked for,
    keyed by the exact native route, using per-native-market-id (entry, exit) prices."""

    def reader(requested: object) -> dict[RouteKey, Outcome]:
        out: dict[RouteKey, Outcome] = {}
        for d in requested:  # type: ignore[attr-defined]
            entry, exit_ = prices[d.native_market_id]
            out[d.route_key()] = _outcome(d, entry=entry, exit_=exit_)
        return out

    return reader


# --- The load-bearing invariants ---------------------------------------------------


def test_formal_run_refuses_to_read_returns_before_freeze() -> None:
    called = False

    def reader(_req: object) -> dict[RouteKey, Outcome]:
        nonlocal called
        called = True  # pragma: no cover - must never run
        return {}

    replay = FormalReplay(AbnormalFlowContract())  # default: not frozen
    with pytest.raises(NotFrozenError):
        replay.run([_decision()], reader, observed_input_fingerprint=_FINGERPRINT)
    assert called is False, "returns were read against an unfrozen contract"


def test_formal_run_refuses_on_fingerprint_mismatch() -> None:
    called = False

    def reader(_req: object) -> dict[RouteKey, Outcome]:
        nonlocal called
        called = True  # pragma: no cover - must never run
        return {}

    with pytest.raises(FreezeMismatchError):
        FormalReplay(_frozen_contract()).run(
            [_decision()], reader, observed_input_fingerprint="b" * 64
        )
    assert called is False


def test_formal_run_refuses_a_decision_outside_the_registered_window() -> None:
    called = False

    def reader(_req: object) -> dict[RouteKey, Outcome]:
        nonlocal called
        called = True  # pragma: no cover - must never run
        return {}

    # A decision dated before window_start must not be scored under this window.
    early = _decision(decision_at=datetime(2026, 8, 1, tzinfo=UTC))
    with pytest.raises(FreezeMismatchError):
        FormalReplay(_frozen_contract()).run(
            [early], reader, observed_input_fingerprint=_FINGERPRINT
        )
    assert called is False


def test_frozen_run_scores_excess_with_a_reader_that_returns_only_requested() -> None:
    ep = _decision()
    control = _decision(  # eligible, same band, non-firing
        symbol="BARUSDT", native_market_id="BARUSDT", canonical_asset="BAR", buy_pressure=0.4
    )
    reader = _reader_from({"FOOUSDT": (100.0, 110.0), "BARUSDT": (100.0, 101.0)})

    result = FormalReplay(_frozen_contract()).run(
        [ep, control], reader, observed_input_fingerprint=_FINGERPRINT
    )
    assert result.resolved_episodes == 1
    assert result.episodes_with_matched_control == 1
    assert result.resolved_controls == 1
    assert result.mean_excess_over_control is not None and result.mean_excess_over_control > 0.05


def test_outcome_lookup_does_not_conflate_venues_sharing_a_symbol() -> None:
    # Same symbol + minute on two venues must resolve to two distinct outcomes.
    on_bybit = _decision(exchange="bybit", native_market_id="XUSDT", symbol="XUSDT")
    on_binance = _decision(
        exchange="binance",
        native_market_id="XUSDT",
        symbol="XUSDT",
        oi_native_value_usd=None,  # Binance has no USD OI value; amount x price is used
    )

    # Give each venue a different exit, keyed on the exact native route.
    def route_reader(requested: object) -> dict[RouteKey, Outcome]:
        out: dict[RouteKey, Outcome] = {}
        for d in requested:  # type: ignore[attr-defined]
            exit_ = 110.0 if d.exchange == "bybit" else 90.0
            out[d.route_key()] = _outcome(d, entry=100.0, exit_=exit_)
        return out

    result = FormalReplay(_frozen_contract()).run(
        [on_bybit, on_binance], route_reader, observed_input_fingerprint=_FINGERPRINT
    )
    assert result.resolved_episodes == 2  # neither overwrote the other
    # One venue up 10%, the other down 10%; the mean reflects both, not a single dupe.
    assert result.mean_net_return is not None and abs(result.mean_net_return) < 0.02


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
    assert ablation_cell_fires(c, _decision()) is True
    low_oi = _decision(oi_growth_pct=1.0)
    assert primary_cell_fires(c, low_oi) is False
    assert ablation_cell_fires(c, low_oi) is True
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
    r = proxy_net_return(c, 100.0, 110.0)
    assert r is not None and r == pytest.approx(0.10 - (5 + 10 + 10 + 3 * 2) / 10_000.0)
    assert proxy_net_return(c, None, 110.0) is None
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
                market_type="linear",
                native_market_id="FOOUSDT",
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
                last_trade_received_at=bucket + timedelta(seconds=20),
                price_complete=True,
                trades_complete=True,
                open_interest_complete=True,
            )
        )
    return bars


def _assemble(bars: list[MinuteBar]):
    return assemble_decisions(
        bars, scan_lag_minutes=2, entry_execution_window_minutes=5, oi_freshness_limit_seconds=120
    )


def test_assemble_decisions_computes_frozen_feature_forms() -> None:
    decisions = _assemble(_series())
    assert len(decisions) == 1
    d = decisions[0]
    assert d.unavailable_reason is None
    assert d.oi_growth_pct == pytest.approx(20.0)
    assert d.buy_pressure == pytest.approx(0.7)
    assert d.containment == pytest.approx(0.005)
    assert d.decision_price == pytest.approx(100.0)
    assert d.pre_decision_turnover_usd == pytest.approx(5_000.0)
    assert d.native_market_id == "FOOUSDT"
    assert d.decision_at == _T0 + timedelta(minutes=61 + 2)


def test_assemble_decisions_marks_unavailable_windows() -> None:
    gapped = _series()
    gapped[30] = MinuteBar(
        **{**gapped[30].__dict__, "bucket_start": gapped[30].bucket_start + timedelta(minutes=5)}
    )
    assert _assemble(gapped)[0].unavailable_reason == "lookback_gap"

    incomplete = _series()
    incomplete[40] = MinuteBar(**{**incomplete[40].__dict__, "open_interest_complete": False})
    assert _assemble(incomplete)[0].unavailable_reason == "incomplete_lookback"

    # OI observed after the decision (future).
    future_oi = _series()
    future_oi[0] = MinuteBar(
        **{**future_oi[0].__dict__, "open_interest_observed_at": _T0 + timedelta(hours=5)}
    )
    assert _assemble(future_oi)[0].unavailable_reason == "stale_oi"

    # OI observed far too long BEFORE its bar (the colleague's day-old OI repro).
    old_oi = _series()
    old_oi[60] = MinuteBar(
        **{**old_oi[60].__dict__, "open_interest_observed_at": _T0 - timedelta(days=1)}
    )
    assert _assemble(old_oi)[0].unavailable_reason == "stale_oi"

    # A bar whose last trade was not received by the decision instant.
    late_trade = _series()
    late_trade[45] = MinuteBar(
        **{**late_trade[45].__dict__, "last_trade_received_at": _T0 + timedelta(hours=5)}
    )
    assert _assemble(late_trade)[0].unavailable_reason == "late_or_missing_trades"


# --- Portfolio simulation, verdict, and economics report ---------------------------


def test_simulate_portfolio_respects_slots_and_measures_drawdown() -> None:
    c = _frozen_contract(portfolio_max_slots=1)  # one slot, $300 position
    # ep2 arrives while the slot is still held by ep1 (720m horizon) -> skipped.
    winners = simulate_portfolio(
        c,
        [
            (_T0, 0.10),
            (_T0 + timedelta(minutes=5), -0.05),  # slot busy -> capacity skip
            (_T0 + timedelta(minutes=800), 0.20),  # slot free again -> taken
        ],
    )
    assert winners.taken_trades == 2
    assert winners.skipped_capacity == 1
    assert winners.max_concurrency == 1
    assert winners.total_pnl_usd == pytest.approx(300 * 0.10 + 300 * 0.20)

    losers = simulate_portfolio(c, [(_T0, -0.10), (_T0 + timedelta(minutes=800), -0.05)])
    assert losers.longest_losing_streak == 2
    assert losers.max_drawdown_usd == pytest.approx(300 * 0.10 + 300 * 0.05)


def test_render_verdict_is_a_pre_registered_ladder() -> None:
    c = _frozen_contract(min_resolved_episodes=1, min_excess_over_control_pct=0.0)
    # Underpowered.
    assert (
        render_verdict(
            c,
            resolved_episodes=0,
            unresolved_episodes=0,
            mean_net_return=0.1,
            mean_excess_over_control=0.1,
        )
        == "INSUFFICIENT_EVIDENCE"
    )
    # Too incomplete.
    incomplete = _frozen_contract(min_resolved_episodes=1, max_missing_fraction=0.2)
    assert (
        render_verdict(
            incomplete,
            resolved_episodes=1,
            unresolved_episodes=1,  # missing = 0.5 > 0.2
            mean_net_return=0.1,
            mean_excess_over_control=0.1,
        )
        == "INSUFFICIENT_EVIDENCE"
    )
    # Non-positive net, or missing/insufficient excess -> FAIL.
    assert (
        render_verdict(
            c,
            resolved_episodes=1,
            unresolved_episodes=0,
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
            mean_net_return=0.05,
            mean_excess_over_control=0.01,  # 1% < 2% floor
        )
        == "FAIL"
    )
    # Positive net AND sufficient excess -> discovery pass (never live).
    assert (
        render_verdict(
            c,
            resolved_episodes=1,
            unresolved_episodes=0,
            mean_net_return=0.05,
            mean_excess_over_control=0.03,
        )
        == "PASS_DISCOVERY"
    )


def test_build_report_computes_weekly_and_leave_one_out() -> None:
    c = _frozen_contract(min_resolved_episodes=1, min_excess_over_control_pct=0.0)
    records = [
        EpisodeRecord("AAA", "2026-W34", _T0, 0.10, 0.06),
        EpisodeRecord("BBB", "2026-W34", _T0 + timedelta(minutes=5), 0.02, 0.01),
        EpisodeRecord("AAA", "2026-W35", _T0 + timedelta(days=8), 0.04, 0.03),
    ]
    report = build_report(c, records, unresolved_episodes=0)
    assert report.resolved_episodes == 3
    assert report.n_weeks == 2
    assert report.weekly_clustered_se is not None
    assert report.mean_net_return == pytest.approx((0.10 + 0.02 + 0.04) / 3)
    # Drop AAA -> only BBB's 0.01; drop BBB -> AAA's mean(0.06, 0.03) = 0.045.
    assert report.leave_one_out_excess_min == pytest.approx(0.01)
    assert report.leave_one_out_excess_max == pytest.approx(0.045)
    assert report.break_even_extra_cost_bps == pytest.approx(report.mean_net_return * 10_000)
    assert report.verdict == "PASS_DISCOVERY"


def test_frozen_run_populates_the_report() -> None:
    c = _frozen_contract(min_resolved_episodes=1)
    ep = _decision()
    control = _decision(
        symbol="BARUSDT", native_market_id="BARUSDT", canonical_asset="BAR", buy_pressure=0.4
    )
    reader = _reader_from({"FOOUSDT": (100.0, 110.0), "BARUSDT": (100.0, 101.0)})
    result = FormalReplay(c).run([ep, control], reader, observed_input_fingerprint=_FINGERPRINT)
    assert result.report.resolved_episodes == 1
    assert result.report.portfolio.taken_trades == 1
    assert result.report.verdict in {"INSUFFICIENT_EVIDENCE", "FAIL", "PASS_DISCOVERY"}
    assert len(result.episode_records) == 1


# --- Parquet end-to-end (synthetic data; proves the full dataset code path) ---------


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
                symbol="ZUSDT",
                canonical_asset="Z",
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
                exchange VARCHAR, market_type VARCHAR, symbol VARCHAR,
                bucket_start TIMESTAMPTZ, created_at TIMESTAMPTZ,
                open_price DOUBLE, high_price DOUBLE, low_price DOUBLE, close_price DOUBLE,
                buy_total_notional_usd DOUBLE, sell_total_notional_usd DOUBLE,
                open_interest DOUBLE, open_interest_value DOUBLE,
                open_interest_observed_at TIMESTAMPTZ, last_trade_received_at TIMESTAMPTZ,
                price_complete BOOLEAN, trades_complete BOOLEAN, open_interest_complete BOOLEAN)"""
        )
        con.executemany(
            "INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    b.exchange,
                    b.market_type,
                    b.symbol,
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


def test_parquet_end_to_end_pipeline(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = str(tmp_path / "bars.parquet")
    _write_parquet(path, _e2e_bars(785))
    window_start = datetime(2026, 8, 14, tzinfo=UTC)
    window_end = datetime(2026, 9, 14, tzinfo=UTC)

    loaded = load_minute_bars_from_parquet(path, window_start=window_start, window_end=window_end)
    assert len(loaded) == 785
    assert loaded[0].canonical_asset == "Z"  # loader resolved the placeholder canonical

    fingerprint = input_fingerprint_for(loaded)
    contract = _frozen_contract(min_resolved_episodes=1, input_fingerprint=fingerprint)
    decisions = assemble_all(loaded, contract)
    reader = parquet_outcome_reader(path, outcome_horizon_minutes=contract.outcome_horizon_minutes)

    result = FormalReplay(contract).run(decisions, reader, observed_input_fingerprint=fingerprint)
    assert result.resolved_episodes == 1  # one episode after the 720m cooldown
    assert result.report.portfolio.taken_trades == 1
    assert result.mean_net_return is not None and result.mean_net_return > 0


def test_parquet_end_to_end_refuses_on_a_tampered_fingerprint(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = str(tmp_path / "bars.parquet")
    _write_parquet(path, _e2e_bars(200))
    loaded = load_minute_bars_from_parquet(
        path,
        window_start=datetime(2026, 8, 14, tzinfo=UTC),
        window_end=datetime(2026, 9, 14, tzinfo=UTC),
    )
    contract = _frozen_contract(
        min_resolved_episodes=1, input_fingerprint=input_fingerprint_for(loaded)
    )
    decisions = assemble_all(loaded, contract)
    reader = parquet_outcome_reader(path, outcome_horizon_minutes=contract.outcome_horizon_minutes)
    # A loader that read a different dataset (wrong fingerprint) is refused.
    with pytest.raises(FreezeMismatchError):
        FormalReplay(contract).run(decisions, reader, observed_input_fingerprint="deadbeef" * 8)
