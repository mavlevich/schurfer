from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from schurfer_analytics.bybit_carry_feasibility import (
    RUN_AFTER,
    Pair,
    _book,
    evaluate_pair,
    feasibility_gate,
    fetch_catalogs,
    main,
    publish_once,
    require_run_window,
    run_canary,
    sample_pairs,
    select_pairs,
)
from schurfer_analytics.exchange_registry import EXCHANGE_FACTORIES

if TYPE_CHECKING:
    from pathlib import Path


def _spot(base: str = "ABC", **changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "baseCoin": base,
        "symbol": f"{base}USDT",
        "quoteCoin": "USDT",
        "status": "Trading",
        "stTag": "0",
        "lotSizeFilter": {
            "basePrecision": "0.1",
            "minOrderAmt": "5",
            "minOrderQty": "0.1",
            "maxMarketOrderQty": "1000",
        },
    }
    row.update(changes)
    return row


def _perp(base: str = "ABC", **changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "baseCoin": base,
        "symbol": f"{base}USDT",
        "quoteCoin": "USDT",
        "settleCoin": "USDT",
        "status": "Trading",
        "contractType": "LinearPerpetual",
        "leverageFilter": {"minLeverage": "1", "maxLeverage": "50"},
        "lotSizeFilter": {
            "qtyStep": "0.1",
            "minOrderQty": "0.1",
            "minNotionalValue": "5",
            "maxMktOrderQty": "1000",
        },
    }
    row.update(changes)
    return row


def _response(symbol: str, *, bid: str, ask: str, ts: int) -> dict[str, object]:
    return {
        "retCode": 0,
        "result": {
            "s": symbol,
            "ts": ts,
            "b": [[bid, "1000"]],
            "a": [[ask, "1000"]],
            "u": 1,
            "seq": 1,
        },
    }


def _pair() -> Pair:
    pairs, excluded = select_pairs([_spot()], [_perp()])
    assert not excluded
    return pairs[0]


def test_catalog_requires_exact_unambiguous_spot_perp_and_known_limits() -> None:
    spots = [
        _spot(),
        _spot("NO_PERP"),
        _spot("STOCK", xstockMultiplier="100"),
        _spot("DUP"),
        _spot("DUP"),
        _spot("NO_LIMIT", lotSizeFilter={"basePrecision": "0.1"}),
        _spot("1000PEPE"),
    ]
    perps = [
        _perp(),
        _perp("STOCK"),
        _perp("DUP"),
        _perp("NO_LIMIT"),
        _perp("PEPE"),
        _perp("PRE", status="PreLaunch"),
        _perp("FUT", contractType="LinearFutures"),
    ]
    pairs, excluded = select_pairs(spots, perps)
    assert [p.base for p in pairs] == ["ABC"]
    assert excluded["STOCK"] == "spot_special_product"
    assert excluded["DUP"] == "catalog_count:spot=2,linear=1"
    assert excluded["NO_LIMIT"] == "order_limit_unknown"
    assert excluded["PEPE"] == "catalog_count:spot=0,linear=1"
    assert "PRE" not in excluded and "FUT" not in excluded


def test_catalog_rejects_missing_one_x_and_does_not_invent_contract_size() -> None:
    perps = [
        _perp("HI", leverageFilter={"minLeverage": "2", "maxLeverage": "50"}),
        _perp("UNKNOWN", leverageFilter={}),
    ]
    pairs, excluded = select_pairs([_spot("HI"), _spot("UNKNOWN")], perps)
    assert not pairs
    assert excluded == {"HI": "one_x_unavailable", "UNKNOWN": "leverage_limit_unknown"}


def test_sampling_is_deterministic_and_bounded() -> None:
    pairs = [_pair()]
    for index in range(60):
        pairs.append(Pair(**{**vars(pairs[0]), "base": f"B{index}", "spot_id": f"B{index}USDT"}))
    forward = sample_pairs(pairs)
    backward = sample_pairs(list(reversed(pairs)))
    assert len(forward) == 50
    assert [p.spot_id for p in forward] == [p.spot_id for p in backward]


