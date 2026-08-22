import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from schurfer_execution.liquidation_cascade import run_liquidation_cascade_scanner
from schurfer_execution.symbols import ResolvedRoute

# early_momentum's own trigger/scanner tests live in test_early_momentum.py
# (the v3 episode-lifecycle rewrite needs a much larger, dedicated mocking
# surface -- episodes.py's create/claim/reap/list_actionable/etc -- that
# doesn't belong mixed into this file's cross-venue routing focus).


def _market_exchange() -> MagicMock:
    exchange = MagicMock()
    exchange.id = "bybit"
    exchange.markets = {
        "BTC/USDT:USDT": {
            "id": "BTCUSDT",
            "symbol": "BTC/USDT:USDT",
            "base": "BTC",
            "quote": "USDT",
            "settle": "USDT",
            "type": "swap",
            "active": True,
        }
    }
    return exchange


def _route() -> ResolvedRoute:
    return ResolvedRoute(
        source_exchange="binance",
        source_native_id="BTCUSDT",
        source_identity_key="binance:linear_usdt_perpetual:BTCUSDT:1",
        execution_exchange="bybit",
        execution_native_id="BTCUSDT",
        execution_identity_key="bybit:linear_usdt_perpetual:BTCUSDT:2",
        cluster_key="cluster-btc",
    )


def _scanner_db(candidate: dict[str, object]) -> MagicMock:
    cursor = MagicMock()
    cursor.execute = AsyncMock()
    cursor.fetchall = AsyncMock(return_value=[candidate])
    cursor_context = MagicMock()
    cursor_context.__aenter__ = AsyncMock(return_value=cursor)
    cursor_context.__aexit__ = AsyncMock(return_value=None)
    connection = MagicMock()
    connection.__aenter__ = AsyncMock(return_value=connection)
    connection.__aexit__ = AsyncMock(return_value=None)
    connection.cursor.return_value = cursor_context
    return connection


async def test_liquidation_cascade_uses_target_ticker_and_exact_symbol() -> None:
    candidate = {
        "exchange": "binance",
        "symbol": "BTCUSDT",
        "close_price": 94.0,
        "price_15m_ago": 100.0,
        "open_interest": 80.0,
        "oi_15m_ago": 100.0,
    }
    connection = _scanner_db(candidate)
    exchange = _market_exchange()
    exchange.fetch_ticker = AsyncMock(return_value={"last": 95.0})
    cfg = MagicMock(db_url="postgresql://x")

    with (
        patch(
            "schurfer_execution.liquidation_cascade.psycopg.AsyncConnection.connect",
            AsyncMock(return_value=connection),
        ),
        patch(
            "schurfer_execution.symbols.resolve_route",
            AsyncMock(return_value=_route()),
        ),
        patch(
            "schurfer_execution.liquidation_cascade.journal.find_open_trade_id",
            AsyncMock(return_value=None),
        ),
        patch(
            "schurfer_execution.liquidation_cascade.paper.open_paper",
            AsyncMock(),
        ) as open_paper,
        patch(
            "schurfer_execution.liquidation_cascade.asyncio.sleep",
            AsyncMock(side_effect=asyncio.CancelledError),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await run_liquidation_cascade_scanner({"bybit": exchange}, MagicMock(), cfg)

    exchange.fetch_ticker.assert_awaited_once_with("BTC/USDT:USDT")
    instrument = open_paper.await_args.kwargs["instrument"]
    assert instrument.symbol == "BTC/USDT:USDT"
    assert open_paper.await_args.kwargs["price"] == 95.0
