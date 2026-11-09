"""Fees and funding of a live trade, from the exchange's own executions.

docs/engineering/live-net-accounting-design-v1.md, sections 1 to 4 and 6. The
attribution is a pure function over what the exchange lists, so every rule is tested
on hand-built rows; ``BybitCostSource`` only reads.

Ownership is decided by orders, never by time alone (section 2):

  * the trade's orders are its entry, its journalled closes and every order whose
    ``orderLinkId`` carries the entry's derived prefix (``<entry>sN``, ``<entry>cN``);
  * each order with fills must be terminal, and its execution rows must add up to its
    filled amount exactly;
  * the entry rows, and for a closed trade the exit rows, must each add up to the
    entry's filled quantity exactly;
  * a row of another trade of ours is no conflict; a row of an unknown order inside
    the holding period needs an operator.

Funding is matched to settlements (section 3). Bybit does not guarantee that a
position opened or closed within 5 s of a settlement is charged for it, so a
settlement with a fill of the trade that close is a boundary: only a row whose
quantity matches one the trade held within those 5 s (before the first fill there, or
after any of them) counts, and anything else needs an operator. A row is counted only
when the trade is its one candidate owner on the one-way position. Funding is a
confirmed zero only once the settlement list is read past the last exit plus 5 s and
5 minutes have passed.

The funding history lists a settlement only once it is published, so a settlement it
does not list yet would never be checked. Nothing about Bybit's schedule proves the
list complete: a dynamic interval can add a settlement and return to a longer interval
before either history lists it. So the list counts as complete over the checked range
only once the history has published a settlement at or after its end
(``_check_published``); until then the trade stays pending.

Sign conventions (``execFee`` positive is paid, for fills and funding alike) are an
assumption until the LIVE_PROBE testnet run confirms them; they live in ``_cost``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from . import order_ids, probe_loss

STATUS_UNPROVEN_TAIL = "unproven_tail"
REASON_QUANTITY_MISMATCH = "quantity_mismatch"
STATUS_COMPLETE = "complete"
STATUS_PENDING = "pending"
STATUS_INCOMPLETE = "incomplete"

EXEC_TRADE = "trade"
EXEC_FUNDING = "funding"

# Bybit: a position opened or closed within 5 s of a settlement may or may not be
# charged for it.
BOUNDARY = timedelta(seconds=5)
# No settlement is taken as read, and no zero confirmed, before this long after it.
SETTLE_DELAY = timedelta(minutes=5)
# A funding row is stamped with its settlement time; settlements are hours apart.
_FUNDING_ROW_MATCH = timedelta(seconds=5)
# Journal times of another trade of ours only approximate its fills.
_NEIGHBOUR_MARGIN = timedelta(seconds=60)
# The read window bounds the download only (section 2).
WINDOW_MARGIN = timedelta(minutes=1)

_TERMINAL_ORDER_STATUSES = frozenset(
    {"Filled", "Cancelled", "Rejected", "PartiallyFilledCanceled", "Deactivated"}
)
_USDT = "USDT"


@dataclass(frozen=True)
class Execution:
    """One row of ``/v5/execution/list``."""

    exec_id: str
    exec_type: str
    order_id: str | None
    order_link_id: str | None
    side: str | None
    quantity: Decimal
    price: Decimal | None
    value: Decimal | None
    fee: Decimal
    fee_currency: str | None
    executed_at: datetime
    raw: dict[str, Any]


@dataclass(frozen=True)
class OrderState:
    order_id: str
    terminal: bool
    filled: Decimal | None


@dataclass(frozen=True)
class TradeFacts:
    """What the journal knows about one live trade."""

    trade_id: int
    exchange: str
    native_market_id: str
    side: str
    entry_client_order_id: str
    entry_order_id: str | None
    entry_filled: Decimal
    contract_size: Decimal | None
    # Every exchange order id the journal links to the trade: entry and closes.
    order_ids: frozenset[str]
    # Client order ids of its journalled close attempts.
    client_order_ids: frozenset[str]
    # When the entry attempt was written, before the order was sent.
    sent_at: datetime
    exit_at: datetime | None


@dataclass(frozen=True)
class Neighbours:
    """Every other live trade and order of ours on the same instrument."""

    order_ids: frozenset[str]
    client_order_ids: frozenset[str]
    entry_client_order_ids: frozenset[str]
    # (entry_at, exit_at) of each other trade; exit_at None while it is open.
    intervals: tuple[tuple[datetime, datetime | None], ...]


@dataclass(frozen=True)
class Leg:
    trade_id: int
    exec_type: str
    execution: Execution


@dataclass(frozen=True)
class CostResult:
    status: str
    error: str | None = None
    fees_usd: Decimal | None = None
    funding_usd: Decimal | None = None
    entry_value: Decimal | None = None
    exit_value: Decimal | None = None
    gross_pnl_usd: Decimal | None = None
    legs: tuple[Leg, ...] = ()

    @property
    def net_pnl_usd(self) -> Decimal | None:
        if self.gross_pnl_usd is None or self.fees_usd is None or self.funding_usd is None:
            return None
        return self.gross_pnl_usd - self.fees_usd - self.funding_usd

    @property
    def net_pnl_pct(self) -> Decimal | None:
        net = self.net_pnl_usd
        if net is None or not self.entry_value:
            return None
        return net / self.entry_value * 100

    @property
    def gross_pnl_pct(self) -> Decimal | None:
        if self.gross_pnl_usd is None or not self.entry_value:
            return None
        return self.gross_pnl_usd / self.entry_value * 100


class _StopError(Exception):
    def __init__(self, status: str, error: str) -> None:
        super().__init__(error)
        self.status = status
        self.error = error


def _pending(error: str) -> _StopError:
    return _StopError(STATUS_PENDING, error)


def _incomplete(error: str) -> _StopError:
    return _StopError(STATUS_INCOMPLETE, error)


def _cost(execution: Execution) -> Decimal:
    """The amount paid, positive; a maker rebate or funding received is negative."""
    return execution.fee


def _entry_side(position_side: str) -> str:
    return "Buy" if position_side == "long" else "Sell"


def _exit_side(position_side: str) -> str:
    return "Sell" if position_side == "long" else "Buy"


def settle(
    facts: TradeFacts,
    executions: list[Execution],
    settlements: list[datetime],
    orders: dict[str, OrderState],
    neighbours: Neighbours,
    *,
    published_to: datetime | None,
    now: datetime,
    open_position: bool = False,
) -> CostResult:
    """Attribute the listed executions to the trade and price its costs.

    ``executions`` cover the read window, ``settlements`` the instrument's funding
    times over it, ``published_to`` the newest settlement the funding history listed
    before that (None: none), and ``orders`` the state of every order the trade owns
    rows of or the journal links to it. For an open position (section 6) the exits need not add
    up yet, funding runs to ``now``, and the figures are the costs paid so far.
    """
    try:
        return _settle(
            facts,
            executions,
            settlements,
            orders,
            neighbours,
            published_to=published_to,
            now=now,
            open_position=open_position,
        )
    except _StopError as stop:
        return CostResult(status=stop.status, error=stop.error)


def _settle(
    facts: TradeFacts,
    executions: list[Execution],
    settlements: list[datetime],
    orders: dict[str, OrderState],
    neighbours: Neighbours,
    *,
    published_to: datetime | None,
    now: datetime,
    open_position: bool,
) -> CostResult:
    entry_rows, exit_rows, others, unknown = _checked_fills(
        facts, executions, orders, neighbours, now=now, open_position=open_position
    )
    owned = entry_rows + exit_rows
    first_entry = min(row.executed_at for row in entry_rows)
    last_exit = now if open_position else max(row.executed_at for row in exit_rows)
    if not open_position and now < last_exit + SETTLE_DELAY:
        raise _pending("waiting 5 minutes after the last exit for funding")
    _check_published(
        published_to, settlements, start=first_entry - BOUNDARY, end=last_exit + BOUNDARY
    )

    funding_rows = _match_funding(
        facts,
        entry_rows,
        exit_rows,
        [row for row in executions if row.exec_type == EXEC_FUNDING],
        settlements,
        others + unknown,
        neighbours,
        first_entry=first_entry,
        last_exit=last_exit,
        now=now,
    )

    entry_value = _sum_value(entry_rows)
    exit_value = _sum_value(exit_rows)
    gross: Decimal | None = None
    if not open_position:
        gross = exit_value - entry_value if facts.side == "long" else entry_value - exit_value
    legs = tuple(Leg(facts.trade_id, EXEC_TRADE, row) for row in owned) + tuple(
        Leg(facts.trade_id, EXEC_FUNDING, row) for row in funding_rows
    )
    return CostResult(
        status=STATUS_COMPLETE,
        fees_usd=sum((_cost(row) for row in owned), Decimal(0)),
        funding_usd=sum((_cost(row) for row in funding_rows), Decimal(0)),
        entry_value=entry_value,
        exit_value=exit_value,
        gross_pnl_usd=gross,
        legs=legs,
    )


def _checked_fills(
    facts: TradeFacts,
    executions: list[Execution],
    orders: dict[str, OrderState],
    neighbours: Neighbours,
    *,
    now: datetime,
    open_position: bool,
) -> tuple[list[Execution], list[Execution], list[Execution], list[Execution]]:
    if facts.contract_size is not None and facts.contract_size != 1:
        raise _incomplete(f"contract size {facts.contract_size} is not 1")
    if facts.entry_filled <= 0:
        raise _incomplete("the entry attempt has no filled quantity")

    owned, others, unknown, odd = _partition(facts, executions, neighbours)
    entry_rows = [row for row in owned if _is_entry(facts, row)]
    exit_rows = [row for row in owned if not _is_entry(facts, row)]
    if not entry_rows:
        raise _pending("no entry execution listed yet")

    _check_sides(facts, entry_rows, exit_rows)
    _check_quantities(facts, entry_rows, exit_rows, open_position=open_position)
    _check_orders(facts, owned, orders, open_position=open_position)

    first_entry = min(row.executed_at for row in entry_rows)
    last_exit = now if open_position else max(row.executed_at for row in exit_rows)
    for row in unknown + odd:
        if first_entry <= row.executed_at <= last_exit:
            raise _incomplete(
                f"execution {row.exec_id} ({row.exec_type}) of an unknown order inside the "
                "holding period"
            )
    for row in owned:
        if row.fee_currency not in (None, "", _USDT):
            raise _incomplete(f"execution {row.exec_id} fee in {row.fee_currency}")
    return entry_rows, exit_rows, others, unknown


def _partition(
    facts: TradeFacts, executions: list[Execution], neighbours: Neighbours
) -> tuple[list[Execution], list[Execution], list[Execution], list[Execution]]:
    """Fill rows split into this trade's, another trade's of ours, and unknown ones;
    plus rows of any type other than a fill or funding (ADL, liquidation, ...)."""
    owned: list[Execution] = []
    others: list[Execution] = []
    unknown: list[Execution] = []
    odd: list[Execution] = []
    for row in executions:
        if row.exec_type == EXEC_FUNDING:
            continue
        if row.exec_type != EXEC_TRADE:
            odd.append(row)
        elif _owned_by(
            row, facts.order_ids, facts.client_order_ids, (facts.entry_client_order_id,)
        ):
            owned.append(row)
        elif _owned_by(
            row,
            neighbours.order_ids,
            neighbours.client_order_ids,
            tuple(neighbours.entry_client_order_ids),
        ):
            others.append(row)
        else:
            unknown.append(row)
    return owned, others, unknown, odd


def _owned_by(
    row: Execution,
    known_order_ids: frozenset[str],
    known_client_order_ids: frozenset[str],
    entry_client_order_ids: tuple[str, ...],
) -> bool:
    if row.order_id and row.order_id in known_order_ids:
        return True
    link = row.order_link_id or ""
    if not link:
        return False
    if link in known_client_order_ids or link in entry_client_order_ids:
        return True
    return any(
        link.startswith((order_ids.stop_prefix(entry), order_ids.close_prefix(entry)))
        and link[len(entry) + 1 :].isdigit()
        for entry in entry_client_order_ids
    )


def _is_entry(facts: TradeFacts, row: Execution) -> bool:
    if facts.entry_order_id and row.order_id == facts.entry_order_id:
        return True
    return row.order_link_id == facts.entry_client_order_id


def _check_sides(
    facts: TradeFacts, entry_rows: list[Execution], exit_rows: list[Execution]
) -> None:
    for rows, expected in (
        (entry_rows, _entry_side(facts.side)),
        (exit_rows, _exit_side(facts.side)),
    ):
        for row in rows:
            if row.side != expected:
                raise _incomplete(f"execution {row.exec_id} side {row.side}, expected {expected}")


def _check_quantities(
    facts: TradeFacts,
    entry_rows: list[Execution],
    exit_rows: list[Execution],
    *,
    open_position: bool,
) -> None:
    """Exact equality: a lost contract is never absorbed by a tolerance."""
    entered = _sum_quantity(entry_rows)
    if entered < facts.entry_filled:
        raise _pending(f"entry executions list {entered} of {facts.entry_filled}")
    if entered > facts.entry_filled:
        raise _incomplete(f"entry executions list {entered}, the entry filled {facts.entry_filled}")
    exited = _sum_quantity(exit_rows)
    if exited > facts.entry_filled:
        raise _incomplete(f"exit executions list {exited}, the entry filled {facts.entry_filled}")
    if not open_position and exited < facts.entry_filled:
        raise _pending(f"exit executions list {exited} of {facts.entry_filled}")


def _check_orders(
    facts: TradeFacts,
    owned: list[Execution],
    orders: dict[str, OrderState],
    *,
    open_position: bool,
) -> None:
    """Each order with fills is terminal and its rows add up to its filled amount.

    For an open position a non-terminal exit (a resting stop) may still fill; what
    it filled so far must be listed all the same.
    """
    listed: dict[str, Decimal] = {}
    for row in owned:
        if not row.order_id:
            raise _incomplete(f"execution {row.exec_id} has no order id")
        listed[row.order_id] = listed.get(row.order_id, Decimal(0)) + row.quantity
    for order_id in sorted(set(listed) | facts.order_ids):
        state = orders.get(order_id)
        if state is None:
            raise _pending(f"order {order_id} not read yet")
        rows_quantity = listed.get(order_id, Decimal(0))
        if state.filled is None:
            raise _pending(f"order {order_id} reports no filled amount")
        if not state.terminal and (not open_position or order_id == facts.entry_order_id):
            if rows_quantity or state.filled:
                raise _pending(f"order {order_id} is not terminal")
            continue
        if rows_quantity < state.filled:
            raise _pending(f"order {order_id} executions list {rows_quantity} of {state.filled}")
        if rows_quantity > state.filled:
            raise _incomplete(
                f"order {order_id} executions list {rows_quantity}, it filled {state.filled}"
            )


def _check_published(
    published_to: datetime | None,
    settlements: list[datetime],
    *,
    start: datetime,
    end: datetime,
) -> None:
    """Every settlement in [start, end], both included, must be in ``settlements``.

    The history is complete up to the newest settlement it lists, and only up to it.
    The current interval or the next settlement time proves nothing about the time
    since: Bybit may settle on a shorter interval and return to a longer one before
    either history lists that settlement."""
    if published_to is None:
        raise _pending("the funding history lists no settlement yet")
    if start <= published_to <= end and published_to not in settlements:
        raise _pending(f"settlement {published_to.isoformat()} listed after the window was read")
    if published_to < end:
        raise _pending(
            f"funding history published to {published_to.isoformat()}, the checked range "
            f"ends {end.isoformat()}"
        )


def _match_funding(
    facts: TradeFacts,
    entry_rows: list[Execution],
    exit_rows: list[Execution],
    funding_rows: list[Execution],
    settlements: list[datetime],
    foreign_rows: list[Execution],
    neighbours: Neighbours,
    *,
    first_entry: datetime,
    last_exit: datetime,
    now: datetime,
    unlisted: list[datetime] | None = None,
) -> list[Execution]:
    start = first_entry - BOUNDARY
    end = last_exit + BOUNDARY
    checked = sorted({at for at in settlements if start <= at <= end})
    fills = entry_rows + exit_rows

    def held_before(moment: datetime) -> Decimal:
        return _sum_quantity([r for r in entry_rows if r.executed_at < moment]) - _sum_quantity(
            [r for r in exit_rows if r.executed_at < moment]
        )

    counted: list[Execution] = []
    for at in checked:
        rows = [row for row in funding_rows if abs(row.executed_at - at) <= _FUNDING_ROW_MATCH]
        if len(rows) > 1:
            raise _incomplete(f"{len(rows)} funding rows at the settlement {at.isoformat()}")
        row = rows[0] if rows else None
        near = [fill for fill in fills if abs(fill.executed_at - at) <= BOUNDARY]
        if row is None and unlisted is not None:
            quantities = _held_quantities(entry_rows, exit_rows, at) if near else [held_before(at)]
            if max(quantities) > 0:
                _check_single_owner(at, foreign_rows, neighbours)
                unlisted.append(at)
            continue
        if not near:
            held = held_before(at)
            if held == 0:
                continue  # clearly not held: a row at this time is not this trade's
            if row is None:
                raise _pending(f"funding of the settlement {at.isoformat()} not listed yet")
            if row.quantity != held:
                raise _incomplete(
                    f"funding at {at.isoformat()} for {row.quantity}, the trade held {held}"
                )
        else:
            states = _held_quantities(entry_rows, exit_rows, at)
            if row is None:
                if now < at + SETTLE_DELAY:
                    raise _pending(f"funding of the settlement {at.isoformat()} not listed yet")
                raise _incomplete(
                    f"fill within 5 s of the settlement {at.isoformat()} and no funding row"
                )
            if row.quantity == 0 or row.quantity not in states:
                held_text = ", ".join(str(state) for state in states)
                raise _incomplete(
                    f"funding at {at.isoformat()} for {row.quantity} next to a fill; held "
                    f"{held_text} around it"
                )
        if row.fee_currency not in (None, "", _USDT):
            raise _incomplete(f"funding {row.exec_id} in {row.fee_currency}")
        _check_single_owner(at, foreign_rows, neighbours)
        counted.append(row)

    window_rows = [row for row in funding_rows if start <= row.executed_at <= end]
    unmatched = [
        row
        for row in window_rows
        if not any(abs(row.executed_at - at) <= _FUNDING_ROW_MATCH for at in checked)
    ]
    if unmatched:
        raise _incomplete(f"funding row {unmatched[0].exec_id} matches no listed settlement")
    return counted


def _held_quantities(
    entries: list[Execution], exits: list[Execution], at: datetime
) -> list[Decimal]:
    """Every quantity that may be captured within Bybit's five-second boundary."""
    held = _sum_quantity([r for r in entries if r.executed_at < at - BOUNDARY]) - _sum_quantity(
        [r for r in exits if r.executed_at < at - BOUNDARY]
    )
    states = [held]
    entry_ids = {r.exec_id for r in entries}
    near = [r for r in entries + exits if abs(r.executed_at - at) <= BOUNDARY]
    for row in sorted(near, key=lambda r: (r.executed_at, r.exec_id)):
        held += row.quantity if row.exec_id in entry_ids else -row.quantity
        states.append(held)
    return states


