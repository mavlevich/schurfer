import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from schurfer_analytics.persistence import (
    _UPDATE_LAST_PCT,
    _UPSERT_EVENT_SOURCE,
    _high_24h_pct,
    _source_args,
    _true_peak_pct,
    close_retrace,
    get_open_episode_ids,
    get_tracked_bases,
    insert_oi_snapshots,
    update_last_pct,
    upsert_pumps,
)


def _ex(price: str, change_pct: float, high_24h: str) -> dict[str, Any]:
    return {
        "exchange": "bybit",
        "symbol": "BTCUSDT",
        "price": price,
        "change_pct": change_pct,
        "high_24h": high_24h,
        "volume_24h_usd": 1_000_000.0,
    }


def _pump(base: str, max_change_pct: float) -> dict[str, Any]:
    return {
        "base": base,
        "max_change_pct": max_change_pct,
        "exchanges": [_ex("100.0", max_change_pct, "110.0")],
    }


def _db_mocks() -> tuple[AsyncMock, AsyncMock, AsyncMock]:
    mock_cur = AsyncMock()
    mock_cur.__aenter__.return_value = mock_cur
    mock_conn = AsyncMock()
    mock_conn.__aenter__.return_value = mock_conn
    mock_conn.cursor = MagicMock(return_value=mock_cur)
    mock_connect = AsyncMock(return_value=mock_conn)
    return mock_connect, mock_conn, mock_cur


# --- _high_24h_pct ---


def test_high_24h_pct_normal() -> None:
    # price=100, change_pct=+25% => open=80; high=120 => peak=(120/80-1)*100=50%
    result = _high_24h_pct(_ex("100.0", 25.0, "120.0"))
    assert result == 50.0


def test_high_24h_pct_empty_price() -> None:
    assert _high_24h_pct(_ex("", 25.0, "120.0")) == 0.0


def test_high_24h_pct_empty_high() -> None:
    assert _high_24h_pct(_ex("100.0", 25.0, "")) == 0.0


def test_high_24h_pct_zero_price() -> None:
    assert _high_24h_pct(_ex("0", 25.0, "120.0")) == 0.0


def test_high_24h_pct_change_pct_at_minus_100() -> None:
    # change_pct == -100 would cause division by zero in open reconstruction — must return 0
    assert _high_24h_pct(_ex("100.0", -100.0, "120.0")) == 0.0


def test_high_24h_pct_change_pct_below_minus_100() -> None:
    assert _high_24h_pct(_ex("100.0", -150.0, "120.0")) == 0.0


# --- _true_peak_pct ---


def test_true_peak_pct_prefers_high_24h() -> None:
    # max_change_pct=30 but high_24h implies 50% peak
    pump = {
        "base": "BTC",
        "max_change_pct": 30.0,
        "exchanges": [_ex("100.0", 25.0, "120.0")],  # high_24h_pct=50%
    }
    assert _true_peak_pct(pump) == 50.0


def test_true_peak_pct_prefers_current_when_higher() -> None:
    # max_change_pct=80 is higher than high_24h-derived 50%
    pump = {
        "base": "BTC",
        "max_change_pct": 80.0,
        "exchanges": [_ex("100.0", 25.0, "120.0")],  # high_24h_pct=50%
    }
    assert _true_peak_pct(pump) == 80.0


def test_true_peak_pct_no_exchanges() -> None:
    pump = {"base": "BTC", "max_change_pct": 45.0, "exchanges": []}
    assert _true_peak_pct(pump) == 45.0


def test_true_peak_pct_multi_exchange_takes_max() -> None:
    pump = {
        "base": "BTC",
        "max_change_pct": 30.0,
        "exchanges": [
            _ex("100.0", 25.0, "120.0"),  # 50%
            _ex("100.0", 10.0, "115.0"),  # (115/~90.9-1)*100 ≈ 26.5%
        ],
    }
    assert _true_peak_pct(pump) == 50.0


# --- upsert_pumps ---


def test_upsert_pumps_empty_list_skips_db() -> None:
    with patch("psycopg.AsyncConnection.connect") as mock_connect:
        result = asyncio.run(upsert_pumps("postgresql://test", []))
    mock_connect.assert_not_called()
    assert result == {}


def test_upsert_pumps_updates_existing_episode() -> None:
    mock_connect, _, mock_cur = _db_mocks()
    mock_cur.fetchone = AsyncMock(return_value=(42, 50.0))

    with patch("psycopg.AsyncConnection.connect", mock_connect):
        result = asyncio.run(upsert_pumps("postgresql://test", [_pump("BTC", 55.0)]))

    calls = mock_cur.execute.call_args_list
    assert len(calls) == 3
    # UPDATE args: (last_pct, exchanges_json, peak, event_id) — event_id=42 is last
    update_args = calls[1][0][1]
    assert update_args[-1] == 42
    assert calls[2][0][0] == _UPSERT_EVENT_SOURCE
    assert calls[2][0][1][0:3] == (42, "bybit", "BTCUSDT")
    assert result == {"BTC": 42}


