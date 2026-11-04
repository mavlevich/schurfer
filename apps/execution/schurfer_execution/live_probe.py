"""LIVE_PROBE authorization records and entry reservations.

docs/research/live-probe-step2-design-v1.md, sections 2 and 4;
docs/engineering/live-probe-loss-threshold-design-v1.md, sections 3 and 5.

A probe exists only as an authorization record the owner writes with the
``live-probe`` CLI. An entry is possible only through a reservation, written before
any exchange call along the fixed chain
``probe -> idempotency_key -> attempt -> clientOrderId``:

  * ``reserve``: one transaction under the record's row lock checks the record and
    holds the slot, one unit of N and the entry's L reserve. A repeated delivery of
    the same decision returns the existing reservation and holds nothing more.
  * ``begin_sending``: after the network preflight, one short transaction under the
    same row lock checks the record again (unrevoked, not suspended, no costs
    incident, L not reached, the database clock before ``entries_until``) and only then
    marks the reservation ``sending``. A revoke takes the same lock, so the two are
    ordered: revoke first means ``refused_before_send`` and nothing is sent; sending
    first means the send goes ahead and the reservation counts as exposure until
    reconciliation settles it.

What a reservation holds follows from its state and its trade:

  * the slot: ``reserved``, ``sending``, or ``placed`` until its trade is closed;
  * a unit of N: ``reserved``, ``sending`` or ``placed``;
  * its L reserve: ``reserved``, ``sending``, or ``placed`` until its trade's costs are
    settled from the exchange (``live_exchange_costs_v1``, ``complete`` or
    ``operator_settled``), when its real result enters ``realized_closed``.

Nothing calls ``reserve`` yet: the entry gate in ``place_order`` lands in PR 2.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
import structlog
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from schurfer_performance import LIVE_EXCHANGE_COSTS_ACCOUNTING_VERSION

from . import order_ids, probe_loss

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

log = structlog.get_logger()

ENVIRONMENTS = ("testnet", "mainnet")
MAX_NOTIONAL_USD = Decimal(50)
# One slot: the entry preflight wants an empty account (design, sections 3 and 7).
SLOTS = 1
SERVICE_ACTOR = "execution-service"

STATE_RESERVED = "reserved"
STATE_SENDING = "sending"
STATE_REFUSED_BEFORE_SEND = "refused_before_send"
STATE_PLACED = "placed"
STATE_RELEASED = "released"

REFUSED_NO_AUTHORIZATION = "no_authorization"
REFUSED_FOREIGN_ACCOUNT = "foreign_account"
REFUSED_REVOKED = "revoked"
REFUSED_SUSPENDED = "suspended"
REFUSED_L_REACHED = "l_reached"
REFUSED_COSTS_INCIDENT = "costs_incident"
REFUSED_ENTRIES_CLOSED = "entries_closed"
REFUSED_INSTRUMENT = "instrument_not_allowed"
REFUSED_COSTS_PENDING = "probe_trade_costs_pending"
REFUSED_SLOT = "slot_busy"
REFUSED_MAX_ENTRIES = "max_entries_spent"
REFUSED_L_HEADROOM = "l_headroom"


def idempotency_key(probe_id: str, decision_id: str) -> str:
    """Deterministic from the probe id and the strategy's decision id."""
    if not probe_id or not decision_id:
        raise ValueError("an idempotency key needs a probe id and a decision id")
    return hashlib.sha256(f"{probe_id}\x00{decision_id}".encode()).hexdigest()


@dataclass(frozen=True)
class Terms:
    """What the owner authorizes; never changed once issued."""

    probe_id: str
    environment: str
    account_key: str
    strategy: str
    contract_version: str
    contract_sha256: str
    instruments: tuple[str, ...]
    max_entries: int
    notional_usd: Decimal
    loss_threshold_usd: Decimal
    entries_until: datetime
    flat_by: datetime

    def validate(self) -> None:
        if self.environment not in ENVIRONMENTS:
            raise ValueError(f"environment must be one of {ENVIRONMENTS}")
        if not self.account_key or not self.strategy or not self.contract_version:
            raise ValueError("account key, strategy and contract version are required")
        if not self.instruments or any(not i for i in self.instruments):
            raise ValueError("at least one instrument is required")
        if self.max_entries < 1:
            raise ValueError("N must be at least 1")
        if not 0 < self.notional_usd <= MAX_NOTIONAL_USD:
            raise ValueError(f"the notional per entry must be in (0, {MAX_NOTIONAL_USD}]")
        if self.loss_threshold_usd <= 0:
            raise ValueError("L must be positive")
        if self.entries_until.tzinfo is None or self.flat_by.tzinfo is None:
            raise ValueError("entries_until and flat_by need a time zone")
        if self.flat_by < self.entries_until:
            raise ValueError("flat_by must not be before entries_until")


