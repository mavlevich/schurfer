from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import duckdb
import pytest
from schurfer_analytics import edge_loss_study as s

if TYPE_CHECKING:
    from pathlib import Path

T0 = datetime(2026, 9, 1, tzinfo=UTC)
M0 = int(T0.timestamp()) // 60


def _bars(prices: dict[int, float]) -> list[tuple]:
    """(epoch seconds, o, h, l, c) with open = previous close."""
    rows, prev = [], None
    for m in sorted(prices):
        c = prices[m]
        o = prev if prev is not None else c
        rows.append(((M0 + m) * 60, o, max(o, c), min(o, c), c))
        prev = c
    return rows


def test_scanner_entry_weights_split_the_cycle_over_the_next_opens() -> None:
    weights = s.scanner_entry_weights()
    assert set(weights) == {2, 3}
    assert weights[2] == pytest.approx((60 - 2.7) / 101)
    assert sum(weights.values()) == pytest.approx(1.0)


def test_a_timeline_measures_the_share_of_the_move_at_each_moment() -> None:
    prices = {m: 1.0 for m in range(0, 4000)}
    ramp_start = 3000
    for i in range(100):  # +1% a minute: crosses +3% in 5m, then +20% over 24h
        prices[ramp_start + i] = 1.0 * 1.01 ** (i + 1)
    for m in range(ramp_start + 100, 4000 + 1500):
        prices[m] = 1.01**100
    series = s.Series(_bars(prices))
    cross = next(m for m in range(ramp_start, 4000) if prices[m] >= 1.2)
    seen = datetime.fromtimestamp((M0 + cross + 2) * 60 + 30, UTC)
    got = s.timeline(series, seen, M0 + 10_000)
    assert got["status"] == "ok"
    moments = got["moments"]
    assert moments["5m_0.03"]["crossing_close"] < moments["24h_0.2"]["crossing_close"] < 1
    row = moments["24h_0.2"]
    assert row["scanner_lag_s"] == pytest.approx(90.0)
    assert row["stream_ideal_next_open"] <= row["watch_path_entry"] <= row["scanner_actual_entry"]
    assert 0 < row["scanner_model_entry"] < 1


def test_a_timeline_without_a_bar_crossing_or_peak_window_says_so() -> None:
    flat = s.Series(_bars({m: 1.0 for m in range(0, 3000)}))
    assert s.timeline(flat, T0 + timedelta(minutes=2950), M0 + 10_000)["status"] == (
        "no_bar_crossing"
    )
    prices = {m: 1.0 for m in range(0, 3000)} | {m: 1.3 for m in range(3000, 3100)}
    series = s.Series(_bars(prices))
    seen = T0 + timedelta(minutes=3050)
    assert s.timeline(series, seen, M0 + 3100)["status"] == "censored_peak"
    short = T0 + timedelta(minutes=2500)  # less than 48h of bars before it
    assert s.timeline(flat, short, M0 + 10_000)["status"] == "insufficient_history"


def test_firings_keep_one_per_instrument_per_hour_and_respect_the_threshold() -> None:
    ts = int(T0.timestamp())
    rows = [
        ("bybit", "A", ts, 0.06),
        ("bybit", "A", ts + 30 * 60, 0.07),  # inside the cooldown
        ("bybit", "A", ts + 60 * 60, 0.05),
        ("bybit", "B", ts, 0.04),  # below 5%
        ("binance", "A", ts, 0.09),  # another venue
    ]
    kept = s.firings(rows, "bybit", 0.05, ts)
    assert [(r[1], r[2] - ts) for r in kept] == [("A", 0), ("A", 3600)]


def test_outcomes_separate_censored_unresolved_and_resolved() -> None:
    end = int(s.BLIND_END.timestamp())
    # exchange, symbol, ts, r, e1, x1_15, x1_60, x1_240, e2, x2_15, x2_60, x2_240
    rows = [
        ("bybit", "A", end - 6 * 3600, 0.05, 100.0, 101.0, 102.0, 90.0, 101.0, 0, 103.02, 0),
        ("bybit", "B", end - 6 * 3600, 0.05, None, 101.0, 102.0, 90.0, None, 0, 1.0, 0),
        ("bybit", "D", end - 6 * 3600, 0.05, 100.0, 101.0, None, 90.0, 100.0, 0, None, 0),
        ("bybit", "C", end - 30 * 60, 0.05, 100.0, 101.0, None, None, None, 0, None, 0),
    ]
    long = s.outcomes(rows, 60, "long")
    assert long["unresolved"] == {"entry_bar_missing": 1, "exit_bar_missing": 1}
    assert long["censored"] == 1
    assert long["resolved"][0]["gross_bps"] == pytest.approx(200.0)
    short = s.outcomes(rows, 60, "short")
    assert short["resolved"][0]["gross_bps"] == pytest.approx(-200.0)
    later = s.outcomes(rows, 60, "long", entry=2)
    assert later["resolved"][0]["gross_bps"] == pytest.approx(200.0)


