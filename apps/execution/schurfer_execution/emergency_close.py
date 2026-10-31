"""Durable emergency close tasks: flatten a live position we own, exactly once.

docs/engineering/live-entry-failure-paths-design-v1.md, section 1. A task is written
before any close is sent. Each pass, under the instrument's order lock and the task's
database claim, follows the previous close order before it may send another:

  * unknown (written or timed out, not yet found): look it up, no new send;
  * active on the book: follow it, no new send;
  * terminal: count its fill; a next send is allowed.

Before any send the exchange position must equal the owned remainder: the owned
quantity minus every confirmed fill of this task's closes and of the entry's
protective stops. Anything else moves the task to ``manual_required``. The task is
``confirmed_flat`` only when the exchange shows no position, no close is unknown or
active, and no protective stop of the entry rests on the book.

The executor needs the database. Every store failure raises, so the pass fails and
the supervised worker retries it. Once a task is confirmed flat, the separate
accounting worker in emergency_accounting.py journals its round trip.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

import ccxt
import psycopg
import structlog
from psycopg.rows import dict_row

from . import exchange_orders, notify, order_ids
from .order_lock import OrderLockLease

if TYPE_CHECKING:
    from collections.abc import Callable

log = structlog.get_logger()

STATUS_REQUESTED = "requested"
STATUS_SENDING = "sending"
STATUS_UNKNOWN = "unknown"
STATUS_CONFIRMED_FLAT = "confirmed_flat"
STATUS_ACCOUNTED = "accounted"
STATUS_MANUAL_REQUIRED = "manual_required"

REASON_STOP_PLACEMENT_FAILED = "stop_placement_failed"
REASON_LATE_FILL_FLATTENED = "late_fill_flattened"

# The executor works on these; manual_required waits for an operator.
RUNNABLE_STATUSES = (STATUS_REQUESTED, STATUS_SENDING, STATUS_UNKNOWN)
# These hold the identity: no second task, and no monitor close, while one exists.
ACTIVE_STATUSES = (*RUNNABLE_STATUSES, STATUS_MANUAL_REQUIRED)

INCIDENT_DISCREPANCY = "emergency_close_pending"

# Close order rows, by their live_order_attempts status.
_CLOSE_UNKNOWN = frozenset({"pending", "submission_unknown"})
_CLOSE_ACTIVE = "accepted"
_CLOSE_TERMINAL_FILL = "completed"
_CLOSE_MANUAL = "manual_required"

_TOLERANCE = Decimal("0.00000001")


@dataclass(frozen=True)
class TaskSpec:
    source_attempt_id: int
    entry_client_order_id: str
    exchange: str
    base: str
    symbol: str
    native_market_id: str
    market_type: str
    side: str
    owned_quantity: Decimal
    reason: str
    trade_id: int | None = None


@dataclass(frozen=True)
class AdoptedClose:
    """A close order found on the exchange, linked to its task when the task is
    built after a database outage."""

    client_order_id: str
    status: str
    order_id: str | None
    requested_amount: Decimal | None
    filled_amount: Decimal | None


@dataclass(frozen=True)
class EmergencyCloseTask:
    id: int
    source_attempt_id: int
    entry_client_order_id: str
    exchange: str
    base: str
    symbol: str
    native_market_id: str
    market_type: str
    side: str
    trade_id: int | None
    owned_quantity: Decimal
    owned_remainder: Decimal
    reason: str
    status: str
    rejected_sends: int
    confirmed_flat_at: datetime | None = None


@dataclass(frozen=True)
class CloseOrder:
    attempt_id: int
    client_order_id: str
    status: str
    order_id: str | None
    requested_amount: Decimal | None
    filled_amount: Decimal | None
    created_at: datetime


@dataclass(frozen=True)
class RecoveryCandidate:
    """An entry attempt with no task and no journalled trade, which may have a
    close sent while the database was down."""

    attempt_id: int
    entry_client_order_id: str
    exchange: str
    base: str
    symbol: str
    native_market_id: str
    market_type: str
    side: str


class TaskConflictError(RuntimeError):
    """Another active task already holds the exact identity."""


class EmergencyCloseStore(Protocol):
    async def create_task(
        self, spec: TaskSpec, adopted: tuple[AdoptedClose, ...] = ()
    ) -> tuple[int, bool]: ...
    async def runnable_task_ids(self) -> list[int]: ...
    async def claim(
        self, task_id: int, owner: str, lease_seconds: float
    ) -> EmergencyCloseTask | None: ...
    async def load_closes(self, task_id: int) -> list[CloseOrder]: ...
    async def record_close_intent(
        self,
        task: EmergencyCloseTask,
        *,
        client_order_id: str,
        amount: Decimal,
        size_usd: Decimal,
        contract_size: Decimal,
    ) -> int: ...
    async def mark_close(
        self,
        attempt_id: int,
        *,
        status: str,
        order_id: str | None,
        filled_amount: Decimal | None,
        error: str | None,
    ) -> None: ...
    async def update_task(
        self,
        task: EmergencyCloseTask,
        owner: str,
        *,
        status: str,
        owned_remainder: Decimal,
        rejected_sends: int,
        last_error: str | None,
        release: bool,
    ) -> None: ...
    async def recovery_candidates(self) -> list[RecoveryCandidate]: ...


def _same(left: Decimal, right: Decimal) -> bool:
    return abs(left - right) <= _TOLERANCE


def close_side(position_side: str) -> str:
    return "buy" if position_side == "short" else "sell"


def incident_key(task_id: int) -> str:
    return hashlib.sha256(f"{INCIDENT_DISCREPANCY}\x1f{task_id}".encode()).hexdigest()


_INSERT_TASK = """
INSERT INTO app.live_emergency_closes (
    source_attempt_id, entry_client_order_id, exchange, base, symbol, native_market_id,
    market_type, side, trade_id, owned_quantity, owned_remainder, reason, status
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'requested')
ON CONFLICT (source_attempt_id) DO NOTHING
RETURNING id
"""

_INSERT_INCIDENT = """
INSERT INTO app.live_reconciliation_incidents (
    incident_key, exchange, symbol, native_market_id, market_type, side,
    contracts, discrepancy_type, status, evidence_json
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'manual_required', %s::jsonb)
ON CONFLICT (incident_key) DO NOTHING
"""

_CLAIM = """
UPDATE app.live_emergency_closes
SET claim_owner = %s,
    claim_expires_at = now() + make_interval(secs => %s),
    updated_at = now()
