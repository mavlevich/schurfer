from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from schurfer_analytics.portfolio_engine_v2 import (
    PortfolioPosition,
    TradeDirection,
    simulate_portfolio_v2,
)


def _position(
    decision_id: str,
    asset: str,
    *,
    entry_minute: int,
    exit_minute: int | None,
    gross_return: float | None = 0.1,
    direction: TradeDirection = TradeDirection.LONG,
    costs_bps: float = 0.0,
) -> PortfolioPosition:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return PortfolioPosition(
        decision_id=decision_id,
        canonical_asset=asset,
        direction=direction,
        entry_at=start + timedelta(minutes=entry_minute),
        exit_at=start + timedelta(minutes=exit_minute) if exit_minute is not None else None,
        gross_return=gross_return if exit_minute is not None else None,
        entry_slippage_bps=costs_bps,
        exit_slippage_bps=0.0,
        fees_bps=0.0,
        funding_bps=0.0,
        net_return=(gross_return - costs_bps / 10_000.0)
        if exit_minute is not None and gross_return is not None
        else None,
        unresolved_reason="missing_exit" if exit_minute is None else None,
    )


@pytest.mark.parametrize("slots", [2, 4, 6, 8])
def test_fixed_k_slot_scaling(slots: int) -> None:
    positions = [
        _position(str(index), f"asset-{index}", entry_minute=index, exit_minute=100 + index)
        for index in range(slots + 1)
    ]
    metrics = simulate_portfolio_v2(positions, initial_capital=300.0, k_slots=slots)
    assert metrics.total_trades == slots
    assert metrics.rejection_counts == {"max_concurrent_positions": 1}
    assert metrics.max_concurrent_positions == slots
    assert metrics.peak_gross_exposure == pytest.approx(300.0)


def test_insufficient_capital_rejects_without_partial_allocation() -> None:
    positions = [
        _position("1", "A", entry_minute=0, exit_minute=10, gross_return=-1.0),
        _position("2", "B", entry_minute=1, exit_minute=11, gross_return=-1.0),
        _position("3", "C", entry_minute=20, exit_minute=30),
    ]
    metrics = simulate_portfolio_v2(positions, initial_capital=100.0, k_slots=2)
    assert metrics.total_trades == 2
    assert metrics.rejection_counts == {"insufficient_capital": 1}
    assert metrics.rejected_entries[0].decision_id == "3"


def test_unresolved_position_remains_reserved_and_marks_incomplete() -> None:
    position = _position("1", "A", entry_minute=0, exit_minute=None)
    metrics = simulate_portfolio_v2([position], initial_capital=100.0, k_slots=2)
    assert metrics.unresolved_fail_closed == 1
    assert metrics.total_trades == 0
    assert metrics.available_cash == 50.0
    assert metrics.reserved_capital == 50.0
    assert metrics.notional_exposure == 50.0
    assert metrics.accounting_complete is False


def test_same_timestamp_uses_exit_then_stable_entry_order() -> None:
    first = _position("A", "same", entry_minute=0, exit_minute=10)
    later = _position("B", "same", entry_minute=10, exit_minute=20)
    rejected = _position("C", "same", entry_minute=10, exit_minute=20)
    forward = simulate_portfolio_v2(
        [first, rejected, later],
        initial_capital=100.0,
        k_slots=1,
        max_positions_per_asset=1,
    )
    reverse = simulate_portfolio_v2(
        [later, rejected, first],
        initial_capital=100.0,
        k_slots=1,
        max_positions_per_asset=1,
    )
    assert forward == reverse
    assert forward.total_trades == 2
    assert forward.rejected_entries[0].decision_id == "C"


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"max_gross_exposure_usd": 49.0}, "max_gross_exposure"),
        ({"max_abs_net_exposure_usd": 49.0}, "max_abs_net_exposure"),
        ({"leverage": 2.0, "max_leverage": 0.9}, "max_leverage"),
    ],
)
def test_exposure_and_leverage_limits_reject(kwargs: dict[str, Any], reason: str) -> None:
    position = _position("1", "A", entry_minute=0, exit_minute=10)
    metrics = simulate_portfolio_v2([position], initial_capital=100.0, k_slots=2, **kwargs)
    assert metrics.total_trades == 0
    assert metrics.rejection_counts == {reason: 1}


def test_long_short_open_positions_report_net_exposure() -> None:
    positions = [
        _position("1", "A", entry_minute=0, exit_minute=None),
        _position(
            "2",
            "B",
            entry_minute=1,
            exit_minute=None,
            direction=TradeDirection.SHORT,
        ),
    ]
    metrics = simulate_portfolio_v2(positions, initial_capital=100.0, k_slots=2)
    assert metrics.gross_exposure == 100.0
    assert metrics.net_exposure == 0.0
    assert metrics.leverage == 1.0


@pytest.mark.parametrize("k_slots", [0, -1])
def test_invalid_k_fails_closed(k_slots: int) -> None:
    with pytest.raises(ValueError, match="k_slots"):
        simulate_portfolio_v2([], k_slots=k_slots)


def test_duplicate_ids_fail_closed() -> None:
    position = _position("same", "A", entry_minute=0, exit_minute=10)
    with pytest.raises(ValueError, match="unique"):
        simulate_portfolio_v2([position, replace(position, canonical_asset="B")])


def test_resolved_position_requires_complete_economics() -> None:
    position = replace(
        _position("1", "A", entry_minute=0, exit_minute=10),
        net_return=None,
    )
    with pytest.raises(ValueError, match="gross and net returns"):
        simulate_portfolio_v2([position])


def test_net_return_must_match_cost_provenance() -> None:
    position = replace(
        _position("1", "A", entry_minute=0, exit_minute=10, costs_bps=10.0),
        net_return=0.1,
    )
    with pytest.raises(ValueError, match="cost provenance"):
        simulate_portfolio_v2([position])


def test_exit_before_entry_fails_closed() -> None:
    position = replace(
        _position("1", "A", entry_minute=10, exit_minute=20),
        exit_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="must not precede"):
        simulate_portfolio_v2([position])


def test_unresolved_position_cannot_claim_returns() -> None:
    position = replace(
        _position("1", "A", entry_minute=0, exit_minute=None),
        gross_return=0.1,
        net_return=0.1,
    )
    with pytest.raises(ValueError, match="cannot have returns"):
        simulate_portfolio_v2([position])