def test_primary_inference_reports_the_mde_and_a_verdict() -> None:
    obs = [
        {"instrument": f"S{i % 25}", "utc_day": f"d{i % 20}", "net_bps": -50.0 + (i % 7)}
        for i in range(400)
    ]
    report = s.primary_inference(obs, seed=7)
    keys = list(report)
    assert keys.index("mde_bps_80pct_power") < keys.index("mean_net_bps")
    assert report["verdict"] == "negative_established"
    noisy = [
        {**o, "net_bps": (o["net_bps"] + 50) * (1 if i % 2 else -1)} for i, o in enumerate(obs)
    ]
    assert s.primary_inference(noisy, seed=7)["verdict"] == "not_established"


def _bars_table(
    prices: dict[int, float],
    *,
    burst_minute: int = 100,
    price_bad: frozenset[int] = frozenset(),
    flow_bad: frozenset[int] = frozenset(),
    missing: frozenset[int] = frozenset(),
) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute(
        "CREATE TABLE bars (exchange VARCHAR, symbol VARCHAR, t TIMESTAMPTZ, o DOUBLE,"
        " h DOUBLE, l DOUBLE, c DOUBLE, n DOUBLE, price_ok BOOLEAN, flow_ok BOOLEAN)"
    )
    for ts, o, h, lo, c in _bars(prices):
        m = ts // 60 - M0
        if m in missing:
            continue
        n = 50_000.0 if m == burst_minute else 1_000.0
        con.execute(
            "INSERT INTO bars VALUES ('bybit', 'A', to_timestamp(?), ?, ?, ?, ?, ?, ?, ?)",
            [ts, o, h, lo, c, n, m not in price_bad, m not in flow_bad],
        )
    con.execute("CREATE VIEW good AS SELECT * FROM bars WHERE price_ok")
    return con


def _step_prices() -> dict[int, float]:
    return {m: 1.0 for m in range(0, 100)} | {m: 1.06 for m in range(100, 300)}


def test_trigger_candidates_find_five_and_one_minute_triggers_with_forward_opens() -> None:
    got = s.trigger_candidates(_bars_table(_step_prices()))
    five = [r for r in got["five_minute"] if r[3] >= 0.05]
    assert five and five[0][2] == (M0 + 100) * 60
    assert five[0][4] == pytest.approx(1.06)  # the next minute's open
    assert [r[2] for r in got["one_minute"]] == [(M0 + 100) * 60]


def test_a_five_minute_window_with_a_missing_or_incomplete_minute_does_not_fire() -> None:
    for bad in ({"missing": frozenset({97})}, {"price_bad": frozenset({97})}):
        got = s.trigger_candidates(_bars_table(_step_prices(), **bad))  # type: ignore[arg-type]
        assert all(r[2] != (M0 + 100) * 60 for r in got["five_minute"])
        lost = got["exclusions"]["five_minute_candidates_lost_to_incomplete_window"]
        assert lost["bybit"] >= 1


def test_a_trade_incomplete_burst_bar_does_not_fire_the_one_minute_family() -> None:
    got = s.trigger_candidates(_bars_table(_step_prices(), flow_bad=frozenset({100})))
    assert got["one_minute"] == []
    assert got["exclusions"]["bars"]["bybit"]["trade_incomplete"] == 1


def test_an_incomplete_entry_bar_leaves_the_firing_unresolved() -> None:
    got = s.trigger_candidates(_bars_table(_step_prices(), price_bad=frozenset({101})))
    five = [r for r in got["five_minute"] if r[2] == (M0 + 100) * 60]
    assert five and five[0][4] is None


def test_too_little_evidence_is_never_established() -> None:
    two = [{"instrument": "A", "utc_day": "d", "net_bps": 100.0}] * 2
    report = s.primary_inference(two, seed=1)
    assert report["verdict"] == "not_established"
    assert report["reason"] == "below_registered_minimums"


