"""Attribution of Bybit executions to a live trade, on hand-built rows.

docs/engineering/live-net-accounting-design-v1.md, the test table. The Bybit reads
are tested on a fake client at the end, and the store on Postgres in
test_cost_worker.py.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
from schurfer_execution import live_costs as lc

ENTRY = "a" * 32
OTHER_ENTRY = "b" * 32
T_ENTRY = datetime(2026, 11, 3, 7, 0, 30, tzinfo=UTC)
T_EXIT = datetime(2026, 11, 3, 9, 30, 0, tzinfo=UTC)
SETTLEMENTS = [datetime(2026, 11, 3, hour, tzinfo=UTC) for hour in range(6, 12)]
NO_NEIGHBOURS = lc.Neighbours(frozenset(), frozenset(), frozenset(), ())


def _row(
    exec_id: str,
    *,
    order_id: str,
    side: str,
    qty: str,
    price: str,
    at: datetime,
    fee: str,
    link: str | None = None,
    exec_type: str = lc.EXEC_TRADE,
    fee_currency: str | None = "USDT",
) -> lc.Execution:
    quantity = Decimal(qty)
    return lc.Execution(
        exec_id=exec_id,
        exec_type=exec_type,
        order_id=order_id,
        order_link_id=link,
        side=side,
        quantity=quantity,
        price=Decimal(price),
        value=quantity * Decimal(price),
        fee=Decimal(fee),
        fee_currency=fee_currency,
        executed_at=at,
        raw={"execId": exec_id},
    )


def _funding(exec_id: str, *, qty: str, at: datetime, fee: str) -> lc.Execution:
    return lc.Execution(
        exec_id=exec_id,
        exec_type=lc.EXEC_FUNDING,
        order_id=None,
        order_link_id=None,
        side="Buy",
        quantity=Decimal(qty),
        price=None,
        value=None,
        fee=Decimal(fee),
        fee_currency="USDT",
        executed_at=at,
        raw={"execId": exec_id},
    )


def _entry(qty: str = "100", *, at: datetime = T_ENTRY, exec_id: str = "e1") -> lc.Execution:
    return _row(
        exec_id,
        order_id="o-entry",
        link=ENTRY,
        side="Buy",
        qty=qty,
        price="1.00",
        at=at,
        fee="0.055",
    )


def _exit(
    qty: str = "100",
    *,
    at: datetime = T_EXIT,
    exec_id: str = "x1",
    order_id: str = "o-exit",
    link: str | None = None,
    fee: str = "0.0605",
) -> lc.Execution:
    return _row(
        exec_id, order_id=order_id, link=link, side="Sell", qty=qty, price="1.10", at=at, fee=fee
    )


def _facts(**changes: Any) -> lc.TradeFacts:
    facts = lc.TradeFacts(
        trade_id=1,
        exchange="bybit",
        native_market_id="BTCUSDT",
        side="long",
        entry_client_order_id=ENTRY,
        entry_order_id="o-entry",
        entry_filled=Decimal(100),
        contract_size=Decimal(1),
        order_ids=frozenset({"o-entry", "o-exit"}),
        client_order_ids=frozenset(),
        sent_at=T_ENTRY - timedelta(seconds=1),
        exit_at=T_EXIT,
    )
    return replace(facts, **changes)


def _orders(*fills: tuple[str, str], terminal: bool = True) -> dict[str, lc.OrderState]:
    return {
        order_id: lc.OrderState(order_id=order_id, terminal=terminal, filled=Decimal(filled))
        for order_id, filled in fills
    }


ORDERS = _orders(("o-entry", "100"), ("o-exit", "100"))
HELD_FUNDING = [
    _funding("f8", qty="100", at=SETTLEMENTS[2], fee="0.05"),
    _funding("f9", qty="100", at=SETTLEMENTS[3], fee="-0.02"),
]
LATER = T_EXIT + timedelta(minutes=10)


def _settle(
    executions: list[lc.Execution],
    *,
    facts: lc.TradeFacts | None = None,
    orders: dict[str, lc.OrderState] | None = None,
    neighbours: lc.Neighbours = NO_NEIGHBOURS,
    settlements: list[datetime] | None = None,
    now: datetime = LATER,
    open_position: bool = False,
) -> lc.CostResult:
    return lc.settle(
        facts or _facts(),
        executions,
        SETTLEMENTS if settlements is None else settlements,
        ORDERS if orders is None else orders,
        neighbours,
        now=now,
        open_position=open_position,
    )


def test_market_round_trip_with_two_settlements_held() -> None:
    result = _settle([_entry(), *HELD_FUNDING, _exit()])

    assert result.status == lc.STATUS_COMPLETE, result.error
    assert result.fees_usd == Decimal("0.1155")
    assert result.funding_usd == Decimal("0.03")  # 0.05 paid, 0.02 received
    assert result.gross_pnl_usd == Decimal("10.00")
    assert result.net_pnl_usd == Decimal("9.8545")
    assert result.net_pnl_pct == Decimal("9.8545")
    assert sorted(leg.execution.exec_id for leg in result.legs) == ["e1", "f8", "f9", "x1"]


def test_maker_exit_rebate_counts_as_a_negative_fee() -> None:
    result = _settle([_entry(), *HELD_FUNDING, _exit(fee="-0.0220")])

    assert result.status == lc.STATUS_COMPLETE, result.error
    assert result.fees_usd == Decimal("0.0330")


def test_funding_received_on_a_short_is_negative() -> None:
    entry = _row(
        "e1",
        order_id="o-entry",
        link=ENTRY,
        side="Sell",
        qty="100",
        price="1.10",
        at=T_ENTRY,
        fee="0.0605",
    )
    exit_ = _row(
        "x1", order_id="o-exit", side="Buy", qty="100", price="1.00", at=T_EXIT, fee="0.055"
    )
    funding = [
        _funding("f8", qty="100", at=SETTLEMENTS[2], fee="-0.04"),
        _funding("f9", qty="100", at=SETTLEMENTS[3], fee="-0.01"),
    ]
    result = _settle([entry, *funding, exit_], facts=_facts(side="short"))

    assert result.status == lc.STATUS_COMPLETE, result.error
    assert result.funding_usd == Decimal("-0.05")
    assert result.gross_pnl_usd == Decimal("10.00")


def test_exit_by_an_exchange_stop_never_journalled_is_attributed_by_its_link() -> None:
    stop = _exit(order_id="o-stop", link=f"{ENTRY}s1")
    result = _settle(
        [_entry(), *HELD_FUNDING, stop],
        facts=_facts(order_ids=frozenset({"o-entry"})),
        orders=_orders(("o-entry", "100"), ("o-stop", "100")),
    )

    assert result.status == lc.STATUS_COMPLETE, result.error
    assert {leg.execution.order_id for leg in result.legs if leg.exec_type == "trade"} == {
        "o-entry",
        "o-stop",
    }


def test_emergency_close_round_trip_with_two_close_orders() -> None:
    c1 = _exit("40", order_id="o-c1", link=f"{ENTRY}c1", exec_id="x1")
    c2 = _exit("60", order_id="o-c2", link=f"{ENTRY}c2", exec_id="x2")
    result = _settle(
        [_entry(), *HELD_FUNDING, c1, c2],
        facts=_facts(order_ids=frozenset({"o-entry"})),
        orders=_orders(("o-entry", "100"), ("o-c1", "40"), ("o-c2", "60")),
    )

    assert result.status == lc.STATUS_COMPLETE, result.error
    assert result.gross_pnl_usd == Decimal("10.00")


def test_orders_filled_100_but_listed_50_each_never_complete() -> None:
    """The colleague's case: equal sums of what was found prove nothing."""
    result = _settle(
        [_entry("50"), *HELD_FUNDING, _exit("50")],
        facts=_facts(entry_filled=Decimal(100)),
    )

    assert result.status == lc.STATUS_PENDING
    assert "50 of 100" in (result.error or "")


