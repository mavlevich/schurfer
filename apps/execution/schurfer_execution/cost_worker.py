"""Settle a closed live trade's fees and funding from the exchange.

docs/engineering/live-net-accounting-design-v1.md, section 5. A live trade closes
``pending`` with an estimate (journal.close_trade_in), and while any closed live trade
is ``pending`` or ``incomplete`` the PnL tracker keeps entries blocked. This worker,
about every 60 s, takes each closed ``pending`` trade at least 5 minutes after its exit
and either:

  * settles it in one transaction: its evidence rows in ``app.trade_cost_legs`` and
    its final figures, ``accounting_status = 'complete'``. A crash before the commit
    leaves nothing, so the next pass writes everything once;
  * leaves it ``pending`` when something is not listed yet, or a read or write fails;
  * or sets ``incomplete`` with the reason, and alerts once, when an operator is
    needed: an unknown fill in the holding period, quantities that do not match, a fee
    not in USDT, funding at a boundary that does not match or with another possible
    owner. A trade still ``pending`` 24 h after its exit becomes ``incomplete`` the
    same way.

An operator settles an ``incomplete`` trade from the exchange with this module's CLI,
which records ``operator_settled``:

    python -m schurfer_execution.cost_worker settle --trade-id 123 \\
        --gross-usd -1.25 --fees-usd 0.11 --funding-usd 0.02 \\
        --note "checked against Bybit closed PnL and the funding history"

Nothing here sends an order. Entries depend on it only through PnL readiness.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import TYPE_CHECKING, Any, Protocol

import psycopg
import structlog
from psycopg.rows import dict_row
from schurfer_performance import LIVE_EXCHANGE_COSTS_ACCOUNTING_VERSION

from . import journal, live_costs, notify

if TYPE_CHECKING:
    from collections.abc import Callable

log = structlog.get_logger()

DEADLINE = timedelta(hours=24)
STATUS_OPERATOR_SETTLED = "operator_settled"
_CENT = Decimal("0.0001")


class ExecutionClaimedError(RuntimeError):
    """An execution the trade owns is already attributed to another trade: an
    operator decides, it is never counted twice."""


@dataclass(frozen=True)
class PendingTrade:
    facts: live_costs.TradeFacts
    neighbours: live_costs.Neighbours


class CostStore(Protocol):
    async def pending_trade_ids(self, settled_before: datetime) -> list[int]: ...

    async def load(self, trade_id: int) -> PendingTrade | None: ...

    async def settle(self, trade_id: int, result: live_costs.CostResult) -> bool: ...

    async def mark_incomplete(self, trade_id: int, error: str) -> bool: ...


class LiveCostWorker:
    """Supervised loop over closed live trades whose costs are not settled."""

    def __init__(
        self,
        sources: dict[str, live_costs.CostSource],
        store: CostStore,
        rdb: Any,
        *,
        alert_credentials: tuple[str, str] | None = None,
        interval_seconds: float = 60.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.sources = sources
        self.store = store
        self.rdb = rdb
        self.alert_credentials = alert_credentials
        self.interval_seconds = interval_seconds
        self._now = now or (lambda: datetime.now(tz=UTC))

    async def run_once(self) -> bool:
        succeeded = True
        now = self._now()
        for trade_id in await self.store.pending_trade_ids(now - live_costs.SETTLE_DELAY):
            try:
                await self.settle(trade_id)
            except Exception as exc:
                succeeded = False
                log.error("live_costs.failed", trade_id=trade_id, err=str(exc))
        return succeeded

    async def __call__(self, tracker: Any) -> None:
        log.info("live_cost_worker.started")
        while True:
            tracker.tick_started()
            try:
                succeeded = await self.run_once()
            except Exception as exc:
                log.error("live_costs.scan_failed", err=str(exc))
                succeeded = False
            if succeeded:
                tracker.tick_succeeded()
            else:
                tracker.tick_failed(RuntimeError("live cost pass failed"))
            await asyncio.sleep(self.interval_seconds)

    async def settle(self, trade_id: int) -> str:
        """One pass over one trade. Returns its accounting status after the pass."""
        now = self._now()
        trade = await self.store.load(trade_id)
        if trade is None:
            return await self._incomplete(
                trade_id, "no entry attempt with a fill links to the trade"
            )
        facts = trade.facts
        source = self.sources.get(facts.exchange)
        if source is None:
            return await self._incomplete(
                trade_id, f"no execution source for exchange {facts.exchange!r}"
            )
        try:
            result = await live_costs.read(source, facts, trade.neighbours, now=now)
        except Exception as exc:
            result = live_costs.CostResult(
                status=live_costs.STATUS_PENDING, error=f"{type(exc).__name__}: {exc}"
            )
        if result.status == live_costs.STATUS_INCOMPLETE:
            return await self._incomplete(trade_id, result.error or "incomplete")
        if result.status == live_costs.STATUS_PENDING:
            if self._past_deadline(facts, now):
                return await self._incomplete(
                    trade_id, f"not settled 24 h after the exit: {result.error}"
                )
            log.info("live_costs.pending", trade_id=trade_id, reason=result.error)
            return live_costs.STATUS_PENDING
        try:
            settled = await self.store.settle(trade_id, result)
        except ExecutionClaimedError as exc:
            return await self._incomplete(trade_id, str(exc))
        except Exception as exc:
            # The read succeeded but its figures cannot be written. Retried like a
            # failed read, and like one it needs an operator after 24 h; until then
            # the pass fails, so the worker's health shows it.
            if self._past_deadline(facts, now):
                return await self._incomplete(
                    trade_id,
                    f"not settled 24 h after the exit: writing failed: {type(exc).__name__}: {exc}",
                )
            raise
        if not settled:
            return "settled_elsewhere"
        await journal.revoke_pnl_readiness(self.rdb)
        log.info(
            "live_costs.settled",
            trade_id=trade_id,
            fees_usd=str(result.fees_usd),
            funding_usd=str(result.funding_usd),
            net_pnl_usd=str(result.net_pnl_usd),
        )
        return live_costs.STATUS_COMPLETE

    @staticmethod
    def _past_deadline(facts: live_costs.TradeFacts, now: datetime) -> bool:
        return now - (facts.exit_at or now) >= DEADLINE

    async def _incomplete(self, trade_id: int, error: str) -> str:
        if await self.store.mark_incomplete(trade_id, error):
            log.error("live_costs.incomplete", trade_id=trade_id, reason=error)
            await self._alert(
                f"Live trade {trade_id}: fees and funding cannot be settled from the "
                f"exchange. {error}. Entries stay blocked until an operator settles it "
                "(python -m schurfer_execution.cost_worker settle)."
            )
        return live_costs.STATUS_INCOMPLETE

    async def _alert(self, text: str) -> None:
        if self.alert_credentials is None:
            return
        delivered = False
        try:
            delivered = await notify.notify_alert(*self.alert_credentials, text=text)
        except Exception as exc:
            log.error("live_costs.alert_delivery_failed", err=str(exc))
        if not delivered:
            log.error("live_costs.alert_not_delivered", text=text)


# --- Postgres ----------------------------------------------------------------------

_PENDING_TRADES = """
SELECT id
FROM app.trades
WHERE status = 'closed'
  AND accounting_version = %s
  AND accounting_status = 'pending'
  AND exit_at <= %s
