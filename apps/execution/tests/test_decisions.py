import asyncio
import contextlib
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg
import pytest
from fakeredis.aioredis import FakeRedis
from schurfer_execution import decisions
from schurfer_execution.decisions import (
    _CONSUMER,
    _DLQ,
    _GROUP,
    _SCHEMA_VERSION,
    _STREAM,
    _ensure_group,
    _handle,
    _read_batch,
    _row_from_payload,
    run_decision_writer,
    write_decision,
)


def _valid_payload(**over: object) -> dict[str, object]:
    p: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "ts": datetime.now(tz=UTC).isoformat(),
        "base": "BEAT",
        "exchange": "bybit",
        "action": "skipped",
        "reason": "x",
        "score": 3,
        "pump_pct": 45.5,
        "decision_id": "11111111-1111-1111-1111-111111111111",
        "strategy_version": "v1",
        "features": None,
        "liquidity": None,
        "price": 1.5,
        "pump_event_id": 42,
        "strategy_id": None,
        "trading_mode": None,
        "side": None,
    }
    p.update(over)
    return p


def _cursor(fail: Exception | None = None) -> MagicMock:
    calls: list[tuple[object, ...]] = []

    async def execute(_sql: str, params: tuple[object, ...]) -> None:
        calls.append(params)
        if fail is not None:
            raise fail

    cur = MagicMock()
    cur.execute = execute
    cur.calls = calls
    return cur


async def _enqueue_and_read(rdb: FakeRedis, payload: dict[str, object]) -> tuple[object, dict]:
    await _ensure_group(rdb)
    await rdb.xadd(_STREAM, {"data": json.dumps(payload)})
    resp = await rdb.xreadgroup(_GROUP, _CONSUMER, {_STREAM: ">"}, count=10)
    return resp[0][1][0]


# ---- producer ----


async def test_write_decision_requires_decision_id() -> None:
    rdb = FakeRedis()
    with pytest.raises(ValueError, match="decision_id"):
        await write_decision(
            rdb, base="BEAT", exchange="bybit", action="skipped", reason="x", decision_id=""
        )
    await rdb.aclose()


async def test_write_decision_xadd_and_seen_atomic() -> None:
    rdb = FakeRedis()
    await write_decision(
        rdb,
        base="BEAT",
        exchange="bybit",
        action="skipped",
        reason="x",
        decision_id="d1",
        pump_event_id=42,
        seen_key="trader:seen:BEAT",
        seen_ttl=1800,
    )
    assert await rdb.xlen(_STREAM) == 1
    entry = (await rdb.xrange(_STREAM))[0][1]
    body = json.loads(entry[b"data"])
    assert body["schema_version"] == _SCHEMA_VERSION
    assert body["base"] == "BEAT"
    assert body["pump_event_id"] == 42
    # seen flag set atomically with the XADD (one MULTI/EXEC)
    assert await rdb.get("trader:seen:BEAT") == b"1"
    assert 0 < await rdb.ttl("trader:seen:BEAT") <= 1800
    await rdb.aclose()


async def test_write_decision_rejects_invalid_pump_event_id_before_xadd() -> None:
    rdb = FakeRedis()
    with pytest.raises(ValueError, match="pump_event_id"):
        await write_decision(
            rdb,
            base="BEAT",
            exchange="bybit",
            action="skipped",
            reason="x",
            decision_id="d1",
            pump_event_id=0,
        )
    assert await rdb.xlen(_STREAM) == 0
    await rdb.aclose()


async def test_write_decision_carries_strategy_id_and_trading_mode() -> None:
    rdb = FakeRedis()
    await write_decision(
        rdb,
        base="BEAT",
        exchange="bybit",
        action="shadow_recorded",
        reason="x",
        decision_id="d1",
        strategy_id=7,
        trading_mode="shadow",
        side="long",
    )
    entry = (await rdb.xrange(_STREAM))[0][1]
    body = json.loads(entry[b"data"])
    assert body["strategy_id"] == 7
    assert body["trading_mode"] == "shadow"
    assert body["side"] == "long"
    assert body["schema_version"] == 2
    await rdb.aclose()


async def test_write_decision_rejects_invalid_strategy_id_before_xadd() -> None:
    rdb = FakeRedis()
    with pytest.raises(ValueError, match="strategy_id"):
        await write_decision(
            rdb,
            base="BEAT",
            exchange="bybit",
            action="skipped",
            reason="x",
            decision_id="d1",
            strategy_id=0,
        )
    assert await rdb.xlen(_STREAM) == 0
    await rdb.aclose()


