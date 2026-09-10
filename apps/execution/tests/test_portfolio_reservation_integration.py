"""Real-Postgres concurrency gates for ENG-022 portfolio reservation."""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlsplit

import psycopg
import pytest
from schurfer_execution import order_attempts
from schurfer_execution.orders import place_order
from schurfer_execution.risk import PNL_READY_KEY, TRADING_ENABLED_KEY
from schurfer_execution.supervisor import WorkerReadinessGate

TEST_DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://schurfer:schurfer_dev@localhost:5432/schurfer"
)

_ACTIVE_SLOT_COUNT = """
SELECT count(*)
FROM (
    SELECT DISTINCT a.exchange, upper(a.base)
    FROM app.live_order_attempts AS a
    LEFT JOIN app.trades AS t ON t.id = a.trade_id
    WHERE a.operation = 'entry'
      AND (
          a.status IN ('pending', 'accepted', 'submission_unknown', 'manual_required')
          OR (a.status = 'completed' AND (a.trade_id IS NULL OR t.status = 'open'))
      )
) AS slots
"""


async def _connect_or_skip() -> psycopg.AsyncConnection:
    parsed = urlsplit(TEST_DATABASE_URL)
    if parsed.hostname not in {"localhost", "127.0.0.1"} or parsed.port != 5432:
        raise RuntimeError("portfolio reservation test only permits local PostgreSQL:5432")
    try:
        connection = await psycopg.AsyncConnection.connect(TEST_DATABASE_URL, autocommit=True)
        async with connection.cursor() as cursor:
            await cursor.execute("SELECT to_regclass('app.live_order_attempts')")
            row = await cursor.fetchone()
        if row is None or row[0] is None:
            await connection.close()
            pytest.skip("live_order_attempts is not migrated")
        return connection
    except Exception as exc:
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise RuntimeError(
                f"REQUIRE_INTEGRATION_DB=1 but PostgreSQL/head is unavailable: {exc}"
            ) from exc
        pytest.skip(f"no local postgres/head reachable: {exc}")


async def _active_slot_count(connection: psycopg.AsyncConnection) -> int:
    async with connection.cursor() as cursor:
        await cursor.execute(_ACTIVE_SLOT_COUNT)
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


async def _reserve(
    *, client_order_id: str, base: str, max_positions: int, open_positions: list[dict[str, Any]]
) -> int | order_attempts.PortfolioCapacityReached | None:
    return await order_attempts.create_attempt(
        TEST_DATABASE_URL,
        client_order_id=client_order_id,
        exchange="test_eng022",
        base=base,
        symbol=f"{base}/USDT:USDT",
        native_market_id=f"{base}USDT",
        market_type="swap",
        side="short",
        size_usd=50.0,
        requested_amount=5.0,
        leverage=1,
        contract_size=1.0,
        exit_params={"initial_sl_pct": 10.0},
        setup_context={"test": "portfolio_reservation"},
        open_positions=open_positions,
        max_positions=max_positions,
    )


async def test_concurrent_distinct_entries_share_one_last_portfolio_slot() -> None:
    connection = await _connect_or_skip()
    suffix = uuid.uuid4().hex[:12].upper()
    client_ids = [f"test-eng022-{suffix}-a", f"test-eng022-{suffix}-b"]
    try:
        baseline = await _active_slot_count(connection)
        results = await asyncio.gather(
            _reserve(
                client_order_id=client_ids[0],
                base=f"A{suffix}",
                max_positions=baseline + 1,
                open_positions=[],
            ),
            _reserve(
                client_order_id=client_ids[1],
                base=f"B{suffix}",
                max_positions=baseline + 1,
                open_positions=[],
            ),
        )

        assert sum(isinstance(result, int) for result in results) == 1
        denied = [
            result
            for result in results
            if isinstance(result, order_attempts.PortfolioCapacityReached)
        ]
        assert denied == [
            order_attempts.PortfolioCapacityReached(
                occupied_slots=baseline + 1,
                max_positions=baseline + 1,
            )
        ]
        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT count(*) FROM app.live_order_attempts WHERE client_order_id = ANY(%s)",
                (client_ids,),
            )
            assert await cursor.fetchone() == (1,)
    finally:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "DELETE FROM app.live_order_attempts WHERE client_order_id = ANY(%s)",
                (client_ids,),
            )
        await connection.close()


