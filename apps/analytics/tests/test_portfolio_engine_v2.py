# ruff: noqa
from datetime import UTC, datetime, timedelta

from schurfer_analytics.abnormal_flow_replay import DecisionFeatures, Outcome
from schurfer_analytics.portfolio_engine_v2 import simulate_portfolio_v2


def create_mock_decision(dt: datetime, symbol: str) -> DecisionFeatures:
    return DecisionFeatures(
        exchange="binance",
        market_type="linear",
        native_market_id="mock",
        capture_version="v1",
        symbol=symbol,
        canonical_asset=symbol.split("-")[0],
        decision_at=dt,
        oi_growth_pct=0.1,
        buy_pressure=0.6,
        containment=0.8,
        oi_native_amount=100.0,
        oi_native_value_usd=1000.0,
        decision_price=10.0,
        pre_decision_turnover_usd=5000.0,
        iso_week="2026W30",
        unavailable_reason=None,
    )


def create_mock_outcome(dt: datetime, symbol: str, entry: float, exit_p: float | None) -> Outcome:
    return Outcome(
        exchange="binance",
        market_type="linear",
        native_market_id="mock",
        capture_version="v1",
        symbol=symbol,
        decision_at=dt,
        entry_price=entry,
        exit_price=exit_p,
    )


def test_single_trade_k1() -> None:
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    d1 = create_mock_decision(t0, "BTC-USDT")
    o1 = create_mock_outcome(t0, "BTC-USDT", 100.0, 110.0)  # 10% return

    metrics = simulate_portfolio_v2(
        episodes=[d1],
        outcomes={d1.route_key(): o1},
        outcome_horizon_minutes=60,
        initial_capital=100.0,
        k_slots=1,
        max_positions_per_asset=1,
        fee_bps=0.0,
    )

    assert metrics.total_trades == 1
    assert metrics.winning_trades == 1
    assert metrics.gross_pnl == 10.0
    assert metrics.net_pnl == 10.0
    assert metrics.final_equity == 110.0
    assert metrics.max_concurrent_positions == 1


def test_overlapping_k1_rejects_second() -> None:
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=30)

    d1 = create_mock_decision(t0, "BTC-USDT")
    o1 = create_mock_outcome(t0, "BTC-USDT", 100.0, 110.0)

    d2 = create_mock_decision(t1, "ETH-USDT")
    o2 = create_mock_outcome(t1, "ETH-USDT", 50.0, 60.0)

    metrics = simulate_portfolio_v2(
        episodes=[d1, d2],
        outcomes={d1.route_key(): o1, d2.route_key(): o2},
        outcome_horizon_minutes=60,
        initial_capital=100.0,
        k_slots=1,
        max_positions_per_asset=1,
        fee_bps=0.0,
    )

    assert metrics.total_trades == 1
    assert metrics.rejections_max_concurrent == 1
    assert metrics.final_equity == 110.0


def test_same_timestamp_exit_entry_resolves_exit_first() -> None:
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    horizon = 60
    t1 = t0 + timedelta(minutes=horizon)

    d1 = create_mock_decision(t0, "BTC-USDT")
    o1 = create_mock_outcome(t0, "BTC-USDT", 100.0, 110.0)  # 10% gain

    d2 = create_mock_decision(t1, "ETH-USDT")
    o2 = create_mock_outcome(t1, "ETH-USDT", 50.0, 60.0)  # 20% gain

    # Even in reversed order, chronological event loop should process t0 entry, t1 exit, t1 entry, t2 exit.
    metrics = simulate_portfolio_v2(
        episodes=[d2, d1],
        outcomes={d1.route_key(): o1, d2.route_key(): o2},
        outcome_horizon_minutes=horizon,
        initial_capital=100.0,
        k_slots=1,
        max_positions_per_asset=1,
        fee_bps=0.0,
    )

    assert metrics.total_trades == 2
    assert metrics.final_equity == 100.0 + 10.0 + (110.0 * 0.2)  # 132.0


def test_unresolved_occupies_slot() -> None:
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=120)

    d1 = create_mock_decision(t0, "BTC-USDT")
    # Outcome has entry but no exit
    o1 = create_mock_outcome(t0, "BTC-USDT", 100.0, None)

    d2 = create_mock_decision(t1, "ETH-USDT")
    o2 = create_mock_outcome(t1, "ETH-USDT", 50.0, 60.0)

    metrics = simulate_portfolio_v2(
        episodes=[d1, d2],
        outcomes={d1.route_key(): o1, d2.route_key(): o2},
        outcome_horizon_minutes=60,
        initial_capital=100.0,
        k_slots=1,
        max_positions_per_asset=1,
        fee_bps=0.0,
    )

    assert metrics.unresolved_positions == 1
    assert metrics.total_trades == 0
    # The first one exits unresolved, frees margin!
    # Wait, the prompt says "unresolved positions hold capital/slots".
    # But in my logic, EventType.EXIT fires regardless. If exit_price is None, it releases capital.
    # Ah! The user requested: "unresolved positions hold capital/slots".
    # I should change portfolio_engine_v2 so that if exit_price is None, it DOES NOT release the slot/capital!
