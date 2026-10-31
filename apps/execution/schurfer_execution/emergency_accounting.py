"""Journal the round trip of an emergency close task once it is confirmed flat.

docs/engineering/live-entry-failure-paths-design-v1.md, section 1 (``accounted``) and
section 2, item 4. Accounting is never a precondition of the close: it starts only
after the executor has confirmed the position flat, it sends nothing, and it runs in
its own supervised worker, so a slow exchange or database here never delays a close.

Every fact comes from durable rows and the exchange, never from process memory:

  * the entry: the source attempt row and the entry order found by its derived id;
  * the close legs: the task's close rows (``<entry>c*``) and the entry's protective
    stops (``<entry>s*``) that ended with a fill.

A fill amount the exchange does not report yet is unknown, not zero. Once every amount
is known, the legs must add up to the owned quantity, or an operator is needed. Each
price is resolved from its exchange order, never from a ticker.

The round trip is then published in one transaction: the open, each leg in execution
order (the last one terminal), the close at the legs' amount-weighted price, and the
task, entry attempt and close rows linked to the trade. Nothing partial is ever
visible, so a failure at any point leaves nothing to reconcile, and the next run
builds the round trip again from the same facts. Anything still unknown, or any read
or write that fails, is retried until the deadline, then needs an operator.

The task's ``emergency_close_pending`` incident stays ``manual_required``: an operator
clears it once the task is ``accounted``.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

import psycopg
import structlog
from psycopg.rows import dict_row

from . import exchange_orders, journal, notify, order_ids
from .emergency_close import (
    STATUS_ACCOUNTED,
    STATUS_CONFIRMED_FLAT,
    STATUS_MANUAL_REQUIRED,
    CloseOrder,
    EmergencyCloseTask,
    PostgresEmergencyCloseStore,
    task_from_row,
)
from .fill_price import FILL_CONFIRMED, FILL_PARTIAL, FillResolution, resolve_fill_price

if TYPE_CHECKING:
    from collections.abc import Callable

log = structlog.get_logger()

_TOLERANCE = Decimal("0.00000001")
_CLOSE_TERMINAL_FILL = "completed"


@dataclass(frozen=True)
class EntryAttempt:
    trade_id: int | None
    requested_amount: Decimal | None
    leverage: int
    contract_size: Decimal | None
    setup_context: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class TradeOpen:
    """The open to journal when no trade exists yet for the entry."""

    order_id: str
    size_usd: float
    leverage: int
    entry_price: float
    setup_context: dict[str, Any]
    entry_at: datetime


@dataclass(frozen=True)
class CloseLeg:
    order_id: str
    price: float
    filled_amount: Decimal
    requested_amount: Decimal
    remaining_amount: Decimal
    terminal: bool
    source: str
    executed_at: datetime | None
    execution_time_source: str | None


@dataclass(frozen=True)
class RoundTrip:
    """Everything one publish writes. ``trade_id`` is an already journalled trade;
    without one, ``opening`` is journalled first."""

    trade_id: int | None
    opening: TradeOpen | None
    legs: tuple[CloseLeg, ...]
    reason: str


class _NotYetError(Exception):
    """The round trip cannot be journalled yet; the next run tries again."""


class _NeedsOperatorError(Exception):
    """The round trip can never be journalled as it stands."""


class JournalRefusedError(RuntimeError):
    """The journal refused one write; the whole publish is rolled back."""


class AccountingStore(Protocol):
    async def accountable_task_ids(self) -> list[int]: ...
    async def claim_flat(
        self, task_id: int, owner: str, lease_seconds: float
    ) -> EmergencyCloseTask | None: ...
    async def load_entry(self, attempt_id: int) -> EntryAttempt: ...
    async def load_closes(self, task_id: int) -> list[CloseOrder]: ...
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
    async def publish(self, task: EmergencyCloseTask, owner: str, round_trip: RoundTrip) -> int: ...


_ACCOUNTABLE = """
SELECT id FROM app.live_emergency_closes WHERE status = 'confirmed_flat' ORDER BY id
"""

_CLAIM_FLAT = """
UPDATE app.live_emergency_closes
SET claim_owner = %s,
    claim_expires_at = now() + make_interval(secs => %s),
    updated_at = now()
