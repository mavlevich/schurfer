import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from schurfer_analytics.persistence import (
    _high_24h_pct,
    _true_peak_pct,
    close_retrace,
    upsert_pumps,
)


def _ex(price: str, change_pct: float, high_24h: str) -> dict[str, object]:
    return {
        "exchange": "bybit",
        "symbol": "BTCUSDT",
        "price": price,
        "change_pct": change_pct,
        "high_24h": high_24h,
        "volume_24h_usd": 1_000_000.0,
    }


def _pump(base: str, max_change_pct: float) -> dict[str, object]:
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
        asyncio.run(upsert_pumps("postgresql://test", []))
    mock_connect.assert_not_called()


def test_upsert_pumps_updates_existing_episode() -> None:
    mock_connect, _, mock_cur = _db_mocks()
    mock_cur.fetchone = AsyncMock(return_value=(42, 50.0))

    with patch("psycopg.AsyncConnection.connect", mock_connect):
        asyncio.run(upsert_pumps("postgresql://test", [_pump("BTC", 55.0)]))

    calls = mock_cur.execute.call_args_list
    assert len(calls) == 2
    # UPDATE args: (last_pct, exchanges_json, peak, event_id) — event_id=42 is last
    update_args = calls[1][0][1]
    assert update_args[-1] == 42


def test_upsert_pumps_inserts_new_episode() -> None:
    mock_connect, _, mock_cur = _db_mocks()
    mock_cur.fetchone = AsyncMock(return_value=None)

    with patch("psycopg.AsyncConnection.connect", mock_connect):
        asyncio.run(upsert_pumps("postgresql://test", [_pump("ETH", 40.0)]))

    calls = mock_cur.execute.call_args_list
    assert len(calls) == 2
    # INSERT args: (base, base, peak, last_pct, exchanges_json)
    insert_args = calls[1][0][1]
    assert insert_args[0] == "ETH"
    assert insert_args[1] == "ETH"


def test_upsert_pumps_exchanges_serialized_as_json() -> None:
    mock_connect, _, mock_cur = _db_mocks()
    mock_cur.fetchone = AsyncMock(return_value=None)
    pump = _pump("SOL", 35.0)

    with patch("psycopg.AsyncConnection.connect", mock_connect):
        asyncio.run(upsert_pumps("postgresql://test", [pump]))

    calls = mock_cur.execute.call_args_list
    insert_args = calls[1][0][1]
    exchanges_json = insert_args[4]
    parsed = json.loads(exchanges_json)
    assert isinstance(parsed, list)
    assert parsed[0]["exchange"] == "bybit"


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


def test_close_retrace_empty_open_events() -> None:
    mock_connect, mock_cur = _close_retrace_mocks(open_events=[])
    with patch("psycopg.AsyncConnection.connect", mock_connect):
        asyncio.run(close_retrace("postgresql://test", {"BTC"}))

    # SELECT_OPEN_ALL + CLOSE_DUE (no increments)
    assert mock_cur.execute.call_count == 2
