"""Protective stop placement and the response when it cannot be placed.

docs/engineering/live-entry-failure-paths-design-v1.md, section 2. The stop's id is
derived from the entry (``<entry>s1``, ``<entry>s2``, ...), so the attempt row written
before the entry already names every stop it may cause. Placement is bounded:

  * requests at about 0, 1.5 and 4.5 s, under one deadline of about 10 s, each with
    its own timeout;
  * after a timeout or a network error the same id is looked up first, and reused
    while its outcome is unknown;
  * after an explicit rejection the next request takes a new id;
  * a non-retryable rejection (an invalid trigger, for instance) ends the retries,
    but a duplicate id after an unknown outcome means that stop may exist and its
    lookup continues.

Only a stop resting with no fill protects the position. A stop that already
triggered, or ended with a fill, leaves a remainder nobody protects, so the
position is flattened instead.

When no stop protects the position, entries are blocked at once and the position
is flattened through a durable emergency close task, whose first passes run here
under the entry's order lock. Only the confirmed fill of the exact entry is ours to
close: when it is unknown, nothing is sent and an operator decides. With the
database down, at most one reduce-only close ``<entry>c1`` is sent and an alert
goes out; the emergency worker builds the task from the attempt row once the
database is back. Accounting is never a precondition of the close.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import ccxt
import structlog

from . import emergency_close, exchange_orders, notify, order_attempts, order_ids
from . import reconciliation as rec

if TYPE_CHECKING:
    from .order_lock import OrderLockLease
    from .supervisor import WorkerReadinessGate

log = structlog.get_logger()

STOP_RETRY_DELAYS_SECONDS: tuple[float, ...] = (0.0, 1.5, 4.5)
STOP_DEADLINE_SECONDS = 10.0
STOP_REQUEST_TIMEOUT_SECONDS = 3.0


# A stop order, as the exchange reports it.
_RESTING = "resting"  # on the book with no fill: the only state that protects
_FILLING = "filling"  # triggered and filling: the remainder is about to be bare
_TERMINAL_FILL = "terminal_fill"  # ended after filling some or all of the position
_NO_FILL = "no_fill"  # ended with nothing filled: a terminal answer, a new id may follow

# Inline passes after a stop failure: the first sends the close, the second
# usually confirms it, the supervised worker runs any later one.
_INLINE_PASSES = 2


def _stop_state(order: dict[str, Any]) -> str:
    filled = exchange_orders.decimal_or_none(order.get("filled")) or Decimal(0)
    if exchange_orders.is_terminal(order):
        return _TERMINAL_FILL if filled > 0 else _NO_FILL
    return _FILLING if filled > 0 else _RESTING


@dataclass
class StopPlacement:
    order: dict[str, Any] | None
    client_order_id: str
    requests: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def error(self) -> str:
        return self.errors[-1] if self.errors else "no stop confirmed"


async def _look_up(
    ex: Any, symbol: str, client_order_id: str, timeout: float
) -> dict[str, Any] | None:
    """The order by its id, or None when it is not found or the lookup fails:
    either way its outcome stays unknown and the id is reused."""
    try:
        return await asyncio.wait_for(
            exchange_orders.find_by_client_order_id(ex, symbol, client_order_id),
            timeout=max(timeout, 0.1),
        )
    except Exception as exc:
        log.warning("execution.order_lookup_failed", client_order_id=client_order_id, err=str(exc))
        return None


async def place_protective_stop(
    ex: Any,
    *,
    symbol: str,
    stop_side: str,
    amount: float,
    trigger_price: float,
    entry_client_order_id: str,
) -> StopPlacement:
    """``order`` is set only for a stop resting with no fill."""
    started = time.monotonic()
    sequence = 1
    placement = StopPlacement(
        order=None,
        client_order_id=order_ids.stop_client_order_id(entry_client_order_id, sequence),
    )
    unknown = False

    def remaining() -> float:
        return STOP_DEADLINE_SECONDS - (time.monotonic() - started)

    def next_id() -> None:
        nonlocal sequence
        sequence += 1
        placement.client_order_id = order_ids.stop_client_order_id(entry_client_order_id, sequence)

    def settle(order: dict[str, Any]) -> bool:
        """True when the answer ends the placement: protected, or past saving."""
        state = _stop_state(order)
        if state == _RESTING:
            placement.order = order
            return True
        if state == _NO_FILL:
            placement.errors.append(f"stop {placement.client_order_id} ended with no fill")
            next_id()
            return False
        placement.errors.append(f"stop {placement.client_order_id} already {state}")
        return True

    for delay in STOP_RETRY_DELAYS_SECONDS:
        wait = delay - (time.monotonic() - started)
        if wait > 0:
            if wait >= remaining():
                break
            await asyncio.sleep(wait)
        if remaining() <= 0:
            break
        resending_unknown = False
        if unknown:
            found = await _look_up(ex, symbol, placement.client_order_id, remaining())
            if found is not None and settle(found):
                return placement
            unknown = found is None
            resending_unknown = unknown
        placement.requests += 1
        try:
            response = await asyncio.wait_for(
                ex.create_stop_market_order(
                    symbol,
                    stop_side,
                    amount,
                    trigger_price,
                    params={"reduceOnly": True, "clientOrderId": placement.client_order_id},
                ),
                timeout=max(min(STOP_REQUEST_TIMEOUT_SECONDS, remaining()), 0.1),
            )
            unknown = False
            if settle(response):
                return placement
            continue
        except ccxt.InvalidOrder as exc:
            if resending_unknown or isinstance(exc, ccxt.DuplicateOrderId):
                # Bybit 110072 maps to InvalidOrder: a duplicate id after an
                # unknown outcome means the first request may have landed.
                placement.errors.append(f"duplicate id, outcome unknown: {exc}")
                unknown = True
            else:
                placement.errors.append(f"rejected, not retryable: {exc}")
                unknown = False
                break
        except ccxt.NetworkError as exc:
            placement.errors.append(f"unknown: {exc}")
            unknown = True
        except ccxt.ExchangeError as exc:
            placement.errors.append(f"rejected: {exc}")
            unknown = False
            next_id()
        except Exception as exc:
            placement.errors.append(f"unknown: {exc!r}")
            unknown = True
        log.warning(
            "execution.stop_loss.attempt_failed",
            client_order_id=placement.client_order_id,
            err=placement.errors[-1],
        )

    if unknown:
        # The last request's outcome is still unknown: one more look before
        # giving up, so a stop that was created is never mistaken for none.
        found = await _look_up(ex, symbol, placement.client_order_id, max(remaining(), 1.0))
        if found is not None:
            settle(found)
    return placement


@dataclass(frozen=True)
class StopFailureContext:
    """What the entry path knows when no stop protects the position."""

    exchange: str
    base: str
    symbol: str
    native_market_id: str | None
    market_type: str | None
    side: str
    entry_client_order_id: str
    attempt_id: int | None
    requested_amount: float
    filled_amount: float | None
    stop_error: str


async def _entry_fill(ex: Any, context: StopFailureContext) -> Decimal | None:
    """The confirmed fill of the exact entry, or None when it is not known.

    Never the exchange position: the order lock serialises this service's
    orders, not manual ones, so a position can hold volume that is not ours.
    """
    if context.filled_amount is not None and context.filled_amount > 0:
        return Decimal(str(context.filled_amount))
    entry = await _look_up(
        ex, context.symbol, context.entry_client_order_id, STOP_REQUEST_TIMEOUT_SECONDS
    )
    if entry is None or not exchange_orders.is_terminal(entry):
        return None
    filled = exchange_orders.decimal_or_none(entry.get("filled"))
    return filled if filled is not None and filled > 0 else None


async def flatten_after_stop_failure(
    *,
    ex: Any,
    exchanges: dict[str, Any],
    rdb: Any,
    db_url: str | None,
    alert_credentials: tuple[str, str] | None,
    lease: OrderLockLease,
    worker_gate: WorkerReadinessGate,
    context: StopFailureContext,
) -> dict[str, Any]:
    # Entries stop now, before any further exchange or database wait. The
    # task's incident (or reconciliation, once it sees this attempt) keeps
    # the block after the next scan.
    worker_gate.set_safety_blocker(rec.INCIDENT_BLOCKER)

    owned = await _entry_fill(ex, context)
    if db_url and context.attempt_id is not None and owned is not None:
        await order_attempts.mark_completed(
            db_url, context.attempt_id, trade_id=None, filled_amount=float(owned)
        )

    task_id: int | None = None
    status: str | None = None
    if owned is None:
        close_sent = force_closed = False
        outcome = (
            "the entry's fill is not confirmed, so the position's ownership is not "
            "proven; nothing was sent"
        )
    else:
        task_id, status, outcome = await _flatten_through_task(
            exchanges=exchanges,
            rdb=rdb,
            db_url=db_url,
            alert_credentials=alert_credentials,
            lease=lease,
            context=context,
            owned=owned,
        )
        if task_id is not None:
            close_sent = status in (
                emergency_close.STATUS_SENDING,
                emergency_close.STATUS_CONFIRMED_FLAT,
            )
            force_closed = status == emergency_close.STATUS_CONFIRMED_FLAT
        elif outcome:
            close_sent = force_closed = False
        else:
            close_sent, force_closed, outcome = await _close_once_without_database(
                ex, context, owned
            )

    text = (
        f"Stop-loss placement FAILED for {context.base} on {context.exchange} "
        f"({context.stop_error}). {outcome}."
    )
    if not force_closed:
        text += " Position may be UNPROTECTED. Manual intervention required."
    await _alert(alert_credentials, text)
    if force_closed:
        await rdb.delete(f"position:opened_at:{context.exchange}:{context.base.upper()}")
    log.error(
        "execution.stop_loss.flatten",
        base=context.base,
        exchange=context.exchange,
        task_id=task_id,
        status=status,
        close_sent=close_sent,
        force_closed=force_closed,
    )
    if force_closed:
        reason = f"stop-loss placement failed, position force-closed: {context.stop_error}"
    elif close_sent:
        reason = (
            f"stop-loss placement failed, close sent but not confirmed: position may be "
            f"UNPROTECTED on {context.exchange}:{context.base}: {context.stop_error}"
        )
    else:
        reason = (
            f"stop-loss placement failed AND no close sent: position UNPROTECTED on "
            f"{context.exchange}:{context.base}: {context.stop_error}"
        )
    return {
        "allowed": False,
        "reason": f"{reason}; {outcome}",
        "force_closed": force_closed,
        "close_sent": close_sent,
        "emergency_close_task_id": task_id,
    }


async def _flatten_through_task(
    *,
    exchanges: dict[str, Any],
    rdb: Any,
    db_url: str | None,
    alert_credentials: tuple[str, str] | None,
    lease: OrderLockLease,
    context: StopFailureContext,
    owned: Decimal,
) -> tuple[int | None, str | None, str]:
    """``(task_id, status, outcome)``. No task and an empty outcome means the
    database could not take the task, so the caller closes once without it."""
    if not (
        db_url
        and context.attempt_id is not None
        and context.native_market_id
        and context.market_type
        and order_ids.is_entry_client_order_id(context.entry_client_order_id)
    ):
        return None, None, ""
    store = emergency_close.PostgresEmergencyCloseStore(db_url)
    try:
        task_id, _created = await store.create_task(
            emergency_close.TaskSpec(
                source_attempt_id=context.attempt_id,
                entry_client_order_id=context.entry_client_order_id,
                exchange=context.exchange,
                base=context.base,
                symbol=context.symbol,
                native_market_id=context.native_market_id,
                market_type=context.market_type,
                side=context.side,
                owned_quantity=owned,
                reason=emergency_close.REASON_STOP_PLACEMENT_FAILED,
            )
        )
    except emergency_close.TaskConflictError as exc:
        return (
            None,
            None,
            f"another emergency close task holds this instrument ({exc}); nothing sent",
        )
    except Exception as exc:
        log.error("execution.stop_loss.task_create_failed", err=str(exc))
        return None, None, ""
    executor = emergency_close.EmergencyCloseExecutor(
        exchanges, store, rdb, alert_credentials=alert_credentials
    )
    status: str | None = None
    for _ in range(_INLINE_PASSES):
        try:
            status = await executor.run_pass(task_id, held_lease=lease)
        except Exception as exc:
            # The task is durable; the supervised worker runs the next pass.
            log.error("execution.stop_loss.inline_pass_failed", task_id=task_id, err=str(exc))
            status = status or "pass_failed"
            break
        if status not in (emergency_close.STATUS_SENDING, emergency_close.STATUS_UNKNOWN):
            break
    return task_id, status, f"emergency close task {task_id} is {status}"


async def _close_once_without_database(
    ex: Any, context: StopFailureContext, owned: Decimal
) -> tuple[bool, bool, str]:
    """``(close_sent, confirmed, outcome)`` for at most one reduce-only close,
    under the derived id the emergency worker finds by prefix once the database
    answers. Nothing more is sent here; a sent order is confirmed only by a
    terminal fill of the whole owned quantity."""
    if order_ids.is_entry_client_order_id(context.entry_client_order_id):
        client_order_id: str | None = order_ids.close_client_order_id(
            context.entry_client_order_id, 1
        )
        params: dict[str, Any] = {"reduceOnly": True, "clientOrderId": client_order_id}
    else:
        client_order_id = None
        params = {"reduceOnly": True}
    try:
        order = await ex.create_market_order(
            context.symbol, emergency_close.close_side(context.side), float(owned), params=params
        )
    except Exception as exc:
        log.critical("execution.stop_loss.emergency_close_failed", err=str(exc))
        return False, False, f"the one emergency close without a database ALSO failed ({exc})"
    if client_order_id is not None and not exchange_orders.is_terminal(order):
        order = (
            await _look_up(ex, context.symbol, client_order_id, STOP_REQUEST_TIMEOUT_SECONDS)
            or order
        )
    status, filled, _error = exchange_orders.attempt_state_from_order(order)
    confirmed = status == "completed" and filled is not None and filled >= owned
    return (
        True,
        confirmed,
        f"database unavailable: one reduce-only close {client_order_id or ''} sent as order "
        f"{order.get('id')} ({'filled' if confirmed else 'not confirmed filled'}); the "
        "emergency worker takes over once the database answers",
    )


async def _alert(credentials: tuple[str, str] | None, text: str) -> None:
    if credentials is None:
        return
    delivered = False
    try:
        delivered = await notify.notify_alert(*credentials, text=text)
    except Exception as exc:
        log.error("execution.stop_loss.alert_delivery_failed", err=str(exc))
    if not delivered:
        log.error("execution.stop_loss.alert_not_delivered", text=text)
