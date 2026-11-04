"""Closing a LIVE_PROBE position: L reached, and every start of the service.

docs/engineering/live-probe-loss-threshold-design-v1.md, sections 4 and 5. Both close
through the merged emergency close task (``emergency_close.py``), never through a path
of their own:

  * ``record_l_reached``: one transaction writes ``l_reached_at`` with the loss and its
    inputs, so entries stop at once. A second one creates the task
    (``probe_loss_threshold``) for the probe's open position.
  * ``recover_at_start``: one transaction suspends every active record. A second one
    creates a ``probe_recovery`` task for each probe position open in the journal,
    unless a task for that position already exists. A decision held only in process
    dies with it, so a restart never continues a probe on its own.

A position is found from the reservation and its exact entry attempt, not from the
reservation's state alone: a restart can cut the chain ``sending -> placed -> trade``
after the attempt was written. A confirmed fill of the attempt completes the chain in
the same transaction. An attempt whose outcome is not final yet leaves the position
``pending``: the recovery is retried until it is.

The task is written under the instrument's order lock, the lock a monitor exit holds
from its check for an active task until its close row is final. So every close of the
trade either finished before the task (its row is linked to the task) or starts after
it (and finds the task). ``owned_quantity`` is the full confirmed entry fill; the
executor subtracts the fills of the task's closes and the entry's stops itself. A busy
lock raises ``OrderLockBusyError`` and writes no task: the caller retries.

``ProbeRecoveryWorker`` runs the recovery once per start, before anything of the probe
runs, and retries until it succeeds. It is critical: while it has not succeeded, the
worker gate refuses entries.
"""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import psycopg
import structlog
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from . import emergency_close, live_probe, notify
from .order_lock import OrderLockLease

if TYPE_CHECKING:
    from collections.abc import Callable

log = structlog.get_logger()

RECOVERY_REASON = "execution service start"
INCIDENT_ACCOUNTED_OPEN = "probe_task_accounted_position_open"

# Every reservation that may hold a position, with its exact entry attempt: the linked
# one, or before ``link_attempt`` the entry attempt of its own clientOrderId.
_POSITIONS = """
SELECT r.id AS reservation_id, r.probe_id, r.state, r.client_order_id,
       r.native_market_id AS reserved_market, r.side AS reserved_side,
       r.attempt_id AS linked_attempt_id, r.trade_id AS linked_trade_id,
       a.exchange AS reserved_exchange,
       o.id AS attempt_id, o.status AS attempt_status, o.exchange, o.base, o.symbol,
       o.native_market_id, o.market_type, o.side, o.filled_amount,
       o.trade_id AS attempt_trade_id
FROM app.live_probe_reservations AS r
JOIN app.live_probe_authorizations AS a ON a.probe_id = r.probe_id
LEFT JOIN app.live_order_attempts AS o
  ON o.id = r.attempt_id
  OR (r.attempt_id IS NULL AND o.operation = 'entry' AND o.client_order_id = r.client_order_id)
WHERE r.probe_id = ANY(%s) AND r.state IN ('sending', 'placed')
ORDER BY r.id
FOR UPDATE OF r
"""

_EXISTING_TASK = """
SELECT id, reason, status FROM app.live_emergency_closes
WHERE source_attempt_id = %s
   OR (exchange = %s AND native_market_id = %s AND market_type = %s AND side = %s
       AND status IN ('requested', 'sending', 'unknown', 'manual_required'))
ORDER BY id
LIMIT 1
"""

_INSERT_INCIDENT = """
INSERT INTO app.live_reconciliation_incidents (
    incident_key, exchange, symbol, native_market_id, market_type, side,
    contracts, discrepancy_type, status, evidence_json
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'manual_required', %s)
ON CONFLICT (incident_key) DO NOTHING
"""

_MARK_L_REACHED = """
UPDATE app.live_probe_authorizations
SET l_reached_at = clock_timestamp(), l_reached_loss_usd = %s, l_reached_inputs = %s
WHERE probe_id = %s AND l_reached_at IS NULL
"""

_LOCK_RECORDS = "SELECT probe_id FROM app.live_probe_authorizations ORDER BY probe_id FOR UPDATE"


class OrderLockBusyError(RuntimeError):
    """Another order operation holds the instrument's order lock; no task was written."""


@dataclass(frozen=True)
class CloseOutcome:
    probe_id: str
    reservation_id: int | None
    task_id: int | None
    created: bool
    # Why no task was created for a position, for the alert.
    note: str | None = None
    # The position's outcome is not final yet: the recovery is not done.
    pending: bool = False


