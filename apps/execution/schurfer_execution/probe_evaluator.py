"""Monitor-side LIVE_PROBE loss evaluation. No entry path or exchange sends.

The only close request is probe_close.record_l_reached; its durable task is executed
by the emergency closer. A failed write keeps the first decision in memory and the
shared entry gate blocked. Startup recovery guards the loss of that latch.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

import psycopg
import structlog
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from . import cost_worker, live_costs, live_probe, notify, probe_close, probe_loss, symbols

if TYPE_CHECKING:
    from collections.abc import Callable

log = structlog.get_logger()
LATCH_BLOCKER = "probe_loss_decision_pending"
FUNDING_DELAY = timedelta(minutes=30)
HISTORY_MAX_AGE = timedelta(hours=8, minutes=30)
_REQUEST_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class ProbePosition:
    probe_id: str
    trade_id: int
    native_market_id: str
    symbol: str
    market_type: str
    side: str
    exchange: str
    quantity: Decimal
    taker_rate: Decimal | None
    threshold: Decimal
    realized_closed: Decimal
    reached_loss: Decimal | None = None
    reached_inputs: dict[str, Any] | None = None
    initial_funding: live_costs.FundingSettlement | None = None


@dataclass
class _MarkRead:
    # Per-pass evidence, also retained when an awaited read is cancelled by timeout.
    attempted_at: datetime | None = None
    mark: Decimal | None = None
    read_at: datetime | None = None


class ProbeStore(Protocol):
    async def positions(self) -> list[ProbePosition]: ...

    async def load(self, trade_id: int) -> cost_worker.PendingTrade | None: ...

    async def incident(self, probe_id: str, reason: str) -> bool: ...


_POSITIONS = """
SELECT a.probe_id, a.exchange, a.loss_threshold_usd, a.l_reached_at,
       a.l_reached_loss_usd, a.l_reached_inputs,
       o.setup_context->'live_probe_preflight' AS preflight, r.sending_at,
       r.native_market_id, r.side,
       t.id AS trade_id, o.symbol, o.market_type,
       o.filled_amount - COALESCE((
           SELECT SUM(f.filled_amount) FROM app.trade_close_fills f WHERE f.trade_id = t.id
       ), 0) AS quantity
FROM app.live_probe_authorizations a
JOIN app.live_probe_reservations r ON r.probe_id = a.probe_id
JOIN app.live_order_attempts o ON o.id = r.attempt_id
JOIN app.trades t ON t.id = COALESCE(r.trade_id, o.trade_id)
WHERE r.state IN ('sending', 'placed') AND t.status = 'open'
  AND o.operation = 'entry' AND o.client_order_id = r.client_order_id
  AND o.exchange = a.exchange AND o.native_market_id = r.native_market_id
  AND o.side = r.side
  AND NOT EXISTS (
      SELECT 1 FROM app.live_emergency_closes e
      WHERE e.source_attempt_id = o.id AND e.status <> 'accounted'
  )
