from __future__ import annotations

import gzip
import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import duckdb
import pytest
from schurfer_analytics import bybit_burst_decay as d
from schurfer_analytics.source_lead_multi_source_report import write_once

if TYPE_CHECKING:
    from pathlib import Path

BAR = int(datetime(2026, 9, 10, 12, 0, tzinfo=UTC).timestamp())  # burst bar start
B = BAR + 60
HEADER = (
    "timestamp,symbol,side,size,price,tickDirection,trdMatchID,"
    "grossValue,homeNotional,foreignNotional,RPI\n"
)


def _csv(trades: list[tuple[float, float, float]]) -> bytes:
    lines = "".join(f"{t:.4f},AAAUSDT,Buy,1,{p},PlusTick,x,0,1,{n},0\n" for t, p, n in trades)
    return gzip.compress((HEADER + lines).encode())


def _burst_trades() -> list[tuple[float, float, float]]:
    """Flat at 1.00 before the burst minute; inside it the price climbs to 1.06 with heavy
    turnover; after B it keeps climbing for 10 s to 1.10, then holds to the exit."""
    trades: list[tuple[float, float, float]] = [
        (float(BAR - 120 + i), 1.0, 100.0) for i in range(0, 60, 5)
    ]
    trades += [(BAR + i, 1.0 + 0.001 * i, 1_000.0) for i in range(0, 60)]  # 1.00 .. 1.059
    trades += [(B + 0.1 + i, 1.06 + 0.004 * i, 500.0) for i in range(0, 11)]  # 1.06 .. 1.10
    trades += [(B + 12 + i, 1.10, 50.0) for i in range(0, 3700, 10)]
    return trades


def _firing(**over: Any) -> dict[str, Any]:
    return {
        "symbol": "AAAUSDT",
        "bar_start": BAR,
        "prev_close": 1.0,
        "median_turnover_usd": 1_000.0,
        "t1_open": 1.06,
        **over,
    }


def test_trades_at_one_timestamp_keep_their_row_order(tmp_path: Path) -> None:
    path = tmp_path / "t.csv.gz"
    path.write_bytes(_csv([(B, 200.0, 1.0), (B, 100.0, 1.0)]))
    trades = d.Trades(d.read_trades(path, B - 1, B + 1))
    assert trades.last_at(B) == (100.0, 0.0)


def test_measure_traces_the_decay_after_the_bar_end() -> None:
    m = d.measure(d.Trades(_burst_trades()), _firing())
    assert m["status"] == "ok"
    assert m["open_check"]["matches_bar_open"]
    assert m["open_check"]["first_trade_after_b_s"] == pytest.approx(0.1, abs=1e-3)
    g = [m["delays"][f"{x:g}"]["g_bps"] for x in d.DELAYS_S]
    assert g[0] > g[d.DELAYS_S.index(5.0)] > g[d.DELAYS_S.index(20.0)]
    assert m["delays"]["0"]["age_s"] == pytest.approx(1.0, abs=1e-3)  # last burst trade
    assert m["intrabar"]["seconds_before_b"] > 0
    assert m["intrabar"]["g_bps"] > m["delays"]["0"]["g_bps"]


def test_no_intrabar_detection_when_turnover_stays_small() -> None:
    m = d.measure(d.Trades(_burst_trades()), _firing(median_turnover_usd=1e9))
    assert "intrabar" not in m


def test_tape_days_cover_an_exit_after_midnight() -> None:
    late = int(datetime(2026, 9, 10, 23, 30, tzinfo=UTC).timestamp())
    assert d.tape_days([_firing(bar_start=late)]) == [
        ("AAAUSDT", "2026-09-10"),
        ("AAAUSDT", "2026-09-11"),
    ]


def test_freeze_firings_takes_only_complete_paired_bursts() -> None:
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute(
        "CREATE TABLE good (exchange VARCHAR, symbol VARCHAR, t TIMESTAMPTZ, o DOUBLE,"
        " h DOUBLE, l DOUBLE, c DOUBLE, n DOUBLE, price_ok BOOLEAN, flow_ok BOOLEAN)"
    )
    start = BAR - 70 * 60
    for symbol, gap in (("AAA", None), ("BBB", BAR + 6 * 60)):
        for m in range(0, 140):
            ts = start + m * 60
            if ts == gap:
                continue
            c = 1.06 if ts >= BAR else 1.0
            n = 50_000.0 if ts == BAR else 1_000.0
            con.execute(
                "INSERT INTO good VALUES ('bybit', ?, to_timestamp(?), ?, ?, ?, ?, ?, true, true)",
                [symbol, ts, c, c, c, c, n],
            )
    firings = d.freeze_firings(con)
    assert [(f["symbol"], f["bar_start"]) for f in firings] == [("AAA", BAR)]
    assert firings[0]["t1_open"] == pytest.approx(1.06)


def test_read_verifies_files_and_writes_once(tmp_path: Path) -> None:
    stage, tapes_dir = tmp_path / "stage", tmp_path / "tapes"
    stage.mkdir()
    tapes_dir.mkdir()
    firings_sha = write_once(stage / d.FIRINGS_NAME, {"firings": [_firing()]})
    name = "AAAUSDT2026-09-10.csv.gz"
    (tapes_dir / name).write_bytes(_csv(_burst_trades()))
    sha = hashlib.sha256((tapes_dir / name).read_bytes()).hexdigest()
    write_once(
        stage / d.TAPES_NAME,
        {"firings_sha256": firings_sha, "files": {name: {"status": "ok", "sha256": sha}}},
    )
    result = d.read(stage, tapes_dir)
    assert result["statuses"] == {"ok": 1}
    assert result["open_check"] == {"mismatching_bar_open": 0, "first_trade_later_than_1s": 0}
    assert result["decay_curve"]["0"]["n"] == 1
    with pytest.raises(SystemExit, match="read once"):
        d.read(stage, tapes_dir)


def test_fetch_refuses_days_on_or_after_the_blind_boundary(tmp_path: Path) -> None:
    late = int(datetime(2026, 9, 28, 23, 30, tzinfo=UTC).timestamp())
    write_once(tmp_path / d.FIRINGS_NAME, {"firings": [_firing(bar_start=late)]})
    with pytest.raises(SystemExit, match="blind boundary"):
        d.fetch_tapes(tmp_path, tmp_path / "tapes")
    assert not (tmp_path / "tapes").exists()