def test_exit_rows_short_by_one_contract_stay_pending() -> None:
    result = _settle(
        [_entry(), *HELD_FUNDING, _exit("99")],
        orders=_orders(("o-entry", "100"), ("o-exit", "99")),
    )

    assert result.status == lc.STATUS_PENDING
    assert "99 of 100" in (result.error or "")


def test_an_order_listing_fewer_rows_than_its_fill_stays_pending() -> None:
    result = _settle(
        [_entry(), *HELD_FUNDING, _exit("60", exec_id="x1"), _exit("40", order_id="o-c9")],
        facts=_facts(order_ids=frozenset({"o-entry", "o-exit", "o-c9"})),
        orders=_orders(("o-entry", "100"), ("o-exit", "70"), ("o-c9", "40")),
    )

    assert result.status == lc.STATUS_PENDING
    assert "o-exit executions list 60 of 70" in (result.error or "")


def test_an_order_listing_more_than_it_filled_needs_an_operator() -> None:
    result = _settle(
        [_entry(), *HELD_FUNDING, _exit()],
        orders=_orders(("o-entry", "100"), ("o-exit", "90")),
    )

    assert result.status == lc.STATUS_INCOMPLETE


def test_an_order_not_terminal_stays_pending() -> None:
    orders = {**ORDERS, "o-exit": lc.OrderState("o-exit", terminal=False, filled=Decimal(100))}
    result = _settle([_entry(), *HELD_FUNDING, _exit()], orders=orders)

    assert result.status == lc.STATUS_PENDING