ORDER BY a.probe_id, r.id
"""


def _preflight_taker(preflight: Any) -> Decimal | None:
    if not isinstance(preflight, dict):
        return None
    try:
        return live_costs._decimal(preflight.get("taker_rate"), "preflight taker rate")
    except ValueError:
        return None


def _preflight_funding(
    preflight: Any, sending_at: datetime | None
) -> live_costs.FundingSettlement | None:
    if not isinstance(preflight, dict) or sending_at is None:
        return None
    try:
        at = datetime.fromisoformat(preflight["next_funding_at"])
        read_at = datetime.fromisoformat(preflight["read_at"])
        if at.tzinfo is None or read_at.tzinfo is None or read_at >= at or read_at > sending_at:
            return None
        rate = live_costs._decimal(preflight["funding_rate"], "preflight funding rate")
        return live_costs.FundingSettlement(at, rate)
    except (KeyError, TypeError, ValueError):
        return None


class PostgresProbeStore:
    def __init__(self, db_url: str) -> None:
        self.db_url = db_url
        self.costs = cost_worker.PostgresCostStore(db_url)

    async def positions(self) -> list[ProbePosition]:
        async with (
            await psycopg.AsyncConnection.connect(self.db_url, row_factory=dict_row) as conn,
            conn.cursor() as cursor,
        ):
            await cursor.execute(_POSITIONS)
            rows = await cursor.fetchall()
            result = []
            for row in rows:
                ledger = await live_probe.ledger(cursor, row["probe_id"])
                if ledger.closed_unsettled:
                    # Never feed estimated closed economics into the open loss.
                    raise RuntimeError("a closed probe trade has unsettled costs")
                result.append(
                    ProbePosition(
                        probe_id=row["probe_id"],
                        trade_id=int(row["trade_id"]),
                        exchange=row["exchange"],
                        native_market_id=row["native_market_id"],
                        symbol=row["symbol"],
                        market_type=row["market_type"],
                        side=row["side"],
                        quantity=Decimal(str(row["quantity"])),
                        taker_rate=_preflight_taker(row["preflight"]),
                        initial_funding=_preflight_funding(row["preflight"], row["sending_at"]),
                        threshold=Decimal(row["loss_threshold_usd"]),
                        realized_closed=ledger.realized_closed,
                        reached_loss=Decimal(row["l_reached_loss_usd"])
                        if row["l_reached_at"] is not None
                        else None,
                        reached_inputs=row["l_reached_inputs"],
                    )
                )
            return result

    async def load(self, trade_id: int) -> cost_worker.PendingTrade | None:
        return await self.costs.load(trade_id)

    async def incident(self, probe_id: str, reason: str) -> bool:
        async with (
            await psycopg.AsyncConnection.connect(self.db_url, row_factory=dict_row) as conn,
            conn.transaction(),
            conn.cursor() as cursor,
        ):
            if await live_probe.lock_record(cursor, probe_id) is None:
                raise LookupError(probe_id)
            await cursor.execute(
                "UPDATE app.live_probe_authorizations SET costs_incident_at = clock_timestamp(), "
                "costs_incident_reason = %s WHERE probe_id = %s AND costs_incident_at IS NULL",
                (reason, probe_id),
            )
            if cursor.rowcount != 1:
                return False
            await cursor.execute(
                "INSERT INTO app.live_probe_authorization_events (probe_id, event, actor, detail) "
                "VALUES (%s, 'costs_incident', %s, %s)",
                (probe_id, live_probe.SERVICE_ACTOR, Jsonb({"reason": reason})),
            )
        log.error("live_probe.costs_incident", probe_id=probe_id, reason=reason)
        return True


class ProbeLossEvaluator:
    def __init__(
        self,
        db_url: str,
        exchanges: dict[str, Any],
        store: ProbeStore,
        rdb: Any,
        gate: Any,
        recovered: asyncio.Event,
        *,
        alert_credentials: tuple[str, str] | None = None,
        now: Callable[[], datetime] | None = None,
        record: Callable[..., Any] = probe_close.record_l_reached,
    ) -> None:
        self.db_url, self.exchanges, self.store = db_url, exchanges, store
        self.rdb, self.gate, self.recovered = rdb, gate, recovered
        self.alert_credentials = alert_credentials
        self.now = now or (lambda: datetime.now(tz=UTC))
        self.record = record
        self.pending: dict[str, tuple[Decimal, dict[str, Any]]] = {}
        self.observed: dict[int, dict[datetime, live_costs.FundingSettlement]] = {}
        self.stale: dict[int, int] = {}
        self.alerted: set[str] = set()

    async def run_once(self) -> bool:
        if not self.recovered.is_set():
            return True
        succeeded = True
        # Retrying the process latch precedes every read, including a failed DB scan.
        attempted = set(self.pending)
        for probe_id in attempted:
            succeeded = await self._commit(probe_id) and succeeded
        try:
            positions = await self.store.positions()
        except Exception as exc:
            log.error("live_probe.loss_scan_failed", err=str(exc))
            return False
        for position in positions:
            if position.probe_id in attempted:
                continue
            try:
                if position.reached_loss is not None:
                    # A standing request is retried without a mark or collector,
                    # including a decision made by another process before restart.
                    self._latch(
                        position.probe_id, position.reached_loss, position.reached_inputs or {}
                    )
                    succeeded = await self._commit(position.probe_id) and succeeded
                else:
                    mark_read = _MarkRead()
                    try:
                        await asyncio.wait_for(self._evaluate(position, mark_read), timeout=45)
                    finally:
                        await self._note_mark(position, mark_read)
            except Exception as exc:
                log.error(
                    "live_probe.loss_evaluation_failed",
                    probe_id=position.probe_id,
                    error_type=type(exc).__name__,
                    err=str(exc),
                )
                succeeded = False
        live_ids = {p.trade_id for p in positions}
        self.observed = {k: v for k, v in self.observed.items() if k in live_ids}
        self.stale = {k: v for k, v in self.stale.items() if k in live_ids}
        return succeeded and not self.pending

    def _latch(self, probe_id: str, loss: Decimal, inputs: dict[str, Any]) -> None:
        self.pending.setdefault(probe_id, (loss, inputs))
        self.gate.set_safety_blocker(LATCH_BLOCKER)

    async def _commit(self, probe_id: str) -> bool:
        loss, inputs = self.pending[probe_id]
        try:
            result = await asyncio.wait_for(
                self.record(self.db_url, self.rdb, probe_id, loss_usd=loss, inputs=inputs),
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            log.error(
                "live_probe.loss_request_pending",
                probe_id=probe_id,
                error_type=type(exc).__name__,
                err=str(exc),
            )
            await self._alert_once(
                f"decision:{probe_id}",
                f"LIVE_PROBE {probe_id}: L reached, loss {loss}; recording/close request "
                f"not confirmed, retrying with entries blocked. Inputs: {inputs}",
            )
            return False
        del self.pending[probe_id]
        if not self.pending:
            self.gate.clear_safety_blocker(LATCH_BLOCKER)
        await self._alert_once(
            f"decision:{probe_id}",
            f"LIVE_PROBE {probe_id}: L reached, loss {loss}; recorded, emergency tasks "
            f"{[c.task_id for c in result.closes]}. Inputs: {inputs}",
        )
        return True

    async def _evaluate(self, position: ProbePosition, mark_read: _MarkRead) -> None:
        if (
            position.taker_rate is None
            or not position.taker_rate.is_finite()
            or position.taker_rate < 0
        ):
            await self._incident(position, "the entry preflight has no valid persisted taker rate")
            return
        exchange = self.exchanges.get(position.exchange)
        if exchange is None or position.exchange != "bybit":
            await self._incident(position, "no Bybit source for the probe position")
            return
        route = symbols.resolve_execution_instrument(exchange, position.native_market_id)
        if (
            route.exchange != position.exchange
            or route.native_market_id != position.native_market_id
            or route.symbol != position.symbol
            or route.market_type != position.market_type
        ):
            await self._incident(position, "probe instrument does not match its canonical route")
            return
        mark_read.attempted_at = self.now()
        source = live_costs.BybitCostSource(exchange)
        observed = self.observed.setdefault(position.trade_id, {})
        if position.initial_funding is not None:
            observed.setdefault(position.initial_funding.at, position.initial_funding)
        try:
            observation = await source.funding_observation(position.native_market_id)
            # A read after settlement cannot supply the rate observed before it.
            if observation.at > self.now():
                observed[observation.at] = observation
        except Exception as exc:
            log.warning(
                "live_probe.funding_observation_failed", trade_id=position.trade_id, err=str(exc)
            )
        mark: Decimal | None = None
        read_at = self.now()
        try:
            rows = await exchange.fetch_positions([route.symbol])
            read_at = self.now()
            rows = [
                r
                for r in rows
                if r.get("symbol") == route.symbol
                and live_costs._decimal(r.get("contracts"), "contracts") > 0
            ]
        except Exception as exc:
            rows = None
            log.warning("live_probe.mark_read_failed", trade_id=position.trade_id, err=str(exc))
        if rows is not None:
            if (
                len(rows) != 1
                or rows[0].get("side") != position.side
                or live_costs._decimal(rows[0].get("contracts"), "contracts") != position.quantity
            ):
                await self._incident(
                    position, "exchange quantity or side differs from the journalled probe"
                )
                return
            try:
                mark = live_costs._decimal(rows[0].get("markPrice"), "markPrice")
                if mark <= 0:
                    mark = None
            except ValueError:
                mark = None
        mark_read.mark, mark_read.read_at = mark, read_at
        trade = await self.store.load(position.trade_id)
        if trade is None:
            await self._incident(position, "no exact entry evidence for the probe trade")
            return
        if (
            trade.facts.exchange != position.exchange
            or trade.facts.native_market_id != position.native_market_id
            or trade.facts.side != position.side
        ):
            await self._incident(position, "journal and probe entry identities differ")
            return
        collector_error: Exception | None = None
        try:
            result = await live_costs.read_open(
                source,
                trade.facts,
                trade.neighbours,
                journal_quantity=position.quantity,
                observed=tuple(observed.values()),
                now=self.now(),
            )
        except Exception as exc:
            collector_error = exc
            log.error("live_probe.open_cost_read_failed", trade_id=position.trade_id, err=str(exc))
            result = live_costs.OpenCostResult(live_costs.STATUS_PENDING, error=str(exc))
        now = self.now()
        if result.status == live_costs.STATUS_INCOMPLETE:
            await self._incident(position, result.error or "incomplete executions")
            return
        if result.position is None:
            log.info("live_probe.loss_pending", probe_id=position.probe_id, reason=result.error)
            if collector_error is not None:
                raise RuntimeError("open cost read failed") from collector_error
            return
        if mark is not None and probe_loss.mark_is_fresh(read_at, now):
            loss = probe_loss.probe_loss(
                position.realized_closed, result.position, mark=mark, taker_rate=position.taker_rate
            )
            log.info(
                "live_probe.loss_evaluated",
                probe_id=position.probe_id,
                loss=str(loss.loss),
                lower_bound=loss.lower_bound,
            )
            if loss.reached(position.threshold):
                inputs = {
                    "trade_id": position.trade_id,
                    "threshold_usd": str(position.threshold),
                    "mark": str(mark),
                    "mark_read_at": read_at.isoformat(),
                    "open_quantity": str(result.position.open_qty),
                    "entry_vwap": str(result.position.entry_vwap),
                    "terms": {k: str(v) for k, v in loss.terms.items()},
                    "published_to": result.published_to.isoformat()
                    if result.published_to
                    else None,
                    "unlisted": [
                        {
                            "at": s.at.isoformat(),
                            "rate": str(s.rate) if s.rate is not None else None,
                            "quantity_held": str(s.quantity_held)
                            if s.quantity_held is not None
                            else None,
                            "mark": str(s.mark) if s.mark is not None else None,
                        }
                        for s in result.position.unlisted
                    ],
                    "unknown_settlements": [s.isoformat() for s in loss.unknown_settlements],
                    "lower_bound": loss.lower_bound,
                }
                self._latch(position.probe_id, loss.loss, inputs)
                await self._commit(position.probe_id)

        if result.published_to is None or now - result.published_to > HISTORY_MAX_AGE:
            await self._incident(position, "funding history has no settlement within 8 h 30 min")
        if any(now - s.at >= FUNDING_DELAY for s in result.position.unlisted):
            await self._incident(
                position, "an observed funding settlement is unlisted for 30 minutes"
            )

    async def _note_mark(self, position: ProbePosition, read: _MarkRead) -> None:
        if read.attempted_at is None:
            return  # No price read was attempted for invalid preflight/identity.
        if (
            read.mark is not None
            and read.read_at is not None
            and probe_loss.mark_is_fresh(read.read_at, self.now())
        ):
            self.stale[position.trade_id] = 0
            self.alerted.discard(f"mark:{position.trade_id}")
            return
        self.stale[position.trade_id] = self.stale.get(position.trade_id, 0) + 1
        if self.stale[position.trade_id] >= probe_loss.STALE_MARK_ALERT_TICKS:
            await self._alert_once(
                f"mark:{position.trade_id}",
                f"LIVE_PROBE {position.probe_id}: no fresh mark for three monitor ticks; "
                "exchange stop remains the independent protection.",
            )

    async def _incident(self, position: ProbePosition, reason: str) -> None:
        if await self.store.incident(position.probe_id, reason):
            await self._alert(
                f"LIVE_PROBE {position.probe_id}: costs incident; {reason}. Entries blocked "
                "until the owner clears it; this incident does not close a position.",
            )

    async def _alert_once(self, key: str, text: str) -> None:
        if key in self.alerted:
            return
        self.alerted.add(key)
        await self._alert(text)

    async def _alert(self, text: str) -> None:
        if self.alert_credentials is None:
            return
        try:
            if await notify.notify_alert(*self.alert_credentials, text=text):
                return
        except Exception as exc:
            log.error("live_probe.loss_alert_failed", err=str(exc))
        log.error("live_probe.loss_alert_not_delivered", text=text)
