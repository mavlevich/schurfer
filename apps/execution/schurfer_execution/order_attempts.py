"""Durable pre-flight record for a live order, written BEFORE the exchange
is ever called.

orders.place_order creates a row here first, with a locally-generated
client_order_id that is then passed to the exchange as clientOrderId
(bybit: orderLinkId). If this write itself fails, place_order fails closed
and never calls the exchange -- closing the one remaining window where a
real exchange order could exist with no durable trace anywhere, even during
a full Postgres outage (both the normal journal write and the incident
fallback on its failure target the same Postgres instance an outage would
also take down; this write happens strictly before either, and gates
whether the exchange is ever called at all).

Every update after creation (mark_accepted/mark_completed/mark_failed) is
best-effort: if one of them fails, the row simply stays at its last known
status, which is still enough for a human to reconcile by client_order_id
against the exchange directly. Only the initial create is fail-closed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import psycopg
import structlog
from psycopg.rows import dict_row

log = structlog.get_logger()

STATUS_PENDING = "pending"
STATUS_ACCEPTED = "accepted"
STATUS_PARTIAL = "partial"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_SUBMISSION_UNKNOWN = "submission_unknown"
STATUS_NO_FILL = "no_fill"
STATUS_MANUAL_REQUIRED = "manual_required"

# One transaction-scoped lock serializes the short database boundary that
# counts portfolio occupancy and inserts the next entry intent.  The lock is
# global on purpose: MAX_POSITIONS is global across exchanges/instruments.
_PORTFOLIO_RESERVATION_LOCK_ID = 2026091003


@dataclass(frozen=True)
class PortfolioCapacityReached:
    occupied_slots: int
    max_positions: int


_INSERT = """
INSERT INTO app.live_order_attempts (
    operation, client_order_id, exchange, base, symbol, native_market_id, market_type,
    side, size_usd, requested_amount, leverage, contract_size, exit_params, setup_context, status
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s
)
RETURNING id
"""

_COUNT_DURABLE_ONLY_ENTRY_SLOTS = """
WITH observed(exchange, base) AS (
    SELECT * FROM unnest(%s::text[], %s::text[])
), durable_slots AS (
    SELECT DISTINCT a.exchange, upper(a.base) AS base
    FROM app.live_order_attempts AS a
    LEFT JOIN app.trades AS t ON t.id = a.trade_id
    WHERE a.operation = 'entry'
      AND (
          a.status IN ('pending', 'accepted', 'submission_unknown', 'manual_required')
          OR (a.status = 'completed' AND (a.trade_id IS NULL OR t.status = 'open'))
      )
)
SELECT count(*)
FROM durable_slots AS d
WHERE NOT EXISTS (
    SELECT 1
    FROM observed AS o
    WHERE o.exchange = d.exchange AND upper(o.base) = d.base
)
"""

_INSERT_CLOSE = """
INSERT INTO app.live_order_attempts (
    operation, client_order_id, exchange, base, symbol, native_market_id, market_type,
    side, size_usd, requested_amount, leverage, contract_size, exit_params,
    setup_context, status, trade_id
) VALUES (
    'close', %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s, '{}'::jsonb, %s::jsonb, %s, %s
)
RETURNING id
"""

_SELECT_RECOVERABLE_CLOSES = """
SELECT
    a.id, a.client_order_id, a.exchange, a.base, a.symbol, a.side, a.status, a.order_id,
    a.requested_amount, a.filled_amount, a.trade_id, a.setup_context
FROM app.live_order_attempts AS a
LEFT JOIN app.trades AS t ON t.id = a.trade_id
WHERE a.operation = 'close'
  AND (
      a.status IN ('pending', 'accepted', 'submission_unknown')
      OR (a.status = 'completed' AND t.status = 'open')
  )
ORDER BY a.created_at
"""

_MARK_ACCEPTED = """
UPDATE app.live_order_attempts
SET status = %s, order_id = %s, updated_at = now()
WHERE id = %s
"""

_MARK_COMPLETED = """
UPDATE app.live_order_attempts
SET status = %s, trade_id = %s, filled_amount = COALESCE(%s, filled_amount), updated_at = now()
WHERE id = %s
"""

_MARK_PARTIAL = """
UPDATE app.live_order_attempts
SET status = %s, trade_id = %s, filled_amount = %s, updated_at = now()
WHERE id = %s
"""

_MARK_FAILED = """
UPDATE app.live_order_attempts
SET status = %s, last_error = %s, updated_at = now()
WHERE id = %s
"""

_MARK_SUBMISSION_UNKNOWN = """
UPDATE app.live_order_attempts
SET status = %s, last_error = %s, updated_at = now()
WHERE id = %s
"""

_LINK_COMPLETED_TRADE = """
UPDATE app.live_order_attempts
SET status = %s,
    trade_id = %s,
    filled_amount = COALESCE(%s, filled_amount),
    reconciliation_timestamp = now(),
    reconciliation_error = NULL,
    updated_at = now()
WHERE exchange = %s
  AND order_id = %s
  AND operation = 'entry'
  AND status IN ('accepted', 'completed', 'submission_unknown')
