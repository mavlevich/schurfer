"""Real-Postgres invariants for migration 0062 (trade cost legs and the generation).

Every change to an input of the PnL tracker moves ``app.pnl_generation`` inside the
writing transaction: a non-paper trade closed, its PnL or accounting changed, a close
fill, a fill incident opened or resolved. An open trade inserted, a paper trade, or an
update of columns the tracker does not read leaves it alone, so entries are not
refused for nothing. An execution is attributed once only.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import psycopg
import pytest
from schurfer_journal.testing_database import (
    assert_active_test_database_url,
    integration_database_url,
)

TEST_DATABASE_URL = integration_database_url()
assert_active_test_database_url(TEST_DATABASE_URL)


def _connect_or_skip() -> psycopg.Connection[Any]:
    try:
        conn = psycopg.connect(TEST_DATABASE_URL, autocommit=True)
    except psycopg.OperationalError as exc:
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise RuntimeError(
                f"REQUIRE_INTEGRATION_DB=1 but Postgres is unavailable: {exc}"
            ) from exc
        pytest.skip(f"no local postgres reachable: {exc}")
    if conn.execute("SELECT to_regclass('app.pnl_generation')").fetchone() == (None,):
        conn.close()
        pytest.skip("migration 0062 is not applied")
    return conn


@pytest.fixture
def conn() -> Any:
    connection = _connect_or_skip()
    exchange = f"m0062-{uuid.uuid4().hex[:10]}"
    yield connection, exchange
    connection.execute("DELETE FROM app.trade_cost_legs WHERE exchange = %s", (exchange,))
    connection.execute("DELETE FROM app.fill_resolution_incidents WHERE exchange = %s", (exchange,))
    connection.execute("DELETE FROM app.trade_close_fills WHERE exchange = %s", (exchange,))
    connection.execute("DELETE FROM app.trades WHERE exchange = %s", (exchange,))
    connection.close()


def _generation(connection: psycopg.Connection[Any]) -> int:
    row = connection.execute("SELECT value FROM app.pnl_generation WHERE id = 1").fetchone()
    assert row is not None
    return int(row[0])


def _strategy(connection: psycopg.Connection[Any]) -> int:
    row = connection.execute(
        "INSERT INTO app.strategies (name, version, description) VALUES ('m0062', '1', 't') "
        "ON CONFLICT (name, version) DO UPDATE SET updated_at = now() RETURNING id"
    ).fetchone()
    assert row is not None
    return int(row[0])


def _trade(
    connection: psycopg.Connection[Any], exchange: str, *, paper: bool, status: str = "open"
) -> int:
    row = connection.execute(
        "INSERT INTO app.trades (strategy_id, symbol, exchange, market_type, side, size_usd, "
        "entry_price, entry_at, status, setup_context, pnl_usd, exit_at) "
        "VALUES (%s, 'BTC/USDT:USDT', %s, 'perp', 'long', 100, 1, now(), %s, %s::jsonb, "
        "CASE WHEN %s = 'closed' THEN 1 END, CASE WHEN %s = 'closed' THEN now() END) "
        "RETURNING id",
        (_strategy(connection), exchange, status, json.dumps({"paper": paper}), status, status),
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_live_trade_changes_move_the_generation_and_others_do_not(conn: Any) -> None:
    connection, exchange = conn

    before = _generation(connection)
    trade_id = _trade(connection, exchange, paper=False)
    assert _generation(connection) == before, "an open trade changes no tracker input"

    connection.execute("UPDATE app.trades SET notes = 'excursion' WHERE id = %s", (trade_id,))
    assert _generation(connection) == before, "a column the tracker does not read"

    connection.execute(
        "UPDATE app.trades SET status = 'closed', exit_at = now(), pnl_usd = 1, "
        "accounting_status = 'pending' WHERE id = %s",
        (trade_id,),
    )
    closed = _generation(connection)
    assert closed == before + 1, "one statement, one increment"

    connection.execute(
        "UPDATE app.trades SET accounting_status = 'complete', pnl_usd = 0.9 WHERE id = %s",
        (trade_id,),
    )
    assert _generation(connection) == closed + 1

    _trade(connection, exchange, paper=False, status="closed")
    assert _generation(connection) == closed + 2, "a trade inserted closed"

    paper_id = _trade(connection, exchange, paper=True)
    connection.execute(
        "UPDATE app.trades SET status = 'closed', exit_at = now(), pnl_usd = 5 WHERE id = %s",
        (paper_id,),
    )
    assert _generation(connection) == closed + 2, "paper trades are not real PnL"


def test_close_fills_and_incident_status_move_the_generation(conn: Any) -> None:
    connection, exchange = conn
    trade_id = _trade(connection, exchange, paper=False)

    before = _generation(connection)
    connection.execute(
        "INSERT INTO app.trade_close_fills (trade_id, exchange, order_id, fill_price, "
        "filled_amount, requested_amount, remaining_amount, terminal, fill_source) "
        "VALUES (%s, %s, 'o-1', 1, 1, 2, 1, false, 'test')",
        (trade_id, exchange),
    )
    assert _generation(connection) == before + 1

    row = connection.execute(
        "INSERT INTO app.fill_resolution_incidents (exchange, base, operation, order_id, "
        "status, attempt_count, context) VALUES (%s, 'BTC', 'close', 'o-2', 'pending', 0, "
        "'{}'::jsonb) RETURNING id",
        (exchange,),
    ).fetchone()
    assert row is not None
    assert _generation(connection) == before + 2

    connection.execute(
        "UPDATE app.fill_resolution_incidents SET attempt_count = 3 WHERE id = %s", (row[0],)
    )
    assert _generation(connection) == before + 2, "retries do not change the outcome"

    connection.execute(
        "UPDATE app.fill_resolution_incidents SET status = 'resolving' WHERE id = %s", (row[0],)
    )
    assert _generation(connection) == before + 3


def test_the_generation_moves_only_when_the_writer_commits(conn: Any) -> None:
    """A tick reading during an uncommitted close sees the old number, and the
    close's commit moves it: the counter is transactional, unlike a sequence."""
    connection, exchange = conn
    trade_id = _trade(connection, exchange, paper=False)
    before = _generation(connection)

    with psycopg.connect(TEST_DATABASE_URL) as writer:
        writer.execute(
            "UPDATE app.trades SET status = 'closed', exit_at = now(), pnl_usd = 1 WHERE id = %s",
            (trade_id,),
        )
        assert _generation(connection) == before
        writer.rollback()
    assert _generation(connection) == before


def test_an_execution_is_attributed_once(conn: Any) -> None:
    connection, exchange = conn
    first = _trade(connection, exchange, paper=False, status="closed")
    second = _trade(connection, exchange, paper=False, status="closed")
    insert = (
        "INSERT INTO app.trade_cost_legs (trade_id, exchange, exec_id, exec_type, quantity, "
        "fee_usd, executed_at, raw) VALUES (%s, %s, 'exec-1', 'trade', 1, 0.01, now(), "
        "'{}'::jsonb)"
    )
    connection.execute(insert, (first, exchange))
    with pytest.raises(psycopg.errors.UniqueViolation):
        connection.execute(insert, (second, exchange))


def test_one_generation_row_only() -> None:
    connection = _connect_or_skip()
    try:
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute("INSERT INTO app.pnl_generation (id, value) VALUES (2, 0)")
    finally:
        connection.close()
