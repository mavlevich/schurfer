import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from schurfer_analytics.oi import (
    OPEN_INTEREST_MAX_AGE_MS,
    OPEN_INTEREST_MAX_FUTURE_SKEW_MS,
    _parse_xt_open_interest,
    fetch_oi_for_pumps,
)

NOW_MS = 1_800_000_000_000


def _pump(base: str, exchanges: list[dict[str, object]]) -> dict[str, object]:
    return {"base": base, "max_change_pct": 50.0, "exchanges": exchanges}


def _ex_entry(exchange: str, price: str = "100.0") -> dict[str, object]:
    return {"exchange": exchange, "price": price}


def _mock_exchange(open_interest: dict[str, object]) -> AsyncMock:
    exchange = AsyncMock()
    exchange.has = {"fetchOpenInterest": True}
    exchange.load_markets = AsyncMock()
    exchange.markets = {}
    exchange.fetch_open_interest = AsyncMock(return_value=open_interest)
    exchange.close = AsyncMock()
    return exchange


def test_fetch_oi_uses_open_interest_value_when_present() -> None:
    exchange = _mock_exchange({"openInterestValue": 2_000_000.0})
    pumps = [_pump("BTC", [_ex_entry("okx")])]

    with patch.dict(
        "schurfer_analytics.oi.EXCHANGE_FACTORIES", {"okx": lambda: exchange}, clear=True
    ):
        result = asyncio.run(fetch_oi_for_pumps(pumps))

    assert result == [{"base": "BTC", "exchange": "okx", "oi_usd": 2_000_000.0}]


def test_fetch_oi_falls_back_to_amount_times_price() -> None:
    exchange = _mock_exchange({"openInterestValue": None, "openInterestAmount": 500.0})
    pumps = [_pump("ETH", [_ex_entry("binance", price="2000.0")])]

    with patch.dict(
        "schurfer_analytics.oi.EXCHANGE_FACTORIES", {"binance": lambda: exchange}, clear=True
    ):
        result = asyncio.run(fetch_oi_for_pumps(pumps))

    assert result == [{"base": "ETH", "exchange": "binance", "oi_usd": 1_000_000.0}]


def test_fetch_oi_fallback_scales_by_contract_size() -> None:
    # OKX-style market where 1 contract != 1 base unit (contractSize=0.01):
    # amount(contracts) * price would overstate USD value 100x without scaling.
    exchange = _mock_exchange({"openInterestValue": None, "openInterestAmount": 100.0})
    exchange.markets = {"ABC/USDT:USDT": {"contractSize": 0.01}}
    pumps = [_pump("ABC", [_ex_entry("okx", price="50.0")])]

    with patch.dict(
        "schurfer_analytics.oi.EXCHANGE_FACTORIES", {"okx": lambda: exchange}, clear=True
    ):
        result = asyncio.run(fetch_oi_for_pumps(pumps))

    assert result == [{"base": "ABC", "exchange": "okx", "oi_usd": 50.0}]


def test_fetch_oi_skips_unknown_exchange() -> None:
    pumps = [_pump("BTC", [_ex_entry("some_new_exchange")])]

    with patch.dict("schurfer_analytics.oi.EXCHANGE_FACTORIES", {}, clear=True):
        result = asyncio.run(fetch_oi_for_pumps(pumps))

    assert result == []


def test_fetch_oi_skips_exchange_without_supported_adapter() -> None:
    exchange = _mock_exchange({})
    exchange.has = {"fetchOpenInterest": False}
    pumps = [_pump("BTC", [_ex_entry("lbank")])]

    with patch.dict(
        "schurfer_analytics.oi.EXCHANGE_FACTORIES",
        {"lbank": lambda: exchange},
        clear=True,
    ):
        result = asyncio.run(fetch_oi_for_pumps(pumps))

    assert result == []
    exchange.load_markets.assert_not_awaited()
    exchange.fetch_open_interest.assert_not_awaited()
    exchange.close.assert_awaited_once()