def _check_single_owner(
    at: datetime, foreign_rows: list[Execution], neighbours: Neighbours
) -> None:
    """The account is one-way, so a funding row belongs to the instrument's single
    position. It is counted only when no other trade of ours and no unknown order
    could own it (section 3)."""
    for entry_at, exit_at in neighbours.intervals:
        if entry_at - _NEIGHBOUR_MARGIN <= at and (
            exit_at is None or at <= exit_at + _NEIGHBOUR_MARGIN
        ):
            raise _incomplete(
                f"another trade of ours on the instrument could own the funding at {at.isoformat()}"
            )
    for row in foreign_rows:
        if abs(row.executed_at - at) <= BOUNDARY:
            raise _incomplete(
                f"execution {row.exec_id} of another order within 5 s of the settlement "
                f"{at.isoformat()}"
            )


def _sum_quantity(rows: list[Execution]) -> Decimal:
    return sum((row.quantity for row in rows), Decimal(0))


def _sum_value(rows: list[Execution]) -> Decimal:
    total = Decimal(0)
    for row in rows:
        if row.value is None:
            raise _incomplete(f"execution {row.exec_id} has no value")
        total += row.value
    return total


class CostSource(Protocol):
    async def executions(self, symbol: str, start: datetime, end: datetime) -> list[Execution]: ...

    async def settlements(self, symbol: str, start: datetime, end: datetime) -> list[datetime]: ...

    async def published_to(self, symbol: str) -> datetime | None: ...

    async def order(self, symbol: str, order_id: str) -> OrderState | None: ...


