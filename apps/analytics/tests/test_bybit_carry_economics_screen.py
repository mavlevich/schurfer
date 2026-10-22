from __future__ import annotations

import asyncio
import gzip
import json
from collections import Counter
from typing import TYPE_CHECKING, Any

import pytest
from schurfer_analytics import bybit_carry_economics_screen as screen
from schurfer_analytics.bybit_carry_economics_screen import (
    BAR_MS,
    DATA_UNTIL_MS,
    DAY_MS,
    DECISIONS_FROM_MS,
    HOUR_MS,
    Series,
    Signal,
    Trade,
    money,
    reading,
    simulate_one_slot,
    simulate_pair,
)

if TYPE_CHECKING:
    from pathlib import Path

T0 = DECISIONS_FROM_MS


def flat_prices(value: float = 100.0) -> dict[int, float]:
    start = T0 - 2 * DAY_MS
    return {t: value for t in range(start, DATA_UNTIL_MS, BAR_MS)}


def funding_every(
    hours: int, rate_for: Any, *, start: int = T0 - 2 * DAY_MS
) -> list[tuple[int, float]]:
    return [(t, rate_for(t)) for t in range(start, DATA_UNTIL_MS, hours * HOUR_MS)]


def test_f24_sums_the_last_day_and_refuses_short_history_gaps_and_staleness() -> None:
    funding = [(T0 + i * 8 * HOUR_MS, 0.0001) for i in range(6)]
    signal = Signal(funding)
    assert signal.f24(T0 + 16 * HOUR_MS) is None  # less than 24 h of settlements
    assert signal.f24(T0 + 40 * HOUR_MS) == pytest.approx(3.0)  # three settlements in 24 h
    assert signal.f24(T0 + 40 * HOUR_MS + 9 * HOUR_MS) is None  # last one older than 8 h
    gapped = Signal(
        [
            (T0, 0.0001),
            (T0 + 8 * HOUR_MS, 0.0001),
            (T0 + 20 * HOUR_MS, 0.0001),
            (T0 + 28 * HOUR_MS, 0.0001),
            (T0 + 36 * HOUR_MS, 0.0001),
        ]
    )
    assert gapped.f24(T0 + 36 * HOUR_MS) is None  # 12 h gap inside the window is missing


def test_money_counts_basis_strictly_inner_settlements_and_four_fees() -> None:
    spot = flat_prices(100.0)
    perp = flat_prices(100.0)
    entry, exit_ = T0 + BAR_MS, T0 + 4 * BAR_MS
    perp[exit_ - BAR_MS] = 101.0  # the perp rises 1% by the exit fill
    funding = [(entry, 0.01), (entry + 4 * HOUR_MS, 0.001), (exit_, 0.01)]
    trade = money(Series(funding, spot, perp), "XUSDT", 5, entry, exit_, at_end=False)
    assert isinstance(trade, Trade)
    # only the inner settlement counts: q = 0.5, mark 100 -> 0.05 USD = 10 bps of 50
    assert trade.funding_bps == pytest.approx(10.0)
    assert trade.basis_bps == pytest.approx(-100.0)  # short perp loses the 1% basis widening
    assert trade.fee_bps == pytest.approx(10 * 0.5 * (100 + 100 + 100 + 101) / 50)  # 40.1
    assert trade.net_bps == pytest.approx(10.0 - 100.0 - trade.fee_bps)


def test_missing_fill_price_forms_no_trade() -> None:
    spot = flat_prices()
    del spot[T0]
    result = money(
        Series([], spot, flat_prices()), "XUSDT", 5, T0 + BAR_MS, T0 + 3 * BAR_MS, at_end=False
    )
    assert result == "fill_price_missing"


def test_pair_enters_one_bar_after_the_signal_and_exits_below_half_threshold() -> None:
    high_until = T0 + 3 * DAY_MS

    def rate(t: int) -> float:
        return 0.0005 if t < high_until else 0.00001  # 15 bps/day, then 0.3 bps/day

    series = Series(funding_every(8, rate), flat_prices(), flat_prices())
    path = screen.f24_path(series)
    missing: Counter[str] = Counter()
    trades = simulate_pair(series, "XUSDT", 10, path, missing)
    assert trades[0].entry_fill_ms == T0 + BAR_MS
    first_low = next(i for i, v in enumerate(path) if v is not None and v < 5)
    assert trades[0].exit_fill_ms == T0 + first_low * BAR_MS + BAR_MS
    assert not trades[0].closed_at_end
    assert len(trades) == 1 and not missing


def test_open_position_closes_at_the_last_bar_and_is_flagged() -> None:
    series = Series(funding_every(8, lambda t: 0.0005), flat_prices(), flat_prices())
    trades = simulate_pair(series, "XUSDT", 10, screen.f24_path(series), Counter())
    assert len(trades) == 1 and trades[0].closed_at_end
    assert trades[0].exit_fill_ms == DATA_UNTIL_MS + 1


def test_one_slot_takes_the_highest_signal_and_ties_by_symbol() -> None:
    def build(rate: float) -> Series:
        return Series(funding_every(8, lambda t: rate), flat_prices(), flat_prices())

    data = {"AUSDT": build(0.0005), "BUSDT": build(0.0009), "CUSDT": build(0.0009)}
    paths = {symbol: screen.f24_path(series) for symbol, series in data.items()}
    trades = simulate_one_slot(data, paths, 10, Counter())
    assert [trade.symbol for trade in trades] == ["BUSDT"]