def test_an_order_not_read_stays_pending() -> None:
    result = _settle([_entry(), *HELD_FUNDING, _exit()], orders=_orders(("o-entry", "100")))

    assert result.status == lc.STATUS_PENDING


def test_unknown_fill_inside_the_holding_period_needs_an_operator() -> None:
    stranger = _row(
        "u1",
        order_id="o-unknown",
        side="Buy",
        qty="5",
        price="1.05",
        at=T_ENTRY + timedelta(minutes=20),
        fee="0.01",
    )
    result = _settle([_entry(), stranger, *HELD_FUNDING, _exit()])

    assert result.status == lc.STATUS_INCOMPLETE
    assert "unknown order" in (result.error or "")


def test_unknown_fill_outside_the_holding_period_is_not_the_trades() -> None:
    stranger = _row(
        "u1",
        order_id="o-unknown",
        side="Buy",
        qty="5",
        price="1.05",
        at=T_EXIT + timedelta(seconds=30),
        fee="0.01",
    )
    result = _settle([_entry(), *HELD_FUNDING, _exit(), stranger])

    assert result.status == lc.STATUS_COMPLETE, result.error


def test_liquidation_row_inside_the_holding_period_needs_an_operator() -> None:
    bust = _row(
        "b1",
        order_id="",
        side="Sell",
        qty="100",
        price="0.9",
        at=T_ENTRY + timedelta(minutes=5),
        fee="0",
        exec_type="busttrade",
    )
    result = _settle([_entry(), bust, *HELD_FUNDING, _exit()])

    assert result.status == lc.STATUS_INCOMPLETE


def test_trade_b_opening_20_s_after_a_closed_is_bs() -> None:
    """The colleague's neighbour case: B's fills inside A's read window are B's."""
    b_entry = _row(
        "b1",
        order_id="o-b",
        link=OTHER_ENTRY,
        side="Buy",
        qty="100",
        price="1.10",
        at=T_EXIT + timedelta(seconds=20),
        fee="0.06",
    )
    neighbours = lc.Neighbours(
        order_ids=frozenset({"o-b"}),
        client_order_ids=frozenset(),
        entry_client_order_ids=frozenset({OTHER_ENTRY}),
        intervals=((T_EXIT + timedelta(seconds=20), None),),
    )
    result = _settle([_entry(), *HELD_FUNDING, _exit(), b_entry], neighbours=neighbours)

    assert result.status == lc.STATUS_COMPLETE, result.error
    assert "b1" not in {leg.execution.exec_id for leg in result.legs}


