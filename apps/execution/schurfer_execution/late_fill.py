"""An entry fill that reconciliation finds after the entry path gave up on it.

docs/engineering/live-entry-failure-paths-design-v1.md, section 4. The entry path
leaves an attempt ``pending``, ``accepted`` or ``submission_unknown`` when it
crashed, found nothing, or could not confirm the cancel of an active entry. Each
reconciliation scan then takes the instrument's order lock, so the entry path is
not running, reads the attempt again, and looks the entry up by its
``clientOrderId``:

  * not found: nothing changes, the attempt keeps blocking entries;
  * still active: what is held is protected, the rest is cancelled and the cancel
    confirmed, as in the entry path (entry_recovery.py); an unconfirmed cancel
    stays unresolved;
  * terminal with no fill, or without a filled amount: recorded as such;
  * terminal with a fill: the entry is stale, so an emergency close task
    (``late_fill_flattened``) flattens it and journals the round trip. Any
    ``<entry>cN`` close the database-down path already sent is adopted by the task
    as it is created. The task checks the exchange position against what is owned
    before any send.

A task is created only when nothing else may own the instrument: no open trade,
no other unresolved attempt and no other active task on it. Otherwise the attempt
is recorded completed and reconciliation's classifier quarantines it for an
operator. Attempts already ``completed`` without a trade (an unresolved fill price,
a failed journal write) have a stop and their own recovery; they are never
candidates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import psycopg
import structlog
from psycopg.rows import dict_row

from . import emergency_close, entry_recovery, exchange_orders, notify, order_ids
from . import exit as exit_module
from . import reconciliation as rec
from .order_lock import OrderLockLease

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = structlog.get_logger()

CANDIDATE_STATUSES = frozenset({"pending", "accepted", "submission_unknown"})

_ATTEMPT = """
SELECT a.status, a.trade_id, a.exit_params
FROM app.live_order_attempts AS a
WHERE a.id = %s AND a.operation = 'entry'
"""

_OPEN_TRADE = """
SELECT 1 FROM app.trades AS t
WHERE t.status = 'open' AND t.exchange = %s AND t.symbol = %s
  AND COALESCE(t.setup_context->>'paper', 'false') != 'true'
LIMIT 1
"""

_OTHER_UNRESOLVED_ATTEMPT = """
SELECT 1 FROM app.live_order_attempts
WHERE exchange = %s AND upper(base) = upper(%s) AND id != %s
  AND status IN ('pending', 'accepted', 'submission_unknown', 'manual_required')
  -- The closes of this attempt's own task, after a crash before the record.
  AND (task_id IS NULL OR task_id NOT IN (
      SELECT e.id FROM app.live_emergency_closes AS e WHERE e.source_attempt_id = %s
  ))
LIMIT 1
"""

_OTHER_ACTIVE_TASK = """
SELECT 1 FROM app.live_emergency_closes
WHERE exchange = %s AND base = upper(%s) AND source_attempt_id != %s
  AND status IN ('requested', 'sending', 'unknown', 'manual_required')
