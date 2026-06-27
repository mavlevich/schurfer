import asyncio
import contextlib
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from schurfer_execution.decisions import _queue, run_decision_writer, write_decision


def _drain() -> None:
    while not _queue.empty():
        _queue.get_nowait()
        _queue.task_done()


def test_write_decision_no_op_when_no_db_url() -> None:
    _drain()
    write_decision(None, base="BEAT", exchange="bybit", action="skipped", reason="test")
    assert _queue.qsize() == 0


def test_write_decision_enqueues_row() -> None:
    _drain()
    write_decision(
        "postgresql://test",
        base="BEAT",
        exchange="bybit",
        action="skipped",
        reason="score 3 < threshold 6",
        score=3,
        pump_pct=45.5,
    )
    assert _queue.qsize() == 1
    row = _queue.get_nowait()
    assert row[1] == "BEAT"
    assert row[2] == "bybit"
    assert row[3] == "skipped"
    assert row[4] == "score 3 < threshold 6"
    assert row[5] == 3
    assert row[6] == 45.5


def test_write_decision_enqueues_row_without_optional_fields() -> None:
    _drain()
    write_decision("postgresql://test", base="ACT", exchange="bybit", action="opened", reason="ok")
    row = _queue.get_nowait()
    assert row[5] is None
    assert row[6] is None


def test_write_decision_drops_when_queue_full() -> None:
    _drain()
    from schurfer_execution.decisions import _QUEUE_MAXSIZE

    for _ in range(_QUEUE_MAXSIZE):
        write_decision(
            "postgresql://test", base="X", exchange="bybit", action="skipped", reason="x"
        )

    # queue is now full — this call must not raise
    write_decision(
        "postgresql://test", base="EXTRA", exchange="bybit", action="skipped", reason="x"
    )
    assert _queue.qsize() == _QUEUE_MAXSIZE  # extra row was dropped
    _drain()


async def test_run_decision_writer_inserts_row() -> None:
    _drain()
    executed: asyncio.Event = asyncio.Event()
    captured_params: list[tuple[object, ...]] = []

    async def fake_execute(_sql: str, params: tuple[object, ...]) -> None:
        captured_params.append(params)
        executed.set()

    mock_cur = MagicMock()
    mock_cur.execute = fake_execute
    mock_cur.__aenter__ = AsyncMock(return_value=mock_cur)
    mock_cur.__aexit__ = AsyncMock(return_value=False)

    mock_conn = MagicMock()
    mock_conn.cursor = MagicMock(return_value=mock_cur)
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)

    _queue.put_nowait((datetime.now(tz=UTC), "BEAT", "bybit", "skipped", "test reason", 3, 45.5))

    with patch(
        "schurfer_execution.decisions.psycopg.AsyncConnection.connect",
        new_callable=AsyncMock,
        return_value=mock_conn,
    ):
        task = asyncio.create_task(run_decision_writer("postgresql://test"))
        await asyncio.wait_for(executed.wait(), timeout=2.0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert len(captured_params) == 1
    assert captured_params[0][1] == "BEAT"
    assert captured_params[0][3] == "skipped"


async def test_run_decision_writer_retries_row_after_execute_failure() -> None:
    _drain()
    execute_calls = 0
    succeeded = asyncio.Event()

    async def flaky_execute(_sql: str, params: tuple[object, ...]) -> None:
        nonlocal execute_calls
        execute_calls += 1
        if execute_calls == 1:
            raise OSError("transient write error")
        succeeded.set()

    def make_mock_conn() -> MagicMock:
        mock_cur = MagicMock()
        mock_cur.execute = flaky_execute
        mock_cur.__aenter__ = AsyncMock(return_value=mock_cur)
        mock_cur.__aexit__ = AsyncMock(return_value=False)
        mock_conn = MagicMock()
        mock_conn.cursor = MagicMock(return_value=mock_cur)
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=False)
        return mock_conn

    _queue.put_nowait((datetime.now(tz=UTC), "BEAT", "bybit", "skipped", "test", None, None))

    with patch(
        "schurfer_execution.decisions.psycopg.AsyncConnection.connect",
        new_callable=AsyncMock,
        side_effect=lambda *a, **k: make_mock_conn(),
    ):
        task = asyncio.create_task(run_decision_writer("postgresql://test"))
        await asyncio.wait_for(succeeded.wait(), timeout=2.0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert execute_calls == 2  # first failed, second succeeded after reconnect
    _drain()


async def test_run_decision_writer_reconnects_on_connect_failure() -> None:
    connect_attempts = 0
    cancelled = asyncio.Event()

    async def failing_connect(*_args: object, **_kwargs: object) -> object:
        nonlocal connect_attempts
        connect_attempts += 1
        if connect_attempts < 3:
            raise OSError("connection refused")
        cancelled.set()
        raise asyncio.CancelledError

    with (
        patch("schurfer_execution.decisions.psycopg.AsyncConnection.connect", failing_connect),
        patch("schurfer_execution.decisions.asyncio.sleep", new_callable=AsyncMock),
    ):
        task = asyncio.create_task(run_decision_writer("postgresql://test"))
        await asyncio.wait_for(cancelled.wait(), timeout=2.0)
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert connect_attempts == 3


async def test_run_decision_writer_uses_connect_timeout_and_autocommit() -> None:
    from schurfer_execution.decisions import _CONNECT_TIMEOUT

    captured: dict[str, object] = {}

    async def spy_connect(url: str, **kwargs: object) -> object:
        captured.update(kwargs)
        raise asyncio.CancelledError

    with patch("schurfer_execution.decisions.psycopg.AsyncConnection.connect", spy_connect):
        task = asyncio.create_task(run_decision_writer("postgresql://test"))
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert captured.get("connect_timeout") == _CONNECT_TIMEOUT
    assert captured.get("autocommit") is True