WHERE id = %s
  AND status = 'confirmed_flat'
  AND (claim_owner IS NULL OR claim_owner = %s OR claim_expires_at < now())
RETURNING id, source_attempt_id, entry_client_order_id, exchange, base, symbol,
    native_market_id, market_type, side, trade_id, owned_quantity, owned_remainder,
    reason, status, rejected_sends, confirmed_flat_at
"""

_SELECT_ENTRY = """
SELECT trade_id, requested_amount, leverage, contract_size, setup_context, created_at
FROM app.live_order_attempts
WHERE id = %s AND operation = 'entry'
"""

_MARK_TASK_ACCOUNTED = """
UPDATE app.live_emergency_closes
SET status = 'accounted',
    trade_id = %s,
    last_error = NULL,
    accounted_at = now(),
    claim_owner = NULL,
    claim_expires_at = NULL,
    updated_at = now()
WHERE id = %s AND status = 'confirmed_flat' AND claim_owner = %s
"""

_LINK_ENTRY = """
UPDATE app.live_order_attempts
SET status = 'completed', trade_id = %s, updated_at = now()
WHERE id = %s AND operation = 'entry'
"""

_LINK_CLOSES = """
UPDATE app.live_order_attempts SET trade_id = %s, updated_at = now() WHERE task_id = %s
"""


class PostgresAccountingStore(PostgresEmergencyCloseStore):
    async def accountable_task_ids(self) -> list[int]:
        async with (
            await psycopg.AsyncConnection.connect(self.db_url) as connection,
            connection.cursor() as cursor,
        ):
            await cursor.execute(_ACCOUNTABLE)
            return [int(row[0]) for row in await cursor.fetchall()]

    async def claim_flat(
        self, task_id: int, owner: str, lease_seconds: float
    ) -> EmergencyCloseTask | None:
        async with (
            await psycopg.AsyncConnection.connect(self.db_url) as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(_CLAIM_FLAT, (owner, lease_seconds, task_id, owner))
            row = await cursor.fetchone()
        return task_from_row(row) if row is not None else None

    async def load_entry(self, attempt_id: int) -> EntryAttempt:
        async with (
            await psycopg.AsyncConnection.connect(self.db_url) as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(_SELECT_ENTRY, (attempt_id,))
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError(f"entry attempt {attempt_id} not found")
        context = row["setup_context"]
        return EntryAttempt(
            trade_id=int(row["trade_id"]) if row["trade_id"] is not None else None,
            requested_amount=exchange_orders.decimal_or_none(row["requested_amount"]),
            leverage=int(row["leverage"] or 1),
            contract_size=exchange_orders.decimal_or_none(row["contract_size"]),
            setup_context=context if isinstance(context, dict) else {},
            created_at=row["created_at"],
        )

    async def publish(self, task: EmergencyCloseTask, owner: str, round_trip: RoundTrip) -> int:
        """The open, every leg, the close and the links, in one transaction: either
        the whole round trip is visible or none of it is."""
        async with (
            await psycopg.AsyncConnection.connect(self.db_url) as connection,
            connection.cursor() as cursor,
        ):
            trade_id = round_trip.trade_id
            if trade_id is None:
                opening = round_trip.opening
                if opening is None:
                    raise JournalRefusedError("a round trip without a trade needs its open")
                trade_id = await journal.open_trade_in(
                    cursor,
                    symbol=task.symbol,
                    exchange=task.exchange,
                    side=task.side,
                    order_id=opening.order_id,
                    size_usd=opening.size_usd,
                    leverage=opening.leverage,
                    entry_price=opening.entry_price,
                    setup_context=opening.setup_context,
                    entry_at=opening.entry_at,
                )
                if trade_id is None:
                    raise JournalRefusedError("the journal did not take the open")
            aggregate: float | None = None
            for leg in round_trip.legs:
                aggregate = await journal.record_close_fill_in(
                    cursor,
                    trade_id=trade_id,
                    exchange=task.exchange,
                    order_id=leg.order_id,
                    fill_price=leg.price,
                    filled_amount=float(leg.filled_amount),
                    requested_amount=float(leg.requested_amount),
                    remaining_amount=float(leg.remaining_amount),
                    terminal=leg.terminal,
                    fill_source=leg.source,
                    executed_at=leg.executed_at,
                    execution_time_source=leg.execution_time_source,
                )
                if aggregate is None:
                    raise JournalRefusedError(f"the journal did not take close leg {leg.order_id}")
            if aggregate is None:
                raise JournalRefusedError("a round trip needs at least one close leg")
            last = round_trip.legs[-1]
            closed = await journal.close_trade_in(
                cursor,
                trade_id=trade_id,
                exit_order_id=last.order_id,
                exit_price=aggregate,
                reason=round_trip.reason,
                executed_at=last.executed_at,
            )
            if not closed.committed:
                raise JournalRefusedError(f"the journal did not close trade {trade_id}")
            await cursor.execute(_MARK_TASK_ACCOUNTED, (trade_id, task.id, owner))
            if cursor.rowcount != 1:
                raise RuntimeError(f"emergency close task {task.id} claim was lost")
            await cursor.execute(_LINK_ENTRY, (trade_id, task.source_attempt_id))
            await cursor.execute(_LINK_CLOSES, (trade_id, task.id))
        return trade_id


def close_reason(task: EmergencyCloseTask) -> str:
    return f"emergency_close_{task.reason}"


def _filled(order: dict[str, Any]) -> Decimal | None:
    """The order's filled amount, or ``None`` when the exchange does not report it."""
    return exchange_orders.decimal_or_none(order.get("filled"))