ORDER BY exit_at, id
"""

_TRADE = """
SELECT
    t.id, t.exchange, t.symbol, t.side, t.entry_order_id, t.exit_order_id, t.exit_at,
    a.client_order_id, a.order_id AS attempt_order_id, a.filled_amount, a.contract_size,
    a.native_market_id, a.created_at AS sent_at
FROM app.trades AS t
JOIN LATERAL (
    SELECT client_order_id, order_id, filled_amount, contract_size, native_market_id, created_at
    FROM app.live_order_attempts
    WHERE trade_id = t.id AND operation = 'entry'
    ORDER BY id
    LIMIT 1
) AS a ON true
WHERE t.id = %s
"""

# Every close order linked to the trade: its partial and final close fills, its
# close attempts, and the closes of an emergency close task journalled as the trade.
_TRADE_CLOSES = """
SELECT order_id, NULL::text AS client_order_id
FROM app.trade_close_fills
WHERE trade_id = %(trade_id)s
UNION
SELECT a.order_id, a.client_order_id
FROM app.live_order_attempts AS a
LEFT JOIN app.live_emergency_closes AS e ON e.id = a.task_id
WHERE a.operation = 'close'
  AND (a.trade_id = %(trade_id)s OR e.trade_id = %(trade_id)s)
"""

_NEIGHBOUR_TRADES = """
SELECT entry_order_id, exit_order_id, entry_at, exit_at
FROM app.trades
WHERE exchange = %(exchange)s
  AND symbol = %(symbol)s
  AND id <> %(trade_id)s
  AND COALESCE(setup_context->>'paper', 'false') <> 'true'