@dataclass
class _Leases:
    """The order locks a transaction holds until it ends, one per instrument."""

    rdb: Any
    stack: AsyncExitStack
    keys: set[str] = field(default_factory=set)

    async def take(self, exchange: str, base: str) -> None:
        key = f"lock:order:{exchange}:{base.upper()}"
        if key in self.keys:
            return
        lease = await OrderLockLease.acquire(rdb=self.rdb, key=key, operation="probe_close")
        if lease is None:
            raise OrderLockBusyError(f"order lock {key} is busy")
        await self.stack.enter_async_context(lease)
        self.keys.add(key)


async def _close_positions(
    cursor: psycopg.AsyncCursor[dict[str, Any]],
    leases: _Leases,
    probe_ids: list[str],
    *,
    reason: str,
    at_start: bool,
) -> list[CloseOutcome]:
    """A task of ``reason`` for each open probe position of ``probe_ids``, inside the
    caller's transaction. A position that already has a task keeps it."""
    await cursor.execute(_POSITIONS, (probe_ids,))
    outcomes = []
    for row in await cursor.fetchall():
        outcome = await _close_one(cursor, leases, row, reason=reason, at_start=at_start)
        if outcome is not None:
            outcomes.append(outcome)
    return outcomes


async def _close_one(
    cursor: psycopg.AsyncCursor[dict[str, Any]],
    leases: _Leases,
    row: dict[str, Any],
    *,
    reason: str,
    at_start: bool,
) -> CloseOutcome | None:
    """The close of one reservation's position, or None when it holds no position."""
    probe_id, reservation_id = row["probe_id"], row["reservation_id"]

    def note(text: str, *, pending: bool = False) -> CloseOutcome:
        return CloseOutcome(probe_id, reservation_id, None, False, text, pending)

    attempt_id = row["attempt_id"]
    if attempt_id is None:
        if row["state"] == live_probe.STATE_SENDING and at_start:
            # The attempt row is written before any send, so nothing was sent.
            await live_probe.mark_released_in(
                cursor, reservation_id, reason="no entry attempt was written before the start"
            )
            return note(f"reservation {reservation_id} released: no entry attempt was written")
        return note(f"reservation {reservation_id} has no entry attempt yet", pending=True)
    if (row["exchange"], row["native_market_id"], row["side"]) != (
        row["reserved_exchange"],
        row["reserved_market"],
        row["reserved_side"],
    ):
        return note(
            f"entry attempt {attempt_id} is not the entry of reservation {reservation_id}",
            pending=True,
        )
    status = row["attempt_status"]
    filled = Decimal(row["filled_amount"]) if row["filled_amount"] is not None else None
    if status in ("failed", "no_fill"):
        if row["state"] == live_probe.STATE_SENDING:
            if at_start:
                await live_probe.mark_released_in(
                    cursor, reservation_id, reason=f"entry attempt {attempt_id} is {status}"
                )
            return None
        return note(f"placed reservation {reservation_id}: entry attempt {status}", pending=True)
    if status != "completed" or filled is None or filled <= 0:
        return note(f"entry attempt {attempt_id} is {status}, its fill is not final", pending=True)

    # A confirmed fill. The lock first: an entry still in flight holds it.
    await leases.take(row["exchange"], row["base"])
    if row["linked_attempt_id"] is None:
        await live_probe.link_attempt_in(cursor, reservation_id, attempt_id)
    if row["state"] == live_probe.STATE_SENDING:
        await live_probe.mark_placed_in(cursor, reservation_id)
    trade_id = row["linked_trade_id"]
    if row["attempt_trade_id"] is not None and trade_id is None:
        trade_id = row["attempt_trade_id"]
        await live_probe.link_trade_in(cursor, reservation_id, trade_id)
    elif row["attempt_trade_id"] != trade_id:
        return note(
            f"reservation {reservation_id} links trade {trade_id}, its attempt "
            f"{row['attempt_trade_id']}",
            pending=True,
        )
    if trade_id is not None:
        await cursor.execute("SELECT status FROM app.trades WHERE id = %s", (trade_id,))
        trade = await cursor.fetchone()
        if trade is not None and trade["status"] == "closed":
            return None

    await cursor.execute(
        _EXISTING_TASK,
        (attempt_id, row["exchange"], row["native_market_id"], row["market_type"], row["side"]),
    )
    existing = await cursor.fetchone()
    if existing is not None:
        held = f"task {existing['id']} ({existing['reason']}, {existing['status']})"
        if existing["status"] == emergency_close.STATUS_ACCOUNTED:
            await _accounted_incident(cursor, row, int(existing["id"]), trade_id, filled)
            held += " is accounted while the journal shows the position open; incident opened"
        else:
            held += " already holds the position"
        return CloseOutcome(probe_id, reservation_id, int(existing["id"]), False, held)
    task_id, created = await emergency_close.create_task_in(
        cursor,
        emergency_close.TaskSpec(
            source_attempt_id=int(attempt_id),
            entry_client_order_id=row["client_order_id"],
            exchange=row["exchange"],
            base=row["base"],
            symbol=row["symbol"],
            native_market_id=row["native_market_id"],
            market_type=row["market_type"],
            side=row["side"],
            owned_quantity=filled,
            reason=reason,
            trade_id=trade_id,
        ),
        link_trade_closes=True,
    )
    return CloseOutcome(probe_id, reservation_id, task_id, created)


