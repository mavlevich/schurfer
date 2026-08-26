from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import psycopg
import structlog
from redis import exceptions as redis_exc

log = structlog.get_logger()

# Durable outbox: a decision is XADDed to a Redis Stream, and a separate writer task
# drains it into Postgres, XACKing only after the row is committed. Decisions survive
# an execution restart and a Postgres outage (they wait in the stream), which matters
# because the liquidity snapshot on each decision is the one piece we cannot rebuild
# from history later.
_STREAM = "execution:decisions"
_DLQ = "execution:decisions:dlq"
_GROUP = "decision-db-writers"
_CONSUMER = "writer"
# v2 adds strategy_id/trading_mode/side (feat/execution-shadow-evidence-v1).
# _row_from_payload accepts both v1 and v2 on read (a v1 backlog message
# still in the stream from before this deploy must not be DLQ'd), but every
# NEW message this process writes is stamped v2. The bump itself matters for
# the *other* direction: if this deploy is ever rolled back while a v2
# message is still pending, the old (rolled-back) writer's strict `== 1`
# check DLQs it loudly instead of silently building an INSERT that omits
# strategy_id/trading_mode/side -- colleague review (pump_event_id, added
# earlier, was NOT worth a bump: losing it after a rollback loses a nice-to-
# have episode link, not core evidence identity; these three are the
# evidence this PR exists to add).
_SCHEMA_VERSION = 2
_SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2})

_CONNECT_TIMEOUT = 5
_RECONNECT_DELAY = 5
_READ_COUNT = 100
_BLOCK_MS = 5000
# The decision writer's own Redis client (main.py) must use a socket_timeout longer than
# _BLOCK_MS, otherwise the blocking XREADGROUP and the socket read time out together and
# the client closes the connection on every idle window (redis-py 8 defaults it to 5s).
_REDIS_SOCKET_TIMEOUT_SECONDS = 10.0
_CLAIM_MIN_IDLE_MS = 60_000  # reclaim entries a crashed writer left pending this long
_MAX_ATTEMPTS = 5  # after this many failed inserts a message is poison -> DLQ

# Redis MULTI/EXEC does NOT roll back: a command that errors mid-EXEC does not stop the
# others, so a failed XADD would still let SET seen run -> a token marked seen with its
# decision lost. Lua runs sequentially and aborts on the first error, so XADD-before-SET
# guarantees seen is set only if the decision actually reached the stream.
_XADD_SEEN_LUA = """
redis.call('XADD', KEYS[1], '*', 'data', ARGV[1])
redis.call('SET', KEYS[2], '1', 'EX', tonumber(ARGV[2]))
return 1
"""

# Same reasoning for the DLQ move: park the message before removing the original, so a
# failed DLQ XADD leaves the message in the stream (retried) instead of dropping it.
_TO_DLQ_LUA = """
redis.call('XADD', KEYS[1], '*', 'data', ARGV[1], 'reason', ARGV[2])
redis.call('XACK', KEYS[2], ARGV[3], ARGV[4])
redis.call('XDEL', KEYS[2], ARGV[4])
return 1
"""

_INSERT = """
INSERT INTO app.trade_decisions
  (ts, base, exchange, action, reason, score, pump_pct,
   decision_id, strategy_version, features, liquidity, price, pump_event_id,
   strategy_id, trading_mode, side)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s::uuid, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s)
ON CONFLICT (decision_id) DO NOTHING
"""


def _validate_positive_id(value: object, field: str) -> None:
    """Shared shape check for the two FK-backed integer fields (pump_event_id,
    strategy_id): both are populated internally (journal upserts), never from
    raw user input, so a bad value here is a caller bug -- raising immediately
    (producer side) or before insert (consumer side) surfaces it as a loud
    failure/DLQ entry instead of a silent FK violation deep in Postgres."""
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
        raise ValueError(f"{field} must be a positive integer or None")


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


