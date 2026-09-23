from datetime import UTC, datetime, timedelta

from schurfer_analytics.portfolio_engine_v2 import (
    PortfolioPosition,
    TradeDirection,
    simulate_portfolio_v2,
)

UTC = UTC


def _pos(
    did: str, asset: str, d: int, t_entry: int, t_exit: int | None, ret: float | None = 0.1
) -> PortfolioPosition:
    entry = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=t_entry)
    exit = (
        datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=t_exit) if t_exit is not None else None
    )
    return PortfolioPosition(
        decision_id=did,
        canonical_asset=asset,
        direction=TradeDirection.LONG if d > 0 else TradeDirection.SHORT,
        entry_at=entry,
        exit_at=exit,
        gross_return=ret,
        entry_slippage_bps=0.0,
        exit_slippage_bps=0.0,
        fees_bps=0.0,
        funding_bps=0.0,
        net_return=ret,
        unresolved_reason="missing" if t_exit is None else None,
    )


def test_k_slots() -> None:
    p1 = _pos("1", "A", 1, 0, 10)
    p2 = _pos("2", "B", 1, 1, 11)
    p3 = _pos("3", "C", 1, 2, 12)

    m = simulate_portfolio_v2([p1, p2, p3], initial_capital=100.0, k_slots=2)
    assert m.total_trades == 2
    assert m.rejections_max_concurrent == 1
    assert m.rejected_entries[0] == ("3", "max_concurrent_positions")
    assert m.max_concurrent_positions == 2


def test_insufficient_capital() -> None:
    # Two overlapping total-loss trades drain all cash before p3 tries to enter.
    # k=2, initial=100 → position_usd=50.
    # p1 enters at 0: cash=50. p2 enters at 1: cash=0.
    # p1 exits at 10, ret=-1.0: cash=0+50-50=0. p2 exits at 11, ret=-1.0: cash=0+50-50=0.
    # p3 at 20: cash(0) < position_usd(50) → rejected.
    p1 = _pos("1", "A", 1, 0, 10, ret=-1.0)
    p2 = _pos("2", "B", 1, 1, 11, ret=-1.0)
    p3 = _pos("3", "C", 1, 20, 30)

    m = simulate_portfolio_v2([p1, p2, p3], initial_capital=100.0, k_slots=2)
    assert m.total_trades == 2
    assert m.rejections_insufficient_capital == 1
    assert m.rejected_entries[0] == ("3", "insufficient_capital")


def test_unresolved_fail_closed() -> None:
    p1 = _pos("1", "A", 1, 0, None)
    m = simulate_portfolio_v2([p1], initial_capital=100.0, k_slots=2)
    assert m.unresolved_fail_closed == 1
    assert m.total_trades == 0
    assert m.available_cash == 50.0
    assert m.reserved_capital == 50.0
    assert m.net_exposure == 50.0
    assert m.gross_exposure == 50.0


def test_identical_timestamp_same_asset() -> None:
    p1 = _pos("B", "A", 1, 0, 10)
    p2 = _pos("A", "A", 1, 0, 10)

    m = simulate_portfolio_v2([p1, p2], initial_capital=100.0, k_slots=2, max_positions_per_asset=1)
    assert m.total_trades == 1
    assert m.rejections_max_per_asset == 1
    assert m.rejected_entries[0] == ("B", "max_positions_per_asset")


def test_reversed_input_equivalence() -> None:
    p1 = _pos("1", "A", 1, 0, 10)
    p2 = _pos("2", "B", 1, 5, 15)
    p3 = _pos("3", "C", 1, 8, 20)

    m1 = simulate_portfolio_v2([p1, p2, p3], initial_capital=100.0, k_slots=2)
    m2 = simulate_portfolio_v2([p3, p2, p1], initial_capital=100.0, k_slots=2)

    assert m1 == m2


def test_exit_before_entry() -> None:
    p1 = _pos("1", "A", 1, 0, 10)
    p2 = _pos("2", "B", 1, 10, 20)

    m = simulate_portfolio_v2([p1, p2], initial_capital=100.0, k_slots=1)
    assert m.total_trades == 2
    assert m.rejections_max_concurrent == 0


def test_exposure_leverage_limits() -> None:
    p1 = _pos("1", "A", 1, 0, None)
    p2 = _pos("2", "B", -1, 1, None)

    m = simulate_portfolio_v2([p1, p2], initial_capital=100.0, k_slots=4)
    assert m.gross_exposure == 50.0
    assert m.net_exposure == 0.0
    assert m.leverage == 50.0 / 100.0
