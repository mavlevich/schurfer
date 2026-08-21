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
    # LONG entry prices off a fresh order book's ask side, not the ticker
    # print -- a healthy, deep book so the market-quality gate passes.
    exchange.fetch_order_book = AsyncMock(
        return_value={
            "bids": [[100.9, 1000.0]],
            "asks": [[101.1, 1000.0]],
        }
    )
    redis = _redis_with_watch()
    cfg = MagicMock(
        db_url="postgresql://x",
        liquidity_depth_multiplier=2.0,
        max_spread_bps=50.0,
        max_liquidity_impact_bps=50.0,
    )

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
    # Priced off the ask VWAP for the requested notional, not the raw ticker.
    assert open_paper.await_args.kwargs["price"] == pytest.approx(101.1)
    setup_context = open_paper.await_args.kwargs["setup_context"]
    assert setup_context["strategy"] == "early_momentum_v2"
    assert setup_context["market_quality"]["allowed"] is True
    # Entry is priced at an executed VWAP -- accounting_contract must not
    # also charge the same book impact a second time (see journal.py).
    assert setup_context["entry_price_includes_impact"] is True


async def test_early_momentum_prices_entry_at_the_real_size_not_the_gate_depth() -> None:
    # Regression (colleague review): the market-quality gate checks depth at
    # a safety-margined notional (2x the real size, via
    # liquidity_depth_multiplier) -- but the VWAP actually priced and stored
    # must be measured at the real trade size, not that larger gate
    # notional. A two-level ask book where level 1 exactly covers the real
    # $100 size and level 2 only gets touched by the larger $200 gate check
    # makes the two notionals price differently, so a regression back to
    # using the gate's depth_target for entry_vwap is caught.
    exchange = _market_exchange()
    exchange.fetch_tickers = AsyncMock(return_value={"BTC/USDT:USDT": {"last": 101.0}})
    exchange.fetch_order_book = AsyncMock(
        return_value={
            "bids": [[99.9, 100000.0]],
            "asks": [[100.0, 1.0], [100.2, 1.0]],
        }
    )
    redis = _redis_with_watch()
    cfg = MagicMock(
        db_url="postgresql://x",
        liquidity_depth_multiplier=2.0,
        max_spread_bps=50.0,
        max_liquidity_impact_bps=50.0,
    )

    with (
        patch(
            "schurfer_execution.symbols.resolve_route",
            AsyncMock(return_value=_route()),
        ),
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

    # Priced entirely from level 1 (real $100 size) -- not the ~100.10
    # blended VWAP a $200-sized quote would produce by spilling into level 2.
    assert open_paper.await_args.kwargs["price"] == pytest.approx(100.0)
    setup_context = open_paper.await_args.kwargs["setup_context"]
    assert setup_context["entry_vwap_filled_usd"] == pytest.approx(100.0)
    # Kept as separate evidence, not discarded -- distinct from (smaller
    # than) the gate's own $200-notional reading in market_quality.
    assert setup_context["entry_vwap_impact_bps"] is not None
    assert (
        setup_context["entry_vwap_impact_bps"] < setup_context["market_quality"]["ask_impact_bps"]
    )


async def test_early_momentum_skips_open_on_insufficient_entry_depth() -> None:
    exchange = _market_exchange()
    exchange.fetch_tickers = AsyncMock(return_value={"BTC/USDT:USDT": {"last": 101.0}})
    # Thin ask depth: cannot fill the required notional.
    exchange.fetch_order_book = AsyncMock(
        return_value={
            "bids": [[100.9, 1000.0]],
            "asks": [[101.1, 0.001]],
        }
    )
    redis = _redis_with_watch()
    cfg = MagicMock(
        db_url="postgresql://x",
        liquidity_depth_multiplier=2.0,
        max_spread_bps=50.0,
        max_liquidity_impact_bps=50.0,
    )

    with (
        patch(
            "schurfer_execution.symbols.resolve_route",
            AsyncMock(return_value=_route()),
        ),
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

    open_paper.assert_not_awaited()
    # The watch key is still cleared -- a thin book at this instant doesn't
    # keep re-triggering on the same breakout.
    redis.delete.assert_awaited_once()


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