async def write_decision(
    rdb: Any,
    *,
    base: str,
    exchange: str,
    action: str,
    reason: str,
    decision_id: str,
    score: int | None = None,
    pump_pct: float | None = None,
    strategy_version: str | None = None,
    features: dict[str, Any] | None = None,
    liquidity: dict[str, Any] | None = None,
    price: float | None = None,
    pump_event_id: int | None = None,
    strategy_id: int | None = None,
    trading_mode: str | None = None,
    side: str | None = None,
    seen_key: str | None = None,
    seen_ttl: int | None = None,
) -> None:
    """Append a decision to the durable outbox stream, and mark the token seen when
    seen_key is given, in one Lua script (XADD then SET, aborting if XADD fails). Atomic
    so a crash cannot leave a token marked seen with its decision not on the stream (a
    lost decision) nor XADD without seen (a duplicate on reprocess, with a fresh
    decision_id that ON CONFLICT could not dedupe — which would skew the measurement
    dataset). A failure raises, so the caller does not proceed as if the decision were
    recorded.

    decision_id is required and must be non-empty: idempotency on redelivery relies
    entirely on it via ON CONFLICT (decision_id), and a NULL would not dedupe.

    strategy_id/trading_mode/side are for execution_intent.ShadowBroker (and any
    future non-pump_short caller) -- pump_short's own call sites never pass them,
    and all three columns stay NULL on every row it writes, same as before these
    existed.

    No MAXLEN trim: it could silently drop an entry the writer has not committed yet.
    The writer XDELs each entry after its Postgres commit instead.
    """
    if not decision_id:
        raise ValueError("write_decision requires a non-empty decision_id for idempotency")
    _validate_positive_id(pump_event_id, "pump_event_id")
    _validate_positive_id(strategy_id, "strategy_id")
    payload = json.dumps(
        {
            "schema_version": _SCHEMA_VERSION,
            "ts": datetime.now(tz=UTC).isoformat(),
            "base": base,
            "exchange": exchange,
            "action": action,
            "reason": reason,
            "score": score,
            "pump_pct": pump_pct,
            "decision_id": decision_id,
            "strategy_version": strategy_version,
            "features": _to_jsonb(features, base, "features"),
            "liquidity": _to_jsonb(liquidity, base, "liquidity"),
            "price": price,
            "pump_event_id": pump_event_id,
            "strategy_id": strategy_id,
            "trading_mode": trading_mode,
            "side": side,
        }
    )
    if seen_key is not None:
        await rdb.eval(_XADD_SEEN_LUA, 2, _STREAM, seen_key, payload, str(seen_ttl))
    else:
        await rdb.xadd(_STREAM, {"data": payload})


def _row_from_payload(data: str | bytes) -> tuple[object, ...]:
    """Rebuild the INSERT tuple from a stream payload. Raises on malformed JSON, an
    unknown schema_version, or a missing required field, which routes the message to
    the DLQ rather than looping on it."""
    d = json.loads(data)
    if d.get("schema_version") not in _SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError(f"unsupported schema_version {d.get('schema_version')!r}")
    # Idempotency on redelivery is entirely via ON CONFLICT (decision_id); a NULL would
    # not dedupe (distinct NULLs in Postgres), so a message without one is poison -> DLQ.
    decision_id = d["decision_id"]
    if not decision_id:
        raise ValueError("missing decision_id")
    pump_event_id = d.get("pump_event_id")
    _validate_positive_id(pump_event_id, "pump_event_id")
    strategy_id = d.get("strategy_id")
    _validate_positive_id(strategy_id, "strategy_id")
    return (
        datetime.fromisoformat(d["ts"]),
        d["base"],
        d["exchange"],
        d["action"],
        d["reason"],
        d.get("score"),
        d.get("pump_pct"),
        decision_id,
        d.get("strategy_version"),
        d.get("features"),
        d.get("liquidity"),
        d.get("price"),
        pump_event_id,
        strategy_id,
        d.get("trading_mode"),
        d.get("side"),
    )


def _field_data(fields: dict[Any, Any]) -> Any:
    # The shared Redis client returns bytes keys/values (decode_responses=False).
    return fields.get(b"data", fields.get("data"))


def _msg_key(msg_id: Any) -> str:
    return msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)


async def _ensure_group(rdb: Any) -> None:
    try:
        await rdb.xgroup_create(_STREAM, _GROUP, id="0", mkstream=True)
    except redis_exc.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def _read_batch(rdb: Any) -> list[tuple[Any, dict[Any, Any]]]:
    """Reclaim entries a crashed writer left pending, else block for new ones. Pending
    recovery comes first so a restart drains the backlog before fresh decisions."""
    _cursor, claimed, _deleted = await rdb.xautoclaim(
        _STREAM, _GROUP, _CONSUMER, min_idle_time=_CLAIM_MIN_IDLE_MS, count=_READ_COUNT
    )
    if claimed:
        return list(claimed)
    # Relies on the client socket_timeout being > _BLOCK_MS (see main.py): the server
    # returns nil when the BLOCK window elapses, before the socket read times out, so an
    # idle window is a clean empty result and the connection stays open. A real read
    # timeout (network hang) still surfaces and drives reconnect in run_decision_writer.
    resp = await rdb.xreadgroup(
        _GROUP, _CONSUMER, {_STREAM: ">"}, count=_READ_COUNT, block=_BLOCK_MS
    )
    if not resp:
        return []
    return list(resp[0][1])


