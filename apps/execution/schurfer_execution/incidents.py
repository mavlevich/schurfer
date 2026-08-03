"""Durable unresolved-fill incidents.

Created when fill_price.resolve_fill_price cannot confirm a price. A row here is
the source of truth that a position/order needs attention — it survives process
restarts, Redis eviction, and lets a bounded worker retry without ever risking a
duplicate order for the same exchange order id (idempotent on exchange+order_id).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg
import structlog

log = structlog.get_logger()

STATUS_PENDING = "pending"
STATUS_RESOLVING = "resolving"
STATUS_RESOLVED = "resolved"
STATUS_MANUAL_REQUIRED = "manual_required"

OPEN_STATUSES = (STATUS_PENDING, STATUS_RESOLVING)

_INSERT = """
INSERT INTO app.fill_resolution_incidents (
    exchange, base, operation, order_id, trade_id, status, attempt_count, context,
    created_at, updated_at
) VALUES (
    %s, %s, %s, %s, %s, %s, 0, %s::jsonb, NOW(), NOW()
)
ON CONFLICT (exchange, order_id) DO NOTHING
RETURNING id
"""

_SELECT_BY_KEY = """
SELECT id FROM app.fill_resolution_incidents WHERE exchange = %s AND order_id = %s
"""

_SELECT_OPEN = """
SELECT id, exchange, base, operation, order_id, trade_id, status, attempt_count, context
FROM app.fill_resolution_incidents
WHERE status IN ('pending', 'resolving')
ORDER BY created_at
"""

_MARK_ATTEMPT = """
UPDATE app.fill_resolution_incidents
SET status = %s,
    attempt_count = attempt_count + 1,
    last_attempted_at = %s,
    last_error = %s,
    updated_at = NOW()
WHERE id = %s
"""

_MARK_RESOLVED = """
UPDATE app.fill_resolution_incidents
SET status = 'resolved',
    resolved_price = %s,
    resolved_source = %s,
    resolved_at = %s,
    updated_at = NOW()
WHERE id = %s
  AND status IN ('pending', 'resolving', 'manual_required')