async def test_observed_position_and_its_attempt_consume_only_one_slot() -> None:
    connection = await _connect_or_skip()
    suffix = uuid.uuid4().hex[:12].upper()
    first_client_id = f"test-eng022-{suffix}-observed"
    second_client_id = f"test-eng022-{suffix}-next"
    client_ids = [first_client_id, second_client_id]
    first_base = f"O{suffix}"
    try:
        baseline = await _active_slot_count(connection)
        first = await _reserve(
            client_order_id=first_client_id,
            base=first_base,
            max_positions=baseline + 1,
            open_positions=[],
        )
        assert isinstance(first, int)

        second = await _reserve(
            client_order_id=second_client_id,
            base=f"N{suffix}",
            max_positions=baseline + 2,
            open_positions=[{"exchange": "test_eng022", "base": first_base}],
        )
        assert isinstance(second, int)
    finally:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "DELETE FROM app.live_order_attempts WHERE client_order_id = ANY(%s)",
                (client_ids,),
            )
        await connection.close()


async def test_concurrent_place_order_calls_submit_only_the_reserved_entry() -> None:
    connection = await _connect_or_skip()
    suffix = uuid.uuid4().hex[:12].upper()
    bases = [f"X{suffix}", f"Y{suffix}"]
    client_prefix = "test-eng022-place-order-"

    exchange = MagicMock()
    exchange.markets = {
        f"{base}/USDT:USDT": {
            "id": f"{base}USDT",
            "type": "swap",
            "contractSize": 1.0,
            "limits": {},
        }
        for base in bases
    }
    exchange.set_leverage = AsyncMock()
    exchange.fetch_ticker = AsyncMock(return_value={"last": 10.0})
    exchange.amount_to_precision = MagicMock(side_effect=lambda _symbol, amount: str(amount))
    exchange.price_to_precision = MagicMock(side_effect=lambda _symbol, price: str(price))
    exchange.create_market_order = AsyncMock(
        return_value={"id": "entry-reserved", "status": "closed", "average": 10.0, "filled": 5.0}
    )
    exchange.create_stop_market_order = AsyncMock(return_value={"id": "stop-reserved"})

    async def _redis_get(key: str) -> bytes | None:
        if key in {TRADING_ENABLED_KEY, PNL_READY_KEY}:
            return b"1"
        return None

    rdb = MagicMock()
    rdb.get = AsyncMock(side_effect=_redis_get)
    rdb.set = AsyncMock(return_value=True)
    rdb.delete = AsyncMock(return_value=1)
    rdb.eval = AsyncMock(return_value=1)
    cfg = MagicMock(db_url=TEST_DATABASE_URL)

    try:
        baseline = await _active_slot_count(connection)
        with (
            patch(
                "schurfer_execution.orders.fetch_positions",
                AsyncMock(return_value=([], set())),
            ),
            patch(
                "schurfer_execution.orders.fetch_margin_balance",
                AsyncMock(
                    return_value=[
                        {
                            "exchange": "test_eng022",
                            "asset": "USDT",
                            "free": 1000.0,
                            "used": 0.0,
                            "total": 1000.0,
                        }
                    ]
                ),
            ),
            patch(
                "schurfer_execution.orders.uuid.uuid4",
                side_effect=[
                    f"{client_prefix}{suffix}-a",
                    f"{client_prefix}{suffix}-b",
                    "stop-client-id",
                ],
            ),
            patch("schurfer_execution.orders.order_attempts.mark_accepted", AsyncMock()),
            patch("schurfer_execution.orders.order_attempts.mark_completed", AsyncMock()),
            patch(
                "schurfer_execution.orders.journal.complete_open",
                AsyncMock(return_value=77),
            ),
        ):
            results = await asyncio.gather(
                *[
                    place_order(
                        base=base,
                        symbol=f"{base}/USDT:USDT",
                        exchange="test_eng022",
                        side="short",
                        size_usd=50.0,
                        leverage=1,
                        exchanges={"test_eng022": exchange},
                        rdb=rdb,
                        max_positions=baseline + 1,
                        max_position_usd=500.0,
                        daily_loss_limit_usd=200.0,
                        cfg=cfg,
                        worker_gate=WorkerReadinessGate(set()),
                    )
                    for base in bases
                ]
            )

        assert sum(bool(result["allowed"]) for result in results) == 1
        assert exchange.create_market_order.await_count == 1
        assert exchange.create_stop_market_order.await_count == 1
    finally:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "DELETE FROM app.live_order_attempts WHERE client_order_id LIKE %s",
                (f"{client_prefix}{suffix}%",),
            )
        await connection.close()