WHERE id = %s
  AND status IN ('requested', 'sending', 'unknown')
  AND (claim_owner IS NULL OR claim_owner = %s OR claim_expires_at < now())
RETURNING id, source_attempt_id, entry_client_order_id, exchange, base, symbol,
    native_market_id, market_type, side, trade_id, owned_quantity, owned_remainder,
    reason, status, rejected_sends
"""

# A pass that ends releases its claim, so the next pass can run in any
# executor at once; only the write before the exchange call keeps it.
_UPDATE_TASK = """
UPDATE app.live_emergency_closes
SET status = %s,
    owned_remainder = %s,
    rejected_sends = %s,
    last_error = %s,
    -- Set once: the accounting deadline counts from the first confirmation.
    confirmed_flat_at = CASE
        WHEN %s = 'confirmed_flat' THEN COALESCE(confirmed_flat_at, now())
        ELSE confirmed_flat_at
    END,
    claim_owner = CASE WHEN %s THEN NULL ELSE claim_owner END,
    claim_expires_at = CASE WHEN %s THEN NULL ELSE claim_expires_at END,
    updated_at = now()
WHERE id = %s AND claim_owner = %s
"""

_INSERT_CLOSE = """
INSERT INTO app.live_order_attempts (
    operation, client_order_id, exchange, base, symbol, native_market_id, market_type,
    side, size_usd, requested_amount, leverage, contract_size, exit_params,
    setup_context, status, trade_id, task_id
) VALUES (
    'close', %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s, '{}'::jsonb, %s::jsonb,
    'pending', %s, %s
)
RETURNING id
"""

_MARK_CLOSE = """
UPDATE app.live_order_attempts
SET status = %s,
    order_id = COALESCE(%s, order_id),
    filled_amount = COALESCE(%s, filled_amount),
    last_error = COALESCE(%s, last_error),
    updated_at = now()
