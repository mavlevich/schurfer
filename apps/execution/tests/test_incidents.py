from unittest.mock import AsyncMock, MagicMock, patch

from schurfer_execution import incidents


def _mock_conn(*, fetchone_results: list[tuple | None] | None = None, rowcount: int = 1) -> tuple:
    """Build a mock psycopg AsyncConnection matching the test_journal.py pattern."""
    cur = AsyncMock()
    cur.execute = AsyncMock()
    if fetchone_results is not None:
        cur.fetchone = AsyncMock(side_effect=fetchone_results)
    cur.rowcount = rowcount

    cur_cm = MagicMock()
    cur_cm.__aenter__ = AsyncMock(return_value=cur)
    cur_cm.__aexit__ = AsyncMock(return_value=False)

    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur_cm)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    return conn, cur


async def test_create_incident_returns_new_id() -> None:
    conn, _cur = _mock_conn(fetchone_results=[(7,)])
    with patch(
        "schurfer_execution.incidents.psycopg.AsyncConnection.connect",
        AsyncMock(return_value=conn),
    ):
        incident_id = await incidents.create_incident(
            "postgresql://test",
            exchange="bybit",
            base="beat",
            operation="open",
            order_id="ord-1",
            trade_id=None,
            context={"leverage": 3},
        )
    assert incident_id == 7


async def test_create_incident_is_idempotent_on_conflict() -> None:
    # INSERT ... ON CONFLICT DO NOTHING RETURNING id yields no row on a retry;
    # the fallback SELECT must return the existing incident, not a new one.
    conn, _cur = _mock_conn(fetchone_results=[None, (7,)])
    with patch(
        "schurfer_execution.incidents.psycopg.AsyncConnection.connect",
        AsyncMock(return_value=conn),
    ):
        incident_id = await incidents.create_incident(
            "postgresql://test",
            exchange="bybit",
            base="beat",
            operation="open",
            order_id="ord-1",
            trade_id=None,
            context={},
        )
    assert incident_id == 7


async def test_create_incident_failure_returns_none_not_a_fabricated_id() -> None:
    with patch(
        "schurfer_execution.incidents.psycopg.AsyncConnection.connect",
        AsyncMock(side_effect=ConnectionError("db down")),
    ):
        incident_id = await incidents.create_incident(
            "postgresql://test",
            exchange="bybit",
            base="beat",
            operation="open",
            order_id="ord-1",
            trade_id=None,
            context={},
        )
    assert incident_id is None


async def test_mark_resolved_reports_whether_a_row_actually_changed() -> None:
    conn, cur = _mock_conn(rowcount=1)
    with patch(
        "schurfer_execution.incidents.psycopg.AsyncConnection.connect",
        AsyncMock(return_value=conn),
    ):
        result = await incidents.mark_resolved(
            "postgresql://test", 7, price=1.23, source="order.average"
        )
    assert result is True
    cur.execute.assert_awaited_once()


async def test_claim_creation_notification_is_exactly_once() -> None:
    conn, cur = _mock_conn(rowcount=1)
    with patch(
        "schurfer_execution.incidents.psycopg.AsyncConnection.connect",
        AsyncMock(return_value=conn),
    ):
        claimed = await incidents.claim_creation_notification("postgresql://test", 7)
    assert claimed is True

    conn2, cur2 = _mock_conn(rowcount=0)
    with patch(
        "schurfer_execution.incidents.psycopg.AsyncConnection.connect",
        AsyncMock(return_value=conn2),
    ):
        claimed_again = await incidents.claim_creation_notification("postgresql://test", 7)
    assert claimed_again is False
    cur.execute.assert_awaited_once()
    cur2.execute.assert_awaited_once()


async def test_any_open_incidents_fails_closed_on_db_error() -> None:
    with patch(
        "schurfer_execution.incidents.psycopg.AsyncConnection.connect",
        AsyncMock(side_effect=ConnectionError("db down")),
    ):
        assert await incidents.any_open_incidents("postgresql://test") is True


async def test_any_open_incidents_reports_clear_when_none_found() -> None:
    conn, _cur = _mock_conn(fetchone_results=[None])
    with patch(
        "schurfer_execution.incidents.psycopg.AsyncConnection.connect",
        AsyncMock(return_value=conn),
    ):
        assert await incidents.any_open_incidents("postgresql://test") is False


async def test_has_pending_open_true_when_row_found() -> None:
    conn, cur = _mock_conn(fetchone_results=[(1,)])
    with patch(
        "schurfer_execution.incidents.psycopg.AsyncConnection.connect",
        AsyncMock(return_value=conn),
    ):
        result = await incidents.has_pending_open(
            "postgresql://test", exchange="bybit", base="beat"
        )
    assert result is True
    _query, params = cur.execute.call_args_list[0].args
    assert params == ("bybit", "BEAT")


async def test_has_pending_open_false_when_none_found() -> None:
    conn, _cur = _mock_conn(fetchone_results=[None])
    with patch(
        "schurfer_execution.incidents.psycopg.AsyncConnection.connect",
        AsyncMock(return_value=conn),
    ):
        result = await incidents.has_pending_open(
            "postgresql://test", exchange="bybit", base="BEAT"
        )
    assert result is False


async def test_has_pending_open_fails_closed_on_db_error() -> None:
    with patch(
        "schurfer_execution.incidents.psycopg.AsyncConnection.connect",
        AsyncMock(side_effect=ConnectionError("db down")),
    ):
        result = await incidents.has_pending_open(
            "postgresql://test", exchange="bybit", base="BEAT"
        )
    assert result is True


async def test_load_open_incidents_maps_rows() -> None:
    conn, _cur = _mock_conn()
    conn.cursor.return_value.__aenter__.return_value.fetchall = AsyncMock(
        return_value=[
            (7, "bybit", "BEAT", "open", "ord-1", None, "pending", 0, {"leverage": 3}),
        ]
    )
    with patch(
        "schurfer_execution.incidents.psycopg.AsyncConnection.connect",
        AsyncMock(return_value=conn),
    ):
        loaded = await incidents.load_open_incidents("postgresql://test")
    assert len(loaded) == 1
    assert loaded[0].id == 7
    assert loaded[0].order_id == "ord-1"
    assert loaded[0].context == {"leverage": 3}
