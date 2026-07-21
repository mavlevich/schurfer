from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import psycopg
import structlog

log = structlog.get_logger()

_CONNECT_TIMEOUT = 5
_RECONNECT_DELAY = 5
_QUEUE_MAXSIZE = 500  # ~10 min of skips at 50 pumps/tick — drop beyond this

_INSERT = """
INSERT INTO app.trade_decisions
  (ts, base, exchange, action, reason, score, pump_pct,
   decision_id, strategy_version, features, liquidity, price)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s::uuid, %s, %s::jsonb, %s::jsonb, %s)
ON CONFLICT (decision_id) DO NOTHING
"""

_queue: asyncio.Queue[tuple[object, ...]] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)


def _to_jsonb(value: dict[str, Any] | None, base: str, field: str) -> str | None:
    """Serialize a dict for a jsonb column, rejecting NaN/inf.

    allow_nan=False raises on a non-finite value (Postgres jsonb has no NaN, and a
    NaN token would be a poison row the writer could never insert). We drop the
    field to None instead so the decision itself is still recorded.
    """
    if value is None:
        return None
    try:
        return json.dumps(value, allow_nan=False)
    except ValueError:
        log.warning("decisions.jsonb_not_finite.dropping_field", base=base, field=field)
        return None


def write_decision(
    db_url: str | None,
    *,
    base: str,
    exchange: str,
    action: str,
    reason: str,
    score: int | None = None,
    pump_pct: float | None = None,
    decision_id: str | None = None,
    strategy_version: str | None = None,
    features: dict[str, Any] | None = None,
    liquidity: dict[str, Any] | None = None,
    price: float | None = None,
) -> None:
    """Enqueue a decision for async write. Never blocks the caller.

    decision_id makes the write idempotent: the writer re-enqueues a row after a
    failed execute, and if Postgres had already committed it before the failure
    the ON CONFLICT clause drops the retry instead of duplicating the decision.
    """
    if not db_url:
        return
    row = (
        datetime.now(tz=UTC),
        base,
        exchange,
        action,
        reason,
        score,
        pump_pct,
        decision_id,
        strategy_version,
        _to_jsonb(features, base, "features"),
        _to_jsonb(liquidity, base, "liquidity"),
        price,
    )
    try:
        _queue.put_nowait(row)
    except asyncio.QueueFull:
        log.warning(
            "decisions.queue_full.dropping",
            base=base,
            action=action,
            queue_size=_QUEUE_MAXSIZE,
        )


async def run_decision_writer(db_url: str) -> None:
    """Long-running background task: drains the decision queue over a single connection.

    Reconnects automatically on connection loss with a fixed backoff.
    On execute failure the row is returned to the queue for retry after reconnect.
    """
    while True:
        try:
            aconn = await psycopg.AsyncConnection.connect(
                db_url, connect_timeout=_CONNECT_TIMEOUT, autocommit=True
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("decisions.connect_failed", err=str(exc))
            await asyncio.sleep(_RECONNECT_DELAY)
            continue

        log.info("decisions.writer_connected")
        try:
            async with aconn, aconn.cursor() as cur:
                while True:
                    row = await _queue.get()
                    try:
                        await cur.execute(_INSERT, row)
                        _queue.task_done()
                    except Exception as exc:
                        log.error("decisions.write_failed", err=str(exc))
                        _queue.task_done()
                        # Return the row so it's retried after reconnect.
                        # If the queue is full (sustained outage), drop it.
                        try:
                            _queue.put_nowait(row)
                        except asyncio.QueueFull:
                            log.warning("decisions.queue_full.dropping_on_retry", err=str(exc))
                        break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("decisions.writer_error", err=str(exc))
            await asyncio.sleep(_RECONNECT_DELAY)
