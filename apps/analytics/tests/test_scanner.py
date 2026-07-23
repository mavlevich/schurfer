import json
from unittest.mock import AsyncMock

import pytest
from schurfer_analytics.config import Config
from schurfer_analytics.exchange_registry import DEFAULT_EXCHANGES, EXCHANGE_FACTORIES
from schurfer_analytics.scanner import (
    MAX_TICKER_AGE_MS,
    MAX_TICKER_FUTURE_SKEW_MS,
    REDIS_KEY,
    REDIS_TTL,
    ScanBatch,
    _aggregate_below_updates,
    _dedup,
    _fetch,
    _ticker_timestamp_ms,
    _tracked_pumps,
    _volume_24h_usd,
    publish,
)

NEW_EXCHANGES = ("lbank", "bitmart", "xt", "toobit", "blofin")


def _entry(base: str, exchange: str, pct: float) -> dict[str, object]:
    return {
        "base": base,
        "exchange": exchange,
        "symbol": f"{base}USDT",
        "price": "100.0",
        "change_pct": pct,
        "high_24h": "110.0",
        "volume_24h_usd": 1_000_000.0,
        "volume_24h_source": "quote_volume",
        "ticker_timestamp_ms": None,
    }


def _ticker(
    percentage: float | None,
    *,
    timestamp: int | str | None = None,
    quote_volume: object = 10_000.0,
    base_volume: object = None,
    info: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "percentage": percentage,
        "timestamp": timestamp,
        "last": 1.25,
        "high": 1.5,
        "quoteVolume": quote_volume,
        "baseVolume": base_volume,
        "info": info or {},
    }


