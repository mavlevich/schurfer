from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.source_lead_bybit_canary import (
    BookSample,
    InstrumentSpec,
    binance_instruments,
    bybit_instruments,
    evaluate_book,
    min_order_check,
    resolve_instruments,
    summarize_samples,
)

_T0 = datetime(2026, 9, 25, 12, tzinfo=UTC)


def _bybit_item(symbol: str, base: str, **overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "symbol": symbol,
        "baseCoin": base,
        "quoteCoin": "USDT",
        "settleCoin": "USDT",
        "status": "Trading",
        "contractType": "LinearPerpetual",
        "lotSizeFilter": {"minOrderQty": "1", "qtyStep": "1", "minNotionalValue": "5"},
    }
    item.update(overrides)
    return item


def _binance_item(symbol: str, base: str, **overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "symbol": symbol,
        "baseAsset": base,
        "quoteAsset": "USDT",
        "marginAsset": "USDT",
        "status": "TRADING",
        "contractType": "PERPETUAL",
        "filters": [
            {"filterType": "LOT_SIZE", "minQty": "1", "stepSize": "1"},
            {"filterType": "MIN_NOTIONAL", "notional": "5"},
        ],
    }
    item.update(overrides)
    return item


def test_catalogs_keep_only_trading_usdt_linear_perpetuals() -> None:
    bybit = bybit_instruments(
        [
            _bybit_item("ABCUSDT", "ABC"),
            _bybit_item("DEFUSDT", "DEF", status="PreLaunch"),
            _bybit_item("GHIPERP", "GHI", settleCoin="USDC"),
            _bybit_item("JKLUSDT-26DEC", "JKL", contractType="LinearFutures"),
        ]
    )
    assert set(bybit) == {"ABC"}
    assert bybit["ABC"][0].min_notional_usd == 5.0
    binance = binance_instruments(
        [_binance_item("ABCUSDT", "ABC"), _binance_item("XYZUSDT", "XYZ", status="SETTLING")]
    )
    assert set(binance) == {"ABC"}
    assert binance["ABC"][0].min_order_qty == 1.0


def test_every_venue_with_one_exact_instrument_is_sampled() -> None:
    """Review P1: a Bybit-only asset is still checked; pairs are not required."""
    catalogs = {
        "bybit": bybit_instruments(
            [
                _bybit_item("ABCUSDT", "ABC"),
                _bybit_item("ONLYUSDT", "ONLY"),
                _bybit_item("1000PEPEUSDT", "1000PEPE"),
            ]
        ),
        "binance": binance_instruments(
            [_binance_item("ABCUSDT", "ABC"), _binance_item("1000PEPEUSDT", "1000PEPE")]
        ),
    }
    resolved, skipped = resolve_instruments(["ABC", "ONLY", "PEPE"], catalogs)
    assert set(resolved["ABC"]) == {"bybit", "binance"}
    assert set(resolved["ONLY"]) == {"bybit"}
    assert skipped["ONLY"] == {"binance": "0 exact instruments"}
    # A renamed 1000x contract is never matched to the plain base.
    assert "PEPE" not in resolved
    assert set(skipped["PEPE"]) == {"bybit", "binance"}


_SPEC = InstrumentSpec("bybit", "ABC", "ABCUSDT", 1.0, 1.0, 1.0, 5.0)


def _sample(
    bids: object,
    asks: object,
    *,
    spec: InstrumentSpec = _SPEC,
    ts: int | None = None,
) -> BookSample:
    return evaluate_book(
        spec,
        bids=bids,
        asks=asks,
        book_timestamp_ms=ts,
        sequence="u=1",
        round_index=0,
        requested_at=_T0,
        received_at=_T0 + timedelta(milliseconds=300),
        target_usd=50.0,
        max_book_age_ms=2000,
    )


def test_a_fresh_deep_book_meeting_the_minimum_is_executable() -> None:
    ts = round(_T0.timestamp() * 1000) + 100
    sample = _sample([["1.99", "1000"]], [["2.01", "1000"]], ts=ts)
    assert sample.book_ok
    assert sample.fresh is True
    assert sample.min_order_ok is True
    assert sample.executable
    assert sample.latency_ms == 300
    assert sample.book_age_ms == 200
    assert sample.order_qty_at_target == 24.0  # $50 / 2.01 rounded down to a step of 1


