from schurfer_analytics.scanner import _aggregate_below_updates, _dedup


def _entry(base: str, exchange: str, pct: float) -> dict[str, object]:
    return {
        "base": base,
        "exchange": exchange,
        "symbol": f"{base}USDT",
        "price": "100.0",
        "change_pct": pct,
        "high_24h": "110.0",
        "volume_24h_usd": 1_000_000.0,
    }


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