def test_fetch_oi_uses_xt_adapter() -> None:
    exchange = _mock_exchange({})
    exchange.has = {"fetchOpenInterest": False}
    exchange.markets = {
        "ON/USDT:USDT": {"id": "on_usdt", "contractSize": 1},
    }
    exchange.market = lambda symbol: exchange.markets[symbol]
    exchange.public_linear_get_future_market_v1_public_contract_open_interest = AsyncMock(
        return_value={
            "returnCode": "0",
            "msgInfo": "success",
            "error": None,
            "result": {
                "symbol": "on_usdt",
                "openInterest": "102204200",
                "openInterestUsd": "16938265.9457",
                "time": str(NOW_MS),
            },
        }
    )
    pumps = [_pump("ON", [_ex_entry("xt", price="0.1657")])]

    with (
        patch("schurfer_analytics.oi.time.time", return_value=NOW_MS / 1000),
        patch.dict(
            "schurfer_analytics.oi.EXCHANGE_FACTORIES",
            {"xt": lambda: exchange},
            clear=True,
        ),
    ):
        result = asyncio.run(fetch_oi_for_pumps(pumps))

    assert result == [{"base": "ON", "exchange": "xt", "oi_usd": 16938265.9457}]
    exchange.public_linear_get_future_market_v1_public_contract_open_interest.assert_awaited_once_with(
        {"symbol": "on_usdt"}
    )
    exchange.fetch_open_interest.assert_not_awaited()
    exchange.close.assert_awaited_once()


def test_fetch_oi_prefers_native_xt_support_when_available() -> None:
    exchange = _mock_exchange({"openInterestValue": 42.0})
    exchange.has = {"fetchOpenInterest": "emulated"}
    exchange.public_linear_get_future_market_v1_public_contract_open_interest = AsyncMock()
    pumps = [_pump("ON", [_ex_entry("xt")])]

    with patch.dict(
        "schurfer_analytics.oi.EXCHANGE_FACTORIES", {"xt": lambda: exchange}, clear=True
    ):
        result = asyncio.run(fetch_oi_for_pumps(pumps))

    assert result == [{"base": "ON", "exchange": "xt", "oi_usd": 42.0}]
    exchange.fetch_open_interest.assert_awaited_once_with("ON/USDT:USDT")
    exchange.public_linear_get_future_market_v1_public_contract_open_interest.assert_not_awaited()