def test_reading_requires_every_condition() -> None:
    good = {
        "one_slot": {
            "trades": 5,
            "net_usd": 3.0,
            "break_even_crossing_bps": 25.0,
            "positive_month_share": 0.7,
        },
        "all_pairs": {"top5_pair_share_of_net": 0.4},
    }
    assert reading({"10": good})["decision"] == "stage_b_worth_designing"
    concentrated = {**good, "all_pairs": {"top5_pair_share_of_net": 0.6}}
    thin = {**good, "one_slot": {**good["one_slot"], "break_even_crossing_bps": 19.0}}
    assert reading({"10": concentrated, "20": thin})["decision"] == (
        "carry_not_worth_stage_b_at_usd_50"
    )


def test_no_request_may_end_after_the_research_boundary() -> None:
    calls: list[Any] = []

    async def call(params: dict[str, Any]) -> Any:
        calls.append(params)
        return {"retCode": "0", "result": {"list": []}}

    with pytest.raises(SystemExit):
        asyncio.run(
            screen._pages(
                call,
                {"startTime": 0, "endTime": DATA_UNTIL_MS + 1},
                what="x",
                limit=200,
                oldest=lambda item: 0,
                record=lambda p, r: None,
            )
        )
    assert calls == []


class FakeClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def public_get_v5_market_instruments_info(self, params: dict[str, Any]) -> Any:
        common: dict[str, Any] = {
            "status": "Trading",
            "baseCoin": "X",
            "quoteCoin": "USDT",
            "symbol": "XUSDT",
        }
        if params["category"] == "spot":
            rows = [common | {"lotSizeFilter": {"basePrecision": "0.01", "minOrderAmt": "1"}}]
        else:
            rows = [
                common
                | {
                    "contractType": "LinearPerpetual",
                    "settleCoin": "USDT",
                    "lotSizeFilter": {
                        "qtyStep": "0.01",
                        "minOrderQty": "0.01",
                        "minNotionalValue": "5",
                    },
                    "leverageFilter": {"minLeverage": "1", "maxLeverage": "10"},
                }
            ]
        return {"retCode": "0", "result": {"list": rows, "nextPageCursor": ""}}

    async def public_get_v5_market_funding_history(self, params: dict[str, Any]) -> Any:
        self.requests.append(params)
        rows = [{"symbol": "XUSDT", "fundingRate": "0.0001", "fundingRateTimestamp": str(T0)}]
        return {"retCode": "0", "result": {"list": rows}}

    async def public_get_v5_market_kline(self, params: dict[str, Any]) -> Any:
        self.requests.append(params)
        rows = [[str(T0), "1", "1", "1", "100", "1", "1"]]
        return {"retCode": "0", "result": {"list": rows}}


def test_snapshot_round_trip_and_sha_check(tmp_path: Path) -> None:
    client = FakeClient()
    path = tmp_path / "snapshot.jsonl.gz"
    coverage = asyncio.run(screen.fetch_snapshot(client, path))
    assert coverage["catalog_pairs"] == 1 and not coverage["fetch_errors"]
    assert all(
        request.get("endTime", request.get("end")) == DATA_UNTIL_MS for request in client.requests
    )
    with gzip.open(path, "rb") as stream:
        kinds = [json.loads(line)["kind"] for line in stream]
    assert kinds == ["catalog", "catalog", "funding", "kline_spot", "kline_linear"]
    data, mismatched = screen.load_snapshot(path)
    assert data["XUSDT"].funding == [(T0, 0.0001)] and data["XUSDT"].spot == {T0: 100.0}
    assert not mismatched
    with pytest.raises(SystemExit):
        screen.build_report(path, "0" * 64, code_revision="abc", working_tree_dirty=False)
    report = screen.build_report(
        path, screen.file_sha256(path), code_revision="abc", working_tree_dirty=False
    )
    assert report["run"] == {"code_revision": "abc", "working_tree_dirty": False}
    assert report["reading"]["decision"] == "carry_not_worth_stage_b_at_usd_50"


def test_report_requires_the_dirty_flag(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        screen.main(
            [
                "report",
                "--snapshot",
                str(tmp_path / "s"),
                "--snapshot-sha256",
                "0" * 64,
                "--output-dir",
                str(tmp_path),
                "--code-revision",
                "abc",
            ]
        )


def test_a_pair_whose_fetch_fails_is_dropped_whole(tmp_path: Path) -> None:
    class Failing(FakeClient):
        async def public_get_v5_market_kline(self, params: dict[str, Any]) -> Any:
            if params["category"] == "linear":
                return {"retCode": "10001", "retMsg": "boom"}
            return await super().public_get_v5_market_kline(params)

    path = tmp_path / "snapshot.jsonl.gz"
    coverage = asyncio.run(screen.fetch_snapshot(Failing(), path))
    assert "XUSDT" in coverage["fetch_errors"]
    _, failed = screen.load_snapshot(path)
    assert failed == ["XUSDT"]
    report = screen.build_report(
        path, screen.file_sha256(path), code_revision="abc", working_tree_dirty=False
    )
    assert report["coverage"]["pairs"] == 0
