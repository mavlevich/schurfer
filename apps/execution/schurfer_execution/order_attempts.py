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
from typing import Any

import psycopg
import structlog

log = structlog.get_logger()

STATUS_PENDING = "pending"
STATUS_ACCEPTED = "accepted"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_SUBMISSION_UNKNOWN = "submission_unknown"
STATUS_NO_FILL = "no_fill"
STATUS_MANUAL_REQUIRED = "manual_required"

_INSERT = """
INSERT INTO app.live_order_attempts (
    client_order_id, exchange, base, symbol, native_market_id, market_type,
    side, size_usd, requested_amount, leverage, contract_size, exit_params, setup_context, status
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s
)
RETURNING id
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
) -> int | None:
    """The one fail-closed write in this module -- see the module
    docstring. Returns None on any failure (including a full DB outage);
    the caller must refuse to place the order in that case, not proceed as
    if this succeeded."""
    try:
        aconn = await psycopg.AsyncConnection.connect(db_url)
        async with aconn, aconn.cursor() as cur:
            await cur.execute(
                _INSERT,
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
