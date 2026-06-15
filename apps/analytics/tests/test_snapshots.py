import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from schurfer_analytics.snapshots import _extract_price, take_due_snapshots


def _db_mocks() -> tuple[AsyncMock, AsyncMock, AsyncMock]:
    mock_cur = AsyncMock()
    mock_cur.__aenter__.return_value = mock_cur
    mock_conn = AsyncMock()
    mock_conn.__aenter__.return_value = mock_conn
    mock_conn.cursor = MagicMock(return_value=mock_cur)
    mock_connect = AsyncMock(return_value=mock_conn)
    return mock_connect, mock_conn, mock_cur


def _exchange(price: str, change_pct: float) -> dict[str, object]:
    return {
        "exchange": "bybit",
        "symbol": "BTCUSDT",
        "price": price,
        "change_pct": change_pct,
        "high_24h": price,
        "volume_24h_usd": 1_000_000.0,
    }


# --- _extract_price ---


def test_extract_price_returns_first_valid() -> None:
    exchanges = [_exchange("50000.0", 45.0)]
    price, change_pct = _extract_price(exchanges)
    assert price == 50000.0
    assert change_pct == 45.0


def test_extract_price_skips_zero_price() -> None:
    exchanges = [_exchange("0", 10.0), _exchange("200.0", 20.0)]
    price, change_pct = _extract_price(exchanges)
    assert price == 200.0
    assert change_pct == 20.0


def test_extract_price_empty_list() -> None:
    assert _extract_price([]) == (None, None)


def test_extract_price_invalid_price_string() -> None:
    exchanges = [_exchange("not_a_number", 10.0)]
    assert _extract_price(exchanges) == (None, None)


def test_extract_price_missing_key() -> None:
    assert _extract_price([{"exchange": "bybit"}]) == (None, None)


# --- take_due_snapshots ---


def test_take_due_snapshots_no_due_events() -> None:
    mock_connect, _, mock_cur = _db_mocks()
    # All three offsets return no due events
    mock_cur.fetchall = AsyncMock(return_value=[])

    with patch("psycopg.AsyncConnection.connect", mock_connect):
        asyncio.run(take_due_snapshots("postgresql://test"))

    # Only 3 SELECT_DUE calls (one per offset), no inserts
    assert mock_cur.execute.call_count == 3


def test_take_due_snapshots_inserts_for_due_events() -> None:
    mock_connect, _, mock_cur = _db_mocks()
    exchanges = [_exchange("50000.0", 45.0)]
    # +1h has one due event; +4h and +24h have none
    mock_cur.fetchall = AsyncMock(
        side_effect=[
            [(1, "BTC", exchanges)],  # +1h due
            [],  # +4h not yet
            [],  # +24h not yet
        ]
    )

    with patch("psycopg.AsyncConnection.connect", mock_connect):
        asyncio.run(take_due_snapshots("postgresql://test"))

    # 3 SELECTs + 1 INSERT
    assert mock_cur.execute.call_count == 4


def test_take_due_snapshots_insert_args() -> None:
    mock_connect, _, mock_cur = _db_mocks()
    exchanges = [_exchange("50000.0", 45.0)]
    mock_cur.fetchall = AsyncMock(
        side_effect=[
            [(7, "BTC", exchanges)],
            [],
            [],
        ]
    )

    with patch("psycopg.AsyncConnection.connect", mock_connect):
        asyncio.run(take_due_snapshots("postgresql://test"))

    insert_call = mock_cur.execute.call_args_list[1]
    args = insert_call[0][1]
    event_id, label, price, change_pct, exchanges_json = args
    assert event_id == 7
    assert label == "+1h"
    assert price == 50000.0
    assert change_pct == 45.0
    parsed = json.loads(exchanges_json)
    assert parsed[0]["exchange"] == "bybit"


def test_take_due_snapshots_inserts_null_price_when_unresolvable() -> None:
    mock_connect, _, mock_cur = _db_mocks()
    # Exchanges with invalid price
    mock_cur.fetchall = AsyncMock(
        side_effect=[
            [(3, "XYZ", [{"exchange": "bybit"}])],
            [],
            [],
        ]
    )

    with patch("psycopg.AsyncConnection.connect", mock_connect):
        asyncio.run(take_due_snapshots("postgresql://test"))

    insert_call = mock_cur.execute.call_args_list[1]
    args = insert_call[0][1]
    _, _, price, change_pct, _ = args
    assert price is None
    assert change_pct is None
