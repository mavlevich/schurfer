from __future__ import annotations

import json
import zipfile
from typing import TYPE_CHECKING, Any

import pytest
from schurfer_analytics import bybit_burst_cost as cost
from schurfer_analytics import bybit_burst_decay as decay
from schurfer_analytics import bybit_burst_exit as ex
from schurfer_analytics.edge_loss_study import sha256_file

if TYPE_CHECKING:
    from pathlib import Path

BAR = 1_789_000_000  # a bar start (s); B = BAR + 60
B = BAR + 60.0
FIRING = {"symbol": "AAAUSDT", "bar_start": BAR}


def _trades(points: list[tuple[float, float]]) -> decay.Trades:
    return decay.Trades([(B + s, p, 100.0) for s, p in points])


def test_triggers_read_only_trades_after_the_entry_and_before_the_hold_ends() -> None:
    trades = _trades(
        [
            (5.0, 0.90),  # at the entry moment itself: not after it
            (10.0, 1.08),  # the high since entry
            (20.0, 1.0476),  # 3% off the high: T
            (30.0, 0.97),  # 3% under the entry price: S
            (40.0, 1.06),  # 5% over the entry price: P
            (3600.0, 0.50),  # at B + 60 min: outside the hold
        ]
    )
    got = ex.decisions(FIRING, trades, entry_price=1.0)
    assert got["T"] == B + 20.0
    assert got["S"] == B + 30.0
    assert got["P"] == B + 10.0
    assert got["H"] is None


def test_e_decides_on_the_last_trade_at_minute_15_against_the_price_paid() -> None:
    below = _trades([(10.0, 1.2), (899.0, 0.999)])
    above = _trades([(10.0, 0.9), (899.0, 1.001), (901.0, 0.5)])
    assert ex.decisions(FIRING, below, entry_price=1.0)["E"] == B + ex.CHECK_S
    assert ex.decisions(FIRING, above, entry_price=1.0)["E"] is None


def test_a_decided_exit_fills_after_the_delay_and_only_h_is_scheduled() -> None:
    decided = {"H": None, "S": B + 30.0, "T": None, "E": B + ex.CHECK_S, "P": None}
    delayed = ex.fill_moments(FIRING, decided, ex.EXIT_DELAY_S)
    zero = ex.fill_moments(FIRING, decided, 0.0)
    hold = ex.to_ms(B + ex.HOLD_S)
    assert delayed["E"] == (ex.to_ms(B + ex.CHECK_S + 5.0), "decided")
    assert zero["E"] == (ex.to_ms(B + ex.CHECK_S), "decided")
    assert delayed["S"] == (ex.to_ms(B + 35.0), "decided")
    assert delayed["H"] == zero["H"] == (hold, "time")
    assert delayed["T"] == (hold, "time")


def _ok(bid: float, ask: float) -> dict[str, Any]:
    return {"status": "ok", "age_ms": 10, "bids": [(bid, 1e9)], "asks": [(ask, 1e9)]}


ENTRY = {"status": "ok", "at": B + 5.0, "price": 1.0, "qty": 50.0, "notional": 50.0}


def test_settle_charges_fees_on_both_notionals_and_never_fills_at_a_trade_price() -> None:
    funding = {"status": "ok", "settlements": []}
    got = ex.settle(ENTRY, _ok(1.10, 1.11), ex.to_ms(B + 100), funding)
    fees_bps = cost.FEE_BPS * (1 + 1.10)
    assert got["net_bps"] == pytest.approx(1000 - fees_bps)
    stale = ex.settle(ENTRY, {"status": "book_stale"}, ex.to_ms(B + 100), funding)
    assert stale == {"status": "exit_unfilled", "why": "book_stale"}
    thin = ex.settle(ENTRY, {**_ok(1.1, 1.11), "bids": [(1.1, 1.0)]}, ex.to_ms(B + 100), funding)
    assert thin["why"] == "exit_depth_short"
    assert ex.settle(ENTRY, _ok(1.1, 1.11), ex.to_ms(B + 100), None)["status"] == (
        "funding_missing"
    )


def test_funding_runs_to_the_fill_not_to_the_hour() -> None:
    settle_ms = ex.to_ms(B + 1800)
    funding = {"status": "ok", "settlements": [[settle_ms, "0.001", "1.0"]]}
    early = ex.settle(ENTRY, _ok(1.0, 1.01), ex.to_ms(B + 100), funding)
    late = ex.settle(ENTRY, _ok(1.0, 1.01), ex.to_ms(B + 3600), funding)
    assert late["net_bps"] == pytest.approx(early["net_bps"] - 10.0)


