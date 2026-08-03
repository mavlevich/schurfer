from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from schurfer_execution.fill_price import (
    FILL_CONFIRMED,
    FILL_PARTIAL,
    FILL_UNRESOLVED,
    resolve_fill_price,
)


def _exchange(**overrides: Any) -> MagicMock:
    ex = MagicMock()
    ex.has = {"fetchOrderTrades": False, "fetchMyTrades": False}
    ex.fetch_order = AsyncMock(return_value=None)
    ex.fetch_order_trades = AsyncMock(return_value=[])
    ex.fetch_my_trades = AsyncMock(return_value=[])
    for key, value in overrides.items():
        setattr(ex, key, value)
    return ex


async def test_prefers_order_average() -> None:
    result = await resolve_fill_price(
        _exchange(), symbol="BEAT/USDT:USDT", order={"id": "1", "average": 1.23, "price": 9.99}
    )
    assert result.status == FILL_CONFIRMED
    assert result.price == 1.23
    assert result.source == "order.average"


async def test_falls_back_to_order_price() -> None:
    result = await resolve_fill_price(
        _exchange(), symbol="BEAT/USDT:USDT", order={"id": "1", "average": None, "price": 1.5}
    )
    assert result.status == FILL_CONFIRMED
    assert result.price == 1.5
    assert result.source == "order.price"


async def test_falls_back_to_cost_over_filled() -> None:
    result = await resolve_fill_price(
        _exchange(),
        symbol="BEAT/USDT:USDT",
        order={"id": "1", "cost": 100.0, "filled": 50.0},
    )
    assert result.status == FILL_CONFIRMED
    assert result.price == 2.0
    assert result.source == "order.cost_filled"


async def test_partial_status_when_filled_below_requested() -> None:
    result = await resolve_fill_price(
        _exchange(),
        symbol="BEAT/USDT:USDT",
        order={"id": "1", "average": 1.0, "filled": 5.0},
        requested_amount=10.0,
    )
    assert result.status == FILL_PARTIAL
    assert result.price == 1.0


async def test_refetches_order_when_initial_response_is_bare() -> None:
    ex = _exchange(fetch_order=AsyncMock(return_value={"id": "1", "average": 3.5, "filled": 10.0}))
    result = await resolve_fill_price(ex, symbol="BEAT/USDT:USDT", order={"id": "1"})
    assert result.status == FILL_CONFIRMED
    assert result.price == 3.5
    assert result.source == "refetch.order.average"
    ex.fetch_order.assert_awaited_once_with("1", "BEAT/USDT:USDT")


async def test_falls_back_to_trade_vwap() -> None:
    ex = _exchange(
        has={"fetchOrderTrades": True, "fetchMyTrades": False},
        fetch_order_trades=AsyncMock(
            return_value=[{"price": 2.0, "amount": 1.0}, {"price": 4.0, "amount": 1.0}]
        ),
    )
    result = await resolve_fill_price(ex, symbol="BEAT/USDT:USDT", order={"id": "1"})
    assert result.status == FILL_CONFIRMED
    assert result.price == pytest.approx(3.0)
    assert result.source == "trades.vwap"


async def test_uses_fetch_my_trades_when_order_trades_unsupported() -> None:
    ex = _exchange(
        has={"fetchOrderTrades": False, "fetchMyTrades": True},
        fetch_my_trades=AsyncMock(return_value=[{"price": 5.0, "amount": 2.0}]),
    )
    result = await resolve_fill_price(ex, symbol="BEAT/USDT:USDT", order={"id": "1"})
    assert result.status == FILL_CONFIRMED
    assert result.price == 5.0
    assert result.source == "trades.vwap"


async def test_unresolved_when_nothing_confirms_a_price() -> None:
    result = await resolve_fill_price(_exchange(), symbol="BEAT/USDT:USDT", order={"id": "1"})
    assert result.status == FILL_UNRESOLVED
    assert result.price is None


async def test_unresolved_without_order_id_skips_refetch_and_trades() -> None:
    ex = _exchange()
    result = await resolve_fill_price(ex, symbol="BEAT/USDT:USDT", order={})
    assert result.status == FILL_UNRESOLVED
    ex.fetch_order.assert_not_awaited()


async def test_never_uses_ticker_or_mark_price_fields() -> None:
    # A malformed/unexpected order payload carrying a "mark" or "last" field must
    # never be read as a fill — only the recognized fill-evidence fields count.
    result = await resolve_fill_price(
        _exchange(), symbol="BEAT/USDT:USDT", order={"id": "1", "mark": 42.0, "last": 42.0}
    )
    assert result.status == FILL_UNRESOLVED


async def test_fetch_order_exception_falls_through_to_trades() -> None:
    ex = _exchange(
        fetch_order=AsyncMock(side_effect=RuntimeError("timeout")),
        has={"fetchOrderTrades": True, "fetchMyTrades": False},
        fetch_order_trades=AsyncMock(return_value=[{"price": 1.1, "amount": 3.0}]),
    )
    result = await resolve_fill_price(ex, symbol="BEAT/USDT:USDT", order={"id": "1"})
    assert result.status == FILL_CONFIRMED
    assert result.price == 1.1
    assert result.source == "trades.vwap"
