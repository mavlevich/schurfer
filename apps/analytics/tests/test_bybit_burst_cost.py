from __future__ import annotations

import json
import zipfile
from typing import TYPE_CHECKING, Any

import pytest
from schurfer_analytics import bybit_burst_cost as c
from schurfer_analytics.edge_loss_study import BLIND_END

if TYPE_CHECKING:
    from pathlib import Path

BAR = 1_789_000_000  # a bar start (s); B = BAR + 60
B_MS = (BAR + 60) * 1000


def _msg(
    kind: str, ts: int, u: int, bids: list[list[str]], asks: list[list[str]]
) -> dict[str, Any]:
    return {
        "topic": "orderbook.200.AAAUSDT",
        "type": kind,
        "ts": ts,
        "data": {"s": "AAAUSDT", "b": bids, "a": asks, "u": u, "seq": u},
    }


def _zip(path: Path, messages: list[dict[str, Any]]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("book.data", "".join(json.dumps(m) + "\n" for m in messages))
    return path


def test_the_book_at_a_moment_never_reads_ahead(tmp_path: Path) -> None:
    path = _zip(
        tmp_path / "b.zip",
        [
            _msg("snapshot", B_MS - 1000, 1, [["1.00", "100"]], [["1.02", "100"]]),
            _msg("delta", B_MS + 500, 2, [], [["1.02", "0"], ["1.10", "100"]]),  # after B
        ],
    )
    got = c.capture_file(path, [B_MS, B_MS + 600])
    assert got[B_MS]["asks"][0] == (1.02, 100.0)
    assert got[B_MS + 600]["asks"][0] == (1.10, 100.0)


def test_an_update_gap_breaks_the_book_until_the_next_snapshot(tmp_path: Path) -> None:
    path = _zip(
        tmp_path / "b.zip",
        [
            _msg("snapshot", B_MS - 3000, 1, [["1.00", "1"]], [["1.01", "1"]]),
            _msg("delta", B_MS - 2000, 5, [["1.00", "2"]], []),  # 2..4 missing
            _msg("snapshot", B_MS + 2000, 6, [["1.00", "1"]], [["1.01", "1"]]),
        ],
    )
    got = c.capture_file(path, [B_MS, B_MS + 2500])
    assert got[B_MS]["status"] == "book_broken"
    assert got[B_MS + 2500]["status"] == "ok"


def test_a_book_older_than_five_seconds_is_stale(tmp_path: Path) -> None:
    path = _zip(
        tmp_path / "b.zip", [_msg("snapshot", B_MS - 6000, 1, [["1.0", "1"]], [["1.1", "1"]])]
    )
    assert c.capture_file(path, [B_MS])[B_MS]["status"] == "book_stale"


def test_vwap_walks_levels_and_reports_thin_books() -> None:
    asks = [(1.0, 20.0), (2.0, 100.0)]
    got = c.vwap(asks, notional=50.0)  # 20 at 1.0, then 15 at 2.0
    assert got is not None
    price, qty = got
    assert qty == pytest.approx(35.0) and price == pytest.approx(50.0 / 35.0)
    assert c.vwap([(1.0, 1.0)], notional=50.0) is None
    bids = [(2.0, 10.0), (1.0, 100.0)]
    sold = c.vwap(bids, base=15.0)
    assert sold is not None
    price, qty = sold
    assert price == pytest.approx((20.0 + 5.0) / 15.0)


def test_funding_counts_only_settlements_inside_the_hold() -> None:
    settled = [
        [B_MS - 1, "0.01"],
        [B_MS + 10, "0.0001"],
        [B_MS + 3_600_000, "0.0002"],
        [B_MS + 3_600_001, "0.5"],
    ]
    assert c.funding_bps(settled, B_MS, B_MS + 3_600_000) == pytest.approx(3.0)


def _ok(bid: float, ask: float) -> dict[str, Any]:
    return {"status": "ok", "age_ms": 10, "bids": [(bid, 1e9)], "asks": [(ask, 1e9)]}


def test_measure_builds_costs_and_net_per_entry_delay() -> None:
    firing = {"symbol": "AAAUSDT", "bar_start": BAR}
    m = c.moments(firing)
    books = {m[f"entry_{d:g}"]: _ok(1.00, 1.02) for d in c.ENTRY_DELAYS_S}
    books[m["exit"]] = _ok(1.10, 1.12)
    got = c.measure(firing, books, [])
    cell = got["entries"]["5"]
    mid = 1.01
    assert cell["half_spread_bps"] == pytest.approx(0.01 / mid * 1e4)
    assert cell["gross_exec_bps"] == pytest.approx((1.10 / 1.02 - 1) * 1e4)
    assert cell["net_bps"] == pytest.approx(cell["gross_exec_bps"] - 11.0)
    assert cell["round_trip_cost_bps"] == pytest.approx(
        cell["entry_impact_bps"] + cell["exit_impact_bps"] + 11.0
    )
    del books[m["exit"]]
    assert c.measure(firing, books, [])["entries"]["5"]["status"] == "no_book"


def _row(symbol: str, day: str, net: float, cost: float) -> dict[str, Any]:
    entries = {
        f"{d:g}": {
            "status": "ok",
            "half_spread_bps": 1,
            "entry_impact_bps": 1,
            "exit_impact_bps": 1,
            "round_trip_cost_bps": cost,
            "gross_exec_bps": net + 11,
            "net_bps": net,
            "funding_bps": 0,
        }
        for d in c.ENTRY_DELAYS_S
    }
    return {"symbol": symbol, "day": day, "m": {"exit_status": "ok", "entries": entries}}


def test_the_decision_parks_on_high_median_cost_or_clearly_negative_net(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(c, "BOOTSTRAP_ITERATIONS", 200)
    expensive = [_row(f"S{i % 30}", f"d{i % 15}", 10.0, 60.0) for i in range(200)]
    assert c.summarize(expensive, 1, {})["decision"] == "park_hyp030"
    losing = [_row(f"S{i % 30}", f"d{i % 15}", -50.0 + (i % 5), 30.0) for i in range(200)]
    assert c.summarize(losing, 1, {})["decision"] == "park_hyp030"
    mixed = [
        _row(f"S{i % 30}", f"d{i % 15}", (100.0 if i % 2 else -80.0), 30.0) for i in range(200)
    ]
    assert c.summarize(mixed, 1, {})["decision"] == "no_decision"


def test_only_the_frozen_firing_list_is_accepted(tmp_path: Path) -> None:
    from schurfer_analytics.source_lead_multi_source_report import write_once

    write_once(tmp_path / "f.json", {"firings": []})
    with pytest.raises(SystemExit, match="frozen list"):
        c.load_firings(tmp_path / "f.json")


def test_fetch_refuses_days_on_or_after_the_blind_boundary(tmp_path: Path) -> None:
    late = int(BLIND_END.timestamp()) - 1800
    with pytest.raises(SystemExit, match="blind boundary"):
        c.fetch_books([{"symbol": "AAAUSDT", "bar_start": late}], tmp_path / "books")
    assert not (tmp_path / "books").exists()