@pytest.mark.parametrize(
    "response, message",
    [
        ([], "must be an object"),
        ({"returnCode": "invalid"}, "return code is invalid"),
        ({"returnCode": 1001, "msgInfo": "bad symbol"}, "bad symbol"),
        ({"returnCode": 0, "result": None}, "missing result"),
        (
            {"returnCode": 0, "result": {"time": 1}},
            "no amount or USD value",
        ),
        (
            {"returnCode": 0, "result": {"openInterestUsd": "1"}},
            "no timestamp",
        ),
        (
            {
                "returnCode": 0,
                "result": {"openInterestUsd": "1", "time": "invalid"},
            },
            "timestamp is invalid",
        ),
        (
            {
                "returnCode": 0,
                "result": {
                    "openInterestUsd": "1",
                    "time": NOW_MS - OPEN_INTEREST_MAX_AGE_MS - 1,
                },
            },
            "response is stale",
        ),
        (
            {
                "returnCode": 0,
                "result": {
                    "openInterestUsd": "1",
                    "time": NOW_MS + OPEN_INTEREST_MAX_FUTURE_SKEW_MS + 1,
                },
            },
            "in the future",
        ),
    ],
)
def test_parse_xt_open_interest_rejects_malformed_response(response: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _parse_xt_open_interest(response, now_ms=NOW_MS)


def test_fetch_oi_skips_entry_on_fetch_error() -> None:
    failing = AsyncMock()
    failing.load_markets = AsyncMock()
    failing.markets = {}
    failing.fetch_open_interest = AsyncMock(side_effect=RuntimeError("boom"))
    failing.close = AsyncMock()
    pumps = [_pump("BTC", [_ex_entry("okx")])]

    with patch.dict(
        "schurfer_analytics.oi.EXCHANGE_FACTORIES", {"okx": lambda: failing}, clear=True
    ):
        result = asyncio.run(fetch_oi_for_pumps(pumps))

    assert result == []
    failing.close.assert_awaited_once()


def test_fetch_oi_continues_when_load_markets_fails() -> None:
    # ccxt leaves exchange.markets as None (not {}) until load_markets() has
    # succeeded at least once — matches real ccxt behavior, not an idealized mock.
    exchange = AsyncMock()
    exchange.has = {"fetchOpenInterest": True}
    exchange.load_markets = AsyncMock(side_effect=RuntimeError("boom"))
    exchange.markets = None
    exchange.fetch_open_interest = AsyncMock(return_value={"openInterestValue": 5.0})
    exchange.close = AsyncMock()
    pumps = [_pump("BTC", [_ex_entry("okx")])]

    with patch.dict(
        "schurfer_analytics.oi.EXCHANGE_FACTORIES", {"okx": lambda: exchange}, clear=True
    ):
        result = asyncio.run(fetch_oi_for_pumps(pumps))

    assert result == [{"base": "BTC", "exchange": "okx", "oi_usd": 5.0}]


def test_fetch_oi_falls_back_to_contract_size_1_when_markets_unavailable() -> None:
    # load_markets() failed (exchange.markets is still None) AND the exchange
    # didn't give openInterestValue, forcing the contractSize fallback path.
    # Must degrade to contractSize=1 instead of raising AttributeError out of
    # gather() and crashing the whole scan loop.
    exchange = AsyncMock()
    exchange.has = {"fetchOpenInterest": True}
    exchange.load_markets = AsyncMock(side_effect=RuntimeError("boom"))
    exchange.markets = None
    exchange.fetch_open_interest = AsyncMock(
        return_value={"openInterestValue": None, "openInterestAmount": 100.0}
    )
    exchange.close = AsyncMock()
    pumps = [_pump("BTC", [_ex_entry("okx", price="50.0")])]

    with patch.dict(
        "schurfer_analytics.oi.EXCHANGE_FACTORIES", {"okx": lambda: exchange}, clear=True
    ):
        result = asyncio.run(fetch_oi_for_pumps(pumps))

    assert result == [{"base": "BTC", "exchange": "okx", "oi_usd": 5000.0}]
    exchange.close.assert_awaited_once()


def test_fetch_oi_skips_entry_on_malformed_amount() -> None:
    # A non-numeric openInterestAmount must not raise out of gather().
    exchange = _mock_exchange({"openInterestValue": None, "openInterestAmount": "not-a-number"})
    pumps = [_pump("BTC", [_ex_entry("okx", price="50.0")])]

    with patch.dict(
        "schurfer_analytics.oi.EXCHANGE_FACTORIES", {"okx": lambda: exchange}, clear=True
    ):
        result = asyncio.run(fetch_oi_for_pumps(pumps))

    assert result == []


def test_fetch_oi_processes_more_items_than_concurrency_cap() -> None:
    # MAX_CONCURRENT_PER_EXCHANGE is 5 — verify correctness isn't lost when a
    # single exchange has more symbols than that in one scan.
    bases = [f"TOK{i}" for i in range(8)]
    exchange = _mock_exchange({"openInterestValue": 1.0})
    pumps = [_pump(base, [_ex_entry("okx")]) for base in bases]

    with patch.dict(
        "schurfer_analytics.oi.EXCHANGE_FACTORIES", {"okx": lambda: exchange}, clear=True
    ):
        result = asyncio.run(fetch_oi_for_pumps(pumps))

    assert {row["base"] for row in result} == set(bases)


def test_fetch_oi_times_out_gracefully() -> None:
    async def _slow_fetch(symbol: str) -> dict[str, object]:
        await asyncio.sleep(1)
        return {"openInterestValue": 1.0}

    exchange = AsyncMock()
    exchange.has = {"fetchOpenInterest": True}
    exchange.load_markets = AsyncMock()
    exchange.markets = {}
    exchange.fetch_open_interest = _slow_fetch
    exchange.close = AsyncMock()
    pumps = [_pump("BTC", [_ex_entry("okx")])]

    factories = {"okx": lambda: exchange}
    with (
        patch("schurfer_analytics.oi.FETCH_TIMEOUT", 0.05),
        patch.dict("schurfer_analytics.oi.EXCHANGE_FACTORIES", factories, clear=True),
    ):
        result = asyncio.run(fetch_oi_for_pumps(pumps))

    assert result == []
    exchange.close.assert_awaited_once()


def test_fetch_oi_empty_pumps_returns_empty() -> None:
    result = asyncio.run(fetch_oi_for_pumps([]))
    assert result == []


def test_fetch_oi_groups_multiple_bases_on_same_exchange() -> None:
    exchange = AsyncMock()
    exchange.has = {"fetchOpenInterest": True}
    exchange.load_markets = AsyncMock()
    exchange.markets = {}
    exchange.fetch_open_interest = AsyncMock(
        side_effect=[{"openInterestValue": 1.0}, {"openInterestValue": 2.0}]
    )
    exchange.close = AsyncMock()
    pumps = [
        _pump("BTC", [_ex_entry("bybit")]),
        _pump("ETH", [_ex_entry("bybit")]),
    ]

    with patch.dict(
        "schurfer_analytics.oi.EXCHANGE_FACTORIES", {"bybit": lambda: exchange}, clear=True
    ):
        result = asyncio.run(fetch_oi_for_pumps(pumps))

    assert exchange.close.await_count == 1
    bases = {row["base"] for row in result}
    assert bases == {"BTC", "ETH"}
