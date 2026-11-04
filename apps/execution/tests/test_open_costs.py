"""Open loss retains verified fills while funding completeness remains unknown."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from schurfer_execution import live_costs as lc
from schurfer_execution import probe_loss

from .test_live_costs import (
    NO_NEIGHBOURS,
    SETTLEMENTS,
    T_ENTRY,
    T_EXIT,
    _entry,
    _exit,
    _facts,
    _funding,
    _orders,
    _row,
)

D = Decimal
NOW = T_EXIT + timedelta(minutes=1)
JOURNAL_QUANTITY = D(80)


def collect(*, rows=None, orders=None, facts=None, journal_quantity=JOURNAL_QUANTITY, **kwargs):
    return lc.collect_open(
        facts or _facts(exit_at=None),
        [_entry(), _exit("20")] if rows is None else rows,
        kwargs.pop("settlements", []),
        _orders(("o-entry", "100"), ("o-exit", "20")) if orders is None else orders,
        kwargs.pop("neighbours", NO_NEIGHBOURS),
        journal_quantity=journal_quantity,
        published_to=kwargs.pop("published_to", SETTLEMENTS[0]),
        observed=kwargs.pop("observed", ()),
        marks=kwargs.pop("marks", {}),
        now=NOW,
        **kwargs,
    )


def test_fills_after_published_funding_are_not_discarded():
    result = collect()
    assert result.status == lc.STATUS_UNPROVEN_TAIL
    assert result.position.open_qty == D(80)
    assert result.position.entry_vwap == D(1)
    assert result.position.realized_open_parts == D(2)
    assert result.position.fees_paid_open == D("0.1155")
    assert result.published_to == SETTLEMENTS[0]
    assert result.position.funding_paid_open == 0
    # The old open-position settlement waits for funding history past now.
    old = lc.settle(
        _facts(exit_at=None),
        [_entry(), _exit("20")],
        [],
        _orders(("o-entry", "100"), ("o-exit", "20")),
        NO_NEIGHBOURS,
        published_to=SETTLEMENTS[0],
        now=NOW,
        open_position=True,
    )
    assert old.status == lc.STATUS_PENDING
    assert old.fees_usd is None


def test_listed_and_unlisted_settlements_are_counted_once_at_historical_quantity():
    at8, at9 = SETTLEMENTS[2:4]
    result = collect(
        rows=[_entry(), _funding("f8", qty="100", at=at8, fee=".05"), _exit("20")],
        settlements=[lc.FundingSettlement(at8, D(".001"))],
        published_to=at8,
        observed=(lc.FundingSettlement(at8, D(".005")), lc.FundingSettlement(at9, D(".002"))),
        marks={at9: D("1.2")},
    )
    position = result.position
    assert result.status == lc.STATUS_UNPROVEN_TAIL, result.error
    assert position.funding_paid_open == D(".05")
    assert len(position.unlisted) == 1
    assert position.unlisted[0].quantity_held == 100  # before the later partial exit
    assert position.unlisted[0].estimate("long") == D(".2400")


@pytest.mark.parametrize("rate,mark", [(None, D(1)), (D(".001"), None), (None, None)])
def test_missing_estimate_input_stays_unknown(rate, mark):
    at = SETTLEMENTS[2]
    result = collect(observed=(lc.FundingSettlement(at, rate),), marks={at: mark})
    assert result.status == lc.STATUS_UNPROVEN_TAIL
    loss = probe_loss.probe_loss(D(0), result.position, mark=D(1), taker_rate=D(".001"))
    assert loss.unknown_settlements == (at,)
    assert loss.lower_bound
    assert loss.terms["funding_unlisted_estimate"] == 0


def test_unlisted_funding_received_is_not_income():
    at = SETTLEMENTS[2]
    result = collect(observed=(lc.FundingSettlement(at, D("-.001")),), marks={at: D(1)})
    assert result.position.unlisted[0].estimate("long") == 0
    assert result.position.unlisted[0].estimate("short") == D(".100")


def test_short_realized_partial_loss_and_rebate_have_the_right_sign():
    entry = replace(_entry(), side="Sell", fee=D("-.01"))
    exit = replace(_exit("20"), side="Buy", fee=D(".02"))
    result = collect(facts=_facts(exit_at=None, side="short"), rows=[entry, exit])
    assert result.position.realized_open_parts == -2
    assert result.position.fees_paid_open == D(".01")
    loss = probe_loss.probe_loss(D(0), result.position, mark=D(1), taker_rate=D(0))
    assert loss.loss == D("2.01")


def test_unobserved_shorter_funding_interval_is_never_invented_or_proven():
    result = collect(observed=(), published_to=SETTLEMENTS[2])
    # The snapshot must contain the newest published settlement too.
    assert result.status == lc.STATUS_PENDING
    result = collect(published_to=SETTLEMENTS[0])
    assert result.position.unlisted == ()
    assert probe_loss.probe_loss(D(0), result.position, mark=D(1), taker_rate=D(0)).lower_bound


@pytest.mark.parametrize("journal_quantity", [D(79), D(81), D(0)])
def test_quantity_mismatch_never_returns_figures(journal_quantity):
    result = collect(journal_quantity=journal_quantity)
    assert result.status == lc.STATUS_INCOMPLETE
    assert result.position is None


def test_active_partial_exit_waits_even_when_its_executions_are_listed():
    orders = _orders(("o-entry", "100"), ("o-exit", "20"))
    orders["o-exit"] = replace(orders["o-exit"], terminal=False)
    result = collect(orders=orders)
    assert result.status == lc.STATUS_PENDING
    assert result.position is None


def test_entry_rows_not_yet_complete_wait():
    result = collect(rows=[_entry("99"), _exit("20")])
    assert result.status == lc.STATUS_PENDING
    assert result.position is None


@pytest.mark.parametrize("problem", ["foreign", "fee", "price", "odd"])
def test_invalid_evidence_never_returns_partial_figures(problem):
    rows = [_entry(), _exit("20")]
    if problem == "foreign":
        rows.append(
            _row("foreign", order_id="foreign", side="Buy", qty="1", price="1", at=T_EXIT, fee="0")
        )
    elif problem == "fee":
        rows[0] = replace(rows[0], fee_currency="BTC")
    elif problem == "price":
        rows[0] = replace(rows[0], price=None)
    else:
        rows.append(replace(rows[1], exec_id="odd", exec_type="adl"))
    result = collect(rows=rows)
    assert result.status == lc.STATUS_INCOMPLETE
    assert result.position is None


def test_funding_at_entry_boundary_uses_largest_quantity_held():
    at = T_ENTRY + timedelta(seconds=2)
    result = collect(observed=(lc.FundingSettlement(at, D(".001")),), marks={at: D(1)})
    assert result.position.unlisted[0].quantity_held == 100


@pytest.mark.asyncio
async def test_bybit_settlement_mark_requires_both_completed_bars():
    at = SETTLEMENTS[2]

    def millis(moment):
        return int(moment.timestamp() * 1000)

    ex = SimpleNamespace(
        publicGetV5MarketMarkPriceKline=AsyncMock(
            return_value={
                "retCode": 0,
                "result": {
                    "list": [
                        [str(millis(at)), "1", "1.1", ".9", "1"],
                        [str(millis(at - timedelta(minutes=1))), "1", "1.2", ".9", "1"],
                    ]
                },
            }
        )
    )
    source = lc.BybitCostSource(ex)
    assert await source.settlement_mark("BTCUSDT", at, at + timedelta(seconds=30)) is None
    ex.publicGetV5MarketMarkPriceKline.assert_not_called()
    assert await source.settlement_mark("BTCUSDT", at, NOW) == D("1.2")
    ex.publicGetV5MarketMarkPriceKline.return_value["result"]["list"].pop()
    assert await source.settlement_mark("BTCUSDT", at, NOW) is None


@pytest.mark.asyncio
async def test_public_funding_reader_retains_rates_and_observed_time():
    at = SETTLEMENTS[2]
    ex = SimpleNamespace(
        publicGetV5MarketFundingHistory=AsyncMock(
            return_value={
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "fundingRateTimestamp": str(int(at.timestamp() * 1000)),
                            "fundingRate": "-.001",
                        }
                    ]
                },
            }
        ),
        publicGetV5MarketTickers=AsyncMock(
            return_value={
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "symbol": "BTCUSDT",
                            "nextFundingTime": str(int(at.timestamp() * 1000)),
                            "fundingRate": ".002",
                        }
                    ]
                },
            }
        ),
    )
    source = lc.BybitCostSource(ex)
    assert await source.funding_rates("BTCUSDT", T_ENTRY, NOW) == [
        lc.FundingSettlement(at, D("-.001"))
    ]
    assert await source.funding_observation("BTCUSDT") == lc.FundingSettlement(at, D(".002"))


def test_an_execution_timestamped_after_now_is_pending():
    result = collect(rows=[replace(_entry(), executed_at=NOW + timedelta(seconds=1)), _exit("20")])
    assert result.status == lc.STATUS_PENDING
    assert result.position is None


def test_publication_advancing_during_the_window_read_updates_its_proven_prefix():
    at = SETTLEMENTS[2]
    result = collect(settlements=[lc.FundingSettlement(at, D(".001"))])
    assert result.status == lc.STATUS_UNPROVEN_TAIL
    assert result.published_to == at  # the window itself proves newer publication
    assert result.position.unlisted[0].at == at