def test_settlement_held_with_its_row_not_listed_yet_stays_pending() -> None:
    result = _settle([_entry(), HELD_FUNDING[0], _exit()])

    assert result.status == lc.STATUS_PENDING
    assert "09:00" in (result.error or "")


def test_funding_row_for_another_quantity_needs_an_operator() -> None:
    funding = [HELD_FUNDING[0], _funding("f9", qty="80", at=SETTLEMENTS[3], fee="0.01")]
    result = _settle([_entry(), *funding, _exit()])

    assert result.status == lc.STATUS_INCOMPLETE


def _short_hold(exit_at: datetime) -> list[lc.Execution]:
    return [_entry(at=exit_at - timedelta(minutes=30)), _exit(at=exit_at)]


def test_exit_3_s_after_a_settlement_without_a_row_is_never_a_zero() -> None:
    exit_at = SETTLEMENTS[3] + timedelta(seconds=3)
    result = _settle(_short_hold(exit_at), now=exit_at + timedelta(minutes=10))

    assert result.status == lc.STATUS_INCOMPLETE
    assert "no funding row" in (result.error or "")


def test_exit_3_s_after_a_settlement_with_the_quantity_held_is_counted() -> None:
    exit_at = SETTLEMENTS[3] + timedelta(seconds=3)
    row = _funding("f9", qty="100", at=SETTLEMENTS[3], fee="0.03")
    result = _settle([*_short_hold(exit_at), row], now=exit_at + timedelta(minutes=10))

    assert result.status == lc.STATUS_COMPLETE, result.error
    assert result.funding_usd == Decimal("0.03")


def test_exit_2_s_before_a_settlement_checks_that_settlement() -> None:
    """The colleague's case: exit at 07:59:58, settlement at 08:00:00. A list read
    only up to the exit would miss it and confirm a zero."""
    exit_at = datetime(2026, 11, 3, 7, 59, 58, tzinfo=UTC)
    result = _settle(_short_hold(exit_at), now=exit_at + timedelta(minutes=10))

    assert result.status == lc.STATUS_INCOMPLETE
    assert "08:00" in (result.error or "")


def test_exit_2_s_before_a_settlement_with_a_row_of_100_is_counted() -> None:
    exit_at = datetime(2026, 11, 3, 7, 59, 58, tzinfo=UTC)
    row = _funding("f8", qty="100", at=SETTLEMENTS[2], fee="0.02")
    result = _settle([*_short_hold(exit_at), row], now=exit_at + timedelta(minutes=10))

    assert result.status == lc.STATUS_COMPLETE, result.error
    assert result.funding_usd == Decimal("0.02")


def test_entry_3_s_after_a_settlement_with_a_row_is_a_boundary_too() -> None:
    entry_at = SETTLEMENTS[2] + timedelta(seconds=3)
    row = _funding("f8", qty="100", at=SETTLEMENTS[2], fee="0.02")
    result = _settle(
        [_entry(at=entry_at), row, _exit(at=entry_at + timedelta(minutes=20))],
        now=entry_at + timedelta(minutes=30),
    )

    assert result.status == lc.STATUS_COMPLETE, result.error
    assert result.funding_usd == Decimal("0.02")