def test_inputs_are_pinned_once_and_reused_only_when_identical(tmp_path: Path) -> None:
    path = tmp_path / s.INPUTS_NAME
    inputs = {"a": 1, "taken_at": "t1"}
    first = s.pin_inputs(path, inputs)
    assert s.pin_inputs(path, {"a": 1, "taken_at": "t2"}) == first
    with pytest.raises(SystemExit, match="other inputs"):
        s.pin_inputs(path, {"a": 2, "taken_at": "t3"})


def test_verify_bars_refuses_a_missing_or_altered_day(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing"):
        s.verify_bars(tmp_path)
    for day in s.window_days(s.WINDOW_FIRST, s.WINDOW_LAST):
        path = tmp_path / s.reduced_name(day)
        path.write_bytes(day.encode())
        path.with_suffix(".manifest.json").write_text(
            json.dumps(
                {
                    "day": day,
                    "reduced_sha256": s.sha256_file(path),
                    "source_sha256": "x",
                    "rows_by_exchange": {},
                }
            )
        )
    assert len(s.verify_bars(tmp_path)) == 47
    (tmp_path / s.reduced_name("2026-09-01")).write_bytes(b"changed")
    with pytest.raises(ValueError, match="does not match"):
        s.verify_bars(tmp_path)


def test_the_contract_pins_the_registered_primary_and_costs() -> None:
    assert s.PRIMARY["threshold"] == 0.05 and s.PRIMARY["horizon"] == 60
    assert s.COST_SCENARIOS_BPS == (10.5, 20.5, 45.5)
    assert s.PRIMARY_COST_BPS == 20.5
    assert s.WINDOW_FIRST.isoformat() == "2026-08-13"


def test_gap_marked_prices_never_reach_the_analysed_bars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    day = "2026-09-10"
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE t AS SELECT * FROM (VALUES ('bybit', 'A', TIMESTAMPTZ"
        " '2026-09-10 00:00:00+00', NULL::BOOLEAN, false), ('bybit', 'B', TIMESTAMPTZ"
        " '2026-09-10 00:00:00+00', false, true), ('bybit', 'C', TIMESTAMPTZ"
        " '2026-09-10 00:00:00+00', true, true)) v(exchange, symbol, bucket_start,"
        " price_complete, complete)"
    )
    for column in ("open_price", "high_price", "low_price", "close_price"):
        con.execute(f"ALTER TABLE t ADD COLUMN {column} DOUBLE DEFAULT 1")
    for column in ("buy_total_notional_usd", "sell_total_notional_usd"):
        con.execute(f"ALTER TABLE t ADD COLUMN {column} DOUBLE DEFAULT 1")
    con.execute("ALTER TABLE t ADD COLUMN trades_complete BOOLEAN DEFAULT true")
    con.execute(f"COPY t TO '{tmp_path / s.reduced_name(day)}' (FORMAT parquet)")
    monkeypatch.setattr(s, "window_days", lambda *_: [day])
    analysed = duckdb.connect()
    s.load_bars(analysed, tmp_path, [])
    assert analysed.execute("SELECT symbol FROM good").fetchall() == [("C",)]


def test_an_interrupted_read_resumes_on_the_same_pinned_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from schurfer_analytics.source_lead_multi_source_report import write_once

    write_once(tmp_path / s.SCANNER_NAME, {"sources": []})
    monkeypatch.setattr(s, "verify_bars", lambda _: [])
    monkeypatch.setattr(s, "verify_mexc", lambda _: ([], "mexc-sha"))
    monkeypatch.setattr(s, "binance_first_day", lambda *_: None)

    def interrupted(*_: object) -> None:
        raise MemoryError("synthetic interrupted read")

    monkeypatch.setattr(s, "load_bars", interrupted)
    for attempt in range(2):
        now = datetime(2026, 10, 5, attempt, tzinfo=UTC)
        with pytest.raises(MemoryError):
            s.run_read(tmp_path, tmp_path, tmp_path, "rev", now)
    monkeypatch.setattr(s, "verify_mexc", lambda _: ([], "another-sha"))
    with pytest.raises(SystemExit, match="other inputs"):
        s.run_read(tmp_path, tmp_path, tmp_path, "rev", datetime(2026, 10, 6, tzinfo=UTC))