def _row(i: int, nets: dict[str, float], *, start: float, ok: bool = True) -> dict[str, Any]:
    def cell(x: str) -> dict[str, Any]:
        if not ok and x == "S":
            return {"status": "exit_unfilled", "why": "book_stale", "reason": "decided"}
        return {"status": "ok", "net_bps": nets[x], "hold_s": 60.0, "reason": "time"}

    variant = {x: cell(x) for x in ex.EXITS}
    return {
        "symbol": f"S{i % 40}",
        "bar_start": start + (i % 20) * 86_400,
        "day": f"d{i % 20}",
        "entry": {"status": "ok"},
        "delayed": variant,
        "zero_delay": variant,
    }


CHOICE = ex.SPLIT.timestamp() - 30 * 86_400
TEST = ex.SPLIT.timestamp()


def test_the_choice_is_the_best_mean_on_the_choice_half_and_judged_on_the_test_half(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ex, "BOOTSTRAP_ITERATIONS", 200)
    nets = {"H": 10.0, "S": 5.0, "T": 50.0, "E": 0.0, "P": 1.0}
    rows = [_row(i, nets, start=CHOICE) for i in range(200)]
    rows += [_row(i, {**nets, "T": 30.0 + (i % 3)}, start=TEST) for i in range(200)]
    got = ex.summarize(rows, 1)
    assert got["chosen"] == "T"
    assert got["decision"] == "candidate_for_forward_cohort"
    assert got["test_verdict"]["paired_diff_vs_h_mean_bps"] == pytest.approx(21.0, abs=0.01)


def test_an_exit_cannot_win_by_losing_its_losers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A firing where S does not fill leaves the common set for every exit, and too
    small a common set chooses nothing."""
    monkeypatch.setattr(ex, "BOOTSTRAP_ITERATIONS", 200)
    nets = {"H": 10.0, "S": 5.0, "T": 1.0, "E": 0.0, "P": 1.0}
    rows = [_row(i, nets, start=CHOICE, ok=i % 2 == 0) for i in range(400)]
    got = ex.summarize(rows, 1)
    assert got["delayed"]["choice_half"]["common"] == 200
    assert got["delayed"]["choice_half"]["exits"]["S"]["own_status"] == {
        "ok": 200,
        "exit_unfilled:book_stale": 200,
    }
    assert got["decision"] == "insufficient_data_choice_half"  # 50% < 60%


def test_a_thin_test_half_decides_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ex, "BOOTSTRAP_ITERATIONS", 200)
    nets = {"H": 10.0, "S": 5.0, "T": 50.0, "E": 0.0, "P": 1.0}
    rows = [_row(i, nets, start=CHOICE) for i in range(200)]
    rows += [_row(i, nets, start=TEST) for i in range(149)]
    assert ex.summarize(rows, 1)["decision"] == "insufficient_data_test_half"


def _msg(ts: int, u: int, bid: str, ask: str) -> dict[str, Any]:
    return {
        "topic": "orderbook.200.AAAUSDT",
        "type": "snapshot",
        "ts": ts,
        "data": {"s": "AAAUSDT", "b": [[bid, "1e9"]], "a": [[ask, "1e9"]], "u": u, "seq": u},
    }


def test_e_fills_at_the_book_five_seconds_after_minute_15(tmp_path: Path) -> None:
    """End to end on one instrument: the E exit reads the book at B + 905 s, not 900."""
    snapshots = [
        (B + 4, "0.99", "1.00"),  # the entry book
        (B + 899, "0.95", "0.96"),
        (B + 903, "0.80", "0.81"),  # what an E fill 5 s later meets
        (B + 3599, "1.20", "1.21"),
    ]
    day = cost.day_of(ex.to_ms(B))
    path = tmp_path / f"{day}_AAAUSDT_ob200.data.zip"
    with zipfile.ZipFile(path, "w") as archive:
        lines = [_msg(ex.to_ms(t), i + 1, b, a) for i, (t, b, a) in enumerate(snapshots)]
        archive.writestr("book.data", "".join(json.dumps(m) + "\n" for m in lines))
    assert cost.day_of(ex.to_ms(B + 3600)) == day
    record = {"files": {path.name: {"status": "ok", "sha256": sha256_file(path)}}}
    trades = _trades([(5.0, 1.0), (600.0, 0.96), (899.0, 0.95)])
    funding = {"status": "ok", "settlements": []}
    (row,) = ex.measure_symbol([FIRING], trades, record, tmp_path, funding)
    fees = cost.FEE_BPS / 1e4
    assert row["delayed"]["E"]["net_bps"] == pytest.approx((0.80 - 1 - fees * 1.8) * 1e4)
    assert row["zero_delay"]["E"]["net_bps"] == pytest.approx((0.95 - 1 - fees * 1.95) * 1e4)
    assert row["delayed"]["H"]["net_bps"] == pytest.approx((1.20 - 1 - fees * 2.2) * 1e4)
    assert row["delayed"]["S"]["reason"] == "decided"  # 0.95 is 5% under the entry