"""

_NEIGHBOUR_CLOSE_FILLS = """
SELECT f.order_id
FROM app.trade_close_fills AS f
JOIN app.trades AS t ON t.id = f.trade_id
WHERE t.exchange = %(exchange)s AND t.symbol = %(symbol)s AND t.id <> %(trade_id)s
"""

# Attempts of ours on the instrument that are not this trade's. One with no trade
# yet holds, or may hold, a position of its own: from when it was written until it
# ended, or open-ended while it is unresolved.
_NEIGHBOUR_ATTEMPTS = """
SELECT
    a.operation, a.order_id, a.client_order_id, a.trade_id, a.status, a.created_at,
    a.updated_at
FROM app.live_order_attempts AS a
LEFT JOIN app.live_emergency_closes AS e ON e.id = a.task_id
WHERE a.exchange = %(exchange)s
  AND a.symbol = %(symbol)s
  AND a.trade_id IS DISTINCT FROM %(trade_id)s
  AND e.trade_id IS DISTINCT FROM %(trade_id)s
"""

_NEIGHBOUR_TASKS = """
SELECT created_at, confirmed_flat_at
FROM app.live_emergency_closes
WHERE exchange = %(exchange)s
  AND symbol = %(symbol)s
  AND trade_id IS NULL
"""

_LOCK_TRADE = """
SELECT status, accounting_version, accounting_status
FROM app.trades
WHERE id = %s
FOR UPDATE
"""

_CLAIMED_ELSEWHERE = """
SELECT exec_id, trade_id
FROM app.trade_cost_legs
WHERE exchange = %s AND exec_id = ANY(%s) AND trade_id <> %s
LIMIT 1
"""

_INSERT_LEG = """
INSERT INTO app.trade_cost_legs (
    trade_id, exchange, exec_id, exec_type, order_id, client_order_id, side,
    quantity, price, value_usd, fee_usd, fee_currency, executed_at, raw
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
"""

_WRITE_FIGURES = """
UPDATE app.trades
SET gross_pnl_usd = %(gross_usd)s,
    gross_pnl_pct = %(gross_pct)s,
    net_pnl_usd = %(net_usd)s,
    net_pnl_pct = %(net_pct)s,
    fees_usd = %(fees_usd)s,
    funding_usd = %(funding_usd)s,
    slippage_usd = NULL,
    pnl_usd = %(net_usd)s,
    pnl_pct = %(net_pct)s,
    outcome_label = %(outcome)s,
    accounting_status = %(status)s,
    accounting_error = %(error)s,
    updated_at = now()
WHERE id = %(trade_id)s
"""

_MARK_INCOMPLETE = """
UPDATE app.trades
SET accounting_status = 'incomplete', accounting_error = %s, updated_at = now()
WHERE id = %s
  AND status = 'closed'
  AND accounting_version = %s
  AND accounting_status = 'pending'
RETURNING id
"""

_SETTLEABLE_STATUSES = ("pending", "incomplete")


def _usd(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_EVEN)


def _outcome(net: Decimal) -> str:
    return "win" if net > 0 else ("loss" if net < 0 else "breakeven")


def _figures(
    trade_id: int,
    *,
    gross: Decimal,
    fees: Decimal,
    funding: Decimal,
    entry_value: Decimal,
    status: str,
    error: str | None,
) -> dict[str, Any]:
    net = gross - fees - funding
    return {
        "trade_id": trade_id,
        "gross_usd": _usd(gross),
        "gross_pct": _usd(gross / entry_value * 100),
        "net_usd": _usd(net),
        "net_pct": _usd(net / entry_value * 100),
        "fees_usd": _usd(fees),
        "funding_usd": _usd(funding),
        "outcome": _outcome(net),
        "status": status,
        "error": error,
    }


class PostgresCostStore:
    def __init__(self, db_url: str) -> None:
        self.db_url = db_url

    async def pending_trade_ids(self, settled_before: datetime) -> list[int]:
        async with await psycopg.AsyncConnection.connect(self.db_url) as conn:
            cur = await conn.execute(
                _PENDING_TRADES, (LIVE_EXCHANGE_COSTS_ACCOUNTING_VERSION, settled_before)
            )
            return [int(row[0]) for row in await cur.fetchall()]

    async def load(self, trade_id: int) -> PendingTrade | None:
        async with (
            await psycopg.AsyncConnection.connect(self.db_url) as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(_TRADE, (trade_id,))
            row = await cur.fetchone()
            if row is None or row["filled_amount"] is None or not row["native_market_id"]:
                return None
            keys = {"trade_id": trade_id, "exchange": row["exchange"], "symbol": row["symbol"]}
            await cur.execute(_TRADE_CLOSES, keys)
            closes = await cur.fetchall()
            await cur.execute(_NEIGHBOUR_TRADES, keys)
            trades = await cur.fetchall()
            await cur.execute(_NEIGHBOUR_CLOSE_FILLS, keys)
            close_fills = await cur.fetchall()
            await cur.execute(_NEIGHBOUR_ATTEMPTS, keys)
            attempts = await cur.fetchall()
            await cur.execute(_NEIGHBOUR_TASKS, keys)
            tasks = await cur.fetchall()

        trade_orders = {
            str(value)
            for value in (
                row["entry_order_id"],
                row["exit_order_id"],
                row["attempt_order_id"],
                *(close["order_id"] for close in closes),
            )
            if value
        }
        facts = live_costs.TradeFacts(
            trade_id=trade_id,
            exchange=row["exchange"],
            native_market_id=row["native_market_id"],
            side=row["side"],
            entry_client_order_id=row["client_order_id"],
            entry_order_id=str(row["attempt_order_id"] or row["entry_order_id"] or "") or None,
            entry_filled=Decimal(row["filled_amount"]),
            contract_size=(
                Decimal(row["contract_size"]) if row["contract_size"] is not None else None
            ),
            order_ids=frozenset(trade_orders),
            client_order_ids=frozenset(
                str(close["client_order_id"]) for close in closes if close["client_order_id"]
            ),
            sent_at=row["sent_at"],
            exit_at=row["exit_at"],
        )
        intervals: list[tuple[datetime, datetime | None]] = [
            (trade["entry_at"], trade["exit_at"]) for trade in trades
        ]
        for attempt in attempts:
            if attempt["operation"] == "entry" and attempt["trade_id"] is None:
                ended = attempt["status"] in ("completed", "manual_required", "failed", "no_fill")
                intervals.append((attempt["created_at"], attempt["updated_at"] if ended else None))
        intervals.extend((task["created_at"], task["confirmed_flat_at"]) for task in tasks)
        neighbours = live_costs.Neighbours(
            order_ids=frozenset(
                str(value)
                for value in (
                    *(trade["entry_order_id"] for trade in trades),
                    *(trade["exit_order_id"] for trade in trades),
                    *(fill["order_id"] for fill in close_fills),
                    *(attempt["order_id"] for attempt in attempts),
                )
                if value and str(value) not in trade_orders
            ),
            client_order_ids=frozenset(
                str(attempt["client_order_id"])
                for attempt in attempts
                if attempt["client_order_id"]
                and attempt["client_order_id"] != row["client_order_id"]
            ),
            entry_client_order_ids=frozenset(
                str(attempt["client_order_id"])
                for attempt in attempts
                if attempt["operation"] == "entry"
                and attempt["client_order_id"] != row["client_order_id"]
            ),
            intervals=tuple(intervals),
        )
        return PendingTrade(facts=facts, neighbours=neighbours)

    async def settle(self, trade_id: int, result: live_costs.CostResult) -> bool:
        """Evidence rows and final figures in one transaction; False when the trade
        is no longer ``pending`` (an operator settled it meanwhile). An execution
        already attributed to another trade raises: never counted twice."""
        assert result.gross_pnl_usd is not None
        assert result.fees_usd is not None and result.funding_usd is not None
        assert result.entry_value
        async with await psycopg.AsyncConnection.connect(self.db_url) as conn, conn.transaction():
            cur = await conn.execute(_LOCK_TRADE, (trade_id,))
            locked = await cur.fetchone()
            if locked != ("closed", LIVE_EXCHANGE_COSTS_ACCOUNTING_VERSION, "pending"):
                return False
            exchange = await _trade_exchange(conn, trade_id)
            exec_ids = [leg.execution.exec_id for leg in result.legs]
            cur = await conn.execute(_CLAIMED_ELSEWHERE, (exchange, exec_ids, trade_id))
            claimed = await cur.fetchone()
            if claimed is not None:
                raise ExecutionClaimedError(
                    f"execution {claimed[0]} is already trade {claimed[1]}'s"
                )
            for leg in result.legs:
                row = leg.execution
                await conn.execute(
                    _INSERT_LEG,
                    (
                        trade_id,
                        exchange,
                        row.exec_id,
                        leg.exec_type,
                        row.order_id,
                        row.order_link_id,
                        row.side,
                        row.quantity,
                        row.price,
                        row.value,
                        row.fee,
                        row.fee_currency,
                        row.executed_at,
                        json.dumps(row.raw, sort_keys=True),
                    ),
                )
            await conn.execute(
                _WRITE_FIGURES,
                _figures(
                    trade_id,
                    gross=result.gross_pnl_usd,
                    fees=result.fees_usd,
                    funding=result.funding_usd,
                    entry_value=result.entry_value,
                    status=live_costs.STATUS_COMPLETE,
                    error=None,
                ),
            )
        return True

    async def mark_incomplete(self, trade_id: int, error: str) -> bool:
        async with await psycopg.AsyncConnection.connect(self.db_url) as conn:
            cur = await conn.execute(
                _MARK_INCOMPLETE, (error, trade_id, LIVE_EXCHANGE_COSTS_ACCOUNTING_VERSION)
            )
            return await cur.fetchone() is not None

    async def operator_settle(
        self,
        trade_id: int,
        *,
        gross: Decimal,
        fees: Decimal,
        funding: Decimal,
        entry_value: Decimal,
        note: str,
    ) -> bool:
        """Figures an operator confirmed from the exchange, kept marked as entered by
        hand. Only a closed live trade not yet settled; False otherwise."""
        async with await psycopg.AsyncConnection.connect(self.db_url) as conn, conn.transaction():
            cur = await conn.execute(_LOCK_TRADE, (trade_id,))
            locked = await cur.fetchone()
            if (
                locked is None
                or locked[0] != "closed"
                or locked[1] != LIVE_EXCHANGE_COSTS_ACCOUNTING_VERSION
                or locked[2] not in _SETTLEABLE_STATUSES
            ):
                return False
            await conn.execute(
                _WRITE_FIGURES,
                _figures(
                    trade_id,
                    gross=gross,
                    fees=fees,
                    funding=funding,
                    entry_value=entry_value,
                    status=STATUS_OPERATOR_SETTLED,
                    error=f"settled by an operator: {note}",
                ),
            )
        return True


async def _trade_exchange(conn: psycopg.AsyncConnection[Any], trade_id: int) -> str:
    cur = await conn.execute("SELECT exchange FROM app.trades WHERE id = %s", (trade_id,))
    row = await cur.fetchone()
    assert row is not None
    return str(row[0])


# --- Operator CLI ------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    settle = sub.add_parser("settle", help="record costs confirmed from the exchange")
    settle.add_argument("--trade-id", type=int, required=True)
    settle.add_argument("--gross-usd", type=Decimal, required=True)
    settle.add_argument("--fees-usd", type=Decimal, required=True)
    settle.add_argument("--funding-usd", type=Decimal, required=True)
    settle.add_argument(
        "--entry-value-usd",
        type=Decimal,
        help="filled entry notional; defaults to the trade's size_usd",
    )
    settle.add_argument("--note", required=True)
    return parser


async def _run_settle(args: argparse.Namespace, db_url: str) -> None:
    note = args.note.strip()
    if not note:
        raise SystemExit("--note must say what was checked on the exchange")
    entry_value = args.entry_value_usd
    if entry_value is None:
        async with await psycopg.AsyncConnection.connect(db_url) as conn:
            cur = await conn.execute(
                "SELECT size_usd FROM app.trades WHERE id = %s", (args.trade_id,)
            )
            row = await cur.fetchone()
        if row is None:
            raise SystemExit(f"trade {args.trade_id} not found")
        entry_value = Decimal(row[0])
    if entry_value <= 0:
        raise SystemExit("the entry value must be positive")
    settled = await PostgresCostStore(db_url).operator_settle(
        args.trade_id,
        gross=args.gross_usd,
        fees=args.fees_usd,
        funding=args.funding_usd,
        entry_value=entry_value,
        note=note,
    )
    if not settled:
        raise SystemExit(f"trade {args.trade_id} is not a closed live trade awaiting its costs")
    log.info("live_costs.operator_settled", trade_id=args.trade_id, note=note)


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise SystemExit("DATABASE_URL is required")
    asyncio.run(_run_settle(args, db_url))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