def test_upsert_pumps_inserts_new_episode() -> None:
    mock_connect, _, mock_cur = _db_mocks()
    mock_cur.fetchone = AsyncMock(side_effect=[None, (43,)])

    with patch("psycopg.AsyncConnection.connect", mock_connect):
        result = asyncio.run(upsert_pumps("postgresql://test", [_pump("ETH", 40.0)]))

    calls = mock_cur.execute.call_args_list
    assert len(calls) == 3
    # INSERT args: (base, base, peak, last_pct, exchanges_json)
    insert_args = calls[1][0][1]
    assert insert_args[0] == "ETH"
    assert insert_args[1] == "ETH"
    assert calls[2][0][0] == _UPSERT_EVENT_SOURCE
    assert result == {"ETH": 43}


def test_upsert_pumps_exchanges_serialized_as_json() -> None:
    mock_connect, _, mock_cur = _db_mocks()
    mock_cur.fetchone = AsyncMock(side_effect=[None, (44,)])
    pump = _pump("SOL", 35.0)

    with patch("psycopg.AsyncConnection.connect", mock_connect):
        asyncio.run(upsert_pumps("postgresql://test", [pump]))

    calls = mock_cur.execute.call_args_list
    insert_args = calls[1][0][1]
    exchanges_json = insert_args[4]
    parsed = json.loads(exchanges_json)
    assert isinstance(parsed, list)
    assert parsed[0]["exchange"] == "bybit"


def test_upsert_pumps_failure_returns_no_partial_mapping() -> None:
    mock_connect = AsyncMock(side_effect=RuntimeError("db down"))

    with patch("psycopg.AsyncConnection.connect", mock_connect):
        result = asyncio.run(upsert_pumps("postgresql://test", [_pump("BTC", 55.0)]))

    assert result == {}


def test_upsert_pumps_records_every_exchange_source() -> None:
    mock_connect, _, mock_cur = _db_mocks()
    mock_cur.fetchone = AsyncMock(side_effect=[None, (45,)])
    pump = _pump("BTC", 55.0)
    second = _ex("101.0", 50.0, "111.0")
    second["exchange"] = "xt"
    second["symbol"] = "BTC/USDT:USDT"
    pump["exchanges"].append(second)

    with patch("psycopg.AsyncConnection.connect", mock_connect):
        result = asyncio.run(upsert_pumps("postgresql://test", [pump]))

    source_calls = [
        call for call in mock_cur.execute.call_args_list if call[0][0] == _UPSERT_EVENT_SOURCE
    ]
    assert [call[0][1][1] for call in source_calls] == ["bybit", "xt"]
    assert result == {"BTC": 45}


def test_source_args_normalizes_optional_numeric_fields() -> None:
    exchange = _ex("", 40.0, "110.0")
    exchange["volume_24h_usd"] = float("inf")

    args = _source_args(7, exchange)

    assert args[0:3] == (7, "bybit", "BTCUSDT")
    assert args[3:15] == (None,) * 12
    assert args[15:18] == (40.0, 40.0, 40.0)
    assert args[18:] == (None, None, None, None)


def test_source_args_preserves_instrument_identity_and_timestamps() -> None:
    exchange = _ex("100", 40.0, "110")
    exchange.update(
        {
            "identity_key": "bingx:swap:GMEROBINHOOD-USDT:1784805000000",
            "market_id": "GMEROBINHOOD-USDT",
            "unified_symbol": "GMEROBINHOOD/USDT:USDT",
            "display_name": "GME-USDT",
            "market_type": "swap",
            "base_asset": "GMEROBINHOOD",
            "quote_asset": "USDT",
            "settle_asset": "USDT",
            "contract_size": 1,
            "onboarded_at_ms": 1_784_805_000_000,
            "ticker_timestamp_ms": 1_784_806_000_000,
        }
    )

    args = _source_args(7, exchange)

    assert args[3:12] == (
        "bingx:swap:GMEROBINHOOD-USDT:1784805000000",
        "GMEROBINHOOD-USDT",
        "GMEROBINHOOD/USDT:USDT",
        "GME-USDT",
        "swap",
        "GMEROBINHOOD",
        "USDT",
        "USDT",
        1.0,
    )
    assert args[12].isoformat() == "2026-07-23T11:10:00+00:00"
    assert args[13].isoformat() == "2026-07-23T11:26:40+00:00"
    assert args[14] == args[13]
    assert _UPSERT_EVENT_SOURCE.count("%s") == len(args)
    assert "identity_conflict" in _UPSERT_EVENT_SOURCE
    assert "market_id <> EXCLUDED.market_id" in _UPSERT_EVENT_SOURCE
    assert "market_type <> EXCLUDED.market_type" in _UPSERT_EVENT_SOURCE
    assert "onboarded_at <> EXCLUDED.onboarded_at" in _UPSERT_EVENT_SOURCE