WHERE id = %s AND task_id IS NOT NULL
"""

_SELECT_CLOSES = """
SELECT id, client_order_id, status, order_id, requested_amount, filled_amount, created_at
FROM app.live_order_attempts
WHERE task_id = %s
ORDER BY id
"""

# Entry attempts the database-down path may have closed without a task: derived
# ids only, no trade, recent, identity complete, and no task yet.
_RECOVERY_CANDIDATES = """
SELECT a.id, a.client_order_id, a.exchange, a.base, a.symbol, a.native_market_id,
    a.market_type, a.side
FROM app.live_order_attempts AS a
WHERE a.operation = 'entry'
  AND a.client_order_id ~ '^[0-9a-f]{32}$'
  AND a.trade_id IS NULL
  AND a.native_market_id IS NOT NULL
  AND a.market_type IS NOT NULL
  AND a.side IN ('long', 'short')
  AND a.status IN ('pending', 'accepted', 'submission_unknown', 'completed')
  AND a.created_at > now() - interval '7 days'
  AND NOT EXISTS (
      SELECT 1 FROM app.live_emergency_closes AS e WHERE e.source_attempt_id = a.id
  )
ORDER BY a.id
"""

_ADOPT_CLOSE = """
INSERT INTO app.live_order_attempts (
    operation, client_order_id, exchange, base, symbol, native_market_id, market_type,
    side, size_usd, requested_amount, filled_amount, leverage, exit_params,
    setup_context, status, order_id, task_id
) VALUES (
    'close', %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, 1, '{}'::jsonb, %s::jsonb, %s, %s, %s
)
"""

_ACTIVE_TASK_FOR_INSTRUMENT = """
SELECT 1 FROM app.live_emergency_closes
WHERE exchange = %s AND base = upper(%s)
  AND status IN ('requested', 'sending', 'unknown', 'manual_required')