@dataclass(frozen=True)
class EntryRequest:
    """One strategy decision asking to enter under a probe."""

    probe_id: str
    decision_id: str
    environment: str
    account_key: str
    native_market_id: str
    side: str
    reserve_inputs: probe_loss.ReserveInputs


@dataclass(frozen=True)
class Reservation:
    id: int
    probe_id: str
    idempotency_key: str
    decision_id: str
    client_order_id: str
    native_market_id: str
    side: str
    notional_usd: Decimal
    reserve_usd: Decimal
    state: str
    state_reason: str | None
    attempt_id: int | None
    trade_id: int | None
    # False when a repeated delivery found it: nothing more was held.
    created: bool = True


@dataclass(frozen=True)
class Refused:
    reason: str
    detail: dict[str, Any]


@dataclass(frozen=True)
class Ledger:
    entries_spent: int
    slots_held: int
    reserved_open: Decimal
    realized_closed: Decimal
    closed_unsettled: int


def _reservation(row: dict[str, Any], *, created: bool = True) -> Reservation:
    return Reservation(
        id=int(row["id"]),
        probe_id=row["probe_id"],
        idempotency_key=row["idempotency_key"],
        decision_id=row["decision_id"],
        client_order_id=row["client_order_id"],
        native_market_id=row["native_market_id"],
        side=row["side"],
        notional_usd=Decimal(row["notional_usd"]),
        reserve_usd=Decimal(row["reserve_usd"]),
        state=row["state"],
        state_reason=row["state_reason"],
        attempt_id=row["attempt_id"],
        trade_id=row["trade_id"],
        created=created,
    )


_INSERT_AUTHORIZATION = """
INSERT INTO app.live_probe_authorizations (
    probe_id, environment, account_key, exchange, strategy, contract_version,
    contract_sha256, instruments, max_entries, notional_usd, loss_threshold_usd,
    entries_until, flat_by, issued_by
)
SELECT %(probe_id)s, %(environment)s, %(account_key)s, 'bybit', %(strategy)s,
       %(contract_version)s, %(contract_sha256)s, %(instruments)s, %(max_entries)s,
       %(notional_usd)s, %(loss_threshold_usd)s, %(entries_until)s, %(flat_by)s, %(actor)s
WHERE %(entries_until)s > clock_timestamp()
RETURNING *
"""

_INSERT_EVENT = """
INSERT INTO app.live_probe_authorization_events (probe_id, event, actor, detail)
VALUES (%s, %s, %s, %s)
"""

_INSERT_REFUSAL = """
INSERT INTO app.live_probe_refusals (
    probe_id, idempotency_key, decision_id, reservation_id, reason, detail
) VALUES (%s, %s, %s, %s, %s, %s)
"""

_LOCK_AUTHORIZATION = """
SELECT probe_id FROM app.live_probe_authorizations WHERE probe_id = %s FOR UPDATE
"""

# Read after the lock is held: a clock in the locking statement is taken before the
# wait for the lock ends, so a deadline could pass unseen while another writer held it.
_READ_AUTHORIZATION = """
SELECT *, clock_timestamp() AS db_now FROM app.live_probe_authorizations WHERE probe_id = %s
"""

# A trade's costs are settled once the cost worker of #547 wrote them from the exchange.
_LEDGER = """
SELECT
    count(*) FILTER (WHERE r.state IN ('reserved', 'sending', 'placed')) AS entries_spent,
    count(*) FILTER (
        WHERE r.state IN ('reserved', 'sending')
           OR (r.state = 'placed' AND (t.id IS NULL OR t.status <> 'closed'))
    ) AS slots_held,
    COALESCE(sum(r.reserve_usd) FILTER (
        WHERE r.state IN ('reserved', 'sending')
           OR (r.state = 'placed' AND (t.id IS NULL OR NOT (
               t.status = 'closed' AND t.accounting_version = %(costs_version)s
               AND t.accounting_status IN ('complete', 'operator_settled'))))
    ), 0) AS reserved_open,
    COALESCE(sum(t.pnl_usd) FILTER (
        WHERE r.state = 'placed'
          AND t.status = 'closed' AND t.accounting_version = %(costs_version)s
          AND t.accounting_status IN ('complete', 'operator_settled')
    ), 0) AS realized_closed,
    count(*) FILTER (
        WHERE r.state = 'placed' AND t.status = 'closed' AND NOT (
            t.accounting_version = %(costs_version)s
            AND t.accounting_status IN ('complete', 'operator_settled'))
    ) AS closed_unsettled
FROM app.live_probe_reservations AS r
LEFT JOIN app.trades AS t ON t.id = r.trade_id
WHERE r.probe_id = %(probe_id)s
"""

