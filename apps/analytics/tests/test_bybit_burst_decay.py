from __future__ import annotations

import gzip
import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import duckdb
import pytest
from schurfer_analytics import bybit_burst_decay as d
from schurfer_analytics.edge_loss_study import sha256_file
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
    """Flat at 1.00 before the burst minute; inside it the price climbs to 1.059 with heavy
    turnover; from B + 0.1 s it keeps climbing for 10 s to 1.10, then holds; the exit
    trade is 0.5 s after B + 60 min."""
    trades: list[tuple[float, float, float]] = [
        (float(BAR - 120 + i), 1.0, 100.0) for i in range(0, 60, 5)
    ]
    trades += [(BAR + i, 1.0 + 0.001 * i, 1_000.0) for i in range(0, 60)]
    trades += [(B + 0.1 + i, 1.06 + 0.004 * i, 500.0) for i in range(0, 11)]
    trades += [(B + 12 + i, 1.10, 50.0) for i in range(0, 3580, 10)]
    trades += [(B + d.EXIT_AFTER_S + 0.5, 1.21, 50.0)]
    return trades


def _firing(**over: Any) -> dict[str, Any]:
    return {
        "symbol": "AAAUSDT",
        "bar_start": BAR,
        "prev_close": 1.0,
        "median_turnover_usd": 1_000.0,
        "t1_open": 1.06,
        "t61_open": 1.21,
        **over,
    }


def test_trades_at_one_timestamp_keep_their_row_order(tmp_path: Path) -> None:
    path = tmp_path / "t.csv.gz"
    path.write_bytes(_csv([(B, 200.0, 1.0), (B, 100.0, 1.0)]))
    trades = d.Trades(d.read_trades(path, [(B - 1, B + 1)]))
    assert trades.last_at(B) == (100.0, 0.0)


def test_only_the_firing_windows_are_read(tmp_path: Path) -> None:
    path = tmp_path / "t.csv.gz"
    path.write_bytes(_csv([(B - 500, 1.0, 1.0), (B, 2.0, 1.0), (B + 5000, 3.0, 1.0)]))
    assert d.read_trades(path, [(B - 60, B + 60)]) == [(float(B), 2.0, 1.0)]
    assert d.read_trades(path, []) == []


def test_entry_is_the_first_trade_after_the_moment_not_the_last_one_before() -> None:
    m = d.measure(d.Trades(_burst_trades()), _firing())
    assert m["status"] == "ok"
    # at B the last known trade is still inside the burst bar (1.059 at B - 1 s); the
    # first trade from B on comes 0.1 s later at 1.06
    assert m["last_before"]["0"]["age_s"] == pytest.approx(1.0, abs=1e-3)
    assert m["first_after"]["0"]["wait_s"] == pytest.approx(0.1, abs=1e-3)
    assert m["first_after"]["0"]["g_bps"] < m["last_before"]["0"]["g_bps"]


def test_measure_traces_the_decay_against_the_t61_exit() -> None:
    m = d.measure(d.Trades(_burst_trades()), _firing())
    checks = m["checks"]
    assert checks["entry_matches_t1_open"] and checks["exit_matches_t61_open"]
    assert checks["exit_wait_s"] == pytest.approx(0.5, abs=1e-3)
    g = [m["first_after"][f"{x:g}"]["g_bps"] for x in d.DELAYS_S[:7]]
    assert g == sorted(g, reverse=True) and g[0] > g[-1]
    assert m["g_first_bps"] == pytest.approx((1.21 / 1.06 - 1) * 1e4)
    assert m["intrabar"]["seconds_before_b"] > 0
    assert m["intrabar"]["g_bps"] > m["g_first_bps"]


def test_a_tape_that_stops_before_the_exit_is_not_a_result() -> None:
    short = [t for t in _burst_trades() if t[0] <= B + 1]
    assert d.measure(d.Trades(short), _firing()) == {"status": "exit_unavailable"}


def test_a_moment_without_a_trade_within_the_wait_is_missing() -> None:
    trades = d.Trades([(B + 10.0, 1.0, 1.0)])
    assert d.Trades.first_from(trades, B, d.ENTRY_WAIT_S) is None
    assert trades.first_from(B + 9, d.ENTRY_WAIT_S) == (1.0, B + 10.0)


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
    assert firings[0]["t61_open"] == pytest.approx(1.06)


