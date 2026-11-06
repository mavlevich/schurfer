from __future__ import annotations

import gzip
import json
from typing import TYPE_CHECKING

import pytest
from schurfer_analytics import bybit_compressed_spring_cond4 as c4
from schurfer_analytics.bybit_compressed_spring import (
    DAY_MS,
    FIRST_DECISION_MS,
    HOUR_MS,
    RANK_HOURS,
    VERSION,
    Hour,
    Series,
)
from schurfer_analytics.bybit_compressed_spring_cond4 import (
    Decision,
    Tape,
    controls_for,
    mid_rank,
    non_overlapping_events,
    s_value,
    six_hour_return,
)
from schurfer_analytics.bybit_compressed_spring_tape import write_part
from schurfer_analytics.bybit_post_funding_screen import file_sha256

if TYPE_CHECKING:
    from pathlib import Path

T0 = FIRST_DECISION_MS + 20 * DAY_MS
DAY0 = T0 - T0 % DAY_MS


def tape_with(hours: dict[int, tuple[float, float]], days: set[int] | None = None) -> Tape:
    tape = Tape(kept=dict(hours))
    tape.ok_days = days if days is not None else {h - h % DAY_MS for h in hours}
    return tape


def test_s_value_needs_six_usable_hours() -> None:
    hours = {T0 - k * HOUR_MS: (1.0, 3.0) for k in range(1, 7)}
    tape = tape_with(hours)
    assert s_value(tape, tape.ok_days, T0) == pytest.approx(0.5)  # (18 - 6) / 24
    tape.unusable.add(T0 - 3 * HOUR_MS)
    assert s_value(tape, tape.ok_days, T0) == "tape_mismatch"
    assert s_value(tape_with(hours, set()), set(), T0) == "tape_missing"
    quiet = tape_with({}, {DAY0, DAY0 - DAY_MS})
    assert s_value(quiet, quiet.ok_days, T0) == "no_trades"


def test_unmasked_s_adds_masked_minutes_only_away_from_reserved_boundaries() -> None:
    hours = {T0 - k * HOUR_MS: (1.0, 1.0) for k in range(1, 7)}
    tape = tape_with(hours)
    tape.masked = {T0 - HOUR_MS: (0.0, 6.0)}
    assert s_value(tape, tape.ok_days, T0) == 0.0
    assert s_value(tape, tape.ok_days, T0, unmasked=True) == pytest.approx(6 / 18)
    tape.event_hours = {T0 - 6 * HOUR_MS}
    assert s_value(tape, tape.ok_days, T0, unmasked=True) == "reserved"


def test_mid_rank() -> None:
    assert mid_rank(5.0, [1.0, 5.0, 9.0, 5.0]) == pytest.approx((1 + 1) / 4)


def test_six_hour_return_uses_closes_before_t() -> None:
    series = Series(bars={T0 - HOUR_MS: (0, 0, 110.0, 0), T0 - 7 * HOUR_MS: (0, 0, 100.0, 0)})
    assert six_hour_return(series, T0) == pytest.approx(0.10)
    series.bars[T0] = (0, 0, 1e9, 0)  # the bar starting at t is never read
    assert six_hour_return(series, T0) == pytest.approx(0.10)


def _hour(symbol: str = "A", t: int = T0, c2: str = "true", c3: str = "true") -> Hour:
    return Hour(symbol, t, "liquid", "true", c2, c3)


def _d(
    s: float | str = 0.4, pct: float | None = 0.9, r: float | None = 0.01, **kw: object
) -> Decision:
    r_sd = kw.pop("r_sd", 0.2)
    hour = kw.pop("hour", _hour())
    turnover = kw.pop("turnover", 5e6)
    return Decision(hour, s, pct, r, r_sd, turnover)  # type: ignore[arg-type]


def test_condition4_needs_net_selling_a_high_rank_and_no_fall() -> None:
    assert _d().condition4 == "true"
    assert _d(s=-0.1, pct=0.95).condition4 == "false"  # a high rank of net buying
    assert _d(pct=0.79).condition4 == "false"
    assert _d(r=-0.001).condition4 == "false"
    assert _d(pct=None).condition4 == "rank_history_short"
    assert _d(s="tape_mismatch").condition4 == "tape_mismatch"
    assert _d(r=None).condition4 == "kline_gap"


