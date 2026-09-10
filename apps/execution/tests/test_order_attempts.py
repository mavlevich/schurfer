from unittest.mock import AsyncMock, MagicMock, patch

from schurfer_execution import order_attempts


def _mock_conn(*, fetchone_results: list[tuple | None] | None = None) -> tuple:
    """Build a mock psycopg AsyncConnection matching test_journal.py's/
    test_incidents.py's own pattern."""
    cur = AsyncMock()
    cur.execute = AsyncMock()
    cur.rowcount = 1
    if fetchone_results is not None:
        cur.fetchone = AsyncMock(side_effect=fetchone_results)

    cur_cm = MagicMock()
    cur_cm.__aenter__ = AsyncMock(return_value=cur)
    cur_cm.__aexit__ = AsyncMock(return_value=False)

    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur_cm)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    return conn, cur


async def test_create_attempt_returns_new_id() -> None:
    conn, cur = _mock_conn(fetchone_results=[(None,), (0,), (1,)])
    with patch(
        "schurfer_execution.order_attempts.psycopg.AsyncConnection.connect",
        AsyncMock(return_value=conn),
    ):
        attempt_id = await order_attempts.create_attempt(
            "postgresql://test",
            client_order_id="coid-1",
            exchange="bybit",
            base="beat",
            symbol="BEAT/USDT:USDT",
            side="short",
            size_usd=100.0,
            leverage=3,
            contract_size=1.0,
            exit_params={"initial_sl_pct": 10.0},
            setup_context={"pump_pct": 50.0},
            open_positions=[],
            max_positions=5,
        )

    assert attempt_id == 1
    row = cur.execute.call_args_list[-1].args[1]
    assert row[0] == "entry"
    assert row[1] == "coid-1"
    assert row[2] == "bybit"
    assert row[3] == "BEAT"  # base uppercased, matching every other table here


async def test_create_close_attempt_persists_recovery_identity() -> None:
    conn, cur = _mock_conn(fetchone_results=[(9,)])
    with patch(
        "schurfer_execution.order_attempts.psycopg.AsyncConnection.connect",
        AsyncMock(return_value=conn),
    ):
        attempt_id = await order_attempts.create_close_attempt(
            "postgresql://test",
            client_order_id="close-coid",
            exchange="bingx",
            base="beat",
            symbol="BEAT/USDT:USDT",
            native_market_id="BEAT-USDT",
            market_type="swap",
            side="buy",
            size_usd=100.0,
            requested_amount=10.0,
            contract_size=1.0,
            trade_id=42,
            context={"reason": "trailing_stop", "sl_order_id": "sl-1"},
        )

    assert attempt_id == 9
    row = cur.execute.call_args.args[1]
    assert row[:4] == ("close-coid", "bingx", "BEAT", "BEAT/USDT:USDT")
    assert row[-1] == 42


async def test_create_attempt_returns_none_on_db_failure() -> None:
    """The one fail-closed write in this module -- a full DB outage (not
    just one bad query) must return None so the caller (orders.place_order)
    refuses to place the order at all, rather than proceeding as if this
    succeeded (colleague review, P0)."""
    with patch(
        "schurfer_execution.order_attempts.psycopg.AsyncConnection.connect",
        AsyncMock(side_effect=OSError("connection refused")),
    ):
        attempt_id = await order_attempts.create_attempt(
            "postgresql://test",
            client_order_id="coid-1",
            exchange="bybit",
            base="beat",
            symbol="BEAT/USDT:USDT",
            side="short",
            size_usd=100.0,
            leverage=3,
            contract_size=1.0,
            exit_params={"initial_sl_pct": 10.0},
            setup_context={},
            open_positions=[],
            max_positions=5,
        )

    assert attempt_id is None


async def test_create_attempt_denies_capacity_without_inserting() -> None:
    conn, cur = _mock_conn(fetchone_results=[(None,), (1,)])
    with patch(
        "schurfer_execution.order_attempts.psycopg.AsyncConnection.connect",
        AsyncMock(return_value=conn),
    ):
        result = await order_attempts.create_attempt(
            "postgresql://test",
            client_order_id="coid-capacity",
            exchange="bybit",
            base="beat",
            symbol="BEAT/USDT:USDT",
            side="short",
            size_usd=100.0,
            leverage=3,
            contract_size=1.0,
            exit_params={"initial_sl_pct": 10.0},
            setup_context={},
            open_positions=[],
            max_positions=1,
        )

    assert result == order_attempts.PortfolioCapacityReached(
        occupied_slots=1,
        max_positions=1,
    )
    assert len(cur.execute.call_args_list) == 2


