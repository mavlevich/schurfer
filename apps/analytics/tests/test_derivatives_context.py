from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from schurfer_analytics.derivatives_context import (
    DerivativesContextProbeResult,
    DerivativesContextTarget,
    collect_derivatives_context_target,
    probe_derivatives_context,
    result_fingerprint,
    target_fingerprint,
)
from schurfer_analytics.derivatives_history import (
    METHODS,
    DerivativesHistoryMethod,
    effective_limit,
    effective_timeframe,
)

ANCHOR = datetime(2026, 7, 27, 12, tzinfo=UTC)
SINCE_MS = int((ANCHOR - timedelta(minutes=240)).timestamp() * 1000)
UNTIL_MS = int((ANCHOR + timedelta(minutes=480)).timestamp() * 1000)


def _target(exchange: str = "binance") -> DerivativesContextTarget:
    return DerivativesContextTarget(
        event_id=42,
        exchange=exchange,
        base="ERA",
        unified_symbol="ERA/USDT:USDT",
        market_id="ERAUSDT",
        identity_key=f"{exchange}:unknown:ERAUSDT:unknown",
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
    exchange.markets = {"ERA/USDT:USDT": {"id": "ERAUSDT"}} if symbol_available else {}
    exchange.load_markets = AsyncMock()
    exchange.close = AsyncMock()
    for method in METHODS:
        setattr(exchange, method.callable_name, AsyncMock(return_value=response))
    return exchange


def _row(method: DerivativesHistoryMethod, timestamp: int) -> object:
    if method.row_kind == "ohlcv":
        return [timestamp, 1, 2, 0.5, 1.5, 10]
    return {"timestamp": timestamp}


def _complete_response(method: DerivativesHistoryMethod) -> list[object]:
    if method.series_kind == "event":
        return [_row(method, SINCE_MS + 60_000), _row(method, UNTIL_MS)]
    return [_row(method, timestamp) for timestamp in range(SINCE_MS, UNTIL_MS + 1, 5 * 60_000)]


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
    method: DerivativesHistoryMethod,
) -> None:
    selected = method
    exchange = _exchange(
        capability=selected.capability,
        response=_complete_response(selected),
    )

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
    expected_call = (
        ("ERA/USDT:USDT", SINCE_MS, 200)
        if selected.timeframe is None
        else ("ERA/USDT:USDT", "5m", SINCE_MS, 200)
    )
    fetcher.assert_awaited_once_with(*expected_call)
    assert result.status == "sampled"
    assert result.in_window_rows == (144 if selected.series_kind == "regular" else 1)


async def test_probe_reuses_one_loaded_client_for_all_methods() -> None:
    timestamp = {"timestamp": SINCE_MS + 60_000}
    exchange = _exchange(
        capability=METHODS[0].capability,
        response=[timestamp],
    )
    exchange.has = {method.capability: True for method in METHODS}
    for method in METHODS:
        getattr(exchange, method.callable_name).return_value = _complete_response(method)
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
        response=[
            {"timestamp": SINCE_MS + 60_000},
            {"timestamp": UNTIL_MS},
        ],
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
            {"timestamp": UNTIL_MS},
            {"timestamp": (SINCE_MS + 120_000) // 1000},
            {"timestamp": "not-a-number"},
        ],
    )

    result = await _probe(exchange)

    assert result.status == "partial"
    assert result.returned_rows == 4
    assert result.valid_timestamp_rows == 2
    assert result.in_window_rows == 1
    assert result.invalid_rows == 2


async def test_event_rows_with_the_same_timestamp_remain_distinct() -> None:
    event_timestamp = SINCE_MS + 60_000
    exchange = _exchange(
        capability=METHODS[0].capability,
        response=[
            {"timestamp": event_timestamp, "id": "first"},
            {"timestamp": event_timestamp, "id": "second"},
            {"timestamp": UNTIL_MS},
        ],
    )

    result = await _probe(exchange)

    assert result.status == "sampled"
    assert result.in_window_rows == 2
    assert result.duplicate_rows == 0


