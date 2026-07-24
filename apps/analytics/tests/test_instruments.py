from typing import Any

from schurfer_analytics.instruments import _timestamp_ms, instrument_metadata


def test_bingx_identity_preserves_display_alias_and_launch_version() -> None:
    market: dict[str, Any] = {
        "id": "GMEROBINHOOD-USDT",
        "symbol": "GMEROBINHOOD/USDT:USDT",
        "base": "GMEROBINHOOD",
        "quote": "USDT",
        "settle": "USDT",
        "swap": True,
        "contractSize": 1,
        "info": {
            "displayName": "GME-USDT",
            "launchTime": "1784805000000",
        },
    }

    result = instrument_metadata("bingx", market["symbol"], market)

    assert result == {
        "identity_key": "bingx:swap:GMEROBINHOOD-USDT:1784805000000",
        "market_id": "GMEROBINHOOD-USDT",
        "unified_symbol": "GMEROBINHOOD/USDT:USDT",
        "display_name": "GME-USDT",
        "market_type": "swap",
        "base_asset": "GMEROBINHOOD",
        "quote_asset": "USDT",
        "settle_asset": "USDT",
        "contract_size": 1.0,
        "onboarded_at_ms": 1_784_805_000_000,
    }


def test_lbank_identity_does_not_claim_check_and_checkmate_are_equal() -> None:
    market: dict[str, Any] = {
        "id": "CHECKMATEUSDT",
        "symbol": "CHECKMATE/USDT:USDT",
        "base": "CHECKMATE",
        "quote": "USDT",
        "settle": "USDT",
        "swap": True,
        "contractSize": 1,
        "info": {"symbolName": "CHECKMATEUSDT"},
    }

    result = instrument_metadata("lbank", market["symbol"], market)

    assert result["identity_key"] == "lbank:swap:CHECKMATEUSDT:unknown"
    assert result["base_asset"] == "CHECKMATE"
    assert result["display_name"] == "CHECKMATEUSDT"
    assert "CHECK:" not in result["identity_key"]


def test_identity_version_changes_when_market_is_relisted() -> None:
    market: dict[str, Any] = {
        "id": "TOKEN-USDT",
        "symbol": "TOKEN/USDT:USDT",
        "swap": True,
        "info": {"launchTime": "1784805000000"},
    }
    first = instrument_metadata("bingx", market["symbol"], market)
    market["info"]["launchTime"] = "1784905000000"

    second = instrument_metadata("bingx", market["symbol"], market)

    assert first["identity_key"] != second["identity_key"]


def test_gate_second_timestamp_is_normalized() -> None:
    market: dict[str, Any] = {
        "id": "TOKEN_USDT",
        "symbol": "TOKEN/USDT:USDT",
        "swap": True,
        "info": {"launch_time": "1758124392"},
    }

    result = instrument_metadata("gate", market["symbol"], market)

    assert result["onboarded_at_ms"] == 1_758_124_392_000


def test_htx_calendar_date_is_normalized() -> None:
    market: dict[str, Any] = {
        "id": "TOKEN-USDT",
        "symbol": "TOKEN/USDT:USDT",
        "swap": True,
        "info": {"create_date": "20201021"},
    }

    result = instrument_metadata("htx", market["symbol"], market)

    assert result["onboarded_at_ms"] == 1_603_238_400_000


def test_invalid_or_missing_timestamp_stays_unknown() -> None:
    assert _timestamp_ms(None) is None
    assert _timestamp_ms("") is None
    assert _timestamp_ms("not-a-time") is None
    assert _timestamp_ms("20261340") is None
    assert _timestamp_ms("123") is None