LIMIT 1
"""


def task_from_row(row: dict[str, Any]) -> EmergencyCloseTask:
    return EmergencyCloseTask(
        id=int(row["id"]),
        source_attempt_id=int(row["source_attempt_id"]),
        entry_client_order_id=str(row["entry_client_order_id"]),
        exchange=str(row["exchange"]),
        base=str(row["base"]),
        symbol=str(row["symbol"]),
        native_market_id=str(row["native_market_id"]),
        market_type=str(row["market_type"]),
        side=str(row["side"]),
        trade_id=int(row["trade_id"]) if row["trade_id"] is not None else None,
        owned_quantity=Decimal(row["owned_quantity"]),
        owned_remainder=Decimal(row["owned_remainder"]),
        reason=str(row["reason"]),
        status=str(row["status"]),
        rejected_sends=int(row["rejected_sends"]),
        confirmed_flat_at=row.get("confirmed_flat_at"),
    )


class PostgresEmergencyCloseStore:
    def __init__(self, db_url: str) -> None:
        self.db_url = db_url

    async def create_task(
        self, spec: TaskSpec, adopted: tuple[AdoptedClose, ...] = ()
    ) -> tuple[int, bool]:
        """Write the task, its blocking incident and any adopted closes in one
        transaction, so a task is never runnable without the closes it owns.

        Returns ``(task_id, created)``; an existing task for the same source attempt
        is returned, not duplicated. Raises ``TaskConflictError`` when another
        source attempt's task holds the identity.
        """
        if not order_ids.is_entry_client_order_id(spec.entry_client_order_id):
            raise ValueError("emergency close needs a derived-id entry clientOrderId")
        if spec.side not in {"long", "short"} or spec.owned_quantity <= 0:
            raise ValueError("emergency close needs a side and a positive owned quantity")
        try:
            async with (
                await psycopg.AsyncConnection.connect(self.db_url) as connection,
                connection.cursor() as cursor,
            ):
                await cursor.execute(
                    _INSERT_TASK,
                    (
                        spec.source_attempt_id,
                        spec.entry_client_order_id,
                        spec.exchange,
                        spec.base.upper(),
                        spec.symbol,
                        spec.native_market_id,
                        spec.market_type,
                        spec.side,
                        spec.trade_id,
                        spec.owned_quantity,
                        spec.owned_quantity,
                        spec.reason,
                    ),
                )
                row = await cursor.fetchone()
                if row is None:
                    await cursor.execute(
                        "SELECT id FROM app.live_emergency_closes WHERE source_attempt_id = %s",
                        (spec.source_attempt_id,),
                    )
                    existing = await cursor.fetchone()
                    if existing is None:
                        raise RuntimeError("emergency close task vanished on conflict")
                    return int(existing[0]), False
                task_id = int(row[0])
                await cursor.execute(
                    _INSERT_INCIDENT,
                    (
                        incident_key(task_id),
                        spec.exchange,
                        spec.symbol,
                        spec.native_market_id,
                        spec.market_type,
                        spec.side,
                        spec.owned_quantity,
                        INCIDENT_DISCREPANCY,
                        json.dumps(
                            {
                                "task_id": task_id,
                                "source_attempt_id": spec.source_attempt_id,
                                "reason": spec.reason,
                            }
                        ),
                    ),
                )
                for close in adopted:
                    await cursor.execute(
                        _ADOPT_CLOSE,
                        (
                            close.client_order_id,
                            spec.exchange,
                            spec.base.upper(),
                            spec.symbol,
                            spec.native_market_id,
                            spec.market_type,
                            spec.side,
                            close.requested_amount,
                            close.filled_amount,
                            json.dumps({"emergency_close_task_id": task_id, "adopted": True}),
                            close.status,
                            close.order_id,
                            task_id,
                        ),
                    )
                return task_id, True
        except psycopg.errors.UniqueViolation as exc:
            raise TaskConflictError(str(exc)) from exc

    async def runnable_task_ids(self) -> list[int]:
        async with (
            await psycopg.AsyncConnection.connect(self.db_url) as connection,
            connection.cursor() as cursor,
        ):
            await cursor.execute(
                "SELECT id FROM app.live_emergency_closes WHERE status = ANY(%s) ORDER BY id",
                (list(RUNNABLE_STATUSES),),
            )
            return [int(row[0]) for row in await cursor.fetchall()]

    async def claim(
        self, task_id: int, owner: str, lease_seconds: float
    ) -> EmergencyCloseTask | None:
        async with (
            await psycopg.AsyncConnection.connect(self.db_url) as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(_CLAIM, (owner, lease_seconds, task_id, owner))
            row = await cursor.fetchone()
        return task_from_row(row) if row is not None else None

    async def load_closes(self, task_id: int) -> list[CloseOrder]:
        async with (
            await psycopg.AsyncConnection.connect(self.db_url) as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(_SELECT_CLOSES, (task_id,))
            rows = await cursor.fetchall()
        return [
            CloseOrder(
                attempt_id=int(row["id"]),
                client_order_id=str(row["client_order_id"]),
                status=str(row["status"]),
                order_id=str(row["order_id"]) if row["order_id"] is not None else None,
                requested_amount=exchange_orders.decimal_or_none(row["requested_amount"]),
                filled_amount=exchange_orders.decimal_or_none(row["filled_amount"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def record_close_intent(
        self,
        task: EmergencyCloseTask,
        *,
        client_order_id: str,
        amount: Decimal,
        size_usd: Decimal,
        contract_size: Decimal,
    ) -> int:
        async with (
            await psycopg.AsyncConnection.connect(self.db_url) as connection,
            connection.cursor() as cursor,
        ):
            await cursor.execute(
                _INSERT_CLOSE,
                (
                    client_order_id,
                    task.exchange,
                    task.base,
                    task.symbol,
                    task.native_market_id,
                    task.market_type,
                    task.side,
                    size_usd,
                    amount,
                    contract_size,
                    json.dumps({"emergency_close_task_id": task.id, "reason": task.reason}),
                    task.trade_id,
                    task.id,
                ),
            )
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("close intent insert returned no row")
        return int(row[0])

    async def mark_close(
        self,
        attempt_id: int,
        *,
        status: str,
        order_id: str | None,
        filled_amount: Decimal | None,
        error: str | None,
    ) -> None:
        async with (
            await psycopg.AsyncConnection.connect(self.db_url) as connection,
            connection.cursor() as cursor,
        ):
            await cursor.execute(
                _MARK_CLOSE,
                (status, order_id, filled_amount, error[:1000] if error else None, attempt_id),
            )

    async def update_task(
        self,
        task: EmergencyCloseTask,
        owner: str,
        *,
        status: str,
        owned_remainder: Decimal,
        rejected_sends: int,
        last_error: str | None,
        release: bool,
    ) -> None:
        async with (
            await psycopg.AsyncConnection.connect(self.db_url) as connection,
            connection.cursor() as cursor,
        ):
            await cursor.execute(
                _UPDATE_TASK,
                (
                    status,
                    owned_remainder,
                    rejected_sends,
                    last_error[:1000] if last_error else None,
                    status,
                    release,
                    release,
                    task.id,
                    owner,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"emergency close task {task.id} claim was lost")

    async def recovery_candidates(self) -> list[RecoveryCandidate]:
        async with (
            await psycopg.AsyncConnection.connect(self.db_url) as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(_RECOVERY_CANDIDATES)
            rows = await cursor.fetchall()
        return [
            RecoveryCandidate(
                attempt_id=int(row["id"]),
                entry_client_order_id=str(row["client_order_id"]),
                exchange=str(row["exchange"]),
                base=str(row["base"]),
                symbol=str(row["symbol"]),
                native_market_id=str(row["native_market_id"]),
                market_type=str(row["market_type"]),
                side=str(row["side"]),
            )
            for row in rows
        ]


async def has_active_task(db_url: str, *, exchange: str, base: str) -> bool:
    async with (
        await psycopg.AsyncConnection.connect(db_url) as connection,
        connection.cursor() as cursor,
    ):
        await cursor.execute(_ACTIVE_TASK_FOR_INSTRUMENT, (exchange, base))
        return await cursor.fetchone() is not None


@dataclass(frozen=True)
class _Position:
    contracts: Decimal
    opposite_side: bool
    mark_price: Decimal | None


class EmergencyCloseExecutor:
    def __init__(
        self,
        exchanges: dict[str, Any],
        store: EmergencyCloseStore,
        rdb: Any,
        *,
        alert_credentials: tuple[str, str] | None = None,
        owner: str | None = None,
        claim_seconds: float = 60.0,
        unknown_deadline_seconds: float = 60.0,
        max_rejected_sends: int = 3,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.exchanges = exchanges
        self.store = store
        self.rdb = rdb
        self.alert_credentials = alert_credentials
        self.owner = owner or f"emergency-close:{uuid.uuid4().hex[:16]}"
        self.claim_seconds = claim_seconds
        self.unknown_deadline = timedelta(seconds=unknown_deadline_seconds)
        self.max_rejected_sends = max_rejected_sends
        self._now = now or (lambda: datetime.now(tz=UTC))

    async def run_pass(self, task_id: int, *, held_lease: OrderLockLease | None = None) -> str:
        """One pass over the task. ``held_lease`` is the order lock of an inline
        caller that already holds it; otherwise the pass takes the lock itself.

        Returns the task status after the pass, or ``lock_busy`` /
        ``claimed_elsewhere`` when the pass did not run.
        """
        task = await self.store.claim(task_id, self.owner, self.claim_seconds)
        if task is None:
            return "claimed_elsewhere"
        if held_lease is not None:
            return await self._pass(task)
        lease = await OrderLockLease.acquire(
            rdb=self.rdb,
            key=f"lock:order:{task.exchange}:{task.base.upper()}",
            operation="emergency_close",
        )
        if lease is None:
            await self._set(task, task.status)
            return "lock_busy"
        async with lease:
            return await self._pass(task)
        raise RuntimeError("emergency close lease exited without a pass result")

    async def _pass(self, task: EmergencyCloseTask) -> str:
        exchange = self.exchanges.get(task.exchange)
        if exchange is None:
            return await self._manual(task, f"exchange {task.exchange!r} is not configured")

        closes = await self._follow_closes(exchange, task)
        if any(close.status == _CLOSE_MANUAL for close in closes):
            return await self._manual(task, "a close order is terminal without a filled amount")

        unknown = [close for close in closes if close.status in _CLOSE_UNKNOWN]
        active = [close for close in closes if close.status == _CLOSE_ACTIVE]
        if unknown:
            oldest = min(close.created_at for close in unknown)
            if self._now() - oldest > self.unknown_deadline:
                return await self._manual(
                    task, f"close order {unknown[0].client_order_id} not found within the deadline"
                )
            return await self._set(task, STATUS_UNKNOWN)
        if active:
            return await self._set(task, STATUS_SENDING)

        stops = await exchange_orders.orders_with_prefix(
            exchange, task.symbol, order_ids.stop_prefix(task.entry_client_order_id)
        )
        stop_filled = sum(
            (
                exchange_orders.decimal_or_none(order.get("filled")) or Decimal(0)
                for order in stops
                if exchange_orders.is_terminal(order)
            ),
            Decimal(0),
        )
        open_stops = [order for order in stops if not exchange_orders.is_terminal(order)]
        if any(
            (exchange_orders.decimal_or_none(order.get("filled")) or Decimal(0)) > 0
            for order in open_stops
        ):
            # A triggered stop is still filling: its final amount is not known,
            # so neither the remainder nor a send can be decided yet.
            return await self._set(task, STATUS_REQUESTED)
        close_filled = sum(
            (
                close.filled_amount or Decimal(0)
                for close in closes
                if close.status == _CLOSE_TERMINAL_FILL
            ),
            Decimal(0),
        )
        remainder = max(Decimal(0), task.owned_quantity - close_filled - stop_filled)
        task = replace(task, owned_remainder=remainder)

        position = await self._position(exchange, task)
        if position.opposite_side:
            return await self._manual(task, "the exchange holds the opposite side")

        if position.contracts <= 0:
            if open_stops:
                for order in open_stops:
                    await exchange.cancel_order(
                        str(order["id"]), task.symbol, params={"trigger": True}
                    )
                # Flat is confirmed on a later pass, once the cancels show as terminal.
                return await self._set(task, STATUS_REQUESTED)
            status = await self._set(task, STATUS_CONFIRMED_FLAT)
            await self._alert(
                f"Emergency close task {task.id} confirmed flat: {task.exchange} "
                f"{task.symbol} {task.side}. Entries stay blocked until it is accounted."
            )
            return status

        if remainder <= 0 or not _same(position.contracts, remainder):
            return await self._manual(
                task,
                f"exchange position {position.contracts} differs from the owned "
                f"remainder {remainder}",
            )
        return await self._send(exchange, task, closes, position)

    async def recover(self, candidate: RecoveryCandidate) -> int | None:
        """Build the task for a close the database-down path sent, from the attempt
        row and the exchange. Returns the task id, or None when there is nothing to
        recover or the entry's fill is not yet final."""
        exchange = self.exchanges.get(candidate.exchange)
        if exchange is None:
            return None
        closes = await exchange_orders.orders_with_prefix(
            exchange, candidate.symbol, order_ids.close_prefix(candidate.entry_client_order_id)
        )
        if not closes:
            return None
        entry = await exchange_orders.find_by_client_order_id(
            exchange, candidate.symbol, candidate.entry_client_order_id
        )
        filled = exchange_orders.decimal_or_none(entry.get("filled")) if entry is not None else None
        if entry is None or not exchange_orders.is_terminal(entry) or not filled:
            log.warning(
                "emergency_close.recovery_waiting_on_entry",
                attempt_id=candidate.attempt_id,
            )
            return None
        adopted = tuple(
            AdoptedClose(
                client_order_id=str(exchange_orders.client_order_id(order)),
                status=exchange_orders.attempt_state_from_order(order)[0],
                order_id=str(order["id"]) if order.get("id") is not None else None,
                requested_amount=exchange_orders.decimal_or_none(order.get("amount")),
                filled_amount=exchange_orders.attempt_state_from_order(order)[1],
            )
            for order in closes
        )
        task_id, created = await self.store.create_task(
            TaskSpec(
                source_attempt_id=candidate.attempt_id,
                entry_client_order_id=candidate.entry_client_order_id,
                exchange=candidate.exchange,
                base=candidate.base,
                symbol=candidate.symbol,
                native_market_id=candidate.native_market_id,
                market_type=candidate.market_type,
                side=candidate.side,
                owned_quantity=filled,
                reason=REASON_STOP_PLACEMENT_FAILED,
            ),
            adopted,
        )
        if created:
            await self._alert(
                f"Emergency close task {task_id} built after a database outage: "
                f"{candidate.exchange} {candidate.symbol} {candidate.side}, "
                f"{len(closes)} close order(s) found on the exchange."
            )
        return task_id

    async def _follow_closes(self, exchange: Any, task: EmergencyCloseTask) -> list[CloseOrder]:
        closes = await self.store.load_closes(task.id)
        followed: list[CloseOrder] = []
        for close in closes:
            if close.status in _CLOSE_UNKNOWN or close.status == _CLOSE_ACTIVE:
                order = await exchange_orders.find_by_client_order_id(
                    exchange, task.symbol, close.client_order_id
                )
                if order is not None:
                    status, filled, error = exchange_orders.attempt_state_from_order(order)
                    order_id = str(order["id"]) if order.get("id") is not None else None
                    await self.store.mark_close(
                        close.attempt_id,
                        status=status,
                        order_id=order_id,
                        filled_amount=filled,
                        error=error,
                    )
                    close = replace(
                        close,
                        status=status,
                        order_id=order_id or close.order_id,
                        filled_amount=filled if filled is not None else close.filled_amount,
                    )
            followed.append(close)
        return followed

    async def _position(self, exchange: Any, task: EmergencyCloseTask) -> _Position:
        contracts = Decimal(0)
        opposite = False
        mark_price: Decimal | None = None
        for position in await exchange.fetch_positions([task.symbol]):
            if position.get("symbol") != task.symbol:
                continue
            size = exchange_orders.decimal_or_none(position.get("contracts")) or Decimal(0)
            if size <= 0:
                continue
            if position.get("side") == task.side:
                contracts += size
                mark_price = exchange_orders.decimal_or_none(position.get("markPrice"))
            else:
                opposite = True
        return _Position(contracts=contracts, opposite_side=opposite, mark_price=mark_price)

    async def _send(
        self,
        exchange: Any,
        task: EmergencyCloseTask,
        closes: list[CloseOrder],
        position: _Position,
    ) -> str:
        amount = Decimal(
            str(exchange.amount_to_precision(task.symbol, float(task.owned_remainder)))
        )
        if amount <= 0:
            return await self._manual(task, f"remainder {task.owned_remainder} rounds to zero")
        market = (exchange.markets or {}).get(task.symbol) or {}
        contract_size = Decimal(str(market.get("contractSize") or 1))
        size_usd = amount * contract_size * (position.mark_price or Decimal(0))
        client_order_id = order_ids.close_client_order_id(
            task.entry_client_order_id, len(closes) + 1
        )
        # Durable before acting: the close row exists before the exchange is called.
        attempt_id = await self.store.record_close_intent(
            task,
            client_order_id=client_order_id,
            amount=amount,
            size_usd=size_usd,
            contract_size=contract_size,
        )
        await self._set(task, STATUS_SENDING, release=False)
        try:
            order = await exchange.create_market_order(
                task.symbol,
                close_side(task.side),
                float(amount),
                params={"reduceOnly": True, "clientOrderId": client_order_id},
            )
        except ccxt.NetworkError as exc:
            return await self._send_unknown(task, attempt_id, exc)
        except ccxt.ExchangeError as exc:
            await self.store.mark_close(
                attempt_id, status="failed", order_id=None, filled_amount=None, error=str(exc)
            )
            rejected = task.rejected_sends + 1
            task = replace(task, rejected_sends=rejected)
            if rejected >= self.max_rejected_sends:
                return await self._manual(task, f"close rejected {rejected} times: {exc}")
            return await self._set(task, STATUS_REQUESTED, error=str(exc))
        except Exception as exc:
            return await self._send_unknown(task, attempt_id, exc)

        status, filled, error = exchange_orders.attempt_state_from_order(order)
        if status != _CLOSE_TERMINAL_FILL:
            # The create response can arrive before the fill is indexed (see
            # fill_price.py): a terminal status with no or zero filled amount
            # here proves nothing, so the order is followed on the next pass.
            status, error = _CLOSE_ACTIVE, None
        order_id = str(order["id"]) if order.get("id") is not None else None
        await self.store.mark_close(
            attempt_id, status=status, order_id=order_id, filled_amount=filled, error=error
        )
        log.info(
            "emergency_close.sent",
            task_id=task.id,
            client_order_id=client_order_id,
            amount=str(amount),
            status=status,
        )
        return await self._set(task, STATUS_SENDING)

    async def _send_unknown(self, task: EmergencyCloseTask, attempt_id: int, exc: Exception) -> str:
        await self.store.mark_close(
            attempt_id,
            status="submission_unknown",
            order_id=None,
            filled_amount=None,
            error=str(exc),
        )
        return await self._set(task, STATUS_UNKNOWN, error=str(exc))

    async def _set(
        self,
        task: EmergencyCloseTask,
        status: str,
        *,
        error: str | None = None,
        release: bool = True,
    ) -> str:
        await self.store.update_task(
            task,
            self.owner,
            status=status,
            owned_remainder=task.owned_remainder,
            rejected_sends=task.rejected_sends,
            last_error=error,
            release=release,
        )
        return status

    async def _manual(self, task: EmergencyCloseTask, reason: str) -> str:
        log.error("emergency_close.manual_required", task_id=task.id, reason=reason)
        status = await self._set(task, STATUS_MANUAL_REQUIRED, error=reason)
        await self._alert(
            f"Emergency close task {task.id} needs an operator: {task.exchange} "
            f"{task.symbol} {task.side}. {reason}. Nothing more is sent."
        )
        return status

    async def _alert(self, text: str) -> None:
        if self.alert_credentials is None:
            return
        delivered = False
        try:
            delivered = await notify.notify_alert(*self.alert_credentials, text=text)
        except Exception as exc:
            log.error("emergency_close.alert_delivery_failed", err=str(exc))
        if not delivered:
            log.error("emergency_close.alert_not_delivered", text=text)