# --- Bybit ------------------------------------------------------------------------

_MAX_WINDOW = timedelta(days=7)
_EXECUTION_PAGE = 100
_SETTLEMENT_PAGE = 200
_MAX_PAGES = 1000


def _decimal(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} {value!r} is not a number") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} {value!r} is not finite")
    return parsed


def _optional_decimal(value: Any, field: str) -> Decimal | None:
    return None if value in (None, "") else _decimal(value, field)


def _millis(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _from_millis(value: Any) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


def execution_from_row(row: dict[str, Any]) -> Execution:
    """One raw Bybit execution row; anything malformed raises, never defaults."""
    exec_type = str(row.get("execType") or "")
    exec_id = str(row.get("execId") or "")
    if not exec_id:
        raise ValueError("execution row without execId")
    return Execution(
        exec_id=exec_id,
        exec_type=(
            EXEC_TRADE
            if exec_type == "Trade"
            else EXEC_FUNDING
            if exec_type == "Funding"
            else exec_type.lower()
        ),
        order_id=str(row["orderId"]) if row.get("orderId") else None,
        order_link_id=str(row["orderLinkId"]) if row.get("orderLinkId") else None,
        side=str(row["side"]) if row.get("side") else None,
        quantity=_decimal(row.get("execQty"), "execQty"),
        price=_optional_decimal(row.get("execPrice"), "execPrice"),
        value=_optional_decimal(row.get("execValue"), "execValue"),
        fee=_decimal(row.get("execFee"), "execFee"),
        fee_currency=str(row["feeCurrency"]) if row.get("feeCurrency") else None,
        executed_at=_from_millis(row.get("execTime")),
        raw=dict(row),
    )


def _result(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict) or str(response.get("retCode", "0")) != "0":
        raise RuntimeError(f"Bybit error: {response!r}"[:300])
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("Bybit response without result")
    return result


class BybitCostSource:
    """Reads Bybit's execution list, funding history and order history for one
    linear instrument. Every failed or malformed read raises: a partial view is
    never taken as complete."""

    def __init__(self, exchange: Any) -> None:
        self.exchange = exchange

    async def executions(self, symbol: str, start: datetime, end: datetime) -> list[Execution]:
        rows: dict[str, Execution] = {}
        chunk_start = start
        while chunk_start < end:
            chunk_end = min(chunk_start + _MAX_WINDOW, end)
            cursor: str | None = None
            for _ in range(_MAX_PAGES):
                params: dict[str, Any] = {
                    "category": "linear",
                    "symbol": symbol,
                    "startTime": _millis(chunk_start),
                    "endTime": _millis(chunk_end),
                    "limit": _EXECUTION_PAGE,
                }
                if cursor:
                    params["cursor"] = cursor
                result = _result(await self.exchange.privateGetV5ExecutionList(params))
                for raw in result.get("list") or []:
                    execution = execution_from_row(raw)
                    rows[execution.exec_id] = execution
                cursor = result.get("nextPageCursor") or None
                if not cursor:
                    break
            else:
                raise RuntimeError("execution list did not end")
            chunk_start = chunk_end
        return sorted(rows.values(), key=lambda row: (row.executed_at, row.exec_id))

    async def settlements(self, symbol: str, start: datetime, end: datetime) -> list[datetime]:
        return sorted(await self._funding_history(symbol, start, end))

    async def funding_rates(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[FundingSettlement]:
        rows = await self._funding_history(symbol, start, end)
        return [
            FundingSettlement(at, _optional_decimal(rows[at].get("fundingRate"), "fundingRate"))
            for at in sorted(rows)
        ]

    async def _funding_history(
        self, symbol: str, start: datetime, end: datetime
    ) -> dict[datetime, dict[str, Any]]:
        """Page all settlements in [start, end], keeping the native rates."""
        found: dict[datetime, dict[str, Any]] = {}
        page_end = end
        for _ in range(_MAX_PAGES):
            result = _result(
                await self.exchange.publicGetV5MarketFundingHistory(
                    {
                        "category": "linear",
                        "symbol": symbol,
                        "startTime": _millis(start),
                        "endTime": _millis(page_end),
                        "limit": _SETTLEMENT_PAGE,
                    }
                )
            )
            listed = [_from_millis(r["fundingRateTimestamp"]) for r in result.get("list") or []]
            for row in result.get("list") or []:
                at = _from_millis(row["fundingRateTimestamp"])
                if start <= at <= end:
                    found[at] = row
            if len(listed) < _SETTLEMENT_PAGE:
                return found
            page_end = min(listed) - timedelta(milliseconds=1)
            if page_end < start:
                return found
        raise RuntimeError("funding history did not end")

    async def published_to(self, symbol: str) -> datetime | None:
        """The newest settlement the funding history lists; None when it lists none."""
        result = _result(
            await self.exchange.publicGetV5MarketFundingHistory(
                {"category": "linear", "symbol": symbol, "limit": 1}
            )
        )
        listed = [_from_millis(row["fundingRateTimestamp"]) for row in result.get("list") or []]
        return max(listed) if listed else None

    async def funding_observation(self, symbol: str) -> FundingSettlement:
        result = _result(
            await self.exchange.publicGetV5MarketTickers({"category": "linear", "symbol": symbol})
        )
        rows = [r for r in result.get("list") or [] if r.get("symbol") == symbol]
        if len(rows) != 1:
            raise RuntimeError("ticker did not identify one exact instrument")
        return FundingSettlement(
            _from_millis(rows[0]["nextFundingTime"]),
            _optional_decimal(rows[0].get("fundingRate"), "fundingRate"),
        )

    async def settlement_mark(self, symbol: str, at: datetime, now: datetime) -> Decimal | None:
        minute = at.replace(second=0, microsecond=0)
        if now < minute + timedelta(minutes=1):
            return None  # the second bar has not closed yet
        expected = {minute - timedelta(minutes=1), minute}
        result = _result(
            await self.exchange.publicGetV5MarketMarkPriceKline(
                {
                    "category": "linear",
                    "symbol": symbol,
                    "interval": "1",
                    "start": _millis(min(expected)),
                    "end": _millis(minute),
                    "limit": 2,
                }
            )
        )
        bars = {_from_millis(r[0]): _decimal(r[2], "mark high") for r in result.get("list") or []}
        if set(bars) != expected or min(bars.values()) <= 0:
            return None
        return max(bars.values())

    async def order(self, symbol: str, order_id: str) -> OrderState | None:
        """The order from Bybit's order history, then its open orders; None when
        neither lists it (not known yet, never "absent")."""
        for method in ("privateGetV5OrderHistory", "privateGetV5OrderRealtime"):
            result = _result(
                await getattr(self.exchange, method)(
                    {"category": "linear", "symbol": symbol, "orderId": order_id}
                )
            )
            for row in result.get("list") or []:
                if str(row.get("orderId")) == order_id:
                    return OrderState(
                        order_id=order_id,
                        terminal=str(row.get("orderStatus")) in _TERMINAL_ORDER_STATUSES,
                        filled=_optional_decimal(row.get("cumExecQty"), "cumExecQty"),
                    )
        return None


async def read(
    source: CostSource,
    facts: TradeFacts,
    neighbours: Neighbours,
    *,
    now: datetime,
    open_position: bool = False,
) -> CostResult:
    """Read the window and the orders, then settle.

    The window runs from a minute before the entry attempt was written (no fill comes
    before the order was sent) to now, which the worker reaches only 5 minutes after
    the exit, so it always covers the last exit plus 5 s. It bounds the download only.
    The newest published settlement is read before the settlement list, so the list
    holds every settlement up to it.
    """
    end = now
    start = facts.sent_at - WINDOW_MARGIN
    symbol = facts.native_market_id
    executions = await source.executions(symbol, start, end)
    published_to = await source.published_to(symbol)
    settlements = await source.settlements(symbol, start, end)
    orders = await _read_orders(source, facts, executions)
    return settle(
        facts,
        executions,
        settlements,
        orders,
        neighbours,
        published_to=published_to,
        now=now,
        open_position=open_position,
    )


async def _read_orders(
    source: CostSource, facts: TradeFacts, executions: list[Execution]
) -> dict[str, OrderState]:
    symbol = facts.native_market_id
    wanted = set(facts.order_ids)
    wanted.update(
        row.order_id
        for row in executions
        if row.exec_type == EXEC_TRADE
        and row.order_id
        and _owned_by(row, facts.order_ids, facts.client_order_ids, (facts.entry_client_order_id,))
    )
    orders: dict[str, OrderState] = {}
    for order_id in sorted(wanted):
        state = await source.order(symbol, order_id)
        if state is not None:
            orders[order_id] = state
    return orders


@dataclass(frozen=True)
class FundingSettlement:
    at: datetime
    rate: Decimal | None


@dataclass(frozen=True)
class OpenCostResult:
    status: str
    error: str | None = None
    position: probe_loss.OpenPosition | None = None
    published_to: datetime | None = None
    reason: str | None = None
    observed_quantity: Decimal | None = None


class OpenCostSource(CostSource, Protocol):
    async def funding_rates(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[FundingSettlement]: ...

    async def settlement_mark(self, symbol: str, at: datetime, now: datetime) -> Decimal | None: ...


def collect_open(
    facts: TradeFacts,
    executions: list[Execution],
    settlements: list[FundingSettlement],
    orders: dict[str, OrderState],
    neighbours: Neighbours,
    *,
    journal_quantity: Decimal,
    published_to: datetime | None,
    observed: tuple[FundingSettlement, ...],
    marks: dict[datetime, Decimal | None],
    now: datetime,
) -> OpenCostResult:
    """Prove fills independently of the funding tail; never label an open trade complete.

    Published settlements and times actually observed on tickers may own funding.
    Missing rows are estimated separately; unobserved times remain an unknown tail.
    """
    try:
        if facts.exit_at is not None:
            raise _incomplete("the trade is already closed")
        entry, exits, others, unknown = _checked_fills(
            facts, executions, orders, neighbours, now=now, open_position=True
        )
        if any(row.executed_at > now for row in entry + exits):
            raise _pending("a fill is timestamped after the observation time")
        # An active partially filled order can still change ownership. Unlike the
        # legacy open_costs path, the probe waits for terminal evidence.
        _check_orders(facts, entry + exits, orders, open_position=False)
        quantity = _sum_quantity(entry) - _sum_quantity(exits)
        if quantity <= 0 or quantity != journal_quantity:
            return OpenCostResult(
                STATUS_INCOMPLETE,
                error=f"open executions hold {quantity}, journal holds {journal_quantity}",
                published_to=published_to,
                reason=REASON_QUANTITY_MISMATCH,
                observed_quantity=quantity,
            )
        for row in entry + exits:
            if row.price is None or row.price <= 0 or row.quantity <= 0:
                raise _incomplete(f"execution {row.exec_id} has no positive price or quantity")
        if published_to is not None:
            _check_published(
                published_to,
                [settlement.at for settlement in settlements],
                start=min(r.executed_at for r in entry) - BOUNDARY,
                end=published_to,
            )
        # Publication may advance between the latest-time read and window paging.
        # Keep the earlier consistency check, then retain the newer proof too.
        published_to = max(
            ([published_to] if published_to is not None else [])
            + [s.at for s in settlements if s.at <= now],
            default=None,
        )
        listed = {s.at: s.rate for s in settlements if s.at <= now}
        # An announced time absent from history's proven prefix was moved/cancelled.
        # Keep actual published settlements even while their execution row is pending.
        rates = {
            s.at: s.rate
            for s in observed
            if s.at <= now and (published_to is None or s.at > published_to or s.at in listed)
        }
        rates.update(listed)
        missing: list[datetime] = []
        funding = _match_funding(
            facts,
            entry,
            exits,
            [r for r in executions if r.exec_type == EXEC_FUNDING],
            list(rates),
            others + unknown,
            neighbours,
            first_entry=min(r.executed_at for r in entry),
            last_exit=now,
            now=now,
            unlisted=missing,
        )
        vwap = probe_loss.entry_vwap(
            probe_loss.Fill(r.quantity, r.price) for r in entry if r.price is not None
        )
        estimates = []
        for at in missing:
            estimates.append(
                probe_loss.UnlistedSettlement(
                    at, rates[at], max(_held_quantities(entry, exits, at)), marks.get(at)
                )
            )
        return OpenCostResult(
            STATUS_UNPROVEN_TAIL,
            position=probe_loss.OpenPosition(
                side=facts.side,
                entry_vwap=vwap,
                open_qty=quantity,
                realized_open_parts=probe_loss.realized_open_parts(
                    facts.side,
                    vwap,
                    (probe_loss.Fill(r.quantity, r.price) for r in exits if r.price is not None),
                ),
                fees_paid_open=sum((_cost(r) for r in entry + exits), Decimal(0)),
                funding_paid_open=sum((_cost(r) for r in funding), Decimal(0)),
                unlisted=tuple(estimates),
            ),
            published_to=published_to,
        )
    except _StopError as stop:
        return OpenCostResult(stop.status, error=stop.error, published_to=published_to)


async def read_open(
    source: OpenCostSource,
    facts: TradeFacts,
    neighbours: Neighbours,
    *,
    journal_quantity: Decimal,
    observed: tuple[FundingSettlement, ...],
    now: datetime,
) -> OpenCostResult:
    symbol = facts.native_market_id
    start = facts.sent_at - WINDOW_MARGIN
    executions = await source.executions(symbol, start, now)
    published_to = await source.published_to(symbol)
    settlements = await source.funding_rates(symbol, start, now)
    orders = await _read_orders(source, facts, executions)
    # First validate ownership. Do not read mark bars for rows we cannot attribute.
    result = collect_open(
        facts,
        executions,
        settlements,
        orders,
        neighbours,
        journal_quantity=journal_quantity,
        published_to=published_to,
        observed=observed,
        marks={},
        now=now,
    )
    if result.position is None:
        return result
    marks: dict[datetime, Decimal | None] = {}
    for settlement in result.position.unlisted:
        try:
            marks[settlement.at] = await source.settlement_mark(symbol, settlement.at, now)
        except Exception:
            # The settlement stays explicitly unknown, never priced at today's mark.
            marks[settlement.at] = None
    return collect_open(
        facts,
        executions,
        settlements,
        orders,
        neighbours,
        journal_quantity=journal_quantity,
        published_to=published_to,
        observed=observed,
        marks=marks,
        now=now,
    )