RETURNING id
"""


async def create_attempt(
    db_url: str,
    *,
    client_order_id: str,
    exchange: str,
    base: str,
    symbol: str,
    native_market_id: str | None = None,
    market_type: str | None = None,
    side: str,
    size_usd: float,
    requested_amount: float | None = None,
    leverage: int,
    contract_size: float | None,
    exit_params: dict[str, float],
    setup_context: dict[str, Any],
    open_positions: list[dict[str, Any]],
    max_positions: int,
) -> int | PortfolioCapacityReached | None:
    """Atomically reserve one global portfolio slot and persist entry intent.

    ``open_positions`` is the caller's exchange snapshot.  Durable attempts
    cover the race after that snapshot: under one Postgres advisory lock we
    count attempts which may represent exposure but are not already reflected
    by the snapshot, then insert this attempt only when capacity remains.

    Returns ``None`` on any database failure, so the caller fails closed.
    ``PortfolioCapacityReached`` is a normal admission denial, not an outage.
    """
    try:
        aconn = await psycopg.AsyncConnection.connect(db_url)
        async with aconn, aconn.cursor() as cur:
            await cur.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_PORTFOLIO_RESERVATION_LOCK_ID,),
            )
            if await cur.fetchone() is None:
                raise RuntimeError("portfolio reservation lock returned no row")
            observed_pairs = sorted(
                {
                    (str(position.get("exchange", "")), str(position.get("base", "")).upper())
                    for position in open_positions
                    if position.get("exchange") and position.get("base")
                }
            )
            await cur.execute(
                _COUNT_DURABLE_ONLY_ENTRY_SLOTS,
                (
                    [exchange_name for exchange_name, _base in observed_pairs],
                    [observed_base for _exchange_name, observed_base in observed_pairs],
                ),
            )
            count_row = await cur.fetchone()
            if count_row is None:
                raise RuntimeError("portfolio reservation count returned no row")
            occupied_slots = len(open_positions) + int(count_row[0])
            if occupied_slots >= max_positions:
                log.info(
                    "order_attempts.portfolio_capacity_reached",
                    occupied_slots=occupied_slots,
                    max_positions=max_positions,
                    exchange=exchange,
                    base=base,
                )
                return PortfolioCapacityReached(
                    occupied_slots=occupied_slots,
                    max_positions=max_positions,
                )

            await cur.execute(
                _INSERT,
                (
                    "entry",
                    client_order_id,
                    exchange,
                    base.upper(),
                    symbol,
                    native_market_id,
                    market_type,
                    side,
                    size_usd,
                    requested_amount,
                    leverage,
                    contract_size,
                    json.dumps(exit_params),
                    json.dumps(setup_context),
                    STATUS_PENDING,
                ),
            )
            row = await cur.fetchone()
            return int(row[0]) if row else None
    except Exception as exc:
        log.error(
            "order_attempts.create_failed",
            client_order_id=client_order_id,
            exchange=exchange,
            base=base,
            err=str(exc),
        )
        return None


async def create_close_attempt(
    db_url: str,
    *,
    client_order_id: str,
    exchange: str,
    base: str,
    symbol: str,
    native_market_id: str | None,
    market_type: str | None,
    side: str,
    size_usd: float,
    requested_amount: float,
    contract_size: float,
    trade_id: int | None,
    context: dict[str, Any],
) -> int | None:
    """Best-effort durable close intent written before the exchange call.

    Unlike an entry, a protective exit must still proceed during a database
    outage.  Returning None therefore revokes PnL readiness at the caller but
    does not block the reduce-only order.
    """
    try:
        async with await psycopg.AsyncConnection.connect(db_url) as aconn, aconn.cursor() as cur:
            await cur.execute(
                _INSERT_CLOSE,
                (
                    client_order_id,
                    exchange,
                    base.upper(),
                    symbol,
                    native_market_id,
                    market_type,
                    side,
                    size_usd,
                    requested_amount,
                    contract_size,
                    json.dumps(context),
                    STATUS_PENDING,
                    trade_id,
                ),
            )
            row = await cur.fetchone()
            return int(row[0]) if row else None
    except Exception as exc:
        log.error(
            "order_attempts.create_close_failed",
            client_order_id=client_order_id,
            exchange=exchange,
            base=base,
            err=str(exc),
        )
        return None


@dataclass(frozen=True)
class CloseAttempt:
    id: int
    client_order_id: str
    exchange: str
    base: str
    symbol: str
    side: str
    status: str
    order_id: str | None
    requested_amount: float
    filled_amount: float | None
    trade_id: int | None
    context: dict[str, Any]


async def load_recoverable_close_attempts(db_url: str) -> list[CloseAttempt]:
    try:
        async with (
            await psycopg.AsyncConnection.connect(db_url) as aconn,
            aconn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(_SELECT_RECOVERABLE_CLOSES)
            rows = await cur.fetchall()
        return [
            CloseAttempt(
                id=int(row["id"]),
                client_order_id=str(row["client_order_id"]),
                exchange=str(row["exchange"]),
                base=str(row["base"]),
                symbol=str(row["symbol"]),
                side=str(row["side"]),
                status=str(row["status"]),
                order_id=str(row["order_id"]) if row["order_id"] is not None else None,
                requested_amount=float(row["requested_amount"]),
                filled_amount=(
                    float(row["filled_amount"]) if row["filled_amount"] is not None else None
                ),
                trade_id=int(row["trade_id"]) if row["trade_id"] is not None else None,
                context=row["setup_context"] if isinstance(row["setup_context"], dict) else {},
            )
            for row in rows
        ]
    except Exception as exc:
        log.error("order_attempts.load_recoverable_closes_failed", err=str(exc))
        return []


async def mark_accepted(db_url: str, attempt_id: int, *, order_id: str) -> None:
    """Best-effort: the exchange has already confirmed the order at this
    point regardless of whether this update lands -- the row's
    client_order_id is still enough to find it on the exchange later even
    if order_id never gets recorded here."""
    try:
        aconn = await psycopg.AsyncConnection.connect(db_url)
        async with aconn, aconn.cursor() as cur:
            await cur.execute(_MARK_ACCEPTED, (STATUS_ACCEPTED, order_id, attempt_id))
    except Exception as exc:
        log.error("order_attempts.mark_accepted_failed", attempt_id=attempt_id, err=str(exc))


async def mark_completed(
    db_url: str, attempt_id: int, *, trade_id: int | None, filled_amount: float | None = None
) -> None:
    """Best-effort. trade_id may be None (the journal write itself failed
    even though the exchange fill succeeded) -- still marks the exchange
    side of this attempt as done; incidents.py/incident_worker.py own
    retrying the journal write from here, this row is not itself a retry
    queue."""
    try:
        aconn = await psycopg.AsyncConnection.connect(db_url)
        async with aconn, aconn.cursor() as cur:
            await cur.execute(
                _MARK_COMPLETED, (STATUS_COMPLETED, trade_id, filled_amount, attempt_id)
            )
    except Exception as exc:
        log.error("order_attempts.mark_completed_failed", attempt_id=attempt_id, err=str(exc))


async def mark_partial(
    db_url: str, attempt_id: int, *, trade_id: int, filled_amount: float
) -> bool:
    """Finish this order attempt while its parent position remains open."""
    try:
        async with await psycopg.AsyncConnection.connect(db_url) as aconn, aconn.cursor() as cur:
            await cur.execute(
                _MARK_PARTIAL,
                (STATUS_PARTIAL, trade_id, filled_amount, attempt_id),
            )
            return cur.rowcount > 0
    except Exception as exc:
        log.error("order_attempts.mark_partial_failed", attempt_id=attempt_id, err=str(exc))
        return False


async def mark_failed(db_url: str, attempt_id: int, *, error: str) -> None:
    """Record an explicit exchange rejection or a pre-submission failure.

    Ambiguous network errors must use :func:`mark_submission_unknown`; they
    cannot safely be treated as proof that no real order exists.
    """
    try:
        aconn = await psycopg.AsyncConnection.connect(db_url)
        async with aconn, aconn.cursor() as cur:
            await cur.execute(_MARK_FAILED, (STATUS_FAILED, error[:1000], attempt_id))
    except Exception as exc:
        log.error("order_attempts.mark_failed_failed", attempt_id=attempt_id, err=str(exc))


async def mark_submission_unknown(db_url: str, attempt_id: int, *, error: str) -> None:
    """The exchange call timed out or failed to return a determinable result.
    The order might exist on the exchange, must be reconciled later."""
    try:
        aconn = await psycopg.AsyncConnection.connect(db_url)
        async with aconn, aconn.cursor() as cur:
            await cur.execute(
                _MARK_SUBMISSION_UNKNOWN,
                (STATUS_SUBMISSION_UNKNOWN, error[:1000], attempt_id),
            )
    except Exception as exc:
        log.error(
            "order_attempts.mark_submission_unknown_failed", attempt_id=attempt_id, err=str(exc)
        )


async def link_completed_trade(
    db_url: str,
    *,
    exchange: str,
    order_id: str,
    trade_id: int,
    filled_amount: float | None,
) -> bool:
    """Durably link an incident-recovered open to its exact pre-flight attempt."""
    try:
        aconn = await psycopg.AsyncConnection.connect(db_url)
        async with aconn, aconn.cursor() as cur:
            await cur.execute(
                _LINK_COMPLETED_TRADE,
                (
                    STATUS_COMPLETED,
                    trade_id,
                    filled_amount,
                    exchange,
                    order_id,
                ),
            )
            row = await cur.fetchone()
            if row is None:
                log.warning(
                    "order_attempts.link_completed_trade_missing",
                    exchange=exchange,
                    order_id=order_id,
                    trade_id=trade_id,
                )
            return True
    except Exception as exc:
        log.error(
            "order_attempts.link_completed_trade_failed",
            exchange=exchange,
            order_id=order_id,
            trade_id=trade_id,
            err=str(exc),
        )
        return False
