from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from schurfer_execution.fill_price import (
    FILL_CONFIRMED,
    FILL_NONE,
    FILL_PARTIAL,
    FILL_UNRESOLVED,
    order_is_terminal,
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


@pytest.mark.parametrize("status", ["closed", "canceled", "cancelled", "expired", "rejected"])
def test_terminal_order_statuses(status: str) -> None:
    assert order_is_terminal({"status": status})


@pytest.mark.parametrize("status", ["open", "new", None, ""])
def test_non_terminal_order_statuses(status: str | None) -> None:
    assert not order_is_terminal({"status": status})


async def test_prefers_order_average() -> None:
    result = await resolve_fill_price(
        _exchange(),
        symbol="BEAT/USDT:USDT",
        order={"id": "1", "average": 1.23, "price": 9.99, "filled": 10.0},
    )
    assert result.status == FILL_CONFIRMED
    assert result.price == 1.23
    assert result.source == "order.average"
    assert result.filled_amount == 10.0


async def test_preserves_last_trade_timestamp_with_provenance() -> None:
    result = await resolve_fill_price(
        _exchange(),
        symbol="BEAT/USDT:USDT",
        order={
            "id": "1",
            "average": 1.23,
            "filled": 10.0,
            "timestamp": 1_788_937_100_000,
            "lastTradeTimestamp": 1_788_937_200_000,
        },
    )

    assert result.executed_at == datetime.fromtimestamp(1_788_937_200, tz=UTC)
    assert result.execution_time_source == "exchange.lastTradeTimestamp"


async def test_order_creation_timestamp_is_not_treated_as_fill_time() -> None:
    result = await resolve_fill_price(
        _exchange(),
        symbol="BEAT/USDT:USDT",
        order={
            "id": "stop-1",
            "average": 1.23,
            "filled": 10.0,
            "timestamp": 1_788_900_000_000,
            "datetime": "2026-09-09T12:00:00Z",
        },
    )

    assert result.executed_at is None
    assert result.execution_time_source is None


async def test_falls_back_to_order_price() -> None:
    result = await resolve_fill_price(
        _exchange(),
        symbol="BEAT/USDT:USDT",
        order={"id": "1", "average": None, "price": 1.5, "filled": 10.0},
    )
    assert result.status == FILL_CONFIRMED
    assert result.price == 1.5
    assert result.source == "order.price"


class TestFillEvidenceIsRequired:
    """Regression for ENG-022 / audit C-3: a price was reported as a confirmed
    fill with no filled volume behind it at all, so an order that never
    executed was journalled as a real open position at whatever average the
    payload happened to carry."""

    async def test_zero_filled_with_a_price_is_not_a_fill(self) -> None:
        result = await resolve_fill_price(
            _exchange(),
            symbol="BEAT/USDT:USDT",
            order={"id": "1", "average": 1.23, "filled": 0.0},
            requested_amount=10.0,
        )
        assert result.status == FILL_NONE
        assert result.price is None
        assert result.filled_amount == 0.0

    async def test_missing_filled_field_still_resolves_but_reports_no_volume(self) -> None:
        """Unknown volume is not the same as zero: an exchange that simply does
        not report `filled` still gives a usable price, and the caller is the
        one that must not turn a None filled_amount into an assumed full
        fill."""
        result = await resolve_fill_price(
            _exchange(), symbol="BEAT/USDT:USDT", order={"id": "1", "average": 1.23}
        )
        assert result.status == FILL_CONFIRMED
        assert result.price == 1.23
        assert result.filled_amount is None

    async def test_zero_filled_create_response_settles_on_refetch(self) -> None:
        """A market order can report nothing filled in the immediate create
        response and settle a moment later, so the chain re-fetches before
        calling it a no-fill."""
        ex = _exchange()
        ex.fetch_order = AsyncMock(return_value={"id": "1", "average": 1.5, "filled": 10.0})

        result = await resolve_fill_price(
            ex,
            symbol="BEAT/USDT:USDT",
            order={"id": "1", "average": 1.23, "filled": 0.0},
            requested_amount=10.0,
        )

        assert result.status == FILL_CONFIRMED
        assert result.price == 1.5
        assert result.filled_amount == 10.0
        assert result.source == "refetch.order.average"

    async def test_zero_filled_confirmed_by_trades_lookup_is_no_fill(self) -> None:
        ex = _exchange()
        ex.has = {"fetchOrderTrades": True}
        ex.fetch_order = AsyncMock(return_value={"id": "1", "average": 1.23, "filled": 0.0})
        ex.fetch_order_trades = AsyncMock(return_value=[])

        result = await resolve_fill_price(
            ex,
            symbol="BEAT/USDT:USDT",
            order={"id": "1", "average": 1.23, "filled": 0.0},
            requested_amount=10.0,
        )

        assert result.status == FILL_NONE
        assert result.price is None


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


async def test_trade_vwap_uses_latest_trade_timestamp() -> None:
    ex = _exchange(
        has={"fetchOrderTrades": True, "fetchMyTrades": False},
        fetch_order_trades=AsyncMock(
            return_value=[
                {"price": 2.0, "amount": 1.0, "timestamp": 1_788_937_100_000},
                {"price": 4.0, "amount": 1.0, "timestamp": 1_788_937_200_000},
            ]
        ),
    )

    result = await resolve_fill_price(ex, symbol="BEAT/USDT:USDT", order={"id": "1"})

    assert result.executed_at == datetime.fromtimestamp(1_788_937_200, tz=UTC)
    assert result.execution_time_source == "exchange.timestamp"


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
