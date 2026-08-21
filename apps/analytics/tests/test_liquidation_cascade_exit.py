from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.liquidation_cascade_exit import (
    ExitPolicy,
    net_return_from_replay,
    simulate_exit,
)
from schurfer_analytics.liquidation_cascade_repository import OutcomeBar, Quote

_START = datetime(2026, 8, 17, tzinfo=UTC)
_POLICY = ExitPolicy(initial_sl_pct=3.0, take_profit_pct=5.0, max_hold_minutes=60)


def _bar(
    minute: int,
    *,
    close: float,
    low: float | None = None,
    high: float | None = None,
    complete: bool = True,
) -> OutcomeBar:
    return OutcomeBar(
        bucket_start=_START + timedelta(minutes=minute),
        close_price=close,
        low_price=close if low is None else low,
        high_price=close if high is None else high,
        complete=complete,
    )


def test_stop_loss_fires_before_take_profit_when_both_would_hit_the_same_bar() -> None:
    bars = [_bar(0, close=100.0), _bar(1, close=97.0, low=96.0, high=106.0)]
    result = simulate_exit(entry_at=_START, entry_price=100.0, bars=bars, policy=_POLICY)
    assert result is not None
    assert result.exit_reason == "stop_loss"
    assert result.exit_price == pytest.approx(97.0)


def test_take_profit_fires_when_high_reaches_target() -> None:
    bars = [_bar(0, close=100.0), _bar(1, close=104.0, low=99.5, high=105.5)]
    result = simulate_exit(entry_at=_START, entry_price=100.0, bars=bars, policy=_POLICY)
    assert result is not None
    assert result.exit_reason == "take_profit"
    assert result.exit_price == pytest.approx(105.0)


def test_max_hold_fires_at_the_deadline_bar_close() -> None:
    bars = [_bar(0, close=100.0)] + [
        _bar(m, close=101.0, low=99.0, high=102.0) for m in range(1, 61)
    ]
    result = simulate_exit(entry_at=_START, entry_price=100.0, bars=bars, policy=_POLICY)
    assert result is not None
    assert result.exit_reason == "max_hold"
    assert result.exit_at == _START + timedelta(minutes=60)


def test_missing_bar_coverage_before_max_hold_is_immature_not_a_silent_exit() -> None:
    bars = [_bar(0, close=100.0)] + [
        _bar(m, close=101.0, low=99.0, high=102.0) for m in range(1, 10)
    ]
    result = simulate_exit(entry_at=_START, entry_price=100.0, bars=bars, policy=_POLICY)
    assert result is None


def test_incomplete_bar_never_triggers_an_exit() -> None:
    bars = [
        _bar(0, close=100.0),
        _bar(1, close=90.0, low=90.0, high=90.0, complete=False),
    ]
    result = simulate_exit(entry_at=_START, entry_price=100.0, bars=bars, policy=_POLICY)
    assert result is None


def test_a_gap_that_could_hide_the_real_stop_never_lets_a_later_bar_win() -> None:
    # Regression for the colleague review finding (2026-08-21): minute 1 is
    # simply absent (a real gap, not just incomplete) and could have
    # breached the stop; minute 2 is a complete bar that reaches take-
    # profit. The replay must not skip past the gap and report a win --
    # the true path through minute 1 is unknown.
    bars = [
        _bar(0, close=100.0),
        # minute 1 missing entirely
        _bar(2, close=104.0, low=99.5, high=105.5),
    ]
    result = simulate_exit(entry_at=_START, entry_price=100.0, bars=bars, policy=_POLICY)
    assert result is None


def test_an_incomplete_bar_between_entry_and_a_later_winning_bar_stays_unresolved() -> None:
    bars = [
        _bar(0, close=100.0),
        _bar(1, close=90.0, low=90.0, high=90.0, complete=False),
        _bar(2, close=104.0, low=99.5, high=105.5),
    ]
    result = simulate_exit(entry_at=_START, entry_price=100.0, bars=bars, policy=_POLICY)
    assert result is None


def test_missing_slippage_fails_net_accounting_closed() -> None:
    bars = [_bar(0, close=100.0), _bar(1, close=105.0, low=99.5, high=105.5)]
    exit_result = simulate_exit(entry_at=_START, entry_price=100.0, bars=bars, policy=_POLICY)
    assert exit_result is not None
    accounting = net_return_from_replay(
        entry_price=100.0,
        exit_result=exit_result,
        entry_quote=None,
        exit_quote=None,
        position_usd=50.0,
    )
    assert accounting.status == "incomplete"
    assert accounting.net_return_pct is None
    # Gross is still observable even when net fails closed.
    assert accounting.gross_return_pct == pytest.approx(5.0)


def test_costs_correctly_turn_gross_into_a_smaller_net_return() -> None:
    bars = [_bar(0, close=100.0), _bar(1, close=105.0, low=99.5, high=105.5)]
    exit_result = simulate_exit(entry_at=_START, entry_price=100.0, bars=bars, policy=_POLICY)
    assert exit_result is not None
    quote = Quote(last_bid_price=99.9, last_ask_price=100.1, price_complete=True)
    accounting = net_return_from_replay(
        entry_price=100.0,
        exit_result=exit_result,
        entry_quote=quote,
        exit_quote=quote,
        position_usd=50.0,
    )
    assert accounting.status == "complete"
    assert accounting.net_return_pct is not None
    assert accounting.net_return_pct < accounting.gross_return_pct
    assert accounting.fees_usd > 0