async def test_write_decision_without_seen_key_only_xadds() -> None:
    rdb = FakeRedis()
    await write_decision(
        rdb,
        base="BEAT",
        exchange="bybit",
        action="opened_dry_run",
        reason="paper",
        decision_id="d1",
    )
    assert await rdb.xlen(_STREAM) == 1
    assert await rdb.get("trader:seen:BEAT") is None
    await rdb.aclose()


async def test_write_decision_eval_failure_propagates() -> None:
    # The XADD + SET seen run as one Lua script; a failure propagates so the caller does
    # not proceed as if the decision were recorded.
    rdb = MagicMock()
    rdb.eval = AsyncMock(side_effect=RuntimeError("redis down"))
    with pytest.raises(RuntimeError):
        await write_decision(
            rdb,
            base="BEAT",
            exchange="bybit",
            action="skipped",
            reason="x",
            decision_id="d1",
            seen_key="k",
            seen_ttl=60,
        )


async def test_write_decision_wrongtype_xadd_does_not_set_seen() -> None:
    # The core reason for Lua over MULTI/EXEC: if XADD errors (here a wrong-typed stream
    # key), the script aborts BEFORE SET seen, so a token is never marked seen with its
    # decision lost. MULTI/EXEC would have run the SET anyway.
    rdb = FakeRedis()
    await rdb.set(_STREAM, "not-a-stream")  # force WRONGTYPE on XADD
    with contextlib.suppress(Exception):
        await write_decision(
            rdb,
            base="BEAT",
            exchange="bybit",
            action="skipped",
            reason="x",
            decision_id="d1",
            seen_key="trader:seen:BEAT",
            seen_ttl=1800,
        )
    assert await rdb.get("trader:seen:BEAT") is None  # seen NOT set
    await rdb.aclose()


# ---- payload decoding ----


def test_row_from_payload_maps_all_fields() -> None:
    row = _row_from_payload(
        json.dumps(
            _valid_payload(base="ACT", price=2.5, strategy_id=7, trading_mode="shadow", side="long")
        )
    )
    assert row[1] == "ACT"
    assert row[11] == 2.5
    assert row[12] == 42
    assert row[13] == 7
    assert row[14] == "shadow"
    assert row[15] == "long"


def test_row_from_payload_accepts_old_message_without_pump_event_id() -> None:
    payload = _valid_payload()
    del payload["pump_event_id"]

    row = _row_from_payload(json.dumps(payload))

    assert row[12] is None


def test_row_from_payload_accepts_message_without_strategy_id_or_trading_mode() -> None:
    """pump_short's own writes never set these -- a message that predates
    (or simply never uses) them must still parse, with both columns NULL."""
    payload = _valid_payload()
    del payload["strategy_id"]
    del payload["trading_mode"]
    del payload["side"]

    row = _row_from_payload(json.dumps(payload))

    assert row[13] is None
    assert row[14] is None
    assert row[15] is None


def test_row_from_payload_accepts_a_v1_backlog_message() -> None:
    """A message enqueued by pre-this-deploy code (schema_version=1, no
    strategy_id/trading_mode/side keys at all) must still parse -- the
    stream can carry a v1 backlog across a deploy."""
    payload = _valid_payload(schema_version=1)
    del payload["strategy_id"]
    del payload["trading_mode"]
    del payload["side"]

    row = _row_from_payload(json.dumps(payload))

    assert row[13] is None
    assert row[14] is None
    assert row[15] is None


def test_schema_version_is_2_and_accepts_v1_and_v2() -> None:
    """Locks in the contract the version bump exists for: new writes are
    stamped v2, but v1 (pre-this-deploy) messages still in the stream must
    not be DLQ'd. See _SCHEMA_VERSION's own comment for why the bump itself
    matters (an old writer after a rollback must DLQ a v2 message loudly,
    not silently drop its new fields)."""
    assert decisions._SCHEMA_VERSION == 2
    assert {1, 2} == decisions._SUPPORTED_SCHEMA_VERSIONS


@pytest.mark.parametrize("pump_event_id", [0, -1, True, 1.5, "42"])
def test_row_from_payload_rejects_invalid_pump_event_id(pump_event_id: object) -> None:
    with pytest.raises(ValueError, match="pump_event_id"):
        _row_from_payload(json.dumps(_valid_payload(pump_event_id=pump_event_id)))


