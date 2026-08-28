"""Supervised, fail-closed reconciliation of live position ownership."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Any

import psycopg
import structlog
from psycopg.rows import dict_row

from . import exit as exit_module
from . import reconciliation as rec
from .supervisor import SUBMISSION_UNKNOWN_BLOCKER, WorkerReadinessGate

log = structlog.get_logger()

_ACTIVE_ATTEMPT_STATUSES = {
    "pending",
    "accepted",
    "submission_unknown",
    "manual_required",
}
_NO_FILL_STATUSES = {"canceled", "cancelled", "rejected", "expired"}
_POSITION_KEY_TTL_NAMES = (
    "position:opened_at:{exchange}:{base}",
    "position:sl_order_id:{exchange}:{base}",
)

_ATTEMPTS_SQL = """
SELECT
    a.id AS attempt_id,
    a.client_order_id,
    a.exchange,
    a.base,
    a.symbol,
    a.native_market_id,
    a.market_type,
    a.side,
    a.status,
    a.order_id,
    a.requested_amount,
    a.filled_amount,
    a.trade_id
FROM app.live_order_attempts AS a
LEFT JOIN app.trades AS t ON t.id = a.trade_id
WHERE a.status IN ('pending', 'accepted', 'submission_unknown', 'manual_required')
   OR (a.status = 'completed' AND (a.trade_id IS NULL OR t.status = 'open'))
"""

_OPEN_TRADES_SQL = """
SELECT
    t.id AS trade_id,
    t.exchange,
    t.symbol,
    t.side,
    t.entry_price,
    a.id AS attempt_id,
    a.base,
    a.native_market_id,
    a.market_type,
    a.filled_amount,
    a.client_order_id,
    a.order_id,
    a.status AS attempt_status,
    a.requested_amount
FROM app.trades AS t
LEFT JOIN app.live_order_attempts AS a ON a.trade_id = t.id
WHERE t.status = 'open'
  AND COALESCE(t.setup_context->>'paper', 'false') != 'true'
"""

_UPDATE_ATTEMPT_SQL = """
UPDATE app.live_order_attempts
SET status = %s,
    order_id = COALESCE(%s, order_id),
    filled_amount = COALESCE(%s, filled_amount),
    reconciliation_timestamp = now(),
    reconciliation_error = %s,
    updated_at = now()