async def _ack_del(rdb: Any, msg_id: Any) -> None:
    """XACK then XDEL in one transaction. Separately, an XDEL that fails after the XACK
    leaves the entry in the stream forever (acked, so never redelivered) — a slow leak."""
    async with rdb.pipeline(transaction=True) as pipe:
        pipe.xack(_STREAM, _GROUP, msg_id)
        pipe.xdel(_STREAM, msg_id)
        await pipe.execute()


async def _to_dlq(rdb: Any, msg_id: Any, data: Any, reason: str) -> None:
    """Park a message that cannot be inserted, then remove it from the main stream so
    one poison row cannot block the queue forever nor be dropped silently. One Lua
    script (XADD DLQ, then XACK+XDEL, aborting if the DLQ XADD fails) so the message
    cannot be both dropped from the stream and missing from the DLQ."""
    await rdb.eval(
        _TO_DLQ_LUA,
        2,
        _DLQ,
        _STREAM,
        data if data is not None else b"",
        reason,
        _GROUP,
        _msg_key(msg_id),
    )
    log.error("decisions.poison_to_dlq", id=_msg_key(msg_id), reason=reason)


async def _handle(
    cur: Any, rdb: Any, msg_id: Any, fields: dict[Any, Any], attempts: dict[str, int]
) -> None:
    key = _msg_key(msg_id)
    data = _field_data(fields)
    try:
        row = _row_from_payload(data)
    except (ValueError, KeyError, TypeError) as exc:
        await _to_dlq(rdb, msg_id, data, reason=f"parse: {exc}")
        attempts.pop(key, None)
        return
    try:
        await cur.execute(_INSERT, row)
    except (psycopg.OperationalError, psycopg.InterfaceError):
        # Transient connection loss: leave the message pending and let the outer loop
        # reconnect and redeliver it. Re-raise so we do not XACK an uncommitted row.
        raise
    except Exception as exc:
        n = attempts.get(key, 0) + 1
        attempts[key] = n
        if n >= _MAX_ATTEMPTS:
            await _to_dlq(rdb, msg_id, data, reason=str(exc))
            attempts.pop(key, None)
        else:
            log.warning("decisions.insert_failed", id=key, attempt=n, err=str(exc))
        return
    # Committed (autocommit): ack + delete so the stream does not grow unbounded.
    await _ack_del(rdb, msg_id)
    attempts.pop(key, None)


async def run_decision_writer(rdb: Any, db_url: str, tracker: Any = None) -> None:
    """Long-running task: drain the decision outbox into Postgres, at-least-once.

    New entries via XREADGROUP, entries a crashed writer left pending via XAUTOCLAIM.
    INSERT is idempotent (ON CONFLICT (decision_id) DO NOTHING), so a redelivery after
    a commit-then-crash does not duplicate. XACK+XDEL only after the commit.

    Single consumer name: fine for one execution instance. Multiple replicas would need
    distinct names (and a shared poison count) — revisit if execution is ever scaled out.
    """
    # Poison-retry count is in-process, so a restart resets it: a stuck message gets a
    # few extra tries before the DLQ, never fewer. Acceptable; move to XPENDING delivery
    # count if that ever matters.
    attempts: dict[str, int] = {}
    while True:
        try:
            aconn = await psycopg.AsyncConnection.connect(
                db_url, connect_timeout=_CONNECT_TIMEOUT, autocommit=True
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if tracker:
                tracker.tick_failed(exc)
            log.error("decisions.connect_failed", err=str(exc))
            await asyncio.sleep(_RECONNECT_DELAY)
            continue

        log.info("decisions.writer_connected")
        try:
            await _ensure_group(rdb)
            async with aconn, aconn.cursor() as cur:
                while True:
                    if tracker:
                        tracker.tick_started()
                    batch = await _read_batch(rdb)
                    for msg_id, fields in batch:
                        await _handle(cur, rdb, msg_id, fields, attempts)
                    if tracker:
                        if not batch:
                            tracker.tick_idle()
                        else:
                            tracker.tick_succeeded()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if tracker:
                tracker.tick_failed(exc)
            log.error("decisions.writer_error", err=str(exc))
            await asyncio.sleep(_RECONNECT_DELAY)
