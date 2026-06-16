import asyncio
from unittest.mock import AsyncMock, patch

from schurfer_analytics.funding import (
    ELEVATED_THRESHOLD,
    PERIODS_PER_YEAR,
    fetch_funding_rates_for_pumps,
)


def _pump(base: str, exchanges: list[dict[str, object]]) -> dict[str, object]:
    return {"base": base, "max_change_pct": 50.0, "exchanges": exchanges}


def _ex_entry(exchange: str) -> dict[str, object]:
    return {"exchange": exchange}


def _mock_exchange(rate: float | None) -> AsyncMock:
    exchange = AsyncMock()
    exchange.fetch_funding_rate = AsyncMock(return_value={"fundingRate": rate})
    exchange.close = AsyncMock()
    return exchange


def test_fetch_funding_returns_rate() -> None:
    exchange = _mock_exchange(0.001)
    pumps = [_pump("BTC", [_ex_entry("binance")])]

    with patch.dict(
        "schurfer_analytics.funding.EXCHANGE_FACTORIES", {"binance": lambda: exchange}, clear=True
    ):
        result = asyncio.run(fetch_funding_rates_for_pumps(pumps))

    assert result == [{"base": "BTC", "exchange": "binance", "rate": 0.001}]


def test_fetch_funding_skips_none_rate() -> None:
    exchange = _mock_exchange(None)
    pumps = [_pump("BTC", [_ex_entry("binance")])]

    with patch.dict(
        "schurfer_analytics.funding.EXCHANGE_FACTORIES", {"binance": lambda: exchange}, clear=True
    ):
        result = asyncio.run(fetch_funding_rates_for_pumps(pumps))

    assert result == []


def test_fetch_funding_skips_entry_on_fetch_error() -> None:
    exchange = AsyncMock()
    exchange.fetch_funding_rate = AsyncMock(side_effect=RuntimeError("not supported"))
    exchange.close = AsyncMock()
    pumps = [_pump("BTC", [_ex_entry("binance")])]

    with patch.dict(
        "schurfer_analytics.funding.EXCHANGE_FACTORIES", {"binance": lambda: exchange}, clear=True
    ):
        result = asyncio.run(fetch_funding_rates_for_pumps(pumps))

    assert result == []
    exchange.close.assert_awaited_once()


def test_fetch_funding_skips_unknown_exchange() -> None:
    pumps = [_pump("BTC", [_ex_entry("unknown_exchange")])]

    with patch.dict("schurfer_analytics.funding.EXCHANGE_FACTORIES", {}, clear=True):
        result = asyncio.run(fetch_funding_rates_for_pumps(pumps))

    assert result == []


def test_fetch_funding_empty_pumps_returns_empty() -> None:
    result = asyncio.run(fetch_funding_rates_for_pumps([]))
    assert result == []


def test_fetch_funding_groups_multiple_bases_on_same_exchange() -> None:
    exchange = AsyncMock()
    exchange.fetch_funding_rate = AsyncMock(
        side_effect=[{"fundingRate": 0.0005}, {"fundingRate": 0.002}]
    )
    exchange.close = AsyncMock()
    pumps = [
        _pump("BTC", [_ex_entry("bybit")]),
        _pump("ETH", [_ex_entry("bybit")]),
    ]

    with patch.dict(
        "schurfer_analytics.funding.EXCHANGE_FACTORIES", {"bybit": lambda: exchange}, clear=True
    ):
        result = asyncio.run(fetch_funding_rates_for_pumps(pumps))

    assert exchange.close.await_count == 1
    bases = {row["base"] for row in result}
    assert bases == {"BTC", "ETH"}
    rates = {row["base"]: row["rate"] for row in result}
    assert rates["BTC"] == 0.0005
    assert rates["ETH"] == 0.002


def test_fetch_funding_processes_more_items_than_concurrency_cap() -> None:
    bases = [f"TOK{i}" for i in range(8)]
    exchange = AsyncMock()
    exchange.fetch_funding_rate = AsyncMock(return_value={"fundingRate": 0.0001})
    exchange.close = AsyncMock()
    pumps = [_pump(base, [_ex_entry("okx")]) for base in bases]

    with patch.dict(
        "schurfer_analytics.funding.EXCHANGE_FACTORIES", {"okx": lambda: exchange}, clear=True
    ):
        result = asyncio.run(fetch_funding_rates_for_pumps(pumps))

    assert {row["base"] for row in result} == set(bases)


def test_fetch_funding_times_out_gracefully() -> None:
    async def _slow(symbol: str) -> dict[str, object]:
        await asyncio.sleep(1)
        return {"fundingRate": 0.001}

    exchange = AsyncMock()
    exchange.fetch_funding_rate = _slow
    exchange.close = AsyncMock()
    pumps = [_pump("BTC", [_ex_entry("okx")])]

    factories = {"okx": lambda: exchange}
    with (
        patch("schurfer_analytics.funding.FETCH_TIMEOUT", 0.05),
        patch.dict("schurfer_analytics.funding.EXCHANGE_FACTORIES", factories, clear=True),
    ):
        result = asyncio.run(fetch_funding_rates_for_pumps(pumps))

    assert result == []
    exchange.close.assert_awaited_once()


def test_elevated_threshold_and_periods_constants() -> None:
    # Sanity check: 0.1% / 8h = 0.001 as a decimal, APR = 109.5%
    assert ELEVATED_THRESHOLD == 0.001
    assert PERIODS_PER_YEAR == 3 * 365
    assert abs(ELEVATED_THRESHOLD * PERIODS_PER_YEAR * 100 - 109.5) < 1e-9


def test_fetch_funding_negative_rate_allowed() -> None:
    # Negative funding = shorts paying longs (bearish momentum); still a valid reading.
    exchange = _mock_exchange(-0.0005)
    pumps = [_pump("BTC", [_ex_entry("okx")])]

    with patch.dict(
        "schurfer_analytics.funding.EXCHANGE_FACTORIES", {"okx": lambda: exchange}, clear=True
    ):
        result = asyncio.run(fetch_funding_rates_for_pumps(pumps))

    assert result == [{"base": "BTC", "exchange": "okx", "rate": -0.0005}]