async def _accounted_incident(
    cursor: psycopg.AsyncCursor[dict[str, Any]],
    row: dict[str, Any],
    task_id: int,
    trade_id: int | None,
    filled: Decimal,
) -> None:
    """A permanent incident for the operator: the task that held the position is
    accounted, yet the journal still shows the position open."""
    key = hashlib.sha256(f"{INCIDENT_ACCOUNTED_OPEN}\x1f{task_id}".encode()).hexdigest()
    await cursor.execute(
        _INSERT_INCIDENT,
        (
            key,
            row["exchange"],
            row["symbol"],
            row["native_market_id"],
            row["market_type"],
            row["side"],
            filled,
            INCIDENT_ACCOUNTED_OPEN,
            Jsonb(
                {
                    "task_id": task_id,
                    "probe_id": row["probe_id"],
                    "reservation_id": row["reservation_id"],
                    "trade_id": trade_id,
                }
            ),
        ),
    )


async def _close_in_transaction(
    db_url: str, rdb: Any, probe_id: str | None, *, reason: str, at_start: bool
) -> list[CloseOutcome]:
    """Lock the records (one probe, or every one in probe_id order), then close their
    positions under the order locks, which are released after the commit."""
    async with AsyncExitStack() as stack:
        leases = _Leases(rdb=rdb, stack=stack)
        async with (
            await psycopg.AsyncConnection.connect(db_url, row_factory=dict_row) as conn,
            conn.transaction(),
            conn.cursor() as cursor,
        ):
            if probe_id is not None:
                if await live_probe.lock_record(cursor, probe_id) is None:
                    raise LookupError(f"no authorization record {probe_id!r}")
                probe_ids = [probe_id]
            else:
                # A revoked record may still have an open position.
                await cursor.execute(_LOCK_RECORDS)
                probe_ids = [r["probe_id"] for r in await cursor.fetchall()]
            return await _close_positions(
                cursor, leases, probe_ids, reason=reason, at_start=at_start
            )
    raise RuntimeError("probe close transaction exited without a result")


@dataclass(frozen=True)
class LReached:
    recorded: bool
    closes: list[CloseOutcome]


async def record_l_reached(
    db_url: str, rdb: Any, probe_id: str, *, loss_usd: Decimal, inputs: dict[str, Any]
) -> LReached:
    """``l_reached_at`` with the loss and its inputs, then the close task of the open
    probe position. ``recorded`` is False when L was already reached; the close is
    still ensured. ``OrderLockBusyError`` leaves L recorded and no task: call again."""
    async with (
        await psycopg.AsyncConnection.connect(db_url, row_factory=dict_row) as conn,
        conn.transaction(),
        conn.cursor() as cursor,
    ):
        if await live_probe.lock_record(cursor, probe_id) is None:
            raise LookupError(f"no authorization record {probe_id!r}")
        await cursor.execute(_MARK_L_REACHED, (loss_usd, Jsonb(inputs), probe_id))
        recorded = cursor.rowcount == 1
        if recorded:
            await cursor.execute(
                "INSERT INTO app.live_probe_authorization_events (probe_id, event, actor, detail)"
                " VALUES (%s, 'l_reached', %s, %s)",
                (probe_id, live_probe.SERVICE_ACTOR, Jsonb({"loss_usd": str(loss_usd), **inputs})),
            )
    if recorded:
        log.warning("live_probe.l_reached", probe_id=probe_id, loss_usd=str(loss_usd))
    closes = await _close_in_transaction(
        db_url,
        rdb,
        probe_id,
        reason=emergency_close.REASON_PROBE_LOSS_THRESHOLD,
        at_start=False,
    )
    log.warning(
        "live_probe.l_reached_close",
        probe_id=probe_id,
        recorded=recorded,
        tasks=[c.task_id for c in closes],
    )
    return LReached(recorded=recorded, closes=closes)


