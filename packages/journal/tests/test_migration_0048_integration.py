"""Real-Postgres invariants for close execution time, migration 0048."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import DBAPIError

TEST_DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://schurfer:schurfer_dev@localhost:5432/schurfer"
)
ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def _connect_or_skip() -> psycopg.Connection:
    parsed = urlsplit(TEST_DATABASE_URL)
    if parsed.hostname not in {"localhost", "127.0.0.1"} or parsed.port != 5432:
        raise RuntimeError("migration 0048 integration test only permits local PostgreSQL:5432")
    try:
        connection = psycopg.connect(TEST_DATABASE_URL)
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*)
                FROM information_schema.columns
                WHERE table_schema = 'app'
                  AND table_name = 'trade_close_fills'
                  AND column_name IN ('executed_at', 'execution_time_source')
                """
            )
            row = cursor.fetchone()
        if row is None or row[0] != 2:
            connection.close()
            pytest.skip("migration 0048 is not applied")
        return connection
    except Exception as exc:
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise RuntimeError(
                f"REQUIRE_INTEGRATION_DB=1 but PostgreSQL/head is unavailable: {exc}"
            ) from exc
        pytest.skip(f"no local postgres/head reachable: {exc}")


def _alembic_config() -> Config:
    config = Config(str(ALEMBIC_INI))
    url = TEST_DATABASE_URL
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg://" + url[len(prefix) :]
            break
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_execution_time_is_distinct_from_recorded_time_and_blocks_lossy_downgrade() -> None:
    connection = _connect_or_skip()
    suffix = uuid.uuid4().hex[:12]
    strategy_name = f"test_close_time_{suffix}"
    exchange = f"test0048_{suffix}"
    trade_id: int | None = None
    try:
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO app.strategies (name, version, description)
                VALUES (%s, '0048', 'migration test')
                RETURNING id
                """,
                (strategy_name,),
            )
            strategy = cursor.fetchone()
            assert strategy is not None
            cursor.execute(
                """
                INSERT INTO app.trades (
                    strategy_id, symbol, exchange, market_type, side,
                    size_usd, leverage, entry_price, entry_at, status,
                    accounting_version, accounting_status, setup_context
                ) VALUES (%s, 'T48/USDT:USDT', %s, 'perp', 'short',
                          100, 1, 100, %s, 'open', 'legacy_price_only_v1',
                          'legacy', '{}'::jsonb)
                RETURNING id
                """,
                (strategy[0], exchange, datetime.now(UTC)),
            )
            trade = cursor.fetchone()
            assert trade is not None
            trade_id = int(trade[0])
            executed_at = datetime(2026, 9, 9, 23, 58, tzinfo=UTC)
            cursor.execute(
                """
                INSERT INTO app.trade_close_fills (
                    trade_id, exchange, order_id, fill_price, filled_amount,
                    requested_amount, remaining_amount, terminal, fill_source,
                    executed_at, execution_time_source
                ) VALUES (%s, %s, 'terminal', 110, 1, 1, 0, true, 'test', %s,
                          'exchange.lastTradeTimestamp')
                RETURNING executed_at, created_at, execution_time_source
                """,
                (trade_id, exchange, executed_at),
            )
            evidence = cursor.fetchone()
            assert evidence is not None
            assert evidence[0] == executed_at
            assert evidence[1] != executed_at
            assert evidence[2] == "exchange.lastTradeTimestamp"

        with pytest.raises(DBAPIError, match="cannot downgrade 0048"):
            command.downgrade(_alembic_config(), "0047")
    finally:
        if trade_id is not None:
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute("DELETE FROM app.trades WHERE id = %s", (trade_id,))
                cursor.execute(
                    "DELETE FROM app.strategies WHERE name = %s AND version = '0048'",
                    (strategy_name,),
                )
        connection.close()