async def test_seconds_only_response_is_invalid_not_empty_history() -> None:
    exchange = _exchange(
        capability=METHODS[0].capability,
        response=[{"timestamp": (SINCE_MS + 60_000) // 1000}],
    )

    result = await _probe(exchange)

    assert result.status == "invalid_response"
    assert result.error == "response contained no valid unified millisecond timestamps"


async def test_out_of_window_response_is_not_reported_as_empty_history() -> None:
    exchange = _exchange(
        capability=METHODS[0].capability,
        response=[{"timestamp": SINCE_MS - 1}],
    )

    result = await _probe(exchange)

    assert result.status == "window_mismatch"
    assert result.valid_timestamp_rows == 1
    assert result.in_window_rows == 0


async def test_regular_series_paginates_until_the_window_is_complete() -> None:
    method = METHODS[2]
    first_page = [
        _row(method, timestamp)
        for timestamp in range(SINCE_MS, SINCE_MS + 100 * 5 * 60_000, 5 * 60_000)
    ]
    second_page = [
        _row(method, timestamp)
        for timestamp in range(
            SINCE_MS + 100 * 5 * 60_000,
            UNTIL_MS + 1,
            5 * 60_000,
        )
    ]
    exchange = _exchange(capability=method.capability, response=[])
    exchange.fetch_mark_ohlcv.side_effect = [first_page, second_page]

    result = await _probe(exchange, method_index=2)

    assert result.status == "sampled"
    assert result.request_count == 2
    assert result.in_window_rows == 144
    assert result.expected_rows == 144
    assert result.coverage_ratio == 1
    assert result.covers_start is True
    assert result.covers_end is True
    assert result.missing_rows == 0
    assert result.max_gap_minutes == 5
    assert exchange.fetch_mark_ohlcv.await_args_list[1].args[2] == (SINCE_MS + 100 * 5 * 60_000)


async def test_regular_series_with_gap_is_incomplete() -> None:
    method = METHODS[2]
    response = [
        _row(method, timestamp)
        for timestamp in range(SINCE_MS, UNTIL_MS + 1, 5 * 60_000)
        if timestamp != SINCE_MS + 60 * 60_000
    ]
    exchange = _exchange(capability=method.capability, response=response)

    result = await _probe(exchange, method_index=2)

    assert result.status == "incomplete"
    assert result.coverage_ratio == pytest.approx(143 / 144)
    assert result.missing_rows == 1
    assert result.max_gap_minutes == 10


async def test_regular_series_uses_the_observed_grid_for_an_unaligned_window() -> None:
    method = METHODS[2]
    target = replace(_target(), anchor_at=ANCHOR + timedelta(minutes=2))
    first_timestamp = int((target.anchor_at - timedelta(minutes=2)).timestamp() * 1000)
    exchange = _exchange(
        capability=method.capability,
        response=[
            _row(method, first_timestamp),
            _row(method, first_timestamp + 5 * 60_000),
        ],
    )

    result = (
        await probe_derivatives_context(
            (target,),
            {"binance": lambda: exchange},
            (method,),
            before_minutes=6,
            after_minutes=8,
            limit=200,
            timeout_seconds=1,
        )
    )[0]

    assert result.status == "sampled"
    assert result.expected_rows == 2
    assert result.in_window_rows == 2
    assert result.coverage_ratio == 1
    assert result.covers_start is True
    assert result.covers_end is True


async def test_regular_pagination_stall_is_visible_and_deduplicated() -> None:
    method = METHODS[2]
    page = [
        _row(method, timestamp)
        for timestamp in range(SINCE_MS, SINCE_MS + 100 * 5 * 60_000, 5 * 60_000)
    ]
    exchange = _exchange(capability=method.capability, response=[])
    exchange.fetch_mark_ohlcv.side_effect = [page, page]

    result = await _probe(exchange, method_index=2)

    assert result.status == "incomplete"
    assert result.pagination_exhausted is True
    assert result.request_count == 2
    assert result.returned_rows == 200
    assert result.in_window_rows == 100
    assert result.duplicate_rows == 100
    assert result.error == "pagination made no forward progress"


async def test_later_page_failure_preserves_coverage_diagnostics() -> None:
    method = METHODS[2]
    page = [
        _row(method, timestamp)
        for timestamp in range(SINCE_MS, SINCE_MS + 100 * 5 * 60_000, 5 * 60_000)
    ]
    exchange = _exchange(capability=method.capability, response=[])
    exchange.fetch_mark_ohlcv.side_effect = [page, RuntimeError("second page failed")]

    result = await _probe(exchange, method_index=2)

    assert result.status == "fetch_failed"
    assert result.request_count == 2
    assert result.in_window_rows == 100
    assert result.coverage_ratio == pytest.approx(100 / 144)
    assert result.error == "second page failed"


async def test_htx_open_interest_uses_registered_hourly_override() -> None:
    method = METHODS[1]
    rows = [_row(method, timestamp) for timestamp in range(SINCE_MS, UNTIL_MS + 1, 60 * 60_000)]
    exchange = _exchange(capability=method.capability, response=rows)

    result = (
        await probe_derivatives_context(
            (_target("htx"),),
            {"htx": lambda: exchange},
            (method,),
            before_minutes=240,
            after_minutes=480,
            limit=200,
            timeout_seconds=1,
        )
    )[0]

    exchange.fetch_open_interest_history.assert_awaited_once_with(
        "ERA/USDT:USDT",
        "1h",
        SINCE_MS,
        200,
    )
    assert effective_timeframe("htx", method) == "1h"
    assert result.effective_timeframe == "1h"
    assert result.expected_rows == 12
    assert result.status == "sampled"


@pytest.mark.parametrize("method_index", [0, 6])
async def test_htx_event_history_uses_documented_100_row_limit(
    method_index: int,
) -> None:
    method = METHODS[method_index]
    exchange = _exchange(
        capability=method.capability,
        response=[
            _row(method, SINCE_MS + 60_000),
            _row(method, UNTIL_MS),
        ],
    )

    result = (
        await probe_derivatives_context(
            (_target("htx"),),
            {"htx": lambda: exchange},
            (method,),
            before_minutes=240,
            after_minutes=480,
            limit=200,
            timeout_seconds=1,
        )
    )[0]

    getattr(exchange, method.callable_name).assert_awaited_once_with(
        "ERA/USDT:USDT",
        SINCE_MS,
        100,
    )
    assert effective_limit("htx", method, 200) == 100
    assert result.effective_limit == 100


async def test_collector_returns_idempotent_in_window_samples() -> None:
    method = METHODS[0]
    duplicate = {"timestamp": SINCE_MS + 60_000, "id": "same"}
    distinct = {"timestamp": SINCE_MS + 60_000, "id": "other", "rate": float("nan")}
    exchange = _exchange(
        capability=method.capability,
        response=[
            {"timestamp": SINCE_MS - 1, "id": "before"},
            duplicate,
            duplicate,
            distinct,
            {"timestamp": UNTIL_MS, "id": "after"},
        ],
    )

    observation = (
        await collect_derivatives_context_target(
            "binance",
            exchange,
            _target(),
            (method,),
            before_minutes=240,
            after_minutes=480,
            limit=200,
            max_pages=10,
            timeout_seconds=1,
        )
    )[0]

    assert observation.result.in_window_rows == 3
    assert len(observation.samples) == 2
    assert observation.samples[0].source_at == observation.samples[1].source_at
    assert all(len(sample.sample_key) == 64 for sample in observation.samples)
    payloads = [
        sample.payload for sample in observation.samples if isinstance(sample.payload, dict)
    ]
    assert len(payloads) == 2
    assert {payload["id"] for payload in payloads} == {"same", "other"}
    assert next(payload for payload in payloads if payload["id"] == "other")["rate"] is None


async def test_collector_fails_closed_when_current_market_identity_changed() -> None:
    method = METHODS[0]
    exchange = _exchange(capability=method.capability, response=[])
    exchange.markets["ERA/USDT:USDT"]["id"] = "NEWERAUSDT"

    observation = (
        await collect_derivatives_context_target(
            "binance",
            exchange,
            _target(),
            (method,),
            before_minutes=240,
            after_minutes=480,
            limit=200,
            max_pages=10,
            timeout_seconds=1,
        )
    )[0]

    assert observation.result.status == "identity_mismatch"
    assert observation.result.declared_support is True
    assert "NEWERAUSDT" in (observation.result.error or "")
    assert observation.samples == ()
    exchange.fetch_funding_rate_history.assert_not_awaited()


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
    with pytest.raises(ValueError, match="max pages"):
        await probe_derivatives_context(
            (_target(),),
            {"binance": MagicMock()},
            (METHODS[0],),
            **arguments,
            max_pages=0,
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
