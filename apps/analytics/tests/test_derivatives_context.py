from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from schurfer_analytics.derivatives_context import (
    METHODS,
    DerivativesContextMethod,
    DerivativesContextProbeResult,
    DerivativesContextTarget,
    probe_derivatives_context,
    result_fingerprint,
    target_fingerprint,
)

ANCHOR = datetime(2026, 7, 27, 12, tzinfo=UTC)
SINCE_MS = int((ANCHOR - timedelta(minutes=240)).timestamp() * 1000)


def _target(exchange: str = "binance") -> DerivativesContextTarget:
    return DerivativesContextTarget(
        event_id=42,
        exchange=exchange,
        base="ERA",
        unified_symbol="ERA/USDT:USDT",
        market_id="ERAUSDT",
        identity_key="era",
        anchor_at=ANCHOR,
    )


def _exchange(
    *,
    capability: str,
    response: object,
    symbol_available: bool = True,
) -> MagicMock:
    exchange = MagicMock()
    exchange.has = {capability: True}
    exchange.markets = {"ERA/USDT:USDT": {}} if symbol_available else {}
    exchange.load_markets = AsyncMock()
    exchange.close = AsyncMock()
    for method in METHODS:
        setattr(exchange, method.callable_name, AsyncMock(return_value=response))
    return exchange


async def _probe(
    exchange: MagicMock,
    *,
    method_index: int = 0,
) -> DerivativesContextProbeResult:
    method = METHODS[method_index]
    results = await probe_derivatives_context(
        (_target(),),
        {"binance": lambda: exchange},
        (method,),
        before_minutes=240,
        after_minutes=480,
        limit=200,
        timeout_seconds=1,
    )
    return results[0]


@pytest.mark.parametrize("method", METHODS, ids=lambda method: method.name)
async def test_every_method_uses_the_locked_ccxt_signature(
    method: DerivativesContextMethod,
) -> None:
    selected = method
    row = (
        [SINCE_MS + 60_000, 1, 2, 0.5, 1.5, 10]
        if selected.row_kind == "ohlcv"
        else {"timestamp": SINCE_MS + 60_000}
    )
    exchange = _exchange(capability=selected.capability, response=[row])

    result = (
        await probe_derivatives_context(
            (_target(),),
            {"binance": lambda: exchange},
            (selected,),
            before_minutes=240,
            after_minutes=480,
            limit=200,
            timeout_seconds=1,
        )
    )[0]

    fetcher = getattr(exchange, selected.callable_name)
    expected = (
        ("ERA/USDT:USDT", SINCE_MS, 200)
        if selected.timeframe is None
        else ("ERA/USDT:USDT", "5m", SINCE_MS, 200)
    )
    fetcher.assert_awaited_once_with(*expected)
    assert result.status == "sampled"
    assert result.in_window_rows == 1


async def test_probe_reuses_one_loaded_client_for_all_methods() -> None:
    timestamp = {"timestamp": SINCE_MS + 60_000}
    exchange = _exchange(
        capability=METHODS[0].capability,
        response=[timestamp],
    )
    exchange.has = {method.capability: True for method in METHODS}
    for method in METHODS:
        response = (
            [[SINCE_MS + 60_000, 1, 2, 0.5, 1.5, 10]] if method.row_kind == "ohlcv" else [timestamp]
        )
        getattr(exchange, method.callable_name).return_value = response
    factory = MagicMock(return_value=exchange)

    results = await probe_derivatives_context(
        (_target(),),
        {"binance": factory},
        METHODS,
        before_minutes=240,
        after_minutes=480,
        limit=200,
        timeout_seconds=1,
    )

    factory.assert_called_once_with()
    exchange.load_markets.assert_awaited_once_with()
    exchange.close.assert_awaited_once_with()
    assert len(results) == len(METHODS)
    assert {result.status for result in results} == {"sampled"}


async def test_no_target_or_unsupported_capability_never_fetches() -> None:
    factory = MagicMock()
    no_target = await probe_derivatives_context(
        (),
        {"binance": factory},
        (METHODS[0],),
        before_minutes=240,
        after_minutes=480,
        limit=200,
        timeout_seconds=1,
    )
    assert no_target[0].status == "no_target"
    factory.assert_not_called()

    exchange = _exchange(capability="differentCapability", response=[])
    unsupported = await _probe(exchange)
    assert unsupported.status == "unsupported"
    exchange.fetch_funding_rate_history.assert_not_awaited()


async def test_emulated_capability_is_reported_separately_and_probed() -> None:
    exchange = _exchange(
        capability=METHODS[0].capability,
        response=[{"timestamp": SINCE_MS + 60_000}],
    )
    exchange.has = {METHODS[0].capability: "emulated"}

    result = await _probe(exchange)

    assert result.declared_support == "emulated"
    assert result.status == "sampled"
    exchange.fetch_funding_rate_history.assert_awaited_once()