def test_gate_requires_ten_distinct_pairs_passing_twice() -> None:
    rows = [
        {"base": f"B{base}", "round": round_index, "status": "book_and_limits_pass"}
        for base in range(9)
        for round_index in (0, 1)
    ]
    rows.extend(
        {"base": "ONLY_ONCE", "round": round_index, "status": "book_and_limits_pass"}
        for round_index in (0,)
    )
    assert feasibility_gate(rows)["decision"] == "stop_bybit_50_usd_carry_feasibility"
    rows.extend(
        {"base": "B9", "round": round_index, "status": "book_and_limits_pass"}
        for round_index in (0, 1)
    )
    assert feasibility_gate(rows)["n_repeatably_book_feasible"] == 10
    assert feasibility_gate(rows)["decision"] == "funding_capture_design_permitted"


def test_four_side_cost_and_descriptive_stress_use_same_rounded_quantity() -> None:
    ts = 1_000_000
    row = evaluate_pair(
        _pair(),
        _response("ABCUSDT", bid="1.99", ask="2.01", ts=ts),
        _response("ABCUSDT", bid="2.00", ask="2.02", ts=ts + 100),
        spot_received_ms=ts + 300,
        perp_received_ms=ts + 350,
    )
    assert row["status"] == "book_and_limits_pass"
    assert Decimal(row["quantity"]) == Decimal("24.7")
    assert Decimal(row["spot_buy_usd"]) <= 50
    assert Decimal(row["perp_cover_usd"]) <= 50
    assert Decimal(row["illustrative_stress_cash_usd"]) < 300
    assert row["margin_safety"] == "unverified"
    assert Decimal(row["four_trade_friction_usd"]) > 1


def test_deprecated_spot_min_qty_is_recorded_but_not_an_order_gate() -> None:
    spot = _spot(
        lotSizeFilter={
            "basePrecision": "0.1",
            "minOrderAmt": "5",
            "minOrderQty": "1000",
            "maxMarketOrderQty": "1000",
        }
    )
    pairs, excluded = select_pairs([spot], [_perp()])
    assert not excluded
    assert pairs[0].spot_min_qty_deprecated == Decimal("1000")
    ts = 1_000_000
    result = evaluate_pair(
        pairs[0],
        _response("ABCUSDT", bid="1.99", ask="2.01", ts=ts),
        _response("ABCUSDT", bid="2.00", ask="2.02", ts=ts),
        spot_received_ms=ts + 100,
        perp_received_ms=ts + 100,
    )
    assert result["status"] == "book_and_limits_pass"


def test_run_window_is_enforced_before_exchange_client_is_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from schurfer_analytics import bybit_carry_feasibility as module

    def no_exchange_client() -> None:
        pytest.fail("exchange client created before date gate")

    class BeforeGate:
        @staticmethod
        def now(_tz: object) -> datetime:
            return RUN_AFTER - timedelta(microseconds=1)

    monkeypatch.setitem(EXCHANGE_FACTORIES, "bybit", no_exchange_client)
    monkeypatch.setattr(module, "datetime", BeforeGate)
    monkeypatch.setattr(
        sys, "argv", ["bybit-carry-feasibility", "--output", str(tmp_path / "x.json")]
    )
    with pytest.raises(SystemExit, match="cannot run before"):
        main()
    assert not (tmp_path / "x.json").exists()
    with pytest.raises(SystemExit, match="cannot run before"):
        require_run_window(RUN_AFTER - timedelta(microseconds=1))
    require_run_window(RUN_AFTER)
    require_run_window(datetime(2026, 10, 31, 1, tzinfo=UTC))


@pytest.mark.parametrize(
    ("spot_ts", "perp_ts", "expected"),
    [
        (996_000, 1_000_000, "book_stale"),
        (1_000_000, 1_002_000, "book_time_skew"),
    ],
)
def test_stale_and_asynchronous_books_fail_closed(
    spot_ts: int, perp_ts: int, expected: str
) -> None:
    row = evaluate_pair(
        _pair(),
        _response("ABCUSDT", bid="1.99", ask="2.01", ts=spot_ts),
        _response("ABCUSDT", bid="2.00", ask="2.02", ts=perp_ts),
        spot_received_ms=1_000_300,
        perp_received_ms=perp_ts + 300,
    )
    assert row["status"] == expected