_SELECT_RESERVATION_BY_KEY = """
SELECT * FROM app.live_probe_reservations WHERE probe_id = %s AND idempotency_key = %s
"""

# The deadline is checked again by the statement that writes the reservation, and by
# the table's trigger.
_INSERT_RESERVATION = """
INSERT INTO app.live_probe_reservations (
    probe_id, idempotency_key, decision_id, client_order_id, native_market_id, side,
    notional_usd, reserve_usd, reserve_inputs, state
)
SELECT a.probe_id, %s, %s, %s, %s, %s, %s, %s, %s, 'reserved'
FROM app.live_probe_authorizations AS a
WHERE a.probe_id = %s AND clock_timestamp() < a.entries_until
RETURNING *
"""

_BEGIN_SENDING = """
UPDATE app.live_probe_reservations AS r
SET state = 'sending', sending_at = clock_timestamp()
FROM app.live_probe_authorizations AS a
WHERE r.id = %s AND r.state = 'reserved'
  AND a.probe_id = r.probe_id AND clock_timestamp() < a.entries_until
RETURNING r.*
"""

_REFUSE_BEFORE_SEND = """
UPDATE app.live_probe_reservations
SET state = 'refused_before_send', state_reason = %s, finished_at = clock_timestamp()
WHERE id = %s
"""

_MARK_L_REACHED = """
UPDATE app.live_probe_authorizations
SET l_reached_at = clock_timestamp(), l_reached_loss_usd = %s, l_reached_inputs = %s
WHERE probe_id = %s AND l_reached_at IS NULL
"""


async def _connect(db_url: str) -> psycopg.AsyncConnection[dict[str, Any]]:
    return await psycopg.AsyncConnection.connect(db_url, row_factory=dict_row)


async def lock_record(
    cursor: psycopg.AsyncCursor[dict[str, Any]], probe_id: str
) -> dict[str, Any] | None:
    """Take the record's row lock, then read it with the database clock."""
    await cursor.execute(_LOCK_AUTHORIZATION, (probe_id,))
    if await cursor.fetchone() is None:
        return None
    await cursor.execute(_READ_AUTHORIZATION, (probe_id,))
    return await cursor.fetchone()


async def ledger(cursor: psycopg.AsyncCursor[dict[str, Any]], probe_id: str) -> Ledger:
    await cursor.execute(
        _LEDGER, {"probe_id": probe_id, "costs_version": LIVE_EXCHANGE_COSTS_ACCOUNTING_VERSION}
    )
    row = await cursor.fetchone()
    assert row is not None
    return Ledger(
        entries_spent=int(row["entries_spent"]),
        slots_held=int(row["slots_held"]),
        reserved_open=Decimal(row["reserved_open"]),
        realized_closed=Decimal(row["realized_closed"]),
        closed_unsettled=int(row["closed_unsettled"]),
    )