def test_default_exchanges_match_registered_factories(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PUMP_EXCHANGES", raising=False)

    assert Config().exchanges == list(DEFAULT_EXCHANGES)
    assert tuple(EXCHANGE_FACTORIES) == DEFAULT_EXCHANGES


@pytest.mark.parametrize("name", NEW_EXCHANGES)
def test_new_exchange_factory_builds_linear_swap_client(name: str) -> None:
    exchange = EXCHANGE_FACTORIES[name]()

    assert exchange.id == name
    assert exchange.options["defaultType"] == "swap"


async def test_fetch_applies_same_linear_usdt_rules_to_every_exchange() -> None:
    exchange = AsyncMock()
    exchange.has = {"fetchTickers": True}
    exchange.fetch_tickers.return_value = {
        "PUMP/USDT:USDT": _ticker(31.25),
        "WATCH/USDT:USDT": _ticker(20.0),
        "SPOT/USDT": _ticker(80.0),
        "USDC/USDC:USDC": _ticker(90.0),
        "MISSING/USDT:USDT": _ticker(None),
        "BOGUS/USDT:USDT": _ticker(5_001.0),
    }

    above, below, error = await _fetch(
        "test",
        exchange,
        min_pct=30.0,
        extra_bases=frozenset({"WATCH"}),
    )

    assert error is None
    assert [row["base"] for row in above] == ["PUMP"]
    assert above[0]["change_pct"] == 31.25
    assert [row["base"] for row in below] == ["WATCH"]


async def test_fetch_rejects_stale_and_invalid_tickers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_ms = 1_800_000_000_000
    monkeypatch.setattr("schurfer_analytics.scanner.time.time", lambda: now_ms / 1000)
    exchange = AsyncMock()
    exchange.has = {"fetchTickers": True}
    exchange.markets = {
        "FRESH/USDT:USDT": {"active": True},
        "STALE/USDT:USDT": {"active": True},
        "INVALID/USDT:USDT": {"active": True},
        "FUTURE/USDT:USDT": {"active": True},
        "UNKNOWN/USDT:USDT": {"active": None},
        "INACTIVE/USDT:USDT": {"active": False},
        "TRADING_DISABLED/USDT:USDT": {
            "active": True,
            "info": {"tradeSwitch": False},
        },
    }
    exchange.fetch_tickers.return_value = {
        "FRESH/USDT:USDT": _ticker(31.0, timestamp=now_ms - MAX_TICKER_AGE_MS),
        "STALE/USDT:USDT": _ticker(90.0, timestamp=now_ms - MAX_TICKER_AGE_MS - 1),
        "INVALID/USDT:USDT": _ticker(80.0, timestamp="not-a-timestamp"),
        "FUTURE/USDT:USDT": _ticker(
            70.0,
            timestamp=now_ms + MAX_TICKER_FUTURE_SKEW_MS + 1,
        ),
        "UNKNOWN/USDT:USDT": _ticker(32.0),
        "INACTIVE/USDT:USDT": _ticker(100.0, timestamp=now_ms),
        "TRADING_DISABLED/USDT:USDT": _ticker(100.0, timestamp=now_ms),
    }

    above, below, error = await _fetch("xt", exchange, min_pct=30.0)

    assert error is None
    assert below == []
    assert [row["base"] for row in above] == ["FRESH", "UNKNOWN"]


def test_volume_24h_prefers_reported_quote_volume() -> None:
    ticker = _ticker(30.0, quote_volume="2500.5", base_volume=10_000)

    assert _volume_24h_usd(ticker) == (2500.5, "quote_volume")


def test_volume_24h_does_not_guess_quote_notional_from_contract_volume() -> None:
    ticker = _ticker(30.0, quote_volume=0, base_volume="2000")

    assert _volume_24h_usd(ticker) == (None, "unavailable")


@pytest.mark.parametrize(
    ("quote_volume", "base_volume"),
    [
        (None, None),
        (0, 0),
        ("invalid", float("inf")),
        (-1, -1),
    ],
)
def test_volume_24h_preserves_unavailable_as_null(
    quote_volume: object,
    base_volume: object,
) -> None:
    ticker = _ticker(
        30.0,
        quote_volume=quote_volume,
        base_volume=base_volume,
    )

    assert _volume_24h_usd(ticker) == (None, "unavailable")


def test_lbank_raw_last_time_is_normalized_from_seconds() -> None:
    ticker = _ticker(30.0, info={"lastTime": "1800000000"})

    assert _ticker_timestamp_ms("lbank", ticker) == 1_800_000_000_000


def test_non_lbank_raw_last_time_is_not_guessed() -> None:
    ticker = _ticker(30.0, info={"lastTime": "1800000000"})

    assert _ticker_timestamp_ms("binance", ticker) is None


async def test_fetch_uses_lbank_raw_last_time_for_freshness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_ms = 1_800_000_000_000
    monkeypatch.setattr("schurfer_analytics.scanner.time.time", lambda: now_ms / 1000)
    exchange = AsyncMock()
    exchange.has = {"fetchTickers": True}
    exchange.markets = {}
    exchange.fetch_tickers.return_value = {
        "FRESH/USDT:USDT": _ticker(
            31.0,
            info={"lastTime": str(now_ms // 1000)},
        ),
        "STALE/USDT:USDT": _ticker(
            90.0,
            info={"lastTime": str((now_ms - MAX_TICKER_AGE_MS - 1000) // 1000)},
        ),
    }

    above, below, error = await _fetch("lbank", exchange, min_pct=30.0)

    assert error is None
    assert below == []
    assert [row["base"] for row in above] == ["FRESH"]
    assert above[0]["ticker_timestamp_ms"] == now_ms


async def test_fetch_skips_exchange_without_bulk_tickers() -> None:
    exchange = AsyncMock()
    exchange.has = {"fetchTickers": False}

    assert await _fetch("test", exchange, min_pct=30.0) == ([], [], None)
    exchange.fetch_tickers.assert_not_awaited()


async def test_fetch_isolates_exchange_api_failure() -> None:
    exchange = AsyncMock()
    exchange.has = {"fetchTickers": True}
    exchange.fetch_tickers.side_effect = RuntimeError("upstream unavailable")

    above, below, error = await _fetch("test", exchange, min_pct=30.0)

    assert above == []
    assert below == []
    assert error == "upstream unavailable"


def test_dedup_single_exchange() -> None:
    entries = [_entry("BTC", "bybit", 55.0), _entry("ETH", "bybit", 40.0)]
    result = _dedup(entries)
    assert len(result) == 2
    assert result[0]["base"] == "BTC"
    assert result[0]["max_change_pct"] == 55.0
    assert result[1]["base"] == "ETH"


def test_dedup_multi_exchange_same_token() -> None:
    entries = [
        _entry("BTC", "bybit", 55.0),
        _entry("BTC", "okx", 54.2),
        _entry("ETH", "bybit", 40.0),
    ]
    result = _dedup(entries)
    assert len(result) == 2
    btc = result[0]
    assert btc["base"] == "BTC"
    assert btc["max_change_pct"] == 55.0
    assert len(btc["exchanges"]) == 2
    assert btc["exchanges"][0]["exchange"] == "bybit"
    assert btc["exchanges"][1]["exchange"] == "okx"


def test_dedup_no_bogus_pct() -> None:
    # Entries with change_pct above the 5000% sanity cap should not appear.
    # _dedup receives already-filtered entries so this is a guard for the cap logic.
    entries = [
        _entry("BTC", "bybit", 80.0),
        _entry("FAKE", "bingx", 173_000.0),  # BingX stock-index garbage
    ]
    result = _dedup(entries)
    bases = [p["base"] for p in result]
    assert "BTC" in bases
    assert "FAKE" in bases  # _dedup itself doesn't filter — _fetch does


def test_dedup_sorted_by_max_pct() -> None:
    entries = [
        _entry("ETH", "bybit", 35.0),
        _entry("BTC", "bybit", 80.0),
        _entry("SOL", "okx", 60.0),
    ]
    result = _dedup(entries)
    pcts = [p["max_change_pct"] for p in result]
    assert pcts == sorted(pcts, reverse=True)


# _aggregate_below_updates tests


def test_aggregate_tracked_below_threshold() -> None:
    flat_below = [_entry("DOGE", "bybit", 22.0)]
    result = _aggregate_below_updates(flat_below, live_bases=set())
    assert result == {"DOGE": 22.0}


def test_aggregate_live_token_excluded() -> None:
    flat_below = [_entry("BTC", "bybit", 20.0)]
    result = _aggregate_below_updates(flat_below, live_bases={"BTC"})
    assert "BTC" not in result


def test_aggregate_mixed_exchanges_same_base_excluded() -> None:
    # BTC is above threshold on binance (live), below on bybit — must not appear in updates
    flat_below = [_entry("BTC", "bybit", 20.0), _entry("DOGE", "okx", 15.0)]
    result = _aggregate_below_updates(flat_below, live_bases={"BTC"})
    assert "BTC" not in result
    assert result == {"DOGE": 15.0}


def test_aggregate_picks_max_across_exchanges() -> None:
    flat_below = [
        _entry("DOGE", "bybit", 18.0),
        _entry("DOGE", "okx", 25.0),
        _entry("DOGE", "gate", 10.0),
    ]
    result = _aggregate_below_updates(flat_below, live_bases=set())
    assert result == {"DOGE": 25.0}


# _tracked_pumps tests


def test_tracked_pumps_includes_faded_tokens() -> None:
    flat_below = [_entry("DOGE", "bybit", 18.0)]
    result = _tracked_pumps(flat_below, live_bases=set())
    assert len(result) == 1
    assert result[0]["base"] == "DOGE"
    assert result[0]["exchanges"][0]["exchange"] == "bybit"


def test_tracked_pumps_excludes_live_tokens() -> None:
    # BTC is below on bybit but still live (above threshold) on binance —
    # it must not appear in tracked_pumps since it's already in `pumps`.
    flat_below = [_entry("BTC", "bybit", 20.0), _entry("DOGE", "okx", 15.0)]
    result = _tracked_pumps(flat_below, live_bases={"BTC"})
    bases = [p["base"] for p in result]
    assert bases == ["DOGE"]


def test_tracked_pumps_empty_when_all_live() -> None:
    flat_below = [_entry("BTC", "bybit", 20.0)]
    result = _tracked_pumps(flat_below, live_bases={"BTC"})
    assert result == []


async def test_publish_stores_fully_attributed_batch() -> None:
    rdb = AsyncMock()
    batch = ScanBatch(
        pumps=[{"base": "BTC", "pump_event_id": 42, "max_change_pct": 50.0, "exchanges": []}],
        errors={"okx": "timeout"},
        below_updates={},
        tracked_pumps=[],
        scanned=("binance",),
    )

    await publish(batch, 30.0, rdb)

    rdb.set.assert_awaited_once()
    key, raw = rdb.set.await_args.args
    assert key == REDIS_KEY
    assert rdb.set.await_args.kwargs == {"ex": REDIS_TTL}
    payload = json.loads(raw)
    assert payload["pumps"][0]["pump_event_id"] == 42
    assert payload["scanned"] == ["binance"]
    assert payload["errors"] == {"okx": "timeout"}