def test_entry_filled_in_two_executions_around_a_settlement_counts_the_part_held() -> None:
    """Colleague review (P2): 50 filled a second before the settlement and 50 a
    second after it; Bybit charged the 50 held at the time. The window's ends alone
    (0 and 100) would reject a correct charge."""
    first = _entry("50", at=SETTLEMENTS[2] - timedelta(seconds=1), exec_id="e1")
    second = _entry("50", at=SETTLEMENTS[2] + timedelta(seconds=1), exec_id="e2")
    row = _funding("f8", qty="50", at=SETTLEMENTS[2], fee="0.01")
    exit_at = SETTLEMENTS[2] + timedelta(minutes=20)
    result = _settle([first, row, second, _exit(at=exit_at)], now=exit_at + timedelta(minutes=10))

    assert result.status == lc.STATUS_COMPLETE, result.error
    assert result.funding_usd == Decimal("0.01")


def test_a_quantity_never_held_around_a_settlement_still_needs_an_operator() -> None:
    first = _entry("50", at=SETTLEMENTS[2] - timedelta(seconds=1), exec_id="e1")
    second = _entry("50", at=SETTLEMENTS[2] + timedelta(seconds=1), exec_id="e2")
    row = _funding("f8", qty="70", at=SETTLEMENTS[2], fee="0.01")
    exit_at = SETTLEMENTS[2] + timedelta(minutes=20)
    result = _settle([first, row, second, _exit(at=exit_at)], now=exit_at + timedelta(minutes=10))

    assert result.status == lc.STATUS_INCOMPLETE
    assert "0, 50, 100" in (result.error or "")


def test_funding_in_another_currency_needs_an_operator() -> None:
    """Colleague review (P2): its amount would be subtracted from USD PnL."""
    funding = [HELD_FUNDING[0], replace(HELD_FUNDING[1], fee_currency="BTC")]
    result = _settle([_entry(), *funding, _exit()])

    assert result.status == lc.STATUS_INCOMPLETE
    assert "BTC" in (result.error or "")


def test_legacy_position_closed_2_s_before_and_new_opened_2_s_after_both_match() -> None:
    """The colleague's case: a row of 100 fits both; one proven owner is required."""
    entry_at = SETTLEMENTS[2] + timedelta(seconds=2)
    row = _funding("f8", qty="100", at=SETTLEMENTS[2], fee="0.02")
    legacy = lc.Neighbours(
        order_ids=frozenset(),
        client_order_ids=frozenset(),
        entry_client_order_ids=frozenset(),
        intervals=((SETTLEMENTS[1], SETTLEMENTS[2] - timedelta(seconds=2)),),
    )
    result = _settle(
        [_entry(at=entry_at), row, _exit(at=entry_at + timedelta(minutes=20))],
        neighbours=legacy,
        now=entry_at + timedelta(minutes=30),
    )

    assert result.status == lc.STATUS_INCOMPLETE
    assert "another trade of ours" in (result.error or "")


def test_unknown_fill_3_s_from_a_settlement_held_leaves_two_candidates() -> None:
    stranger = _row(
        "u1",
        order_id="o-unknown",
        side="Buy",
        qty="5",
        price="1.05",
        at=SETTLEMENTS[3] + timedelta(seconds=3),
        fee="0.01",
    )
    result = _settle(
        [_entry(), *HELD_FUNDING, _exit(), stranger],
        facts=_facts(),
        # The unknown fill is inside the holding period, which needs an operator
        # anyway; outside it the ownership rule alone must refuse.
        now=LATER,
    )
    assert result.status == lc.STATUS_INCOMPLETE

    # The trade exits 2 s before the settlement and its row matches, but an unknown
    # fill 3 s after it, outside the holding period, could own the row as well.
    exit_at = SETTLEMENTS[3] - timedelta(seconds=2)
    outside = replace(stranger, executed_at=SETTLEMENTS[3] + timedelta(seconds=3))
    row = _funding("f9", qty="100", at=SETTLEMENTS[3], fee="0.03")
    result = _settle(
        [_entry(at=exit_at - timedelta(minutes=30)), _exit(at=exit_at), row, outside],
        now=exit_at + timedelta(minutes=10),
    )
    assert result.status == lc.STATUS_INCOMPLETE
    assert "within 5 s of the settlement" in (result.error or "")


