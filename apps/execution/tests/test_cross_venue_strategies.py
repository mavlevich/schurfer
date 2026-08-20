import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from schurfer_execution.early_momentum import run_early_momentum_trigger
from schurfer_execution.liquidation_cascade import run_liquidation_cascade_scanner
from schurfer_execution.symbols import ResolvedRoute


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


def _redis_with_watch() -> MagicMock:
    redis = MagicMock()

    async def scan_iter(_pattern: str):
        yield b"market:early_momentum:watch:binance:BTCUSDT"

    redis.scan_iter = scan_iter
    redis.get = AsyncMock(
        return_value=json.dumps(
            {
                "ceiling": 100.0,
                "symbol": "BTCUSDT",
                "source_exchange": "binance",
            }
        ).encode()
    )
    redis.delete = AsyncMock()
    return redis


async def test_early_momentum_routes_native_id_to_exact_target_symbol() -> None:
    exchange = _market_exchange()
    exchange.fetch_tickers = AsyncMock(return_value={"BTC/USDT:USDT": {"last": 101.0}})
    redis = _redis_with_watch()
    cfg = MagicMock(db_url="postgresql://x")

    with (
        patch(
            "schurfer_execution.symbols.resolve_route",
            AsyncMock(return_value=_route()),
        ) as resolve_route,
        patch(
            "schurfer_execution.early_momentum.journal.find_open_trade_id",
            AsyncMock(return_value=None),
        ),
        patch(
            "schurfer_execution.early_momentum.paper.open_paper",
            AsyncMock(),
        ) as open_paper,
        patch(
            "schurfer_execution.early_momentum.asyncio.sleep",
            AsyncMock(side_effect=asyncio.CancelledError),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await run_early_momentum_trigger({"bybit": exchange}, redis, cfg)

    resolve_route.assert_awaited_once_with(
        "postgresql://x",
        "binance",
        "BTCUSDT",
        "bybit",
    )
    instrument = open_paper.await_args.kwargs["instrument"]
    assert instrument.symbol == "BTC/USDT:USDT"
    assert instrument.native_market_id == "BTCUSDT"
    assert open_paper.await_args.kwargs["price"] == 101.0


async def test_early_momentum_unresolved_route_does_not_open() -> None:
    exchange = _market_exchange()
    exchange.fetch_tickers = AsyncMock(return_value={})
    redis = _redis_with_watch()
    cfg = MagicMock(db_url="postgresql://x")

    with (
        patch(
            "schurfer_execution.symbols.resolve_route",
            AsyncMock(return_value=None),
        ),
        patch(
            "schurfer_execution.early_momentum.paper.open_paper",
            AsyncMock(),
        ) as open_paper,
        patch(
            "schurfer_execution.early_momentum.asyncio.sleep",
            AsyncMock(side_effect=asyncio.CancelledError),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await run_early_momentum_trigger({"bybit": exchange}, redis, cfg)

    open_paper.assert_not_awaited()


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