async def test_create_attempt_does_not_double_count_observed_durable_slot() -> None:
    conn, cur = _mock_conn(fetchone_results=[(None,), (0,), (2,)])
    with patch(
        "schurfer_execution.order_attempts.psycopg.AsyncConnection.connect",
        AsyncMock(return_value=conn),
    ):
        result = await order_attempts.create_attempt(
            "postgresql://test",
            client_order_id="coid-next",
            exchange="bybit",
            base="next",
            symbol="NEXT/USDT:USDT",
            side="short",
            size_usd=100.0,
            leverage=3,
            contract_size=1.0,
            exit_params={"initial_sl_pct": 10.0},
            setup_context={},
            open_positions=[{"exchange": "bingx", "base": "beat"}],
            max_positions=2,
        )

    assert result == 2
    count_params = cur.execute.call_args_list[1].args[1]
    assert count_params == (["bingx"], ["BEAT"])


async def test_mark_accepted_sets_status_and_order_id() -> None:
    conn, cur = _mock_conn()
    with patch(
        "schurfer_execution.order_attempts.psycopg.AsyncConnection.connect",
        AsyncMock(return_value=conn),
    ):
        await order_attempts.mark_accepted("postgresql://test", 1, order_id="ord-1")

    row = cur.execute.call_args.args[1]
    assert row == (order_attempts.STATUS_ACCEPTED, "ord-1", 1)


async def test_mark_accepted_swallows_db_failure() -> None:
    """Best-effort: the exchange already confirmed the order regardless of
    whether this update lands -- must never raise into the caller."""
    with patch(
        "schurfer_execution.order_attempts.psycopg.AsyncConnection.connect",
        AsyncMock(side_effect=OSError("connection refused")),
    ):
        await order_attempts.mark_accepted("postgresql://test", 1, order_id="ord-1")  # no raise


async def test_mark_completed_sets_trade_id_and_filled_amount() -> None:
    conn, cur = _mock_conn()
    with patch(
        "schurfer_execution.order_attempts.psycopg.AsyncConnection.connect",
        AsyncMock(return_value=conn),
    ):
        await order_attempts.mark_completed("postgresql://test", 1, trade_id=42, filled_amount=99.5)

    row = cur.execute.call_args.args[1]
    assert row == (order_attempts.STATUS_COMPLETED, 42, 99.5, 1)


async def test_mark_completed_accepts_none_trade_id() -> None:
    """The journal write can fail even though the exchange fill succeeded --
    this row still records the attempt as exchange-complete; incidents.py/
    incident_worker.py own retrying the journal write from here."""
    conn, cur = _mock_conn()
    with patch(
        "schurfer_execution.order_attempts.psycopg.AsyncConnection.connect",
        AsyncMock(return_value=conn),
    ):
        await order_attempts.mark_completed("postgresql://test", 1, trade_id=None)

    row = cur.execute.call_args.args[1]
    assert row[1] is None


async def test_mark_partial_keeps_trade_link_and_actual_fill() -> None:
    conn, cur = _mock_conn()
    with patch(
        "schurfer_execution.order_attempts.psycopg.AsyncConnection.connect",
        AsyncMock(return_value=conn),
    ):
        recorded = await order_attempts.mark_partial(
            "postgresql://test", 7, trade_id=42, filled_amount=4.0
        )

    assert recorded is True
    row = cur.execute.call_args.args[1]
    assert row == (order_attempts.STATUS_PARTIAL, 42, 4.0, 7)


async def test_mark_failed_records_error() -> None:
    conn, cur = _mock_conn()
    with patch(
        "schurfer_execution.order_attempts.psycopg.AsyncConnection.connect",
        AsyncMock(return_value=conn),
    ):
        await order_attempts.mark_failed("postgresql://test", 1, error="exchange rejected")

    row = cur.execute.call_args.args[1]
    assert row == (order_attempts.STATUS_FAILED, "exchange rejected", 1)


async def test_mark_failed_truncates_long_errors() -> None:
    conn, cur = _mock_conn()
    with patch(
        "schurfer_execution.order_attempts.psycopg.AsyncConnection.connect",
        AsyncMock(return_value=conn),
    ):
        await order_attempts.mark_failed("postgresql://test", 1, error="x" * 2000)

    row = cur.execute.call_args.args[1]
    assert len(row[1]) == 1000


async def test_link_completed_trade_uses_exact_exchange_order_identity() -> None:
    conn, cur = _mock_conn(fetchone_results=[(5,)])
    with patch(
        "schurfer_execution.order_attempts.psycopg.AsyncConnection.connect",
        AsyncMock(return_value=conn),
    ):
        linked = await order_attempts.link_completed_trade(
            "postgresql://test",
            exchange="bybit",
            order_id="ord-1",
            trade_id=42,
            filled_amount=1.25,
        )

    assert linked
    assert cur.execute.call_args.args[1] == (
        order_attempts.STATUS_COMPLETED,
        42,
        1.25,
        "bybit",
        "ord-1",
    )