class EmergencyCloseAccountant:
    """Supervised loop over confirmed-flat tasks, about every ten seconds."""

    def __init__(
        self,
        exchanges: dict[str, Any],
        store: AccountingStore,
        rdb: Any,
        *,
        alert_credentials: tuple[str, str] | None = None,
        owner: str | None = None,
        claim_seconds: float = 60.0,
        deadline_seconds: float = 600.0,
        interval_seconds: float = 10.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.exchanges = exchanges
        self.store = store
        self.rdb = rdb
        self.alert_credentials = alert_credentials
        self.owner = owner or f"emergency-accounting:{uuid.uuid4().hex[:16]}"
        self.claim_seconds = claim_seconds
        self.deadline = timedelta(seconds=deadline_seconds)
        self.interval_seconds = interval_seconds
        self._now = now or (lambda: datetime.now(tz=UTC))

    async def run_once(self) -> bool:
        succeeded = True
        for task_id in await self.store.accountable_task_ids():
            try:
                await self.account(task_id)
            except Exception as exc:
                succeeded = False
                log.error("emergency_accounting.failed", task_id=task_id, err=str(exc))
        return succeeded

    async def __call__(self, tracker: Any) -> None:
        log.info("emergency_accounting_worker.started")
        while True:
            tracker.tick_started()
            try:
                succeeded = await self.run_once()
            except Exception as exc:
                log.error("emergency_accounting.scan_failed", err=str(exc))
                succeeded = False
            if succeeded:
                tracker.tick_succeeded()
            else:
                tracker.tick_failed(RuntimeError("emergency accounting pass failed"))
            await asyncio.sleep(self.interval_seconds)

    async def account(self, task_id: int) -> str:
        """Journal one confirmed-flat task. Returns the task status after the run,
        or ``claimed_elsewhere``."""
        task = await self.store.claim_flat(task_id, self.owner, self.claim_seconds)
        if task is None:
            return "claimed_elsewhere"
        try:
            round_trip = await self._round_trip(task)
            await journal.revoke_pnl_readiness(self.rdb)
            trade_id = await self.store.publish(task, self.owner, round_trip)
        except _NeedsOperatorError as manual:
            return await self._manual(task, str(manual))
        except Exception as exc:
            # Not known yet, or a read or the publish failed: all retried until the
            # deadline. A failed publish wrote nothing.
            reason = str(exc) if isinstance(exc, _NotYetError) else f"{type(exc).__name__}: {exc}"
            return await self._pending(task, reason)
        try:
            await self.rdb.delete(f"position:opened_at:{task.exchange}:{task.base.upper()}")
        except Exception as exc:
            log.error("emergency_accounting.opened_at_cleanup_failed", err=str(exc))
        log.info("emergency_accounting.accounted", task_id=task.id, trade_id=trade_id)
        await self._alert(
            f"Emergency close task {task.id} accounted: {task.exchange} {task.symbol} "
            f"{task.side}, trade {trade_id} journalled and closed. Entries stay blocked "
            f"until an operator clears its emergency_close_pending incident."
        )
        return STATUS_ACCOUNTED

    async def _round_trip(self, task: EmergencyCloseTask) -> RoundTrip:
        exchange = self.exchanges.get(task.exchange)
        if exchange is None:
            raise _NeedsOperatorError(f"exchange {task.exchange!r} is not configured")
        entry = await self.store.load_entry(task.source_attempt_id)
        closes = await self.store.load_closes(task.id)
        orders = {
            exchange_orders.client_order_id(order): order
            for order in await exchange_orders.list_orders(exchange, task.symbol)
        }

        leg_orders: list[dict[str, Any]] = []
        for close in closes:
            if close.status != _CLOSE_TERMINAL_FILL or not close.filled_amount:
                continue
            order = orders.get(close.client_order_id)
            if order is None:
                raise _NotYetError(f"close order {close.client_order_id} not found on the exchange")
            leg_orders.append(order)
        stop_prefix = order_ids.stop_prefix(task.entry_client_order_id)
        for client_order_id, order in orders.items():
            if not (client_order_id or "").startswith(stop_prefix) or _filled(order) == 0:
                continue
            if not exchange_orders.is_terminal(order):
                raise _NotYetError(f"stop {client_order_id} is not final and may have filled")
            leg_orders.append(order)
        amounts = [_filled(order) for order in leg_orders]
        unknown = [
            str(order.get("id"))
            for order, amount in zip(leg_orders, amounts, strict=True)
            if amount is None
        ]
        if unknown:
            raise _NotYetError(f"filled amount of {', '.join(unknown)} is unknown")
        filled = sum((amount for amount in amounts if amount is not None), Decimal(0))
        if not leg_orders or abs(filled - task.owned_quantity) > _TOLERANCE:
            raise _NeedsOperatorError(
                f"the task's closes and stops filled {filled} of the owned "
                f"{task.owned_quantity}; the rest was closed outside the task"
            )

        priced = sorted(
            [await self._priced(exchange, task, order) for order in leg_orders],
            key=lambda item: (
                item[1].executed_at is None,
                item[1].executed_at or self._now(),
                str(item[0]["id"]),
            ),
        )
        legs: list[CloseLeg] = []
        remaining = task.owned_quantity
        for index, (order, resolution) in enumerate(priced):
            amount = _filled(order) or Decimal(0)
            requested = exchange_orders.decimal_or_none(order.get("amount"))
            remaining = max(Decimal(0), remaining - amount)
            assert resolution.price is not None
            legs.append(
                CloseLeg(
                    order_id=str(order["id"]),
                    price=resolution.price,
                    filled_amount=amount,
                    requested_amount=max(requested, amount) if requested is not None else amount,
                    remaining_amount=remaining,
                    terminal=index == len(priced) - 1,
                    source=resolution.source,
                    executed_at=resolution.executed_at,
                    execution_time_source=resolution.execution_time_source,
                )
            )

        trade_id = task.trade_id or entry.trade_id
        opening: TradeOpen | None = None
        if trade_id is None:
            opening = await self._opening(exchange, task, entry, orders)
        return RoundTrip(
            trade_id=trade_id, opening=opening, legs=tuple(legs), reason=close_reason(task)
        )

    async def _opening(
        self,
        exchange: Any,
        task: EmergencyCloseTask,
        entry: EntryAttempt,
        orders: dict[str | None, dict[str, Any]],
    ) -> TradeOpen:
        entry_order = orders.get(task.entry_client_order_id)
        if entry_order is None or not exchange_orders.is_terminal(entry_order):
            raise _NotYetError("the entry order is not found terminal on the exchange")
        resolution = await self._price(exchange, task, entry_order, entry.requested_amount, "entry")
        assert resolution.price is not None
        contract_size = entry.contract_size or Decimal(1)
        return TradeOpen(
            order_id=str(entry_order["id"]),
            size_usd=round(float(task.owned_quantity * contract_size) * resolution.price, 2),
            leverage=entry.leverage,
            entry_price=resolution.price,
            setup_context={
                **entry.setup_context,
                "entry_time_source": (
                    resolution.execution_time_source or "local.attempt_created_at"
                ),
                "emergency_close_task_id": task.id,
            },
            entry_at=resolution.executed_at or entry.created_at,
        )

    async def _priced(
        self, exchange: Any, task: EmergencyCloseTask, order: dict[str, Any]
    ) -> tuple[dict[str, Any], FillResolution]:
        filled = _filled(order) or Decimal(0)
        requested = exchange_orders.decimal_or_none(order.get("amount"))
        requested = max(requested, filled) if requested is not None else filled
        return order, await self._price(exchange, task, order, requested, "close leg")

    async def _price(
        self,
        exchange: Any,
        task: EmergencyCloseTask,
        order: dict[str, Any],
        requested: Decimal | None,
        what: str,
    ) -> FillResolution:
        resolution = await resolve_fill_price(
            exchange,
            symbol=task.symbol,
            order=order,
            requested_amount=float(requested) if requested is not None else None,
        )
        if resolution.status not in (FILL_CONFIRMED, FILL_PARTIAL) or resolution.price is None:
            raise _NotYetError(f"{what} {order.get('id')} fill price is {resolution.status}")
        return resolution

    async def _pending(self, task: EmergencyCloseTask, reason: str) -> str:
        started = task.confirmed_flat_at or self._now()
        if self._now() - started > self.deadline:
            return await self._manual(task, f"not accounted within the deadline: {reason}")
        log.warning("emergency_accounting.pending", task_id=task.id, reason=reason)
        await self._set(task, STATUS_CONFIRMED_FLAT, reason)
        return STATUS_CONFIRMED_FLAT

    async def _manual(self, task: EmergencyCloseTask, reason: str) -> str:
        log.error("emergency_accounting.manual_required", task_id=task.id, reason=reason)
        await self._set(task, STATUS_MANUAL_REQUIRED, reason)
        await self._alert(
            f"Emergency close task {task.id} is flat but cannot be accounted: "
            f"{task.exchange} {task.symbol} {task.side}. {reason}. Journal the round "
            "trip by hand."
        )
        return STATUS_MANUAL_REQUIRED

    async def _set(self, task: EmergencyCloseTask, status: str, error: str) -> None:
        await self.store.update_task(
            task,
            self.owner,
            status=status,
            owned_remainder=task.owned_remainder,
            rejected_sends=task.rejected_sends,
            last_error=error,
            release=True,
        )

    async def _alert(self, text: str) -> None:
        if self.alert_credentials is None:
            return
        delivered = False
        try:
            delivered = await notify.notify_alert(*self.alert_credentials, text=text)
        except Exception as exc:
            log.error("emergency_accounting.alert_delivery_failed", err=str(exc))
        if not delivered:
            log.error("emergency_accounting.alert_not_delivered", text=text)
