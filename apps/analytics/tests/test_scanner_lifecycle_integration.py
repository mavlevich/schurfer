"""Exercise default venue selection through the scanner and real episode closes."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import psycopg
import pytest
import redis.asyncio as aioredis
from schurfer_analytics import main
from schurfer_analytics.exchange_registry import EXCHANGE_FACTORIES
from schurfer_journal.testing_database import integration_database_url


@pytest.mark.parametrize("explicit_coinex", [False, True])
async def test_coinex_outage_and_episode_closure(
    monkeypatch: pytest.MonkeyPatch, explicit_coinex: bool
) -> None:
    db_url = integration_database_url()
    try:
        connection = await psycopg.AsyncConnection.connect(db_url)
    except psycopg.OperationalError as exc:
        pytest.skip(f"no integration postgres reachable: {exc}")

    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("SOURCE_LEAD_CAPTURE_ENABLED", "false")
    monkeypatch.setenv("PUMP_CLOSE_AFTER_MISSES", "3")
    # The deployed Compose stack passes an empty value to use scanner defaults.
    monkeypatch.setenv("PUMP_EXCHANGES", "gate,coinex" if explicit_coinex else "")
    monkeypatch.setattr(aioredis, "from_url", lambda *_args: AsyncMock())
    monkeypatch.setattr(main, "publish", AsyncMock())
    monkeypatch.setattr(main, "take_due_snapshots", AsyncMock())

    clients = {}
    for name in EXCHANGE_FACTORIES:
        client = SimpleNamespace(
            has={"fetchTickers": True},
            markets={},
            fetch_tickers=AsyncMock(return_value={}),
            close=AsyncMock(),
        )
        if name == "coinex":
            client.fetch_tickers.side_effect = RuntimeError("CoinEx HTTP 503")
        clients[name] = client

        def build(client: SimpleNamespace = client) -> SimpleNamespace:
            return client

        monkeypatch.setitem(EXCHANGE_FACTORIES, name, build)

    base = f"TCEX_{uuid4().hex[:12]}"
    async with connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "INSERT INTO app.pump_events "
                "(base, peak_pct, last_pct, exchanges) "
                "VALUES (%s, 25, 10, '[]'::jsonb) RETURNING id",
                (base,),
            )
            row = await cursor.fetchone()
            assert row is not None
            event_id = row[0]
        await connection.commit()
        try:
            for _ in range(3):
                # Real run_once, _fetch, get_tracked_bases and close_retrace.
                await main._run(once=True)
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT closed_at, miss_count FROM app.pump_events WHERE id=%s",
                    (event_id,),
                )
                state = await cursor.fetchone()
            assert state is not None
            if explicit_coinex:
                assert state == (None, 0)
                assert clients["coinex"].fetch_tickers.await_count == 3
            else:
                assert state[0] is not None
                assert state[1] == 3
                clients["coinex"].fetch_tickers.assert_not_awaited()
        finally:
            async with connection.cursor() as cursor:
                await cursor.execute("DELETE FROM app.pump_events WHERE id=%s", (event_id,))
            await connection.commit()