def test_minimum_and_depth_are_not_replaced_by_zero_cost() -> None:
    pair = _pair()
    restricted = Pair(**{**vars(pair), "perp_min_notional": Decimal("60")})
    ts = 1_000_000
    spot = _response("ABCUSDT", bid="1.99", ask="2.01", ts=ts)
    perp = _response("ABCUSDT", bid="2.00", ask="2.02", ts=ts)
    row = evaluate_pair(
        restricted, spot, perp, spot_received_ms=ts + 100, perp_received_ms=ts + 100
    )
    assert row["status"] == "minimum_order_failed"
    spot["result"]["b"] = [["1.99", "1"]]  # type: ignore[index]
    row = evaluate_pair(pair, spot, perp, spot_received_ms=ts + 100, perp_received_ms=ts + 100)
    assert row["status"] == "insufficient_four_side_depth"


def test_book_rejects_nan_and_wrong_symbol() -> None:
    ts = 1_000_000
    wrong = _response("OTHERUSDT", bid="1.99", ask="2.01", ts=ts)
    with pytest.raises(ValueError, match="book_symbol_mismatch"):
        _book(wrong, "ABCUSDT", ts + 100)
    bad = _response("ABCUSDT", bid="NaN", ask="2.01", ts=ts)
    with pytest.raises(ValueError):
        _book(bad, "ABCUSDT", ts + 100)


class _Client:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def public_get_v5_market_instruments_info(
        self, params: dict[str, object]
    ) -> dict[str, object]:
        self.calls.append(params)
        if params["category"] == "spot":
            return {"retCode": 0, "result": {"list": [_spot()]}}
        return {"retCode": 0, "result": {"list": [_perp()], "nextPageCursor": ""}}

    async def public_get_v5_market_orderbook(self, params: dict[str, object]) -> dict[str, object]:
        self.calls.append(params)
        import time

        ts = round(time.time() * 1000)
        if params["category"] == "spot":
            return _response("ABCUSDT", bid="1.99", ask="2.01", ts=ts)
        return _response("ABCUSDT", bid="2.00", ask="2.02", ts=ts)


def test_runner_uses_only_public_catalog_and_books() -> None:
    client = _Client()
    report = asyncio.run(run_canary(client, rounds=1, interval_seconds=0))
    assert report["coverage"]["catalog_pairs"] == 1
    assert report["coverage"]["sample_statuses"] == {"book_and_limits_pass": 1}
    assert len(client.calls) == 4
    assert set(call["category"] for call in client.calls) == {"spot", "linear"}
    assert report["native_catalogs"]["spot"][0]["baseCoin"] == "ABC"
    assert report["samples"][0]["native_books"]["spot"]["response"]["result"]["s"] == "ABCUSDT"


def test_cursor_repeat_aborts_before_a_partial_catalog_is_published() -> None:
    class Repeating(_Client):
        async def public_get_v5_market_instruments_info(
            self, params: dict[str, object]
        ) -> dict[str, object]:
            if params["category"] == "spot":
                return {"retCode": 0, "result": {"list": [_spot()]}}
            return {"retCode": 0, "result": {"list": [_perp()], "nextPageCursor": "again"}}

    with pytest.raises(RuntimeError, match="cursor repeated"):
        asyncio.run(fetch_catalogs(Repeating()))


def test_linear_catalog_consumes_every_page_without_spot_cursor() -> None:
    class Pages(_Client):
        async def public_get_v5_market_instruments_info(
            self, params: dict[str, object]
        ) -> dict[str, object]:
            self.calls.append(params)
            if params["category"] == "spot":
                return {"retCode": 0, "result": {"list": [_spot()]}}
            if "cursor" not in params:
                return {"retCode": 0, "result": {"list": [_perp()], "nextPageCursor": "page2"}}
            assert params["cursor"] == "page2"
            return {"retCode": 0, "result": {"list": [_perp("DEF")], "nextPageCursor": ""}}

    client = Pages()
    spot, linear = asyncio.run(fetch_catalogs(client))
    assert [row["baseCoin"] for row in spot] == ["ABC"]
    assert [row["baseCoin"] for row in linear] == ["ABC", "DEF"]
    assert "cursor" not in client.calls[0]


def test_output_is_write_once(tmp_path: Path) -> None:
    output = tmp_path / "result.json"
    digest = publish_once(output, {"a": 1})
    assert len(digest) == 64
    assert json.loads(output.read_text()) == {"a": 1}
    with pytest.raises(FileExistsError):
        publish_once(output, {"a": 2})
    assert json.loads(output.read_text()) == {"a": 1}
