import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock

import pytest
from schurfer_analytics.momentum_flow_paper_contract import FROZEN_PAPER_CONTRACT
from schurfer_analytics.momentum_flow_paper_market import (
    BybitPaperMarket,
    ExecutableQuote,
    QuoteFailure,
    execution_vwap,
    summarize_book,
)


def test_execution_vwap_consumes_multiple_contract_levels() -> None:
    vwap, filled = execution_vwap(
        [[10, 2], [11, 10]],
        target_usd=50,
        contract_size=1,
    )

    assert filled == 50
    assert vwap == pytest.approx(50 / (2 + 30 / 11))


def test_execution_vwap_fails_closed_on_insufficient_depth() -> None:
    vwap, filled = execution_vwap([[10, 2]], target_usd=50, contract_size=1)

    assert vwap is None
    assert filled == 20


def test_summarize_book_calculates_exact_ask_quote() -> None:
    now = datetime(2026, 8, 14, 12, tzinfo=UTC)
    quote = summarize_book(
        symbol_id="ERAUSDT",
        unified_symbol="ERA/USDT:USDT",
        market_id="ERAUSDT",
        contract_size=1,
        side="ask",
        target_usd=50,
        requested_at=now,
        observed_at=now,
        latency_ms=12,
        book={
            "timestamp": int(now.timestamp() * 1000),
            "bids": [[9.9, 100]],
            "asks": [[10.1, 100]],
        },
    )

    assert quote.vwap == pytest.approx(10.1)
    assert quote.filled_notional_usd == 50
    assert quote.spread_bps == pytest.approx(200)
    assert quote.impact_bps == pytest.approx(100)
    assert quote.exchange_event_at == now


async def test_market_resolves_exact_bybit_market_id() -> None:
    exchange = AsyncMock()
    exchange.markets = {
        "ERA/USDT:USDT": {
            "id": "ERAUSDT",
            "swap": True,
            "linear": True,
            "settle": "USDT",
            "contractSize": 1,
        }
    }
    exchange.load_markets = AsyncMock(return_value=exchange.markets)
    exchange.fetch_order_book = AsyncMock(
        return_value={"bids": [[9.9, 100]], "asks": [[10.1, 100]]}
    )
    market = BybitPaperMarket(exchange, FROZEN_PAPER_CONTRACT)

    result = await market.quote("ERAUSDT", "ask")

    assert isinstance(result, ExecutableQuote)
    assert result.unified_symbol == "ERA/USDT:USDT"
    exchange.fetch_order_book.assert_awaited_once_with("ERA/USDT:USDT", 50)


async def test_market_refreshes_metadata_once_for_new_listing() -> None:
    exchange = AsyncMock()
    exchange.markets = {}

    async def load_markets(*, reload: bool) -> dict[str, object]:
        if reload:
            exchange.markets = {
                "ERA/USDT:USDT": {
                    "id": "ERAUSDT",
                    "swap": True,
                    "linear": True,
                    "settle": "USDT",
                    "contractSize": 1,
                }
            }
        return cast("dict[str, object]", exchange.markets)

    exchange.load_markets = AsyncMock(side_effect=load_markets)
    exchange.fetch_order_book = AsyncMock(
        return_value={"bids": [[9.9, 100]], "asks": [[10.1, 100]]}
    )
    market = BybitPaperMarket(exchange, FROZEN_PAPER_CONTRACT)

    result = await market.quote("ERAUSDT", "ask")

    assert isinstance(result, ExecutableQuote)
    assert exchange.load_markets.await_count == 2


async def test_market_rejects_ambiguous_or_missing_identity() -> None:
    exchange = AsyncMock()
    exchange.markets = {}
    exchange.load_markets = AsyncMock(return_value={})
    market = BybitPaperMarket(exchange, FROZEN_PAPER_CONTRACT)

    result = await market.quote("ERAUSDT", "ask")

    assert isinstance(result, QuoteFailure)
    assert result.reason == "invalid_or_insufficient_book"


async def test_quote_timeout_includes_initial_market_load() -> None:
    exchange = AsyncMock()

    async def slow_load(*, reload: bool) -> None:
        assert not reload
        await asyncio.sleep(1.1)

    exchange.load_markets = AsyncMock(side_effect=slow_load)
    contract = replace(FROZEN_PAPER_CONTRACT, max_quote_latency_seconds=1)
    market = BybitPaperMarket(exchange, contract)

    result = await market.quote("ERAUSDT", "ask")

    assert isinstance(result, QuoteFailure)
    assert result.reason == "quote_timeout"
    exchange.fetch_order_book.assert_not_awaited()