def test_no_settlement_held_is_a_confirmed_zero_after_5_minutes() -> None:
    exit_at = SETTLEMENTS[2] + timedelta(minutes=40)
    hold = [_entry(at=SETTLEMENTS[2] + timedelta(minutes=10)), _exit(at=exit_at)]

    early = _settle(hold, now=exit_at + timedelta(minutes=2))
    assert early.status == lc.STATUS_PENDING

    result = _settle(hold, now=exit_at + timedelta(minutes=5))
    assert result.status == lc.STATUS_COMPLETE, result.error
    assert result.funding_usd == Decimal(0)


def test_funding_row_matching_no_listed_settlement_needs_an_operator() -> None:
    result = _settle(
        [_entry(), *HELD_FUNDING, _exit()],
        settlements=[SETTLEMENTS[2]],
    )

    assert result.status == lc.STATUS_INCOMPLETE


def test_fee_in_another_currency_needs_an_operator() -> None:
    result = _settle([_entry(), *HELD_FUNDING, replace(_exit(), fee_currency="BTC")])

    assert result.status == lc.STATUS_INCOMPLETE
    assert "BTC" in (result.error or "")


def test_contract_size_other_than_1_needs_an_operator() -> None:
    result = _settle([_entry(), *HELD_FUNDING, _exit()], facts=_facts(contract_size=Decimal(10)))

    assert result.status == lc.STATUS_INCOMPLETE


def test_exit_on_the_entry_side_needs_an_operator() -> None:
    wrong = replace(_exit(), side="Buy")
    result = _settle([_entry(), *HELD_FUNDING, wrong])

    assert result.status == lc.STATUS_INCOMPLETE


def test_open_position_returns_entry_fees_and_one_settlement() -> None:
    now = SETTLEMENTS[2] + timedelta(minutes=20)
    stop = lc.OrderState("o-stop", terminal=False, filled=Decimal(0))
    result = _settle(
        [_entry(at=SETTLEMENTS[1] + timedelta(minutes=30)), HELD_FUNDING[0]],
        facts=_facts(order_ids=frozenset({"o-entry", "o-stop"}), exit_at=None),
        orders={**_orders(("o-entry", "100")), "o-stop": stop},
        now=now,
        open_position=True,
    )

    assert result.status == lc.STATUS_COMPLETE, result.error
    assert result.fees_usd == Decimal("0.055")
    assert result.funding_usd == Decimal("0.05")
    assert result.gross_pnl_usd is None


def test_open_position_with_a_settlement_not_listed_yet_is_not_complete() -> None:
    now = SETTLEMENTS[2] + timedelta(minutes=1)
    result = _settle(
        [_entry(at=SETTLEMENTS[1] + timedelta(minutes=30))],
        facts=_facts(order_ids=frozenset({"o-entry"}), exit_at=None),
        orders=_orders(("o-entry", "100")),
        now=now,
        open_position=True,
    )

    assert result.status == lc.STATUS_PENDING


# --- Bybit reads -------------------------------------------------------------------


def _raw(exec_id: str, at: datetime, **fields: str) -> dict[str, str]:
    return {
        "execId": exec_id,
        "execType": "Trade",
        "orderId": "o-1",
        "orderLinkId": ENTRY,
        "side": "Buy",
        "execQty": "1",
        "execPrice": "1.0",
        "execValue": "1.0",
        "execFee": "0.001",
        "feeCurrency": "",
        "execTime": str(int(at.timestamp() * 1000)),
        **fields,
    }


def _ok(rows: list[dict[str, str]], cursor: str = "") -> dict[str, Any]:
    return {"retCode": 0, "result": {"list": rows, "nextPageCursor": cursor}}


