import asyncio
import contextlib
import json
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


def test_write_decision_enqueues_price() -> None:
    _drain()
    write_decision(
        "postgresql://test",
        base="BEAT",
        exchange="bybit",
        action="skipped",
        reason="ok",
        price=0.00012345,
    )
    row = _queue.get_nowait()
    assert row[11] == 0.00012345  # price is the last INSERT field


def test_write_decision_enqueues_row_without_optional_fields() -> None:
    _drain()
    write_decision("postgresql://test", base="ACT", exchange="bybit", action="opened", reason="ok")
    row = _queue.get_nowait()
    assert row[5] is None
    assert row[6] is None
    # measurement fields default to None
    assert row[7] is None  # decision_id
    assert row[8] is None  # strategy_version
    assert row[9] is None  # features
    assert row[10] is None  # liquidity
    assert row[11] is None  # price


def test_write_decision_serializes_measurement_fields() -> None:
    _drain()
    write_decision(
        "postgresql://test",
        base="BEAT",
        exchange="bybit",
        action="opened",
        reason="ok",
        score=7,
        decision_id="11111111-1111-1111-1111-111111111111",
        strategy_version="pump_short_v1",
        features={"signal": {"score": 7}, "candidate_exchanges": ["bybit"]},
        liquidity={"status": "sampled", "spread_bps": 12.0},
    )
    row = _queue.get_nowait()
    assert row[7] == "11111111-1111-1111-1111-111111111111"
    assert row[8] == "pump_short_v1"
    # features and liquidity are JSON-serialized for the jsonb columns
    assert json.loads(row[9]) == {"signal": {"score": 7}, "candidate_exchanges": ["bybit"]}
    assert json.loads(row[10]) == {"status": "sampled", "spread_bps": 12.0}


def test_insert_has_on_conflict_do_nothing() -> None:
    # Idempotency guard: the writer re-enqueues a row after an ambiguous commit, so
    # the INSERT must drop a duplicate decision_id instead of writing it twice. Full
    # behaviour is covered by the real-Postgres migration smoke test; this locks the
    # clause in place against accidental removal.
    from schurfer_execution.decisions import _INSERT

    assert "on conflict (decision_id) do nothing" in _INSERT.lower()


def test_write_decision_drops_non_finite_jsonb_field() -> None:
    _drain()
    # A NaN would serialize to an invalid jsonb token and poison the writer. It must
    # be dropped to None, and the decision still enqueued.
    write_decision(
        "postgresql://test",
        base="BEAT",
        exchange="bybit",
        action="opened",
        reason="ok",
        liquidity={"status": "sampled", "spread_bps": float("nan")},
    )
    row = _queue.get_nowait()
    assert row[10] is None  # liquidity dropped, not a NaN token
    assert row[1] == "BEAT"  # decision still recorded


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

    # Enqueue via write_decision so the row matches the real 12-field INSERT shape
    # (a hand-built short tuple would silently pass the mock but fail real SQL).
    write_decision(
        "postgresql://test",
        base="BEAT",
        exchange="bybit",
        action="skipped",
        reason="test reason",
        score=3,
        pump_pct=45.5,
    )

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
    assert len(captured_params[0]) == 12  # matches the INSERT placeholder count
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

    write_decision(
        "postgresql://test", base="BEAT", exchange="bybit", action="skipped", reason="test"
    )

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