LIMIT 1
"""


def is_candidate(attempt: rec.OrderAttemptSnapshot) -> bool:
    return (
        attempt.status in CANDIDATE_STATUSES
        and attempt.trade_id is None
        and attempt.identity.complete
        and order_ids.is_entry_client_order_id(attempt.client_order_id)
    )


@dataclass(frozen=True)
class _Locked:
    """The attempt as read under the lock, and what else holds its instrument."""

    status: str
    trade_id: int | None
    initial_sl_pct: float
    competitor: str | None


class LateFillRecovery:
    def __init__(
        self,
        exchanges: dict[str, Any],
        db_url: str,
        rdb: Any,
        *,
        alert_credentials: tuple[str, str] | None = None,
        store: emergency_close.EmergencyCloseStore | None = None,
    ) -> None:
        self.exchanges = exchanges
        self.db_url = db_url
        self.rdb = rdb
        self.alert_credentials = alert_credentials
        self.store = store or emergency_close.PostgresEmergencyCloseStore(db_url)
        # Attempts already alerted for an unconfirmed cancel, so a scan every 30 s
        # does not repeat it; a restart alerts once more.
        self._alerted_unconfirmed: set[int] = set()

    async def resolve(
        self,
        attempt: rec.OrderAttemptSnapshot,
        record: Callable[
            [rec.OrderAttemptSnapshot, dict[str, Any]], Awaitable[rec.OrderAttemptSnapshot]
        ],
    ) -> rec.OrderAttemptSnapshot | None:
        """The attempt as now recorded, or None when nothing changed. ``record``
        writes the state an exchange order shows. A failure leaves the attempt
        unresolved, and so blocking entries, until a later scan."""
        exchange = self.exchanges.get(attempt.identity.exchange)
        if exchange is None:
            return None
        lease = await OrderLockLease.acquire(
            rdb=self.rdb,
            key=f"lock:order:{attempt.identity.exchange}:{attempt.base.upper()}",
            operation="late_fill",
        )
        if lease is None:
            # The entry path, a close or a task holds the instrument; it owns
            # this attempt until it lets go.
            log.info("reconciliation.late_fill.lock_busy", attempt_id=attempt.attempt_id)
            return None
        try:
            async with lease:
                return await self._resolve_locked(exchange, attempt, record)
            raise RuntimeError("late fill lease exited without a result")
        except Exception as exc:
            log.error(
                "reconciliation.late_fill.failed", attempt_id=attempt.attempt_id, err=str(exc)
            )
            return None

    async def _resolve_locked(
        self,
        exchange: Any,
        attempt: rec.OrderAttemptSnapshot,
        record: Callable[
            [rec.OrderAttemptSnapshot, dict[str, Any]], Awaitable[rec.OrderAttemptSnapshot]
        ],
    ) -> rec.OrderAttemptSnapshot | None:
        locked = await self._read_locked(attempt)
        if locked is None or locked.status not in CANDIDATE_STATUSES or locked.trade_id:
            # Resolved by the entry path between the scan and the lock.
            return None
        symbol = attempt.identity.symbol
        order = await exchange_orders.look_up(
            exchange, symbol, attempt.client_order_id, entry_recovery.LOOKUP_TIMEOUT_SECONDS
        )
        if order is None:
            return None
        if not exchange_orders.is_terminal(order):
            settled = await self._settle(exchange, attempt, order, locked)
            if settled is None:
                return None
            order, unconfirmed = settled
            if unconfirmed:
                return await record(attempt, order)
        status, filled, _error = exchange_orders.attempt_state_from_order(order)
        if status != "completed" or filled is None or filled <= 0:
            return await record(attempt, order)
        if locked.competitor is not None:
            log.warning(
                "reconciliation.late_fill.not_flattened",
                attempt_id=attempt.attempt_id,
                reason=locked.competitor,
            )
            return await record(attempt, order)
        # A close the database-down path already sent (protective_stop.py) is the
        # task's own, adopted in the same transaction: otherwise the task would
        # count the whole fill as still owned, and the outage recovery, which
        # adopts such closes, never sees an attempt that has a task.
        closes = await exchange_orders.orders_with_prefix(
            exchange, symbol, order_ids.close_prefix(attempt.client_order_id)
        )
        task_id, created = await self.store.create_task(
            emergency_close.TaskSpec(
                source_attempt_id=attempt.attempt_id,
                entry_client_order_id=attempt.client_order_id,
                exchange=attempt.identity.exchange,
                base=attempt.base,
                symbol=symbol,
                native_market_id=attempt.identity.native_market_id,
                market_type=attempt.identity.market_type,
                side=attempt.identity.side,
                owned_quantity=filled,
                reason=emergency_close.REASON_LATE_FILL_FLATTENED,
            ),
            tuple(emergency_close.adopted_close(order) for order in closes),
        )
        # The task first: a crash before this record finds the same task again.
        recorded = await record(attempt, order)
        if created:
            await self._alert(
                f"Late entry fill found by reconciliation: {attempt.identity.exchange} "
                f"{symbol} {attempt.identity.side}, {filled} filled by entry "
                f"{attempt.client_order_id}, {len(closes)} close order(s) already sent. "
                f"Emergency close task {task_id} flattens it; entries stay blocked until "
                "it is accounted."
            )
        log.warning(
            "reconciliation.late_fill.task",
            attempt_id=attempt.attempt_id,
            task_id=task_id,
            created=created,
            filled=str(filled),
        )
        return recorded

    async def _settle(
        self,
        exchange: Any,
        attempt: rec.OrderAttemptSnapshot,
        order: dict[str, Any],
        locked: _Locked,
    ) -> tuple[dict[str, Any], bool] | None:
        """``(order, unconfirmed)`` once the active entry is cancelled, or None when
        its stops cannot be read (nothing is placed blind; a later scan retries)."""
        side = attempt.identity.side
        average = exchange_orders.decimal_or_none(order.get("average"))
        price = average if average else exchange_orders.decimal_or_none(order.get("price"))
        if not price:
            ticker = await exchange.fetch_ticker(attempt.identity.symbol)
            price = exchange_orders.decimal_or_none(ticker.get("last"))
        if not price:
            return None
        guard = entry_recovery.FillGuard(
            exchange,
            symbol=attempt.identity.symbol,
            side=side,
            stop_side="buy" if side == "short" else "sell",
            reference_price=float(price),
            initial_sl_pct=locked.initial_sl_pct,
            entry_client_order_id=attempt.client_order_id,
        )
        if not await guard.adopt_existing():
            return None
        settled, latest = await entry_recovery.settle_active_entry(
            exchange, attempt.identity.symbol, order, on_active=guard.cover
        )
        if settled is not None:
            return settled, False
        if attempt.attempt_id not in self._alerted_unconfirmed:
            self._alerted_unconfirmed.add(attempt.attempt_id)
            await self._alert(
                f"Late entry {attempt.client_order_id} on {attempt.identity.exchange} "
                f"{attempt.identity.symbol} is still active and its cancel is not "
                f"confirmed; as last seen, {guard.report}. Entries stay blocked."
            )
        return latest, True

    async def _read_locked(self, attempt: rec.OrderAttemptSnapshot) -> _Locked | None:
        identity = attempt.identity
        async with (
            await psycopg.AsyncConnection.connect(self.db_url) as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(_ATTEMPT, (attempt.attempt_id,))
            row = await cursor.fetchone()
            if row is None:
                return None
            competitor: str | None = None
            await cursor.execute(_OPEN_TRADE, (identity.exchange, identity.symbol))
            if await cursor.fetchone() is not None:
                competitor = "an open trade holds the instrument"
            await cursor.execute(
                _OTHER_UNRESOLVED_ATTEMPT,
                (identity.exchange, attempt.base, attempt.attempt_id, attempt.attempt_id),
            )
            if competitor is None and await cursor.fetchone() is not None:
                competitor = "another unresolved attempt holds the instrument"
            await cursor.execute(
                _OTHER_ACTIVE_TASK, (identity.exchange, attempt.base, attempt.attempt_id)
            )
            if competitor is None and await cursor.fetchone() is not None:
                competitor = "another emergency close task holds the instrument"
        exit_params = row["exit_params"] if isinstance(row["exit_params"], dict) else None
        initial_sl_pct = (exit_params or {}).get("initial_sl_pct")
        if initial_sl_pct is None:
            initial_sl_pct = exit_module.exit_params(None)["initial_sl_pct"]
        return _Locked(
            status=str(row["status"]),
            trade_id=int(row["trade_id"]) if row["trade_id"] is not None else None,
            initial_sl_pct=float(initial_sl_pct),
            competitor=competitor,
        )

    async def _alert(self, text: str) -> None:
        if self.alert_credentials is None:
            return
        delivered = False
        try:
            delivered = await notify.notify_alert(*self.alert_credentials, text=text)
        except Exception as exc:
            log.error("reconciliation.late_fill.alert_delivery_failed", err=str(exc))
        if not delivered:
            log.error("reconciliation.late_fill.alert_not_delivered", text=text)
