"""Pure reconciliation types and decisions.

The worker deliberately keeps mutation out of this module. A reconciliation
decision may allow entry or quarantine a discrepancy, but v1 never guesses an
instrument identity, adopts an exchange position, closes one, or deletes Redis
state automatically.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

STARTUP_BLOCKER = "startup_reconciliation_pending"
SOURCE_BLOCKER = "reconciliation_source_unavailable"
INCIDENT_BLOCKER = "unresolved_reconciliation_incident"


class DiscrepancyType(enum.Enum):
    HEALTHY = "healthy"
    UNRESOLVED_ORDER_ATTEMPT = "unresolved_order_attempt"
    MISSING_REDIS = "missing_redis"
    MISSING_TRADE_EXACT_ATTEMPT = "missing_trade_exact_attempt"
    EXCHANGE_ONLY_ORPHAN = "exchange_only_orphan"
    DB_ONLY_ORPHAN = "db_only_orphan"
    REDIS_ONLY_STALE = "redis_only_stale"
    POSITION_SIZE_MISMATCH = "position_size_mismatch"
    INCOMPLETE_IDENTITY = "incomplete_identity"
    UNKNOWN_MISMATCH = "unknown_mismatch"


class ActionDirective(enum.Enum):
    ALLOW_ENTRY = "allow_entry"
    QUARANTINE_INCIDENT = "quarantine_incident"
    MANUAL_INTERVENTION_REQUIRED = "manual_intervention_required"


@dataclass(frozen=True, order=True)
class FullIdentity:
    """Canonical exchange instrument identity without synthesized fields."""

    exchange: str
    symbol: str
    native_market_id: str
    market_type: str
    side: str

    @property
    def complete(self) -> bool:
        return all(
            (
                self.exchange,
                self.symbol,
                self.native_market_id,
                self.market_type,
                self.side in {"long", "short"},
            )
        )


@dataclass(frozen=True)
class ExchangePositionSnapshot:
    identity: FullIdentity
    contracts: Decimal
    entry_price: Decimal | None
    raw_evidence: dict[str, Any]


@dataclass(frozen=True)
class OwnedTradeSnapshot:
    identity: FullIdentity
    trade_id: int
    attempt_id: int | None
    base: str
    contracts: Decimal | None
    entry_price: Decimal


@dataclass(frozen=True)
class OrderAttemptSnapshot:
    identity: FullIdentity
    attempt_id: int
    base: str
    client_order_id: str
    order_id: str | None
    status: str
    requested_amount: Decimal | None
    filled_amount: Decimal | None
    trade_id: int | None

    @property
    def unresolved(self) -> bool:
        return self.status in {
            "pending",
            "accepted",
            "submission_unknown",
            "manual_required",
        }


@dataclass(frozen=True)
class RedisPositionSnapshot:
    identity: FullIdentity
    trade_id: int
    exchange: str
    base: str


@dataclass(frozen=True)
class ReconciliationDecision:
    discrepancy: DiscrepancyType
    directive: ActionDirective
    allow_entry: bool
    incident_required: bool
    manual_required: bool
    reason: str


def _same_contracts(left: Decimal | None, right: Decimal | None) -> bool:
    if left is None or right is None:
        return False
    return abs(left - right) <= Decimal("0.00000001")


def _quarantine(
    discrepancy: DiscrepancyType,
    reason: str,
    *,
    manual_required: bool = False,
) -> ReconciliationDecision:
    return ReconciliationDecision(
        discrepancy=discrepancy,
        directive=(
            ActionDirective.MANUAL_INTERVENTION_REQUIRED
            if manual_required
            else ActionDirective.QUARANTINE_INCIDENT
        ),
        allow_entry=False,
        incident_required=True,
        manual_required=manual_required,
        reason=reason,
    )


def classify_position_state(
    identity: FullIdentity,
    exchange_snap: ExchangePositionSnapshot | None,
    db_trade: OwnedTradeSnapshot | None,
    db_attempts: list[OrderAttemptSnapshot],
    redis_snap: RedisPositionSnapshot | None,
) -> ReconciliationDecision:
    """Classify one exact identity without performing I/O or repairs."""

    if not identity.complete:
        return _quarantine(
            DiscrepancyType.INCOMPLETE_IDENTITY,
            "At least one source lacks the exact exchange instrument identity.",
            manual_required=True,
        )

    if any(attempt.unresolved for attempt in db_attempts):
        return _quarantine(
            DiscrepancyType.UNRESOLVED_ORDER_ATTEMPT,
            "An order attempt has no terminal, durably reconciled exchange result.",
        )

    if (
        not exchange_snap
        and not db_trade
        and not redis_snap
        and db_attempts
        and all(attempt.status in {"no_fill", "failed"} for attempt in db_attempts)
    ):
        return ReconciliationDecision(
            discrepancy=DiscrepancyType.HEALTHY,
            directive=ActionDirective.ALLOW_ENTRY,
            allow_entry=True,
            incident_required=False,
            manual_required=False,
            reason="Every durable order attempt has an explicit terminal no-position result.",
        )

    if exchange_snap and db_trade and redis_snap:
        if _same_contracts(exchange_snap.contracts, db_trade.contracts):
            return ReconciliationDecision(
                discrepancy=DiscrepancyType.HEALTHY,
                directive=ActionDirective.ALLOW_ENTRY,
                allow_entry=True,
                incident_required=False,
                manual_required=False,
                reason="Exchange, journal, and Redis point to the same owned position.",
            )
        return _quarantine(
            DiscrepancyType.POSITION_SIZE_MISMATCH,
            "Exchange contracts do not match the exact durable order fill.",
            manual_required=True,
        )

    if exchange_snap and db_trade and not redis_snap:
        return _quarantine(
            DiscrepancyType.MISSING_REDIS,
            "The owned exchange position has no complete Redis monitoring projection.",
            manual_required=True,
        )

    if exchange_snap and not db_trade and db_attempts:
        return _quarantine(
            DiscrepancyType.MISSING_TRADE_EXACT_ATTEMPT,
            "An exact order attempt exists, but its exchange position has no journal trade.",
            manual_required=True,
        )

    if exchange_snap and not db_trade:
        return _quarantine(
            DiscrepancyType.EXCHANGE_ONLY_ORPHAN,
            "An exchange position is not owned by an exact durable order attempt.",
            manual_required=True,
        )

    if db_trade and not exchange_snap:
        return _quarantine(
            DiscrepancyType.DB_ONLY_ORPHAN,
            "An open journal trade has no matching exchange position.",
            manual_required=True,
        )

    if redis_snap and not exchange_snap and not db_trade:
        return _quarantine(
            DiscrepancyType.REDIS_ONLY_STALE,
            "Redis position state has no matching exchange position or journal trade.",
            manual_required=True,
        )

    return _quarantine(
        DiscrepancyType.UNKNOWN_MISMATCH,
        "The source combination is not a safe, fully-owned position state.",
        manual_required=True,
    )
