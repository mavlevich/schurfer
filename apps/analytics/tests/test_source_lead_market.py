from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

from schurfer_analytics.ohlcv import Candle
from schurfer_analytics.source_lead import SourceLeadCandidate
from schurfer_analytics.source_lead_market import fetch_source_lead_paths


def _candidate(event_id: int, exchange: str, symbol: str) -> SourceLeadCandidate:
    source_at = datetime(2026, 7, 24, 12, tzinfo=UTC)
    return SourceLeadCandidate(
        candidate_id=f"{event_id}:mexc:{exchange}",
        event_id=event_id,
        cluster_key=f"base:E{event_id}",
        base=f"E{event_id}",
        source_exchange="mexc",
        execution_exchange=exchange,
        exact_symbol=symbol,
        source_at=source_at,
        confirmation_at=source_at + timedelta(minutes=5),
        confirmation_lag_seconds=300,
        source_change_pct=20,
        source_volume_24h_usd=1_000_000,
    )


async def test_source_lead_paths_use_exact_symbol_and_preserve_input_order() -> None:
    clients: list[AsyncMock] = []

    def factory() -> AsyncMock:
        client = AsyncMock()
        clients.append(client)
        return client

    candles = [Candle(1, 1, 1, 1, 1, 1)]
    candidates = (
        _candidate(42, "bybit", "1000EDGE/USDT:USDT"),
        _candidate(43, "binance", "EDGE/USDT:USDT"),
    )
    with patch(
        "schurfer_analytics.source_lead_market.fetch_symbol_candles",
        AsyncMock(return_value=candles),
    ) as fetch:
        paths = await fetch_source_lead_paths(
            candidates,
            {"binance": factory, "bybit": factory},
        )

    assert [row.event_id for row in paths] == [42, 43]
    assert {call.args[1] for call in fetch.await_args_list} == {
        "1000EDGE/USDT:USDT",
        "EDGE/USDT:USDT",
    }
    assert len(clients) == 2
    for client in clients:
        client.close.assert_awaited_once_with(clean_instance_data=True)


async def test_source_lead_paths_do_not_open_all_exchange_clients_together() -> None:
    active = 0
    maximum = 0

    def factory() -> AsyncMock:
        nonlocal active, maximum
        client = AsyncMock()
        active += 1
        maximum = max(maximum, active)

        async def close(*, clean_instance_data: bool = False) -> None:
            nonlocal active
            assert clean_instance_data is True
            active -= 1

        client.close.side_effect = close
        return client

    with patch(
        "schurfer_analytics.source_lead_market.fetch_symbol_candles",
        AsyncMock(return_value=[]),
    ):
        await fetch_source_lead_paths(
            (
                _candidate(42, "bybit", "EDGE/USDT:USDT"),
                _candidate(43, "binance", "EDGE/USDT:USDT"),
            ),
            {"binance": Mock(side_effect=factory), "bybit": Mock(side_effect=factory)},
        )

    assert maximum == 1
    assert active == 0