def _blocker(record: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The first reason the record admits no entry now, from its own fields."""
    if record["revoked_at"] is not None:
        return REFUSED_REVOKED, {"revoked_at": record["revoked_at"].isoformat()}
    if record["suspended_at"] is not None:
        return REFUSED_SUSPENDED, {"suspended_reason": record["suspended_reason"]}
    if record["l_reached_at"] is not None:
        return REFUSED_L_REACHED, {"l_reached_at": record["l_reached_at"].isoformat()}
    if record["costs_incident_at"] is not None:
        return REFUSED_COSTS_INCIDENT, {"reason": record["costs_incident_reason"]}
    if record["db_now"] >= record["entries_until"]:
        return REFUSED_ENTRIES_CLOSED, {"entries_until": record["entries_until"].isoformat()}
    return None


async def _refuse(
    cursor: psycopg.AsyncCursor[dict[str, Any]],
    *,
    probe_id: str,
    key: str | None,
    decision_id: str | None,
    reason: str,
    detail: dict[str, Any],
    reservation_id: int | None = None,
) -> Refused:
    await cursor.execute(
        _INSERT_REFUSAL, (probe_id, key, decision_id, reservation_id, reason, Jsonb(detail))
    )
    log.warning("live_probe.entry_refused", probe_id=probe_id, reason=reason, detail=detail)
    return Refused(reason=reason, detail=detail)


async def issue(db_url: str, terms: Terms, *, actor: str) -> dict[str, Any]:
    terms.validate()
    params = {
        "probe_id": terms.probe_id,
        "environment": terms.environment,
        "account_key": terms.account_key,
        "strategy": terms.strategy,
        "contract_version": terms.contract_version,
        "contract_sha256": terms.contract_sha256,
        "instruments": list(terms.instruments),
        "max_entries": terms.max_entries,
        "notional_usd": terms.notional_usd,
        "loss_threshold_usd": terms.loss_threshold_usd,
        "entries_until": terms.entries_until,
        "flat_by": terms.flat_by,
        "actor": actor,
    }
    async with await _connect(db_url) as conn, conn.transaction(), conn.cursor() as cursor:
        await cursor.execute(_INSERT_AUTHORIZATION, params)
        record = await cursor.fetchone()
        if record is None:
            raise ValueError("entries_until must be after the database clock")
        await cursor.execute(
            _INSERT_EVENT, (terms.probe_id, "issued", actor, Jsonb({"id": record["id"]}))
        )
    log.info("live_probe.issued", probe_id=terms.probe_id, actor=actor)
    return record


async def _set_field(
    db_url: str,
    probe_id: str,
    *,
    event: str,
    actor: str,
    detail: dict[str, Any],
    sql: str,
    params: tuple[Any, ...],
    applies: Callable[[dict[str, Any]], bool],
) -> bool:
    """Lock the record, change it only when ``applies`` holds for it, and write the
    event. Returns whether anything changed."""
    async with await _connect(db_url) as conn, conn.transaction(), conn.cursor() as cursor:
        record = await lock_record(cursor, probe_id)
        if record is None:
            raise LookupError(f"no authorization record {probe_id!r}")
        if not applies(record):
            return False
        await cursor.execute(sql, params)
        await cursor.execute(_INSERT_EVENT, (probe_id, event, actor, Jsonb(detail)))
    log.info(f"live_probe.{event}", probe_id=probe_id, actor=actor, detail=detail)
    return True


async def revoke(db_url: str, probe_id: str, *, actor: str, reason: str) -> bool:
    return await _set_field(
        db_url,
        probe_id,
        event="revoked",
        actor=actor,
        detail={"reason": reason},
        sql="UPDATE app.live_probe_authorizations SET revoked_at = clock_timestamp(),"
        " revoked_by = %s, revoke_reason = %s WHERE probe_id = %s",
        params=(actor, reason, probe_id),
        applies=lambda r: r["revoked_at"] is None,
    )


async def resume(db_url: str, probe_id: str, *, actor: str) -> bool:
    """The owner's resume after a restart suspended the record (section 5)."""
    return await _set_field(
        db_url,
        probe_id,
        event="resumed",
        actor=actor,
        detail={},
        sql="UPDATE app.live_probe_authorizations SET suspended_at = NULL,"
        " suspended_reason = NULL WHERE probe_id = %s",
        params=(probe_id,),
        applies=lambda r: r["suspended_at"] is not None,
    )


async def clear_costs_incident(db_url: str, probe_id: str, *, actor: str, note: str) -> bool:
    """Only the owner clears a costs incident (section 1.2)."""
    return await _set_field(
        db_url,
        probe_id,
        event="costs_incident_cleared",
        actor=actor,
        detail={"note": note},
        sql="UPDATE app.live_probe_authorizations SET costs_incident_at = NULL,"
        " costs_incident_reason = NULL WHERE probe_id = %s",
        params=(probe_id,),
        applies=lambda r: r["costs_incident_at"] is not None,
    )


async def suspend_active_in(
    cursor: psycopg.AsyncCursor[dict[str, Any]], *, reason: str
) -> list[str]:
    """Suspend every unrevoked record not suspended yet, inside the caller's
    transaction. Returns their probe ids."""
    await cursor.execute(
        "UPDATE app.live_probe_authorizations"
        " SET suspended_at = clock_timestamp(), suspended_reason = %s"
        " WHERE revoked_at IS NULL AND suspended_at IS NULL"
        " RETURNING probe_id",
        (reason,),
    )
    probe_ids = sorted(row["probe_id"] for row in await cursor.fetchall())
    for probe_id in probe_ids:
        await cursor.execute(
            _INSERT_EVENT, (probe_id, "suspended", SERVICE_ACTOR, Jsonb({"reason": reason}))
        )
    return probe_ids


async def suspend_active(db_url: str, *, reason: str) -> list[str]:
    """Suspend every unrevoked record not suspended yet, as every start of the execution
    service does before any probe evaluation (section 5). Returns their probe ids."""
    async with await _connect(db_url) as conn, conn.transaction(), conn.cursor() as cursor:
        probe_ids = await suspend_active_in(cursor, reason=reason)
    for probe_id in probe_ids:
        log.warning("live_probe.suspended", probe_id=probe_id, reason=reason)
    return probe_ids


async def reserve(db_url: str, request: EntryRequest) -> Reservation | Refused:
    """Hold the slot, a unit of N and the entry's L reserve in one transaction, or
    refuse and record why. A repeated delivery returns the existing reservation."""
    key = idempotency_key(request.probe_id, request.decision_id)
    async with await _connect(db_url) as conn, conn.transaction(), conn.cursor() as cursor:

        async def refuse(reason: str, detail: dict[str, Any]) -> Refused:
            return await _refuse(
                cursor,
                probe_id=request.probe_id,
                key=key,
                decision_id=request.decision_id,
                reason=reason,
                detail=detail,
            )

        record = await lock_record(cursor, request.probe_id)
        if record is None:
            return await refuse(REFUSED_NO_AUTHORIZATION, {})
        if (record["environment"], record["account_key"]) != (
            request.environment,
            request.account_key,
        ):
            return await refuse(
                REFUSED_FOREIGN_ACCOUNT,
                {"environment": request.environment, "record_environment": record["environment"]},
            )
        await cursor.execute(_SELECT_RESERVATION_BY_KEY, (request.probe_id, key))
        existing = await cursor.fetchone()
        if existing is not None:
            if (existing["native_market_id"], existing["side"]) != (
                request.native_market_id,
                request.side,
            ):
                raise ValueError(
                    f"decision {request.decision_id!r} of probe {request.probe_id!r} was"
                    " reserved for another instrument or side"
                )
            return _reservation(existing, created=False)
        blocker = _blocker(record)
        if blocker is not None:
            return await refuse(*blocker)
        if request.native_market_id not in record["instruments"]:
            return await refuse(REFUSED_INSTRUMENT, {"instrument": request.native_market_id})
        held = await ledger(cursor, request.probe_id)
        if held.closed_unsettled:
            return await refuse(REFUSED_COSTS_PENDING, {"closed_unsettled": held.closed_unsettled})
        threshold = Decimal(record["loss_threshold_usd"])
        if -held.realized_closed >= threshold:
            inputs = {
                "source": "admission",
                "realized_closed": str(held.realized_closed),
                "loss_threshold_usd": str(threshold),
            }
            await cursor.execute(
                _MARK_L_REACHED, (-held.realized_closed, Jsonb(inputs), request.probe_id)
            )
            await cursor.execute(
                _INSERT_EVENT, (request.probe_id, "l_reached", SERVICE_ACTOR, Jsonb(inputs))
            )
            return await refuse(REFUSED_L_REACHED, inputs)
        if held.slots_held >= SLOTS:
            return await refuse(REFUSED_SLOT, {"slots_held": held.slots_held})
        if held.entries_spent >= record["max_entries"]:
            return await refuse(
                REFUSED_MAX_ENTRIES,
                {"entries_spent": held.entries_spent, "max_entries": record["max_entries"]},
            )
        notional = Decimal(record["notional_usd"])
        reserve_usd = probe_loss.entry_reserve(notional, request.reserve_inputs)
        if not probe_loss.admits(
            realized_closed=held.realized_closed,
            reserved_open=held.reserved_open,
            reserve=reserve_usd,
            threshold=threshold,
        ):
            return await refuse(
                REFUSED_L_HEADROOM,
                {
                    "realized_closed": str(held.realized_closed),
                    "reserved_open": str(held.reserved_open),
                    "reserve": str(reserve_usd),
                    "loss_threshold_usd": str(threshold),
                },
            )
        await cursor.execute(
            _INSERT_RESERVATION,
            (
                key,
                request.decision_id,
                order_ids.new_entry_client_order_id(),
                request.native_market_id,
                request.side,
                notional,
                reserve_usd,
                Jsonb(request.reserve_inputs.as_json()),
                request.probe_id,
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            return await refuse(
                REFUSED_ENTRIES_CLOSED, {"entries_until": record["entries_until"].isoformat()}
            )
    log.info(
        "live_probe.reserved",
        probe_id=request.probe_id,
        reservation_id=row["id"],
        reserve_usd=str(reserve_usd),
    )
    return _reservation(row)


async def _lock_reservation(
    cursor: psycopg.AsyncCursor[dict[str, Any]], reservation_id: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The record's row lock first, then the reservation's: the order every writer of
    a probe takes."""
    await cursor.execute(
        "SELECT probe_id FROM app.live_probe_reservations WHERE id = %s", (reservation_id,)
    )
    found = await cursor.fetchone()
    if found is None:
        raise LookupError(f"no reservation {reservation_id}")
    record = await lock_record(cursor, found["probe_id"])
    assert record is not None
    await cursor.execute(
        "SELECT * FROM app.live_probe_reservations WHERE id = %s FOR UPDATE", (reservation_id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    return record, row


async def begin_sending(db_url: str, reservation_id: int) -> Reservation | Refused:
    """``reserved -> sending`` after the preflight passed, or ``refused_before_send``
    when the record was revoked, suspended or stopped, or ``entries_until`` passed,
    meanwhile. Only a ``sending`` reservation may reach the exchange. The deadline is
    the condition of the transition statement itself."""
    async with await _connect(db_url) as conn, conn.transaction(), conn.cursor() as cursor:
        record, row = await _lock_reservation(cursor, reservation_id)
        if row["state"] == STATE_REFUSED_BEFORE_SEND:
            return Refused(reason=row["state_reason"], detail={"reservation_id": reservation_id})
        if row["state"] != STATE_RESERVED:
            return _reservation(row, created=False)
        sending = None
        blocker = _blocker(record)
        if blocker is None:
            await cursor.execute(_BEGIN_SENDING, (reservation_id,))
            sending = await cursor.fetchone()
            if sending is None:
                blocker = (
                    REFUSED_ENTRIES_CLOSED,
                    {"entries_until": record["entries_until"].isoformat()},
                )
        if blocker is not None:
            reason, detail = blocker
            await cursor.execute(_REFUSE_BEFORE_SEND, (reason, reservation_id))
            return await _refuse(
                cursor,
                probe_id=row["probe_id"],
                key=row["idempotency_key"],
                decision_id=row["decision_id"],
                reason=reason,
                detail=detail,
                reservation_id=reservation_id,
            )
        assert sending is not None
    log.info("live_probe.sending", reservation_id=reservation_id, probe_id=sending["probe_id"])
    return _reservation(sending)


async def refuse_before_send(db_url: str, reservation_id: int, *, reason: str) -> Refused:
    """A check after the reservation failed (the preflight): release it, send nothing."""
    async with await _connect(db_url) as conn, conn.transaction(), conn.cursor() as cursor:
        _, row = await _lock_reservation(cursor, reservation_id)
        if row["state"] != STATE_RESERVED:
            raise ValueError(f"reservation {reservation_id} is {row['state']}, not reserved")
        await cursor.execute(_REFUSE_BEFORE_SEND, (reason, reservation_id))
        return await _refuse(
            cursor,
            probe_id=row["probe_id"],
            key=row["idempotency_key"],
            decision_id=row["decision_id"],
            reason=reason,
            detail={},
            reservation_id=reservation_id,
        )


# The steps after the send. The record's trigger checks each one again.
_LINK_ATTEMPT = """
UPDATE app.live_probe_reservations AS r
SET attempt_id = o.id
FROM app.live_order_attempts AS o, app.live_probe_authorizations AS a
WHERE r.id = %s AND r.state = 'sending' AND r.attempt_id IS NULL
  AND o.id = %s AND o.operation = 'entry'
  AND o.client_order_id = r.client_order_id AND o.exchange = a.exchange
  AND o.native_market_id = r.native_market_id AND o.side = r.side
  AND a.probe_id = r.probe_id
"""

_MARK_PLACED = """
UPDATE app.live_probe_reservations SET state = 'placed', finished_at = clock_timestamp()
WHERE id = %s AND state = 'sending' AND attempt_id IS NOT NULL
"""

_MARK_RELEASED = """
UPDATE app.live_probe_reservations
SET state = 'released', state_reason = %s, finished_at = clock_timestamp()
WHERE id = %s AND state = 'sending'
"""

_LINK_TRADE = """
UPDATE app.live_probe_reservations AS r
SET trade_id = o.trade_id
FROM app.live_order_attempts AS o
WHERE r.id = %s AND r.state = 'placed' AND r.trade_id IS NULL
  AND o.id = r.attempt_id AND o.trade_id = %s
"""


async def _apply_step(
    cursor: psycopg.AsyncCursor[dict[str, Any]],
    reservation_id: int,
    sql: str,
    params: tuple[Any, ...],
) -> None:
    await cursor.execute(sql, params)
    if cursor.rowcount != 1:
        raise ValueError(f"reservation {reservation_id}: step refused")


async def _update_sent(
    db_url: str,
    reservation_id: int,
    sql: str,
    params: tuple[Any, ...],
    *,
    done: Callable[[dict[str, Any]], bool],
) -> None:
    """Apply one step after the send. A repeat of a step already applied changes
    nothing; a step that does not fit the reservation's state raises."""
    async with await _connect(db_url) as conn, conn.transaction(), conn.cursor() as cursor:
        _, row = await _lock_reservation(cursor, reservation_id)
        if done(row):
            return
        await cursor.execute(sql, params)
        if cursor.rowcount != 1:
            raise ValueError(
                f"reservation {reservation_id} is {row['state']}"
                f" (attempt {row['attempt_id']}, trade {row['trade_id']}): step refused"
            )


async def link_attempt(db_url: str, reservation_id: int, attempt_id: int) -> None:
    """The durable entry attempt written for a ``sending`` reservation: its own
    ``clientOrderId``, on the record's exchange, instrument and side."""
    await _update_sent(
        db_url,
        reservation_id,
        _LINK_ATTEMPT,
        (reservation_id, attempt_id),
        done=lambda r: r["attempt_id"] == attempt_id,
    )


async def mark_placed(db_url: str, reservation_id: int) -> None:
    """A fill spends N; the slot and reserve stay until the trade closes and settles."""
    await _update_sent(
        db_url,
        reservation_id,
        _MARK_PLACED,
        (reservation_id,),
        done=lambda r: r["state"] == STATE_PLACED,
    )


async def mark_released(db_url: str, reservation_id: int, *, reason: str) -> None:
    """Reconciliation proved the order was never placed or never filled."""
    await _update_sent(
        db_url,
        reservation_id,
        _MARK_RELEASED,
        (reason, reservation_id),
        done=lambda r: r["state"] == STATE_RELEASED,
    )


async def link_trade(db_url: str, reservation_id: int, trade_id: int) -> None:
    """The trade journalled from the ``placed`` reservation's own attempt."""
    await _update_sent(
        db_url,
        reservation_id,
        _LINK_TRADE,
        (reservation_id, trade_id),
        done=lambda r: r["trade_id"] == trade_id,
    )


async def link_attempt_in(
    cursor: psycopg.AsyncCursor[dict[str, Any]], reservation_id: int, attempt_id: int
) -> None:
    """``link_attempt`` on a reservation the caller's transaction already locked."""
    await _apply_step(cursor, reservation_id, _LINK_ATTEMPT, (reservation_id, attempt_id))


async def mark_placed_in(cursor: psycopg.AsyncCursor[dict[str, Any]], reservation_id: int) -> None:
    """``mark_placed`` on a reservation the caller's transaction already locked."""
    await _apply_step(cursor, reservation_id, _MARK_PLACED, (reservation_id,))


async def mark_released_in(
    cursor: psycopg.AsyncCursor[dict[str, Any]], reservation_id: int, *, reason: str
) -> None:
    """``mark_released`` on a reservation the caller's transaction already locked."""
    await _apply_step(cursor, reservation_id, _MARK_RELEASED, (reason, reservation_id))


async def link_trade_in(
    cursor: psycopg.AsyncCursor[dict[str, Any]], reservation_id: int, trade_id: int
) -> None:
    """``link_trade`` on a reservation the caller's transaction already locked."""
    await _apply_step(cursor, reservation_id, _LINK_TRADE, (reservation_id, trade_id))


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


async def snapshot(db_url: str, probe_id: str) -> dict[str, Any]:
    """The record, its history, its reservations, its refusals and what they hold."""
    async with await _connect(db_url) as conn, conn.cursor() as cursor:
        await cursor.execute(
            "SELECT * FROM app.live_probe_authorizations WHERE probe_id = %s", (probe_id,)
        )
        record = await cursor.fetchone()
        if record is None:
            raise LookupError(f"no authorization record {probe_id!r}")
        sections: dict[str, Any] = {"authorization": record}
        for name, table, order in (
            ("events", "live_probe_authorization_events", "id"),
            ("reservations", "live_probe_reservations", "id"),
            ("refusals", "live_probe_refusals", "id"),
        ):
            await cursor.execute(
                f"SELECT * FROM app.{table} WHERE probe_id = %s ORDER BY {order}",  # noqa: S608 -- table and order come from the fixed tuple below
                (probe_id,),
            )
            sections[name] = await cursor.fetchall()
        held = await ledger(cursor, probe_id)
        sections["ledger"] = asdict(held)
    return sections


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("a time needs its zone, e.g. 2026-11-20T12:00:00+00:00")
    return parsed


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    issue_cmd = sub.add_parser("issue", help="write a new authorization record")
    issue_cmd.add_argument("--probe-id", required=True)
    issue_cmd.add_argument("--environment", choices=ENVIRONMENTS, required=True)
    issue_cmd.add_argument("--account-key", required=True)
    issue_cmd.add_argument("--strategy", required=True)
    issue_cmd.add_argument("--contract-version", required=True)
    issue_cmd.add_argument("--contract-sha256", required=True)
    issue_cmd.add_argument("--instrument", action="append", required=True)
    issue_cmd.add_argument("--max-entries", type=int, required=True)
    issue_cmd.add_argument("--notional-usd", type=Decimal, required=True)
    issue_cmd.add_argument("--loss-threshold-usd", type=Decimal, required=True)
    issue_cmd.add_argument("--entries-until", type=_parse_time, required=True)
    issue_cmd.add_argument("--flat-by", type=_parse_time, required=True)

    revoke_cmd = sub.add_parser("revoke", help="stop every further entry of a probe")
    revoke_cmd.add_argument("--probe-id", required=True)
    revoke_cmd.add_argument("--reason", required=True)

    resume_cmd = sub.add_parser("resume", help="resume a record a restart suspended")
    resume_cmd.add_argument("--probe-id", required=True)

    clear_cmd = sub.add_parser("clear-costs-incident", help="clear a costs incident")
    clear_cmd.add_argument("--probe-id", required=True)
    clear_cmd.add_argument("--note", required=True)

    show_cmd = sub.add_parser("show", help="print the record and what it holds")
    show_cmd.add_argument("--probe-id", required=True)

    export_cmd = sub.add_parser("export", help="write the audit file and its sha256")
    export_cmd.add_argument("--probe-id", required=True)
    export_cmd.add_argument("--out", type=Path, required=True)

    for command in (issue_cmd, revoke_cmd, resume_cmd, clear_cmd):
        command.add_argument("--by", required=True, help="who does it")
    args = parser.parse_args(argv)

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        sys.exit("DATABASE_URL is required")

    result: Any
    if args.command == "issue":
        result = asyncio.run(
            issue(
                db_url,
                Terms(
                    probe_id=args.probe_id,
                    environment=args.environment,
                    account_key=args.account_key,
                    strategy=args.strategy,
                    contract_version=args.contract_version,
                    contract_sha256=args.contract_sha256,
                    instruments=tuple(args.instrument),
                    max_entries=args.max_entries,
                    notional_usd=args.notional_usd,
                    loss_threshold_usd=args.loss_threshold_usd,
                    entries_until=args.entries_until,
                    flat_by=args.flat_by,
                ),
                actor=args.by,
            )
        )
    elif args.command == "revoke":
        result = {
            "revoked": asyncio.run(revoke(db_url, args.probe_id, actor=args.by, reason=args.reason))
        }
    elif args.command == "resume":
        result = {"resumed": asyncio.run(resume(db_url, args.probe_id, actor=args.by))}
    elif args.command == "clear-costs-incident":
        result = {
            "cleared": asyncio.run(
                clear_costs_incident(db_url, args.probe_id, actor=args.by, note=args.note)
            )
        }
    elif args.command == "show":
        result = asyncio.run(snapshot(db_url, args.probe_id))
    else:
        body = json.dumps(
            asyncio.run(snapshot(db_url, args.probe_id)),
            default=_json_default,
            indent=2,
            sort_keys=True,
        ).encode()
        args.out.write_bytes(body)
        digest = hashlib.sha256(body).hexdigest()
        args.out.with_name(args.out.name + ".sha256").write_text(f"{digest}  {args.out.name}\n")
        result = {"file": str(args.out), "sha256": digest}
    sys.stdout.write(json.dumps(result, default=_json_default, indent=2, sort_keys=True) + "\n")