@pytest.mark.parametrize("strategy_id", [0, -1, True, 1.5, "7"])
def test_row_from_payload_rejects_invalid_strategy_id(strategy_id: object) -> None:
    with pytest.raises(ValueError, match="strategy_id"):
        _row_from_payload(json.dumps(_valid_payload(strategy_id=strategy_id)))


def test_row_from_payload_rejects_unknown_schema() -> None:
    with pytest.raises(ValueError, match="schema_version"):
        _row_from_payload(json.dumps(_valid_payload(schema_version=999)))


def test_row_from_payload_rejects_bad_json() -> None:
    with pytest.raises(ValueError):
        _row_from_payload("{not json")


def test_row_from_payload_rejects_missing_decision_id() -> None:
    p = _valid_payload()
    del p["decision_id"]
    with pytest.raises(KeyError):
        _row_from_payload(json.dumps(p))


def test_row_from_payload_rejects_empty_decision_id() -> None:
    with pytest.raises(ValueError, match="decision_id"):
        _row_from_payload(json.dumps(_valid_payload(decision_id="")))


def test_insert_has_on_conflict_do_nothing() -> None:
    # Idempotency on redelivery depends on this clause; lock it in place.
    assert "on conflict (decision_id) do nothing" in decisions._INSERT.lower()


def test_insert_includes_strategy_id_and_trading_mode() -> None:
    assert "strategy_id" in decisions._INSERT
    assert "trading_mode" in decisions._INSERT
    assert "side" in decisions._INSERT


# ---- consumer: _handle ----


async def test_handle_insert_acks_and_deletes() -> None:
    rdb = FakeRedis()
    msg_id, fields = await _enqueue_and_read(rdb, _valid_payload(base="BEAT"))
    cur = _cursor()

    await _handle(cur, rdb, msg_id, fields, {})

    assert cur.calls and cur.calls[0][1] == "BEAT"  # inserted
    assert await rdb.xlen(_STREAM) == 0  # acked + deleted
    await rdb.aclose()


async def test_handle_db_down_leaves_message_pending() -> None:
    rdb = FakeRedis()
    msg_id, fields = await _enqueue_and_read(rdb, _valid_payload())
    cur = _cursor(fail=psycopg.OperationalError("connection lost"))

    with pytest.raises(psycopg.OperationalError):
        await _handle(cur, rdb, msg_id, fields, {})

    # Not acked/deleted: still in the stream for redelivery after reconnect.
    assert await rdb.xlen(_STREAM) == 1
    await rdb.aclose()


async def test_handle_parse_error_goes_to_dlq() -> None:
    rdb = FakeRedis()
    await _ensure_group(rdb)
    await rdb.xadd(_STREAM, {"data": "{not json"})
    resp = await rdb.xreadgroup(_GROUP, _CONSUMER, {_STREAM: ">"}, count=10)
    msg_id, fields = resp[0][1][0]

    await _handle(_cursor(), rdb, msg_id, fields, {})

    assert await rdb.xlen(_STREAM) == 0  # removed from main
    assert await rdb.xlen(_DLQ) == 1  # parked in DLQ
    await rdb.aclose()


async def test_handle_missing_decision_id_goes_to_dlq() -> None:
    rdb = FakeRedis()
    await _ensure_group(rdb)
    payload = _valid_payload()
    del payload["decision_id"]  # a schema-v1 message with no id cannot be deduped
    await rdb.xadd(_STREAM, {"data": json.dumps(payload)})
    resp = await rdb.xreadgroup(_GROUP, _CONSUMER, {_STREAM: ">"}, count=10)
    msg_id, fields = resp[0][1][0]

    await _handle(_cursor(), rdb, msg_id, fields, {})

    assert await rdb.xlen(_STREAM) == 0  # removed from main
    assert await rdb.xlen(_DLQ) == 1  # parked in DLQ, not inserted with NULL id
    await rdb.aclose()


async def test_dlq_xadd_failure_leaves_message_in_stream() -> None:
    # If the DLQ XADD fails, the Lua aborts before XACK+XDEL, so a poison message stays
    # in the stream (retried) instead of being dropped with no DLQ copy.
    rdb = FakeRedis()
    await rdb.set(_DLQ, "not-a-stream")  # force WRONGTYPE on the DLQ XADD
    await _ensure_group(rdb)
    await rdb.xadd(_STREAM, {"data": "{not json"})  # a poison (unparseable) message
    resp = await rdb.xreadgroup(_GROUP, _CONSUMER, {_STREAM: ">"}, count=10)
    msg_id, fields = resp[0][1][0]

    with contextlib.suppress(Exception):
        await _handle(_cursor(), rdb, msg_id, fields, {})

    assert await rdb.xlen(_STREAM) == 1  # not dropped; still there for retry
    await rdb.aclose()