"""

_CLAIM_CREATION_NOTIFICATION = """
UPDATE app.fill_resolution_incidents
SET notified_at = %s, updated_at = NOW()
WHERE id = %s AND notified_at IS NULL
"""

_CLAIM_RECOVERY_NOTIFICATION = """
UPDATE app.fill_resolution_incidents
SET recovery_notified_at = %s, updated_at = NOW()
WHERE id = %s AND recovery_notified_at IS NULL AND notified_at IS NOT NULL
"""

_ANY_OPEN = """
SELECT 1 FROM app.fill_resolution_incidents WHERE status IN ('pending', 'resolving') LIMIT 1
"""

_ANY_PENDING_OPEN_FOR = """
SELECT 1 FROM app.fill_resolution_incidents
WHERE operation = 'open' AND exchange = %s AND base = %s AND status IN ('pending', 'resolving')
LIMIT 1
"""


@dataclass(frozen=True)
class Incident:
    id: int
    exchange: str
    base: str
    operation: str
    order_id: str
    trade_id: int | None
    status: str
    attempt_count: int
    context: dict[str, Any]


async def create_incident(
    db_url: str,
    *,
    exchange: str,
    base: str,
    operation: str,
    order_id: str,
    trade_id: int | None,
    context: dict[str, Any],
) -> int | None:
    """Idempotent on (exchange, order_id): a retried creation returns the existing id."""
    try:
        aconn = await psycopg.AsyncConnection.connect(db_url)
        async with aconn, aconn.cursor() as cur:
            await cur.execute(
                _INSERT,
                (
                    exchange,
                    base.upper(),
                    operation,
                    order_id,
                    trade_id,
                    STATUS_PENDING,
                    json.dumps(context),
                ),
            )
            row = await cur.fetchone()
            if row is not None:
                return int(row[0])
            await cur.execute(_SELECT_BY_KEY, (exchange, order_id))
            existing = await cur.fetchone()
            return int(existing[0]) if existing else None
    except Exception as exc:
        log.error(
            "execution.incident.create_failed",
            exchange=exchange,
            base=base,
            order_id=order_id,
            err=str(exc),
        )
        return None


async def load_open_incidents(db_url: str) -> list[Incident]:
    try:
        aconn = await psycopg.AsyncConnection.connect(db_url)
        async with aconn, aconn.cursor() as cur:
            await cur.execute(_SELECT_OPEN)
            rows = await cur.fetchall()
        return [
            Incident(
                id=int(row[0]),
                exchange=row[1],
                base=row[2],
                operation=row[3],
                order_id=row[4],
                trade_id=int(row[5]) if row[5] is not None else None,
                status=row[6],
                attempt_count=int(row[7]),
                context=row[8] if isinstance(row[8], dict) else {},
            )
            for row in rows
        ]
    except Exception as exc:
        log.error("execution.incident.load_open_failed", err=str(exc))
        return []


async def mark_attempt(
    db_url: str,
    incident_id: int,
    *,
    status: str,
    error: str | None = None,
) -> None:
    try:
        aconn = await psycopg.AsyncConnection.connect(db_url)
        async with aconn, aconn.cursor() as cur:
            await cur.execute(
                _MARK_ATTEMPT,
                (status, datetime.now(UTC), error[:1000] if error else None, incident_id),
            )
    except Exception as exc:
        log.error("execution.incident.mark_attempt_failed", incident_id=incident_id, err=str(exc))


async def mark_resolved(db_url: str, incident_id: int, *, price: float, source: str) -> bool:
    try:
        aconn = await psycopg.AsyncConnection.connect(db_url)
        async with aconn, aconn.cursor() as cur:
            await cur.execute(_MARK_RESOLVED, (price, source, datetime.now(UTC), incident_id))
            return cur.rowcount > 0
    except Exception as exc:
        log.error("execution.incident.mark_resolved_failed", incident_id=incident_id, err=str(exc))
        return False


async def claim_creation_notification(db_url: str, incident_id: int) -> bool:
    """Atomic: returns True only for the caller that first claims the alert-once slot."""
    try:
        aconn = await psycopg.AsyncConnection.connect(db_url)
        async with aconn, aconn.cursor() as cur:
            await cur.execute(_CLAIM_CREATION_NOTIFICATION, (datetime.now(UTC), incident_id))
            return cur.rowcount > 0
    except Exception as exc:
        log.error(
            "execution.incident.claim_creation_notification_failed",
            incident_id=incident_id,
            err=str(exc),
        )
        return False


async def claim_recovery_notification(db_url: str, incident_id: int) -> bool:
    try:
        aconn = await psycopg.AsyncConnection.connect(db_url)
        async with aconn, aconn.cursor() as cur:
            await cur.execute(_CLAIM_RECOVERY_NOTIFICATION, (datetime.now(UTC), incident_id))
            return cur.rowcount > 0
    except Exception as exc:
        log.error(
            "execution.incident.claim_recovery_notification_failed",
            incident_id=incident_id,
            err=str(exc),
        )
        return False


async def has_pending_open(db_url: str, *, exchange: str, base: str) -> bool:
    """True if an 'open' incident for this exchange/base is still pending/resolving.

    A 'close' incident for the same position must wait for this to clear before
    resolving: journal.open_trade for the matching open may not have run yet, so
    the close's trade_id (captured once at creation, never backfilled) can be
    permanently None if it resolves first — see incident_worker._complete_close's
    fallback lookup for the remaining, rarer case (e.g. the open went to
    manual_required instead of completing).
    """
    try:
        aconn = await psycopg.AsyncConnection.connect(db_url)
        async with aconn, aconn.cursor() as cur:
            await cur.execute(_ANY_PENDING_OPEN_FOR, (exchange, base.upper()))
            return await cur.fetchone() is not None
    except Exception as exc:
        log.error(
            "execution.incident.has_pending_open_failed",
            exchange=exchange,
            base=base,
            err=str(exc),
        )
        # Fail closed: if we can't tell, don't risk resolving the close early.
        return True


async def any_open_incidents(db_url: str) -> bool:
    """True if any incident is still pending/resolving.

    Mirrors journal.any_pending_closes: the pnl tracker must not declare daily PnL
    fresh while a fill's price (and therefore its PnL impact) is still unknown.
    """
    try:
        aconn = await psycopg.AsyncConnection.connect(db_url)
        async with aconn, aconn.cursor() as cur:
            await cur.execute(_ANY_OPEN)
            return await cur.fetchone() is not None
    except Exception as exc:
        log.error("execution.incident.any_open_check_failed", err=str(exc))
        # Fail closed: an unknown incident state must not report "clear".
        return True