WHERE id = %s
"""

_UPSERT_INCIDENT_SQL = """
INSERT INTO app.live_reconciliation_incidents (
    incident_key, exchange, symbol, native_market_id, market_type, side,
    contracts, discrepancy_type, status, evidence_json
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
ON CONFLICT (incident_key) DO UPDATE
SET last_seen_at = now(),
    status = CASE
        WHEN app.live_reconciliation_incidents.status = 'manual_required'
            THEN 'manual_required'
        ELSE EXCLUDED.status
    END,
    recovery_timestamp = NULL,
    contracts = EXCLUDED.contracts,
    evidence_json = EXCLUDED.evidence_json
"""


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _client_order_id(order: dict[str, Any]) -> str | None:
    info = order.get("info")
    info_dict = info if isinstance(info, dict) else {}
    value = (
        order.get("clientOrderId")
        or info_dict.get("clientOrderId")
        or info_dict.get("clientOid")
        or info_dict.get("orderLinkId")
    )
    return str(value) if value is not None else None


def _incident_key(
    identity: rec.FullIdentity,
    discrepancy: rec.DiscrepancyType,
    *,
    entity_key: str = "",
) -> str:
    material = "\x1f".join(
        (
            discrepancy.value,
            identity.exchange,
            identity.symbol,
            identity.native_market_id,
            identity.market_type,
            identity.side,
            entity_key,
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _attempt_state_from_order(
    order: dict[str, Any],
) -> tuple[str, Decimal | None, str | None]:
    filled = _decimal(order.get("filled"))
    exchange_status = str(order.get("status") or "").lower()
    if filled is not None and filled > 0:
        return "completed", filled, None
    if exchange_status in _NO_FILL_STATUSES | {"closed"} and filled == 0:
        return "no_fill", filled, None
    if exchange_status in _NO_FILL_STATUSES | {"closed"}:
        return "manual_required", filled, "terminal exchange order omitted filled amount"
    return "accepted", filled, None


class ReconciliationWorker:
    """Audit exact ownership across exchange, Postgres, and Redis.

    The only automatic mutation is resolving an order attempt from an exact
    exchange order/client-order-id match. Position state is never adopted,
    closed, reconstructed, or deleted by v1.
    """

    def __init__(
        self,
        exchanges: dict[str, Any],
        db_url: str,
        rdb: Any,
        gate: WorkerReadinessGate,
        scan_interval_seconds: float = 30.0,
    ) -> None:
        self.exchanges = exchanges
        self.db_url = db_url
        self.rdb = rdb
        self.gate = gate
        self.scan_interval = scan_interval_seconds

    async def _fetch_exchange_snapshots(
        self,
    ) -> dict[rec.FullIdentity, rec.ExchangePositionSnapshot]:
        snapshots: dict[rec.FullIdentity, rec.ExchangePositionSnapshot] = {}
        for exchange_name, exchange in self.exchanges.items():
            if not exchange.markets:
                await exchange.load_markets()
            positions = await exchange.fetch_positions()
            for position in positions:
                contracts = _decimal(position.get("contracts"))
                if contracts is None or contracts == 0:
                    continue

                symbol = str(position.get("symbol") or "")
                market = (exchange.markets or {}).get(symbol) if symbol else None
                market_data = market if isinstance(market, dict) else {}
                native_market_id = str(market_data.get("id") or "")
                market_type = str(market_data.get("type") or "")
                side = str(position.get("side") or "").lower()
                identity = rec.FullIdentity(
                    exchange=exchange_name,
                    symbol=symbol,
                    native_market_id=native_market_id,
                    market_type=market_type,
                    side=side,
                )
                entry_price = _decimal(position.get("entryPrice"))
                existing = snapshots.get(identity)
                if existing is not None:
                    contracts += existing.contracts
                snapshots[identity] = rec.ExchangePositionSnapshot(
                    identity=identity,
                    contracts=abs(contracts),
                    entry_price=entry_price,
                    raw_evidence={
                        "contracts": str(contracts),
                        "entry_price": str(entry_price) if entry_price is not None else None,
                    },
                )
        return snapshots

    @staticmethod
    def _attempt_from_row(row: dict[str, Any]) -> rec.OrderAttemptSnapshot:
        return rec.OrderAttemptSnapshot(
            identity=rec.FullIdentity(
                exchange=str(row["exchange"]),
                symbol=str(row["symbol"]),
                native_market_id=str(row.get("native_market_id") or ""),
                market_type=str(row.get("market_type") or ""),
                side=str(row["side"]),
            ),
            attempt_id=int(row["attempt_id"]),
            base=str(row.get("base") or "").upper(),
            client_order_id=str(row["client_order_id"]),
            order_id=str(row["order_id"]) if row.get("order_id") is not None else None,
            status=str(row.get("status") or row.get("attempt_status")),
            requested_amount=_decimal(row.get("requested_amount")),
            filled_amount=_decimal(row.get("filled_amount")),
            trade_id=int(row["trade_id"]) if row.get("trade_id") is not None else None,
        )

    async def _fetch_db_snapshots(
        self,
    ) -> tuple[
        dict[rec.FullIdentity, rec.OwnedTradeSnapshot],
        dict[rec.FullIdentity, list[rec.OrderAttemptSnapshot]],
        dict[int, rec.OwnedTradeSnapshot],
    ]:
        trades: dict[rec.FullIdentity, rec.OwnedTradeSnapshot] = {}
        trades_by_id: dict[int, rec.OwnedTradeSnapshot] = {}
        attempts: dict[rec.FullIdentity, list[rec.OrderAttemptSnapshot]] = {}

        async with (
            await psycopg.AsyncConnection.connect(self.db_url) as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(_ATTEMPTS_SQL)
            for row in await cursor.fetchall():
                attempt = self._attempt_from_row(row)
                attempts.setdefault(attempt.identity, []).append(attempt)

            await cursor.execute(_OPEN_TRADES_SQL)
            for row in await cursor.fetchall():
                if row.get("attempt_id") is not None:
                    attempt = self._attempt_from_row(row)
                    attempts.setdefault(attempt.identity, [])
                    if all(
                        existing.attempt_id != attempt.attempt_id
                        for existing in attempts[attempt.identity]
                    ):
                        attempts[attempt.identity].append(attempt)
                    identity = attempt.identity
                else:
                    identity = rec.FullIdentity(
                        exchange=str(row["exchange"]),
                        symbol=str(row["symbol"]),
                        native_market_id="",
                        market_type="",
                        side=str(row["side"]),
                    )

                trade = rec.OwnedTradeSnapshot(
                    identity=identity,
                    trade_id=int(row["trade_id"]),
                    attempt_id=(
                        int(row["attempt_id"]) if row.get("attempt_id") is not None else None
                    ),
                    base=str(row.get("base") or "").upper(),
                    contracts=_decimal(row.get("filled_amount")),
                    entry_price=Decimal(str(row["entry_price"])),
                )
                trades[identity] = trade
                trades_by_id[trade.trade_id] = trade

        return trades, attempts, trades_by_id

    async def _fetch_redis_snapshots(
        self,
        trades_by_id: dict[int, rec.OwnedTradeSnapshot],
    ) -> dict[rec.FullIdentity, rec.RedisPositionSnapshot]:
        snapshots: dict[rec.FullIdentity, rec.RedisPositionSnapshot] = {}
        keys_by_position: dict[tuple[str, str], set[str]] = {}
        for exchange_name in self.exchanges:
            for pattern in (
                f"trade:id:{exchange_name}:*",
                f"position:opened_at:{exchange_name}:*",
            ):
                cursor: int | bytes = 0
                while True:
                    cursor, keys = await self.rdb.scan(cursor, match=pattern)
                    for raw_key in keys:
                        key = _text(raw_key)
                        parts = key.split(":", maxsplit=3)
                        if len(parts) != 4 or parts[2] != exchange_name:
                            continue
                        position_key = (parts[2], parts[3].upper())
                        keys_by_position.setdefault(position_key, set()).add(key)
                    if cursor in (0, b"0", "0"):
                        break

        for (exchange, base), _keys in keys_by_position.items():
            trade_id_raw = await self.rdb.get(f"trade:id:{exchange}:{base}")
            try:
                trade_id = int(_text(trade_id_raw)) if trade_id_raw is not None else -1
            except ValueError:
                trade_id = -1
            trade = trades_by_id.get(trade_id)
            if trade is None or trade.identity.exchange != exchange or trade.base != base:
                identity = rec.FullIdentity(
                    exchange=exchange,
                    symbol=f"redis-key:{base}",
                    native_market_id="",
                    market_type="",
                    side="",
                )
                snapshots[identity] = rec.RedisPositionSnapshot(
                    identity=identity,
                    trade_id=trade_id,
                    exchange=exchange,
                    base=base,
                )
                continue

            required_keys = [
                template.format(exchange=exchange, base=base)
                for template in _POSITION_KEY_TTL_NAMES
            ]
            required_keys.extend(
                (
                    exit_module.params_key(exchange, base),
                    exit_module.entry_key(exchange, base),
                    exit_module.side_key(exchange, base),
                    exit_module.size_usd_key(exchange, base),
                )
            )
            required_values = await asyncio.gather(
                *(self.rdb.get(required_key) for required_key in required_keys)
            )
            if all(value is not None for value in required_values):
                snapshots[trade.identity] = rec.RedisPositionSnapshot(
                    identity=trade.identity,
                    trade_id=trade_id,
                    exchange=exchange,
                    base=base,
                )
        return snapshots

    async def _find_exact_order(
        self,
        exchange: Any,
        attempt: rec.OrderAttemptSnapshot,
    ) -> dict[str, Any] | None:
        if attempt.order_id and exchange.has.get("fetchOrder"):
            try:
                order = await exchange.fetch_order(attempt.order_id, attempt.identity.symbol)
                if not isinstance(order, dict):
                    return None
                if _client_order_id(order) in (None, attempt.client_order_id):
                    return order
            except Exception as exc:
                log.warning(
                    "reconciliation.fetch_order_failed",
                    exchange=attempt.identity.exchange,
                    attempt_id=attempt.attempt_id,
                    err=str(exc),
                )

        for capability, method_name in (
            ("fetchOpenOrders", "fetch_open_orders"),
            ("fetchClosedOrders", "fetch_closed_orders"),
        ):
            if not exchange.has.get(capability):
                continue
            try:
                orders = await getattr(exchange, method_name)(attempt.identity.symbol)
            except Exception as exc:
                log.warning(
                    "reconciliation.fetch_orders_failed",
                    exchange=attempt.identity.exchange,
                    capability=capability,
                    attempt_id=attempt.attempt_id,
                    err=str(exc),
                )
                continue
            for order in orders:
                if not isinstance(order, dict):
                    continue
                if _client_order_id(order) == attempt.client_order_id:
                    return order
        return None

    async def _update_attempt_from_order(
        self,
        attempt: rec.OrderAttemptSnapshot,
        order: dict[str, Any],
    ) -> rec.OrderAttemptSnapshot:
        status, filled, reconciliation_error = _attempt_state_from_order(order)
        order_id = str(order["id"]) if order.get("id") is not None else attempt.order_id

        async with (
            await psycopg.AsyncConnection.connect(self.db_url) as connection,
            connection.cursor() as cursor,
        ):
            await cursor.execute(
                _UPDATE_ATTEMPT_SQL,
                (status, order_id, filled, reconciliation_error, attempt.attempt_id),
            )
        return replace(attempt, status=status, order_id=order_id, filled_amount=filled)

    async def _resolve_attempts(
        self,
        attempts: dict[rec.FullIdentity, list[rec.OrderAttemptSnapshot]],
    ) -> None:
        for identity, identity_attempts in attempts.items():
            exchange = self.exchanges.get(identity.exchange)
            if exchange is None:
                continue
            for index, attempt in enumerate(identity_attempts):
                needs_resolution = attempt.status in _ACTIVE_ATTEMPT_STATUSES or (
                    attempt.status == "completed" and attempt.filled_amount is None
                )
                if not needs_resolution:
                    continue
                order = await self._find_exact_order(exchange, attempt)
                if order is not None:
                    identity_attempts[index] = await self._update_attempt_from_order(attempt, order)

    async def _upsert_incident(
        self,
        decision: rec.ReconciliationDecision,
        identity: rec.FullIdentity,
        exchange_snap: rec.ExchangePositionSnapshot | None,
        attempts: list[rec.OrderAttemptSnapshot],
        redis_snap: rec.RedisPositionSnapshot | None,
    ) -> str:
        entity_key = ",".join(
            str(attempt.attempt_id)
            for attempt in sorted(attempts, key=lambda item: item.attempt_id)
        )
        if redis_snap is not None:
            entity_key = f"{entity_key}|redis_trade:{redis_snap.trade_id}"
        incident_key = _incident_key(identity, decision.discrepancy, entity_key=entity_key)
        contracts = exchange_snap.contracts if exchange_snap is not None else None
        evidence = {
            "reason": decision.reason,
            "attempt_ids": [attempt.attempt_id for attempt in attempts],
            "attempt_statuses": [attempt.status for attempt in attempts],
            "redis_trade_id": redis_snap.trade_id if redis_snap is not None else None,
            "exchange": exchange_snap.raw_evidence if exchange_snap is not None else None,
        }
        # First sighting is always recoverable: a subsequent clean scan may
        # prove that an exchange/API projection was merely delayed. Operators
        # can promote a persistent row to manual_required explicitly; the
        # UPSERT below then preserves that sticky acknowledgement state.
        status = "open"
        async with (
            await psycopg.AsyncConnection.connect(self.db_url) as connection,
            connection.cursor() as cursor,
        ):
            await cursor.execute(
                _UPSERT_INCIDENT_SQL,
                (
                    incident_key,
                    identity.exchange,
                    identity.symbol,
                    identity.native_market_id,
                    identity.market_type,
                    identity.side,
                    contracts,
                    decision.discrepancy.value,
                    status,
                    json.dumps(evidence),
                ),
            )
        return incident_key

    async def _resolve_absent_incidents(self, active_keys: set[str]) -> int:
        scanned_exchanges = sorted(self.exchanges)
        async with (
            await psycopg.AsyncConnection.connect(self.db_url) as connection,
            connection.cursor() as cursor,
        ):
            if active_keys:
                await cursor.execute(
                    """
                        UPDATE app.live_reconciliation_incidents
                        SET status = 'resolved', recovery_timestamp = now(), last_seen_at = now()
                        WHERE status = 'open'
                          AND exchange = ANY(%s)
                          AND NOT (incident_key = ANY(%s))
                        """,
                    (scanned_exchanges, list(active_keys)),
                )
            else:
                await cursor.execute(
                    """
                        UPDATE app.live_reconciliation_incidents
                        SET status = 'resolved', recovery_timestamp = now(), last_seen_at = now()
                        WHERE status = 'open' AND exchange = ANY(%s)
                        """,
                    (scanned_exchanges,),
                )
            await cursor.execute(
                """
                    SELECT count(*)
                    FROM app.live_reconciliation_incidents
                    WHERE status IN ('open', 'manual_required')
                    """
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def _run_scan(self) -> bool:
        try:
            exchange_snaps = await self._fetch_exchange_snapshots()
            db_trades, db_attempts, trades_by_id = await self._fetch_db_snapshots()
            redis_snaps = await self._fetch_redis_snapshots(trades_by_id)
            await self._resolve_attempts(db_attempts)

            active_incident_keys: set[str] = set()
            all_identities = (
                set(exchange_snaps) | set(db_trades) | set(db_attempts) | set(redis_snaps)
            )
            for identity in sorted(all_identities):
                exchange_snap = exchange_snaps.get(identity)
                trade = db_trades.get(identity)
                attempts = db_attempts.get(identity, [])
                redis_snap = redis_snaps.get(identity)
                decision = rec.classify_position_state(
                    identity,
                    exchange_snap,
                    trade,
                    attempts,
                    redis_snap,
                )
                if decision.incident_required:
                    active_incident_keys.add(
                        await self._upsert_incident(
                            decision,
                            identity,
                            exchange_snap,
                            attempts,
                            redis_snap,
                        )
                    )
                    log.warning(
                        "reconciliation.quarantined",
                        exchange=identity.exchange,
                        symbol=identity.symbol,
                        discrepancy=decision.discrepancy.value,
                    )

            open_incident_count = await self._resolve_absent_incidents(active_incident_keys)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.gate.set_safety_blocker(rec.SOURCE_BLOCKER)
            log.error("reconciliation.scan_failed", err=str(exc))
            return False

        self.gate.clear_safety_blocker(rec.SOURCE_BLOCKER)
        self.gate.clear_safety_blocker(rec.STARTUP_BLOCKER)
        unresolved_attempts = any(
            attempt.unresolved
            for attempts_for_identity in db_attempts.values()
            for attempt in attempts_for_identity
        )
        if unresolved_attempts:
            self.gate.set_safety_blocker(SUBMISSION_UNKNOWN_BLOCKER)
        else:
            self.gate.clear_safety_blocker(SUBMISSION_UNKNOWN_BLOCKER)
        if open_incident_count:
            self.gate.set_safety_blocker(rec.INCIDENT_BLOCKER)
        else:
            self.gate.clear_safety_blocker(rec.INCIDENT_BLOCKER)
        return True

    async def __call__(self, tracker: Any) -> None:
        log.info("reconciliation_worker.started")
        while True:
            tracker.tick_started()
            if await self._run_scan():
                tracker.tick_succeeded()
            else:
                tracker.tick_failed(RuntimeError("reconciliation source unavailable"))
            await asyncio.sleep(self.scan_interval)