async def test_handle_repeated_insert_failure_goes_to_dlq() -> None:
    rdb = FakeRedis()
    msg_id, fields = await _enqueue_and_read(rdb, _valid_payload())
    cur = _cursor(fail=ValueError("bad row"))  # non-transient -> counts toward poison
    attempts: dict[str, int] = {}

    for _ in range(decisions._MAX_ATTEMPTS):
        await _handle(cur, rdb, msg_id, fields, attempts)

    assert await rdb.xlen(_STREAM) == 0
    assert await rdb.xlen(_DLQ) == 1
    await rdb.aclose()


# ---- consumer: _read_batch ----


async def test_read_batch_returns_new_entries() -> None:
    rdb = FakeRedis()
    await _ensure_group(rdb)
    await rdb.xadd(_STREAM, {"data": json.dumps(_valid_payload())})

    batch = await _read_batch(rdb)

    assert len(batch) == 1
    await rdb.aclose()


def test_redis_socket_timeout_exceeds_block_window() -> None:
    # The blocking XREADGROUP relies on the socket read timeout being longer than the
    # BLOCK window; if they are equal (redis-py 8 defaults socket_timeout to 5s) the read
    # times out and the client reconnects on every idle window.
    assert decisions._REDIS_SOCKET_TIMEOUT_SECONDS * 1000 > decisions._BLOCK_MS


async def test_read_batch_reclaims_pending_from_dead_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rdb = FakeRedis()
    await _ensure_group(rdb)
    await rdb.xadd(_STREAM, {"data": json.dumps(_valid_payload())})
    # A crashed consumer read but never acked -> the entry is pending under its name.
    await rdb.xreadgroup(_GROUP, "dead-writer", {_STREAM: ">"}, count=10)
    monkeypatch.setattr(decisions, "_CLAIM_MIN_IDLE_MS", 0)

    batch = await _read_batch(rdb)

    assert len(batch) == 1  # XAUTOCLAIM reclaimed it
    await rdb.aclose()


# ---- consumer: run_decision_writer integration (fakeredis + mocked psycopg) ----


async def test_read_then_handle_drains_and_removes_entry() -> None:
    # End-to-end over a real (fake) stream: a written decision is read back, inserted,
    # then acked+deleted. Exercises the read->handle path the writer loop runs, without
    # its blocking XREADGROUP (fakeredis does not honor blocking reads).
    rdb = FakeRedis()
    await write_decision(
        rdb, base="BEAT", exchange="bybit", action="skipped", reason="x", decision_id="d1"
    )
    await _ensure_group(rdb)
    cur = _cursor()

    for msg_id, fields in await _read_batch(rdb):
        await _handle(cur, rdb, msg_id, fields, {})

    assert cur.calls and cur.calls[0][1] == "BEAT"  # committed
    assert await rdb.xlen(_STREAM) == 0  # acked + deleted
    await rdb.aclose()


async def test_writer_connects_with_autocommit_and_timeout() -> None:
    # ACK-after-commit is only safe because the connection is autocommit=True (each
    # INSERT commits before we ack). Lock the connect contract so a later change that
    # drops autocommit cannot silently ack rows that were never committed.
    rdb = FakeRedis()
    captured: dict[str, object] = {}

    async def spy_connect(_url: str, **kwargs: object) -> object:
        captured.update(kwargs)
        raise asyncio.CancelledError

    with patch("schurfer_execution.decisions.psycopg.AsyncConnection.connect", spy_connect):
        task = asyncio.create_task(run_decision_writer(rdb, "postgresql://test"))
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert captured.get("autocommit") is True
    assert captured.get("connect_timeout") == decisions._CONNECT_TIMEOUT
    await rdb.aclose()


async def test_writer_reconnects_on_connect_failure() -> None:
    rdb = FakeRedis()
    attempts = 0
    cancelled = asyncio.Event()

    async def failing_connect(*_a: object, **_k: object) -> object:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OSError("connection refused")
        cancelled.set()
        raise asyncio.CancelledError

    with (
        patch("schurfer_execution.decisions.psycopg.AsyncConnection.connect", failing_connect),
        patch("schurfer_execution.decisions.asyncio.sleep", new_callable=AsyncMock),
    ):
        task = asyncio.create_task(run_decision_writer(rdb, "postgresql://test"))
        await asyncio.wait_for(cancelled.wait(), timeout=2.0)
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert attempts == 3
    await rdb.aclose()