class EmergencyCloseWorker:
    """Supervised loop over runnable tasks, about every two seconds."""

    def __init__(
        self,
        executor: EmergencyCloseExecutor,
        store: EmergencyCloseStore,
        *,
        interval_seconds: float = 2.0,
        recovery_every: int = 15,
    ) -> None:
        self.executor = executor
        self.store = store
        self.interval_seconds = interval_seconds
        self.recovery_every = recovery_every
        self._runs = 0

    async def recover_once(self) -> bool:
        succeeded = True
        for candidate in await self.store.recovery_candidates():
            try:
                await self.executor.recover(candidate)
            except Exception as exc:
                succeeded = False
                log.error(
                    "emergency_close.recovery_failed",
                    attempt_id=candidate.attempt_id,
                    err=str(exc),
                )
        return succeeded

    async def run_once(self) -> bool:
        succeeded = True
        # About every 30 s, look for closes the database-down path sent.
        if self._runs % self.recovery_every == 0:
            succeeded = await self.recover_once()
        self._runs += 1
        for task_id in await self.store.runnable_task_ids():
            try:
                await self.executor.run_pass(task_id)
            except Exception as exc:
                succeeded = False
                log.error("emergency_close.pass_failed", task_id=task_id, err=str(exc))
        return succeeded

    async def __call__(self, tracker: Any) -> None:
        log.info("emergency_close_worker.started")
        while True:
            tracker.tick_started()
            try:
                succeeded = await self.run_once()
            except Exception as exc:
                log.error("emergency_close.scan_failed", err=str(exc))
                succeeded = False
            if succeeded:
                tracker.tick_succeeded()
            else:
                tracker.tick_failed(RuntimeError("emergency close pass failed"))
            await asyncio.sleep(self.interval_seconds)