def test_decide_ranks_s_against_its_own_336_hour_history() -> None:
    hours = {T0 - k * HOUR_MS: (2.0, 1.0) for k in range(1, RANK_HOURS + 8)}  # net buying
    for k in range(1, 7):
        hours[T0 - k * HOUR_MS] = (1.0, 3.0)  # the last 6 hours: net selling
    tape = tape_with(hours)
    series = Series(bars={T0 - k * HOUR_MS: (0, 0, 100.0, 1e5) for k in range(1, 400)})
    d = c4.decide(series, tape, tape.ok_days, _hour())
    assert d.s == pytest.approx(0.5)
    assert d.s_pct is not None and d.s_pct > 0.95
    short = tape_with({h: v for h, v in hours.items() if h > T0 - 200 * HOUR_MS})
    assert c4.decide(series, short, short.ok_days, _hour()).s_pct is None


def test_non_overlapping_events_hold_one_position_per_instrument() -> None:
    ds = [_d(hour=_hour(t=T0 + k * HOUR_MS)) for k in range(6)] + [_d(hour=_hour("B"))]
    chosen = non_overlapping_events(ds, 4)
    assert [(d.hour.symbol, (d.hour.t - T0) // HOUR_MS) for d in chosen] == [
        ("A", 0), ("A", 5), ("B", 0),
    ]  # fmt: skip


def test_controls_match_price_state_and_need_a_determinable_failure() -> None:
    event = _d(r_sd=1.0)
    near = [
        _d(hour=_hour("B", c2="false"), r_sd=1.1, turnover=5e6),
        _d(hour=_hour("C", c3="false"), r_sd=0.6, turnover=5e6),
        _d(hour=_hour("D"), s=-0.2, pct=0.1, r_sd=1.0, turnover=5e6),  # fails on S
        _d(hour=_hour("E", c2="false"), r_sd=1.6, turnover=5e6),  # outside the caliper
        _d(hour=_hour("F", c2="false"), r=-0.01, r_sd=1.0),  # fell
        _d(hour=_hour("G"), s="tape_missing", pct=None, r_sd=1.0),  # S unknown, 2 and 3 hold
        _d(hour=_hour("A", c2="false"), r_sd=1.0),  # same instrument
    ]
    assert [c.hour.symbol for c in controls_for(event, near)] == ["D", "B", "C"]
    assert controls_for(_d(r_sd=None), near) == []


def test_load_tape_checks_hashes_and_marks_unusable_hours(tmp_path: Path) -> None:
    parts = tmp_path / "parts"
    day = "2024-11-25"
    start = c4._day_start(day)
    ok = {
        "symbol": "A", "day": day, "status": "ok",
        "s_minutes": {str(start + 30 * 60_000): [1.0, 2.0, 0.5, 1.0, 3]},
        "sensitivity_minutes": {}, "event_hours": [start],
        "reconciliation": {"hours": {str(start): "unchecked_reserved",
                                     str(start + HOUR_MS): "mismatch"}},
    }  # fmt: skip
    bad = {"symbol": "A", "day": "2024-11-26", "status": "invalid"}
    write_part(parts / "A" / f"{day}.json.gz", ok)
    write_part(parts / "A" / "2024-11-26.json.gz", bad)
    entries = [
        {"symbol": "A", "day": day, "part_sha256": file_sha256(parts / "A" / f"{day}.json.gz")},
        {"symbol": "A", "day": "2024-11-26",
         "part_sha256": file_sha256(parts / "A" / "2024-11-26.json.gz")},
    ]  # fmt: skip
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"version": VERSION, "binding": {}, "parts": entries}))
    tapes, prov = c4.load_tape(manifest, file_sha256(manifest))
    tape = tapes["A"]
    assert tape.kept == {start: (1.0, 2.0)}
    assert start + HOUR_MS in tape.unusable
    assert start + DAY_MS + 5 * HOUR_MS in tape.unusable  # the invalid day
    assert tape.ok_days == {start}
    assert prov["part_statuses"] == {"ok": 1, "invalid": 1}
    (parts / "A" / f"{day}.json.gz").write_bytes(gzip.compress(b"{}", mtime=0))
    with pytest.raises(SystemExit, match="does not match"):
        c4.load_tape(manifest, file_sha256(manifest))
