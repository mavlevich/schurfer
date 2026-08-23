"""Tests for worker_health.py -- the reusable background-worker heartbeat."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from schurfer_execution.worker_health import (
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_STARTED,
    WorkerHeartbeat,
    read_heartbeat,
    track_tick,
    write_heartbeat,
)


def _rdb() -> MagicMock:
    rdb = MagicMock()
    rdb.set = AsyncMock()
    rdb.get = AsyncMock(return_value=None)
    return rdb


def _heartbeat(**overrides: object) -> WorkerHeartbeat:
    fields: dict[str, object] = {
        "worker_name": "scanner",
        "worker_version": "v4",
        "state": STATE_COMPLETED,
        "started_at": datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC),
        "completed_at": datetime(2026, 8, 22, 12, 0, 1, tzinfo=UTC),
        "duration_ms": 1000.0,
        "counters": {"candidates_found": 3},
        "last_error": None,
    }
    fields.update(overrides)
    return WorkerHeartbeat(**fields)  # type: ignore[arg-type]


# --- write/read round-trip ---


async def test_write_then_read_round_trips_the_heartbeat() -> None:
    rdb = _rdb()
    heartbeat = _heartbeat()
    await write_heartbeat(rdb, key="worker:x:heartbeat", heartbeat=heartbeat, ttl_seconds=300)
    stored_json = rdb.set.call_args.args[1]
    rdb.get = AsyncMock(return_value=stored_json)

    read_back = await read_heartbeat(rdb, key="worker:x:heartbeat")

    assert read_back == heartbeat
    rdb.set.assert_awaited_once_with("worker:x:heartbeat", stored_json, ex=300)


async def test_read_heartbeat_returns_none_when_key_missing() -> None:
    rdb = _rdb()
    result = await read_heartbeat(rdb, key="worker:x:heartbeat")
    assert result is None


async def test_read_heartbeat_returns_none_on_corrupt_payload() -> None:
    rdb = _rdb()
    rdb.get = AsyncMock(return_value=b"not json")
    result = await read_heartbeat(rdb, key="worker:x:heartbeat")
    assert result is None


async def test_read_heartbeat_handles_bytes_from_redis() -> None:
    rdb = _rdb()
    heartbeat = _heartbeat()
    rdb.get = AsyncMock(return_value=heartbeat.to_json().encode())
    result = await read_heartbeat(rdb, key="worker:x:heartbeat")
    assert result == heartbeat


async def test_heartbeat_with_no_completed_at_round_trips_none() -> None:
    heartbeat = _heartbeat(state=STATE_STARTED, completed_at=None, duration_ms=None, counters={})
    rdb = _rdb()
    rdb.get = AsyncMock(return_value=heartbeat.to_json())
    result = await read_heartbeat(rdb, key="worker:x:heartbeat")
    assert result is not None
    assert result.completed_at is None
    assert result.duration_ms is None


# --- fail-open on Redis errors ---


async def test_write_heartbeat_swallows_redis_errors() -> None:
    rdb = _rdb()
    rdb.set = AsyncMock(side_effect=Exception("connection refused"))
    # Must not raise -- a Redis outage can't be allowed to kill the loop
    # this heartbeat is instrumenting.
    await write_heartbeat(rdb, key="worker:x:heartbeat", heartbeat=_heartbeat(), ttl_seconds=300)


async def test_read_heartbeat_swallows_redis_errors() -> None:
    rdb = _rdb()
    rdb.get = AsyncMock(side_effect=Exception("connection refused"))
    result = await read_heartbeat(rdb, key="worker:x:heartbeat")
    assert result is None


# --- track_tick ---


async def test_track_tick_writes_started_then_completed_on_success() -> None:
    rdb = _rdb()
    async with track_tick(
        rdb, key="worker:x:heartbeat", worker_name="scanner", worker_version="v4", ttl_seconds=300
    ) as tick:
        tick.counters["candidates_found"] = 5

    assert rdb.set.await_count == 2
    first_write = WorkerHeartbeat.from_json(rdb.set.await_args_list[0].args[1])
    second_write = WorkerHeartbeat.from_json(rdb.set.await_args_list[1].args[1])
    assert first_write.state == STATE_STARTED
    assert second_write.state == STATE_COMPLETED
    assert second_write.counters == {"candidates_found": 5}
    assert second_write.duration_ms is not None
    assert second_write.duration_ms >= 0


async def test_track_tick_writes_a_zero_candidates_completed_heartbeat() -> None:
    """A tick that finds nothing must still produce a fresh heartbeat --
    only a genuine hang should let the TTL lapse, never a quiet-but-alive
    tick."""
    rdb = _rdb()
    async with track_tick(
        rdb, key="worker:x:heartbeat", worker_name="scanner", worker_version="v4", ttl_seconds=300
    ):
        pass

    second_write = WorkerHeartbeat.from_json(rdb.set.await_args_list[1].args[1])
    assert second_write.state == STATE_COMPLETED
    assert second_write.counters == {}


async def test_track_tick_writes_failed_heartbeat_and_reraises_on_exception() -> None:
    rdb = _rdb()
    with pytest.raises(RuntimeError, match="boom"):
        async with track_tick(
            rdb,
            key="worker:x:heartbeat",
            worker_name="scanner",
            worker_version="v4",
            ttl_seconds=300,
        ):
            raise RuntimeError("boom")

    second_write = WorkerHeartbeat.from_json(rdb.set.await_args_list[1].args[1])
    assert second_write.state == STATE_FAILED
    assert second_write.last_error == "boom"


async def test_track_tick_uses_the_given_key_and_ttl() -> None:
    rdb = _rdb()
    async with track_tick(
        rdb,
        key="worker:early_momentum:v4:trigger:heartbeat",
        worker_name="trigger",
        worker_version="v4",
        ttl_seconds=360,
    ):
        pass

    for call in rdb.set.await_args_list:
        assert call.args[0] == "worker:early_momentum:v4:trigger:heartbeat"
        assert call.kwargs["ex"] == 360