@dataclass(frozen=True)
class Recovery:
    suspended: list[str]
    closes: list[CloseOutcome]
    close_error: str | None = None

    @property
    def pending(self) -> list[CloseOutcome]:
        return [c for c in self.closes if c.pending]


async def recover_at_start(db_url: str, rdb: Any) -> Recovery:
    """Suspend every active record, then close every probe position open in the
    journal (section 5). A suspension commits even when the close step fails; the
    failure is returned in ``close_error`` with the suspensions, so a retry, which
    suspends nothing more, does not lose their alert."""
    async with (
        await psycopg.AsyncConnection.connect(db_url, row_factory=dict_row) as conn,
        conn.transaction(),
        conn.cursor() as cursor,
    ):
        await cursor.execute(_LOCK_RECORDS)
        suspended = await live_probe.suspend_active_in(cursor, reason=RECOVERY_REASON)
    try:
        closes = await _close_in_transaction(
            db_url, rdb, None, reason=emergency_close.REASON_PROBE_RECOVERY, at_start=True
        )
    except Exception as exc:
        log.error("live_probe.recovery_close_failed", suspended=suspended, err=str(exc))
        return Recovery(suspended=suspended, closes=[], close_error=str(exc))
    return Recovery(suspended=suspended, closes=closes)


class ProbeRecoveryWorker:
    """Runs ``recover_at_start`` once per start, retrying until it succeeds with no
    pending position, then stays healthy. ``recovered`` is set once it has; the
    probe's evaluator and entries wait for it."""

    def __init__(
        self,
        db_url: str,
        rdb: Any,
        *,
        alert_credentials: tuple[str, str] | None,
        interval_seconds: float = 5.0,
        recover: Callable[[str, Any], Any] = recover_at_start,
    ) -> None:
        self.db_url = db_url
        self.rdb = rdb
        self.alert_credentials = alert_credentials
        self.interval_seconds = interval_seconds
        self._recover = recover
        self._alerted: set[str] = set()
        self.recovered = asyncio.Event()

    async def run_once(self) -> bool:
        if self.recovered.is_set():
            return True
        try:
            recovery: Recovery = await self._recover(self.db_url, self.rdb)
        except Exception as exc:
            log.error("live_probe.recovery_failed", err=str(exc))
            return False
        # Each text once per start: a pending position is retried every interval.
        for text in recovery_alerts(recovery):
            if text not in self._alerted:
                self._alerted.add(text)
                await self._alert(text)
        if recovery.close_error is not None:
            return False
        if recovery.pending:
            log.error(
                "live_probe.recovery_pending",
                pending=[c.note for c in recovery.pending],
            )
            return False
        self.recovered.set()
        log.info(
            "live_probe.recovered",
            suspended=recovery.suspended,
            tasks=[c.task_id for c in recovery.closes],
        )
        return True

    async def __call__(self, tracker: Any) -> None:
        log.info("live_probe_recovery.started")
        while True:
            tracker.tick_started()
            if await self.run_once():
                tracker.tick_succeeded()
            else:
                tracker.tick_failed(RuntimeError("live probe recovery failed"))
            await asyncio.sleep(self.interval_seconds)

    async def _alert(self, text: str) -> None:
        if self.alert_credentials is None:
            return
        delivered = False
        try:
            delivered = await notify.notify_alert(*self.alert_credentials, text=text)
        except Exception as exc:
            log.error("live_probe.alert_delivery_failed", err=str(exc))
        if not delivered:
            log.error("live_probe.alert_not_delivered", text=text)


def recovery_alerts(recovery: Recovery) -> list[str]:
    """One alert per record: suspended, and whether a recovery close was created.
    A failed close step adds one alert of its own, the same text on every retry."""
    probes = sorted(set(recovery.suspended) | {c.probe_id for c in recovery.closes})
    alerts = []
    for probe_id in probes:
        parts = []
        if probe_id in recovery.suspended:
            parts.append("suspended until the owner resumes it with the probe CLI")
        for close in (c for c in recovery.closes if c.probe_id == probe_id):
            if close.created:
                parts.append(f"recovery close task {close.task_id} created")
            elif close.pending:
                parts.append(f"recovery waits: {close.note}")
            else:
                parts.append(f"no recovery close: {close.note}")
        alerts.append(f"LIVE_PROBE {probe_id} after a service start: " + "; ".join(parts) + ".")
    if recovery.close_error is not None:
        alerts.append(
            "LIVE_PROBE recovery after a service start could not check the open probe "
            "positions yet; retrying. Entries stay refused."
        )
    return alerts