async def test_holding_longer_than_7_days_is_read_in_7_day_requests_every_row_once() -> None:
    start = datetime(2026, 10, 1, tzinfo=UTC)
    end = start + timedelta(days=16)
    calls: list[dict[str, Any]] = []

    async def execution_list(params: dict[str, Any]) -> dict[str, Any]:
        calls.append(dict(params))
        chunk = datetime.fromtimestamp(params["startTime"] / 1000, tz=UTC)
        if params.get("cursor") is None:
            # The same row again on the next page and in the next chunk: once only.
            return _ok([_raw(f"r-{chunk:%d}", chunk), _raw("dup", start)], cursor="p2")
        return _ok([_raw("dup", start)])

    exchange = type("Ex", (), {})()
    exchange.privateGetV5ExecutionList = execution_list
    rows = await lc.BybitCostSource(exchange).executions("BTCUSDT", start, end)

    windows = [(c["startTime"], c["endTime"]) for c in calls if "cursor" not in c]
    assert len(windows) == 3
    assert all(e - s <= 7 * 86_400_000 for s, e in windows)
    assert windows[-1][1] == int(end.timestamp() * 1000)
    assert sorted(row.exec_id for row in rows) == ["dup", "r-01", "r-08", "r-15"]
    assert rows[0].fee_currency is None


async def test_a_malformed_execution_row_fails_the_read() -> None:
    exchange = type("Ex", (), {})()
    exchange.privateGetV5ExecutionList = AsyncMock(
        return_value=_ok([_raw("bad", T_ENTRY, execQty="")])
    )
    with pytest.raises(ValueError, match="execQty"):
        await lc.BybitCostSource(exchange).executions("BTCUSDT", T_ENTRY, T_EXIT)


async def test_an_exchange_error_fails_the_read() -> None:
    exchange = type("Ex", (), {})()
    exchange.privateGetV5ExecutionList = AsyncMock(return_value={"retCode": 10002})
    with pytest.raises(RuntimeError):
        await lc.BybitCostSource(exchange).executions("BTCUSDT", T_ENTRY, T_EXIT)


async def test_settlements_page_back_from_the_end() -> None:
    start = datetime(2026, 10, 1, tzinfo=UTC)
    hours = [start + timedelta(hours=h) for h in range(250)]

    async def funding_history(params: dict[str, Any]) -> dict[str, Any]:
        page_end = datetime.fromtimestamp(params["endTime"] / 1000, tz=UTC)
        page = [at for at in reversed(hours) if at <= page_end][: params["limit"]]
        return _ok([{"fundingRateTimestamp": str(int(at.timestamp() * 1000))} for at in page])

    exchange = type("Ex", (), {})()
    exchange.publicGetV5MarketFundingHistory = funding_history
    found = await lc.BybitCostSource(exchange).settlements("BTCUSDT", start, hours[-1])

    assert found == hours


async def test_order_state_from_history_then_open_orders() -> None:
    exchange = type("Ex", (), {})()
    exchange.privateGetV5OrderHistory = AsyncMock(return_value=_ok([]))
    exchange.privateGetV5OrderRealtime = AsyncMock(
        return_value=_ok([{"orderId": "o-1", "orderStatus": "New", "cumExecQty": "0"}])
    )
    state = await lc.BybitCostSource(exchange).order("BTCUSDT", "o-1")
    assert state == lc.OrderState("o-1", terminal=False, filled=Decimal(0))

    exchange.privateGetV5OrderHistory = AsyncMock(
        return_value=_ok([{"orderId": "o-1", "orderStatus": "Filled", "cumExecQty": "3"}])
    )
    state = await lc.BybitCostSource(exchange).order("BTCUSDT", "o-1")
    assert state == lc.OrderState("o-1", terminal=True, filled=Decimal(3))

    exchange.privateGetV5OrderHistory = AsyncMock(return_value=_ok([]))
    exchange.privateGetV5OrderRealtime = AsyncMock(return_value=_ok([]))
    assert await lc.BybitCostSource(exchange).order("BTCUSDT", "o-1") is None