def test_a_stale_or_untimestamped_book_is_not_executable_even_if_deep() -> None:
    """Review note: `executable` needs freshness; `book_ok` alone is structure and depth."""
    ts = round(_T0.timestamp() * 1000)
    stale = _sample([["1.99", "1000"]], [["2.01", "1000"]], ts=ts - 5000)
    assert stale.book_ok and stale.fresh is False and not stale.executable
    untimed = _sample([["1.99", "1000"]], [["2.01", "1000"]])
    assert untimed.book_ok and untimed.fresh is None and not untimed.executable


@pytest.mark.parametrize(
    ("bids", "asks", "failure"),
    [
        ([], [["2.01", "1000"]], "empty_side"),
        ([["2.05", "1000"]], [["2.01", "1000"]], "crossed_book"),
        ([["1.99", "1"]], [["2.01", "1"]], "insufficient_depth"),
    ],
)
def test_book_failures_are_recorded_with_a_reason(bids: object, asks: object, failure: str) -> None:
    sample = _sample(bids, asks)
    assert not sample.book_ok
    assert sample.book_failure == failure
    assert not sample.executable


def test_minimum_order_uses_the_quantity_after_rounding_to_the_step() -> None:
    """Review P2: $50 at 2.01 is 24.87 raw, 20 after a step of 10 -> $40.2 < $45."""
    spec = InstrumentSpec("bybit", "ABC", "ABCUSDT", 1.0, 1.0, 10.0, 45.0)
    ok, qty = min_order_check(spec, ask_vwap=2.01, target_usd=50.0)
    assert qty == 20.0
    assert ok is False
    below_qty = InstrumentSpec("binance", "BTC", "BTCUSDT", 1.0, 0.001, 0.001, 5.0)
    ok, qty = min_order_check(below_qty, ask_vwap=100_000.0, target_usd=50.0)
    assert qty == 0.0
    assert ok is False


@pytest.mark.parametrize(
    "spec",
    [
        InstrumentSpec("bybit", "ABC", "ABCUSDT", 1.0, None, 1.0, 5.0),
        InstrumentSpec("bybit", "ABC", "ABCUSDT", 1.0, 1.0, None, 5.0),
        InstrumentSpec("bybit", "ABC", "ABCUSDT", 1.0, 1.0, 1.0, None),
    ],
)
def test_unknown_order_limits_are_unknown_not_a_pass(spec: InstrumentSpec) -> None:
    """Review P2: missing limits used to read as min_order_ok=True."""
    ts = round(_T0.timestamp() * 1000)
    sample = _sample([["1.99", "1000"]], [["2.01", "1000"]], spec=spec, ts=ts)
    assert sample.min_order_ok is None
    assert not sample.executable


def test_an_unknown_contract_size_is_refused_not_defaulted() -> None:
    unknown = InstrumentSpec("bybit", "ABC", "ABCUSDT", None, 1.0, 1.0, 5.0)
    sample = _sample([["1.99", "1000"]], [["2.01", "1000"]], spec=unknown)
    assert sample.book_failure == "contract_size_unknown"


def test_summary_separates_book_freshness_and_order_outcomes() -> None:
    ts = round(_T0.timestamp() * 1000)
    fresh = _sample([["1.99", "1000"]], [["2.01", "1000"]], ts=ts + 250)
    stale = _sample([["1.99", "1000"]], [["2.01", "1000"]], ts=ts - 5000)
    broken = _sample([], [["2.01", "1000"]])
    summary = summarize_samples([fresh, stale, broken], [120, -300])
    bybit = summary["bybit"]
    assert bybit["samples"] == 3
    assert bybit["book_ok"] == 2
    assert bybit["book_failures"] == {"empty_side": 1}
    assert bybit["fresh"] == {"true": 1, "false": 1, "no_timestamp": 1}
    assert bybit["executable"] == 1
    assert bybit["min_order"]["ok"] == 2
    assert summary["cross_venue_book_ts_diff_ms"]["max"] == 300.0
    assert summary["binance"]["samples"] == 0