def test_bars_must_match_the_read2_pins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pinned = tmp_path / "inputs.json"
    write_once(pinned, {"bars": [{"day": "2026-09-10", "reduced_sha256": "a"}]})
    monkeypatch.setattr(d, "verify_bars", lambda _: [{"day": "2026-09-10", "reduced_sha256": "a"}])
    assert len(d.verified_bars_against_read2(tmp_path, pinned)) == 64
    monkeypatch.setattr(d, "verify_bars", lambda _: [{"day": "2026-09-10", "reduced_sha256": "b"}])
    with pytest.raises(SystemExit, match="differ"):
        d.verified_bars_against_read2(tmp_path, pinned)


def _stage(tmp_path: Path, firings: list[dict[str, Any]], **over: Any) -> tuple[Path, str]:
    stage = tmp_path / "stage"
    stage.mkdir()
    payload = {"contract_sha256": d.contract_sha256(), "code_revision": "rev", "firings": firings}
    return stage, write_once(stage / d.FIRINGS_NAME, {**payload, **over})


def test_read_verifies_files_once_per_instrument_and_writes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second = _firing(bar_start=BAR + 2 * 3600, t1_open=1.10, t61_open=1.10)
    stage, firings_sha = _stage(tmp_path, [_firing(), second])
    tapes_dir = tmp_path / "tapes"
    tapes_dir.mkdir()
    name = "AAAUSDT2026-09-10.csv.gz"
    (tapes_dir / name).write_bytes(_csv(_burst_trades()))
    sha = hashlib.sha256((tapes_dir / name).read_bytes()).hexdigest()
    write_once(
        stage / d.TAPES_NAME,
        {
            "contract_sha256": d.contract_sha256(),
            "firings_sha256": firings_sha,
            "files": {name: {"status": "ok", "sha256": sha}},
        },
    )
    hashed: list[str] = []

    def spy(path: Path) -> str:
        hashed.append(path.name)
        return sha256_file(path)

    monkeypatch.setattr(d, "sha256_file", spy)
    result = d.read(stage, tapes_dir, "reader-rev")
    assert hashed == [name]
    assert result["reader_code_revision"] == "reader-rev"
    assert result["firings_code_revision"] == "rev"
    assert result["peak_rss_bytes"] > 0
    assert result["statuses"] == {"ok": 1, "exit_unavailable": 1}
    assert result["checks"]["entry_not_t1_open"] == 0
    assert result["checks"]["exit_not_t61_open"] == 0
    with pytest.raises(SystemExit, match="read once"):
        d.read(stage, tapes_dir, "reader-rev")


def test_every_phase_refuses_another_contract(tmp_path: Path) -> None:
    stage, _ = _stage(tmp_path, [_firing()], contract_sha256="other")
    with pytest.raises(SystemExit, match="another contract"):
        d.fetch_tapes(stage, tmp_path / "tapes")
    with pytest.raises(SystemExit, match="another contract"):
        d.read(stage, tmp_path / "tapes", "reader-rev")


def test_the_cap_counts_files_already_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(d, "MAX_BYTES", 10)  # part of the contract: set before freezing
    stage, _ = _stage(tmp_path, [_firing()])
    tapes_dir = tmp_path / "tapes"
    tapes_dir.mkdir()
    (tapes_dir / "AAAUSDT2026-09-10.csv.gz").write_bytes(b"x" * 100)
    with pytest.raises(SystemExit, match="cap"):
        d.fetch_tapes(stage, tapes_dir)


def test_fetch_refuses_days_on_or_after_the_blind_boundary(tmp_path: Path) -> None:
    late = int(datetime(2026, 9, 28, 23, 30, tzinfo=UTC).timestamp())
    stage, _ = _stage(tmp_path, [_firing(bar_start=late)])
    with pytest.raises(SystemExit, match="blind boundary"):
        d.fetch_tapes(stage, tmp_path / "tapes")
    assert not (tmp_path / "tapes").exists()


def test_the_read_phase_requires_the_checked_out_revision(tmp_path: Path) -> None:
    stage, _ = _stage(tmp_path, [_firing()])
    with pytest.raises(SystemExit, match="code-revision"):
        d.main(["--phase", "read", "--stage-dir", str(stage), "--tapes-dir", str(tmp_path)])