def test_source_upsert_enriches_unknown_listing_time_without_conflict() -> None:
    assert "app.pump_event_sources.onboarded_at IS NULL" in _UPSERT_EVENT_SOURCE
    assert "EXCLUDED.onboarded_at IS NOT NULL" in _UPSERT_EVENT_SOURCE
    assert "THEN EXCLUDED.identity_key" in _UPSERT_EVENT_SOURCE


def test_source_upsert_does_not_erase_last_ticker_time() -> None:
    assert (
        "COALESCE(\n"
        "        EXCLUDED.last_ticker_at,\n"
        "        app.pump_event_sources.last_ticker_at\n"
        "    )"
    ) in _UPSERT_EVENT_SOURCE


def test_upsert_pumps_rejects_non_finite_source_change_atomically() -> None:
    mock_connect, _, mock_cur = _db_mocks()
    mock_cur.fetchone = AsyncMock(side_effect=[None, (46,)])
    pump = _pump("BTC", 55.0)
    pump["exchanges"][0]["change_pct"] = float("nan")

    with patch("psycopg.AsyncConnection.connect", mock_connect):
        result = asyncio.run(upsert_pumps("postgresql://test", [pump]))

    assert result == {}
    assert all(call[0][0] != _UPSERT_EVENT_SOURCE for call in mock_cur.execute.call_args_list)


# --- close_retrace ---
# New logic: absent tokens get miss_count incremented; episodes close after N misses.
# fetchall is called twice: once for SELECT_OPEN_ALL, once for CLOSE_DUE RETURNING.


def _close_retrace_mocks(
    open_events: list[tuple[int, str, float, float]],
    closed_events: list[tuple[str, int]] | None = None,
) -> tuple[AsyncMock, AsyncMock]:
    mock_connect, _, mock_cur = _db_mocks()
    mock_cur.fetchall = AsyncMock(side_effect=[open_events, closed_events or []])
    return mock_connect, mock_cur


def test_close_retrace_increments_miss_for_absent() -> None:
    mock_connect, mock_cur = _close_retrace_mocks(
        open_events=[(1, "BTC", 50.0, 80.0), (2, "ETH", 30.0, 60.0)],
    )
    with patch("psycopg.AsyncConnection.connect", mock_connect):
        asyncio.run(close_retrace("postgresql://test", {"BTC"}))

    # SELECT_OPEN_ALL + 1 INCREMENT_MISS (ETH only) + CLOSE_DUE = 3 execute calls
    assert mock_cur.execute.call_count == 3


def test_close_retrace_skips_increment_for_live_tokens() -> None:
    mock_connect, mock_cur = _close_retrace_mocks(
        open_events=[(1, "BTC", 50.0, 80.0)],
    )
    with patch("psycopg.AsyncConnection.connect", mock_connect):
        asyncio.run(close_retrace("postgresql://test", {"BTC"}))

    # SELECT_OPEN_ALL + CLOSE_DUE only — no INCREMENT_MISS since BTC is live
    assert mock_cur.execute.call_count == 2


def test_close_retrace_close_due_uses_threshold() -> None:
    mock_connect, mock_cur = _close_retrace_mocks(open_events=[])
    with patch("psycopg.AsyncConnection.connect", mock_connect):
        asyncio.run(close_retrace("postgresql://test", set(), close_after_misses=5))

    # Last execute call is CLOSE_DUE with threshold=5
    close_due_call = mock_cur.execute.call_args_list[-1]
    assert close_due_call[0][1] == (5,)


def test_close_retrace_empty_live_increments_all() -> None:
    mock_connect, mock_cur = _close_retrace_mocks(
        open_events=[(1, "BTC", 50.0, 80.0), (2, "ETH", 30.0, 60.0)],
    )
    with patch("psycopg.AsyncConnection.connect", mock_connect):
        asyncio.run(close_retrace("postgresql://test", set()))

    # SELECT_OPEN_ALL + 2 INCREMENT_MISS + CLOSE_DUE = 4
    assert mock_cur.execute.call_count == 4


# --- insert_oi_snapshots ---


def test_insert_oi_snapshots_empty_list_skips_db() -> None:
    with patch("psycopg.AsyncConnection.connect") as mock_connect:
        asyncio.run(insert_oi_snapshots("postgresql://test", []))
    mock_connect.assert_not_called()


