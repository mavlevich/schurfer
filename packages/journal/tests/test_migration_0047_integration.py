"""Real-Postgres invariants for durable partial close legs, migration 0047."""

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
        raise RuntimeError("migration 0047 integration test only permits local PostgreSQL:5432")
    try:
        connection = psycopg.connect(TEST_DATABASE_URL)
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('app.trade_close_fills')")
            row = cursor.fetchone()
        if row is None or row[0] is None:
            connection.close()
            pytest.skip("migration 0047 is not applied")
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


def test_empty_downgrade_and_upgrade_preserve_declared_schema_boundary() -> None:
    connection = _connect_or_skip()
    connection.close()
    config = _alembic_config()

    command.downgrade(config, "0046")
    try:
        with psycopg.connect(TEST_DATABASE_URL) as downgraded, downgraded.cursor() as cursor:
            cursor.execute("SELECT to_regclass('app.trade_close_fills')")
            assert cursor.fetchone() == (None,)
            cursor.execute(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'app'
                  AND table_name = 'live_order_attempts'
                  AND column_name = 'operation'
                """
            )
            assert cursor.fetchone() is None
    finally:
        command.upgrade(config, "head")

    with psycopg.connect(TEST_DATABASE_URL) as upgraded, upgraded.cursor() as cursor:
        cursor.execute("SELECT to_regclass('app.trade_close_fills')")
        assert cursor.fetchone() == ("app.trade_close_fills",)


def test_partial_close_legs_are_idempotent_and_aggregate_exactly() -> None:
    connection = _connect_or_skip()
    suffix = uuid.uuid4().hex[:12]
    strategy_name = f"test_close_fill_{suffix}"
    exchange = f"test0047_{suffix}"
    trade_id: int | None = None
    try:
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conrelid = 'app.live_order_attempts'::regclass
                  AND conname IN (
                      'ck_live_order_attempts_operation',
                      'ck_live_order_attempts_status'
                  )
                ORDER BY conname
                """
            )
            constraints = " ".join(str(row[0]) for row in cursor.fetchall())
            assert "operation" in constraints
            assert "close" in constraints
            assert "partial" in constraints

            cursor.execute(
                """
                INSERT INTO app.strategies (name, version, description)
                VALUES (%s, '0047', 'migration test')
                ON CONFLICT (name, version) DO UPDATE SET updated_at = now()
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
                ) VALUES (%s, 'T47/USDT:USDT', %s, 'perp', 'short',
                          100, 1, 100, %s, 'open', 'legacy_price_only_v1',
                          'legacy', '{}'::jsonb)
                RETURNING id
                """,
                (strategy[0], exchange, datetime.now(UTC)),
            )
            trade = cursor.fetchone()
            assert trade is not None
            trade_id = int(trade[0])

            cursor.execute(
                """
                INSERT INTO app.trade_close_fills (
                    trade_id, exchange, order_id, fill_price, filled_amount,
                    requested_amount, remaining_amount, terminal, fill_source
                ) VALUES
                    (%s, %s, 'partial', 100, 1, 4, 3, false, 'test'),
                    (%s, %s, 'terminal', 110, 3, 3, 0, true, 'test')
                """,
                (trade_id, exchange, trade_id, exchange),
            )
            cursor.execute(
                """
                SELECT SUM(fill_price * filled_amount) / SUM(filled_amount)
                FROM app.trade_close_fills WHERE trade_id = %s
                """,
                (trade_id,),
            )
            aggregate = cursor.fetchone()
            assert aggregate is not None
            assert float(aggregate[0]) == 107.5

            with (
                pytest.raises(psycopg.errors.UniqueViolation),
                connection.transaction(),
                connection.cursor() as conflict_cursor,
            ):
                conflict_cursor.execute(
                    """
                    INSERT INTO app.trade_close_fills (
                        trade_id, exchange, order_id, fill_price, filled_amount,
                        requested_amount, remaining_amount, terminal, fill_source
                    ) VALUES (%s, %s, 'partial', 99, 1, 4, 3, false, 'test')
                    """,
                    (trade_id, exchange),
                )

        with pytest.raises(DBAPIError, match="cannot downgrade 0047"):
            command.downgrade(_alembic_config(), "0046")
    finally:
        if trade_id is not None:
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute("DELETE FROM app.trades WHERE id = %s", (trade_id,))
                cursor.execute(
                    "DELETE FROM app.strategies WHERE name = %s AND version = '0047'",
                    (strategy_name,),
                )
        connection.close()
