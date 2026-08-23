"""Reusable background-worker heartbeat.

A small Redis-backed liveness record any `while True` loop can write on
tick entry and exit, so an independent watcher (see
`early_momentum_health.py`) can detect a hung or crashed loop without
depending on that loop's own state -- a fully stalled loop writes nothing
at all, which is exactly what TTL expiry is for.

Deliberately not in `schurfer_market_quality` -- that package stays
infra-free (no Redis). This module is generic across any worker, not
specific to early_momentum.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = structlog.get_logger()

STATE_STARTED = "started"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"


@dataclass(frozen=True)
class WorkerHeartbeat:
    worker_name: str
    worker_version: str
    state: str  # STATE_STARTED | STATE_COMPLETED | STATE_FAILED
    started_at: datetime
    completed_at: datetime | None
    duration_ms: float | None
    counters: dict[str, int]
    last_error: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "worker_name": self.worker_name,
                "worker_version": self.worker_version,
                "state": self.state,
                "started_at": self.started_at.isoformat(),
                "completed_at": self.completed_at.isoformat() if self.completed_at else None,
                "duration_ms": self.duration_ms,
                "counters": self.counters,
                "last_error": self.last_error,
            }
        )

    @staticmethod
    def from_json(raw: str) -> WorkerHeartbeat:
        data = json.loads(raw)
        completed_at = data.get("completed_at")
        return WorkerHeartbeat(
            worker_name=data["worker_name"],
            worker_version=data["worker_version"],
            state=data["state"],
            started_at=datetime.fromisoformat(data["started_at"]),
            completed_at=datetime.fromisoformat(completed_at) if completed_at else None,
            duration_ms=data.get("duration_ms"),
            counters=dict(data.get("counters") or {}),
            last_error=data.get("last_error"),
        )


async def write_heartbeat(
    rdb: Any, *, key: str, heartbeat: WorkerHeartbeat, ttl_seconds: int
) -> None:
    """Best-effort: a Redis outage must never crash the loop it
    instruments -- observability is never load-bearing for the thing it
    observes."""
    try:
        await rdb.set(key, heartbeat.to_json(), ex=ttl_seconds)
    except Exception as exc:
        log.error("worker_health.write_heartbeat_failed", key=key, err=str(exc))


async def read_heartbeat(rdb: Any, *, key: str) -> WorkerHeartbeat | None:
    try:
        raw = await rdb.get(key)
    except Exception as exc:
        log.error("worker_health.read_heartbeat_failed", key=key, err=str(exc))
        return None
    if not raw:
        return None
    try:
        return WorkerHeartbeat.from_json(raw if isinstance(raw, str) else raw.decode())
    except (ValueError, KeyError, TypeError) as exc:
        log.error("worker_health.heartbeat_corrupt", key=key, err=str(exc))
        return None


class TickCounters:
    """Mutable accumulator a loop body fills in during one tick; handed
    back by `track_tick`'s context manager and folded into the
    "completed"/"failed" heartbeat write on exit."""

    def __init__(self) -> None:
        self.counters: dict[str, int] = {}


@asynccontextmanager
async def track_tick(
    rdb: Any,
    *,
    key: str,
    worker_name: str,
    worker_version: str,
    ttl_seconds: int,
) -> AsyncIterator[TickCounters]:
    """Writes a "started" heartbeat on entry and a "completed"/"failed" one
    on exit (via `finally`, so a zero-candidates tick and a raised
    exception both still produce a fresh heartbeat -- only a genuine hang
    lets the TTL lapse). Re-raises whatever the tick body raised; the
    caller's own `try/except` around the outer loop is unaffected -- this
    only observes, never swallows."""
    started_at = datetime.now(tz=UTC)
    await write_heartbeat(
        rdb,
        key=key,
        heartbeat=WorkerHeartbeat(
            worker_name=worker_name,
            worker_version=worker_version,
            state=STATE_STARTED,
            started_at=started_at,
            completed_at=None,
            duration_ms=None,
            counters={},
        ),
        ttl_seconds=ttl_seconds,
    )
    tick = TickCounters()
    last_error: str | None = None
    try:
        yield tick
    except Exception as exc:
        last_error = str(exc)
        raise
    finally:
        completed_at = datetime.now(tz=UTC)
        duration_ms = (completed_at - started_at).total_seconds() * 1000
        await write_heartbeat(
            rdb,
            key=key,
            heartbeat=WorkerHeartbeat(
                worker_name=worker_name,
                worker_version=worker_version,
                state=STATE_FAILED if last_error else STATE_COMPLETED,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration_ms,
                counters=tick.counters,
                last_error=last_error,
            ),
            ttl_seconds=ttl_seconds,
        )


__all__ = [
    "STATE_COMPLETED",
    "STATE_FAILED",
    "STATE_STARTED",
    "TickCounters",
    "WorkerHeartbeat",
    "read_heartbeat",
    "track_tick",
    "write_heartbeat",
]