def test_insert_oi_snapshots_executemany_rows() -> None:
    mock_connect, _, mock_cur = _db_mocks()
    snapshots = [
        {"event_id": 42, "base": "BTC", "exchange": "okx", "oi_usd": 2_000_000.0},
        {"event_id": 7, "base": "ETH", "exchange": "binance", "oi_usd": 1_000_000.0},
    ]

    with patch("psycopg.AsyncConnection.connect", mock_connect):
        asyncio.run(insert_oi_snapshots("postgresql://test", snapshots))

    mock_cur.executemany.assert_awaited_once()
    rows = mock_cur.executemany.call_args[0][1]
    assert rows == [(42, "BTC", "okx", 2_000_000.0), (7, "ETH", "binance", 1_000_000.0)]


def test_insert_oi_snapshots_drops_rows_without_event_id() -> None:
    mock_connect, _, mock_cur = _db_mocks()
    snapshots: list[dict[str, Any]] = [
        {"event_id": None, "base": "BTC", "exchange": "okx", "oi_usd": 2_000_000.0},
        {"event_id": 7, "base": "ETH", "exchange": "binance", "oi_usd": 1_000_000.0},
    ]

    with patch("psycopg.AsyncConnection.connect", mock_connect):
        asyncio.run(insert_oi_snapshots("postgresql://test", snapshots))

    rows = mock_cur.executemany.call_args[0][1]
    assert rows == [(7, "ETH", "binance", 1_000_000.0)]


def test_insert_oi_snapshots_all_missing_event_id_skips_db() -> None:
    snapshots: list[dict[str, Any]] = [
        {"event_id": None, "base": "BTC", "exchange": "okx", "oi_usd": 1.0}
    ]
    with patch("psycopg.AsyncConnection.connect") as mock_connect:
        asyncio.run(insert_oi_snapshots("postgresql://test", snapshots))
    mock_connect.assert_not_called()


# --- get_open_episode_ids ---


def test_get_open_episode_ids_empty_bases_skips_db() -> None:
    with patch("psycopg.AsyncConnection.connect") as mock_connect:
        result = asyncio.run(get_open_episode_ids("postgresql://test", set()))
    mock_connect.assert_not_called()
    assert result == {}


def test_get_open_episode_ids_maps_base_to_id() -> None:
    mock_connect, _, mock_cur = _db_mocks()
    mock_cur.fetchall = AsyncMock(return_value=[("BTC", 42), ("ETH", 7)])

    with patch("psycopg.AsyncConnection.connect", mock_connect):
        result = asyncio.run(get_open_episode_ids("postgresql://test", {"BTC", "ETH"}))

    assert result == {"BTC": 42, "ETH": 7}


# --- get_tracked_bases / update_last_pct must not touch closed episodes ---
# Closed episodes keep retrace_pct frozen from the moment they closed; letting
# later scans keep mutating last_pct on them would silently corrupt that
# historical record and waste OI requests on tokens that are no longer live.


def test_update_last_pct_query_excludes_closed_episodes() -> None:
    assert "closed_at IS NULL" in _UPDATE_LAST_PCT


def test_get_tracked_bases_query_excludes_closed_episodes() -> None:
    mock_connect, _, mock_cur = _db_mocks()
    mock_cur.fetchall = AsyncMock(return_value=[("BTC",), ("ETH",)])

    with patch("psycopg.AsyncConnection.connect", mock_connect):
        result = asyncio.run(get_tracked_bases("postgresql://test"))

    assert result == frozenset({"BTC", "ETH"})
    query = mock_cur.execute.call_args[0][0]
    assert "closed_at IS NULL" in query


def test_get_tracked_bases_db_error_returns_empty() -> None:
    with patch("psycopg.AsyncConnection.connect", side_effect=RuntimeError("boom")):
        result = asyncio.run(get_tracked_bases("postgresql://test"))
    assert result == frozenset()


def test_update_last_pct_empty_updates_skips_db() -> None:
    with patch("psycopg.AsyncConnection.connect") as mock_connect:
        asyncio.run(update_last_pct("postgresql://test", {}))
    mock_connect.assert_not_called()


def test_update_last_pct_executemany_rows() -> None:
    mock_connect, _, mock_cur = _db_mocks()

    with patch("psycopg.AsyncConnection.connect", mock_connect):
        asyncio.run(update_last_pct("postgresql://test", {"DOGE": 22.0}))

    mock_cur.executemany.assert_awaited_once()
    rows = mock_cur.executemany.call_args[0][1]
    assert rows == [(22.0, "DOGE")]


def test_close_retrace_empty_open_events() -> None:
    mock_connect, mock_cur = _close_retrace_mocks(open_events=[])
    with patch("psycopg.AsyncConnection.connect", mock_connect):
        asyncio.run(close_retrace("postgresql://test", {"BTC"}))

    # SELECT_OPEN_ALL + CLOSE_DUE (no increments)
    assert mock_cur.execute.call_count == 2