@pytest.mark.parametrize(
    ("setup", "expected_status"),
    [
        ("client", "client_init_failed"),
        ("markets", "load_markets_failed"),
        ("symbol", "symbol_unavailable"),
        ("fetch", "fetch_failed"),
        ("response", "invalid_response"),
    ],
)
async def test_probe_contains_exchange_and_response_failures(
    setup: str,
    expected_status: str,
) -> None:
    method = METHODS[0]
    exchange = _exchange(capability=method.capability, response=[])

    def factory() -> MagicMock:
        return exchange

    selected_factory = factory
    if setup == "client":
        selected_factory = MagicMock(side_effect=RuntimeError("cannot initialize"))
    elif setup == "markets":
        exchange.load_markets.side_effect = RuntimeError("markets unavailable")
    elif setup == "symbol":
        exchange.markets = {}
    elif setup == "fetch":
        exchange.fetch_funding_rate_history.side_effect = RuntimeError("venue unavailable")
    elif setup == "response":
        exchange.fetch_funding_rate_history.return_value = {"not": "a list"}

    result = (
        await probe_derivatives_context(
            (_target(),),
            {"binance": selected_factory},
            (method,),
            before_minutes=240,
            after_minutes=480,
            limit=200,
            timeout_seconds=1,
        )
    )[0]

    assert result.status == expected_status
    if setup != "client":
        exchange.close.assert_awaited_once_with()


async def test_invalid_and_seconds_timestamps_are_reported_without_repair() -> None:
    exchange = _exchange(
        capability=METHODS[0].capability,
        response=[
            {"timestamp": SINCE_MS + 60_000},
            {"timestamp": (SINCE_MS + 120_000) // 1000},
            {"timestamp": "not-a-number"},
        ],
    )

    result = await _probe(exchange)

    assert result.status == "partial"
    assert result.returned_rows == 3
    assert result.valid_timestamp_rows == 1
    assert result.in_window_rows == 1
    assert result.invalid_rows == 2


async def test_seconds_only_response_is_invalid_not_empty_history() -> None:
    exchange = _exchange(
        capability=METHODS[0].capability,
        response=[{"timestamp": (SINCE_MS + 60_000) // 1000}],
    )

    result = await _probe(exchange)

    assert result.status == "invalid_response"
    assert result.error == "response contained no valid unified millisecond timestamps"


async def test_empty_or_out_of_window_response_is_no_data() -> None:
    exchange = _exchange(
        capability=METHODS[0].capability,
        response=[{"timestamp": SINCE_MS - 1}],
    )

    result = await _probe(exchange)

    assert result.status == "no_data"
    assert result.valid_timestamp_rows == 1
    assert result.in_window_rows == 0


async def test_probe_rejects_ambiguous_inputs() -> None:
    arguments = {
        "before_minutes": 240,
        "after_minutes": 480,
        "limit": 200,
        "timeout_seconds": 1,
    }
    with pytest.raises(ValueError, match="targets must be unique"):
        await probe_derivatives_context(
            (_target(), _target()),
            {"binance": MagicMock()},
            (METHODS[0],),
            **arguments,
        )
    with pytest.raises(ValueError, match="methods must be unique"):
        await probe_derivatives_context(
            (_target(),),
            {"binance": MagicMock()},
            (METHODS[0], METHODS[0]),
            **arguments,
        )
    with pytest.raises(ValueError, match="must have an exchange factory"):
        await probe_derivatives_context(
            (_target("bybit"),),
            {"binance": MagicMock()},
            (METHODS[0],),
            **arguments,
        )


def test_fingerprints_are_order_independent_and_content_sensitive() -> None:
    first = _target("binance")
    second = _target("bybit")
    assert target_fingerprint((first, second)) == target_fingerprint((second, first))
    assert target_fingerprint((first,)) != target_fingerprint((second,))

    result = DerivativesContextProbeResult(
        exchange="binance",
        method="funding_rate_history",
        capability="fetchFundingRateHistory",
        declared_support=True,
        status="sampled",
        event_id=42,
        base="ERA",
        unified_symbol="ERA/USDT:USDT",
        market_id="ERAUSDT",
        identity_key="era",
        anchor_at=ANCHOR,
        requested_since=ANCHOR - timedelta(minutes=240),
        requested_until=ANCHOR + timedelta(minutes=480),
        fetched_at=ANCHOR,
        returned_rows=1,
        valid_timestamp_rows=1,
        in_window_rows=1,
        invalid_rows=0,
        first_source_at=ANCHOR,
        last_source_at=ANCHOR,
    )
    other = replace(result, exchange="bybit")
    assert result_fingerprint((result, other)) == result_fingerprint((other, result))
