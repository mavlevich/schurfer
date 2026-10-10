from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest
from schurfer_analytics import bybit_order_rules as rules


def _item(symbol: str = "ABCUSDT", **lot: str) -> dict[str, Any]:
    filters = {"qtyStep": "0.1", "minOrderQty": "0.1", "maxMktOrderQty": "1000",
               "minNotionalValue": "5"}  # fmt: skip
    filters.update(lot)
    return {
        "symbol": symbol,
        "status": "Trading",
        "lotSizeFilter": filters,
        "priceFilter": {"tickSize": "0.001"},
    }


def _rule(**lot: str) -> rules.OrderRule:
    return rules.parse_rule(_item(**lot))


def test_a_usd_50_order_rounds_down_to_the_step() -> None:
    sized = rules.size_order(_rule(), price=Decimal("3.7"))
    assert sized.feasible and sized.reason is None
    assert sized.qty == Decimal("13.5")  # 50 / 3.7 = 13.51..., floored to 0.1
    assert sized.notional_usd == Decimal("49.95")


def test_an_order_below_the_minimum_quantity_is_refused() -> None:
    sized = rules.size_order(_rule(qtyStep="1", minOrderQty="1"), price=Decimal("60"))
    assert (sized.feasible, sized.reason, sized.qty) == (False, "below_min_qty", Decimal(0))


def test_an_order_below_the_minimum_value_is_refused() -> None:
    sized = rules.size_order(_rule(minNotionalValue="100"), price=Decimal("2"))
    assert (sized.feasible, sized.reason) == (False, "below_min_notional")


def test_an_order_above_the_market_maximum_is_refused() -> None:
    sized = rules.size_order(_rule(maxMktOrderQty="10"), price=Decimal("0.5"))
    assert (sized.feasible, sized.reason) == (False, "above_max_market_qty")


@pytest.mark.parametrize(
    ("rule", "reason"),
    [
        (None, "rules_missing"),
        (rules.parse_rule({**_item(), "status": "Settling"}), "not_trading"),
        (rules.parse_rule({**_item(), "lotSizeFilter": {"minOrderQty": "1"}}), "rules_incomplete"),
        (_rule(qtyStep="0"), "rules_incomplete"),
        (_rule(minNotionalValue="n/a"), "rules_incomplete"),
    ],
)
def test_whatever_cannot_be_proven_is_refused(rule: rules.OrderRule | None, reason: str) -> None:
    sized = rules.size_order(rule, price=Decimal("1"))
    assert (sized.feasible, sized.reason) == (False, reason)


def test_an_invalid_price_is_refused() -> None:
    assert rules.size_order(_rule(), price=Decimal(0)).reason == "price_invalid"
    assert rules.size_order(_rule(), price=Decimal("NaN")).reason == "price_invalid"


def test_the_rule_key_changes_with_any_field() -> None:
    base = rules.rule_sha256(_item())
    assert rules.rule_sha256(dict(reversed(list(_item().items())))) == base
    assert rules.rule_sha256(_item(qtyStep="0.01")) != base


class _Pages:
    def __init__(self, pages: list[Any]) -> None:
        self.pages = pages
        self.params: list[dict[str, Any]] = []

    async def public_get_v5_market_instruments_info(self, params: dict[str, Any]) -> Any:
        self.params.append(dict(params))
        return self.pages[len(self.params) - 1]


def _page(items: list[dict[str, Any]], cursor: str = "") -> dict[str, Any]:
    return {"retCode": 0, "result": {"list": items, "nextPageCursor": cursor}}


def test_every_page_is_read_until_the_cursor_ends() -> None:
    client = _Pages([_page([_item("A")], "c1"), _page([_item("B")], "c2"), _page([_item("C")])])
    items, requests = asyncio.run(rules.fetch_linear(client))
    assert [i["symbol"] for i in items] == ["A", "B", "C"]
    assert requests == 3
    assert [p.get("cursor") for p in client.params] == [None, "c1", "c2"]
    assert all(p["category"] == "linear" for p in client.params)


@pytest.mark.parametrize(
    ("pages", "message"),
    [
        ([{"retCode": 10001, "retMsg": "bad"}], "error"),
        ([{"retCode": 0, "result": {}}], "malformed"),
        ([_page([{"status": "Trading"}])], "malformed"),
        ([_page([_item("A")], "c1"), _page([_item("B")], "c1")], "cursor repeated"),
        ([_page([_item("A")], "c1"), _page([_item("A")])], "twice"),
    ],
)
def test_an_incomplete_answer_fails_the_pass(pages: list[Any], message: str) -> None:
    with pytest.raises(rules.CaptureError, match=message):
        asyncio.run(rules.fetch_linear(_Pages(pages)))


def test_a_cursor_that_never_ends_fails_the_pass() -> None:
    pages = [_page([_item(f"S{i}")], f"c{i}") for i in range(rules.MAX_PAGES + 1)]
    with pytest.raises(rules.CaptureError, match="did not end"):
        asyncio.run(rules.fetch_linear(_Pages(pages)))
