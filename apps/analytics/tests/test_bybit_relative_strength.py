from __future__ import annotations

import math
from collections import Counter

import pytest
from schurfer_analytics import bybit_relative_strength as rs
from schurfer_analytics.bybit_compressed_spring import (
    DAY_MS,
    FIRST_DECISION_MS,
    HOUR_MS,
    WINDOW_FIRST_MS,
    Series,
)
from schurfer_analytics.bybit_relative_strength import (
    FORMATION_H,
    Member,
    balance,
    cross_section,
    day_sigmas,
    floor,
    match,
    mde,
    member,
    portfolio_segments,
    ranked,
    segment_ends,
    weekly_factor,
    weights_a,
    weights_b,
)
from schurfer_analytics.bybit_squeeze_continuation import FEATURES_SHA256, Instrument

T = FIRST_DECISION_MS + 2 * DAY_MS
SPAN_END = T + 3 * DAY_MS


def series(drift: float = 0.0, turnover: float = 200_000.0, wiggle: float = 0.004) -> Series:
    """Hourly closes alternating +-wiggle around a drift per hour."""
    s = Series(0, SPAN_END)
    price = 100.0
    for k, start in enumerate(range(WINDOW_FIRST_MS, SPAN_END, HOUR_MS)):
        price *= (1 + wiggle if k % 2 else 1 - wiggle) * (1 + drift)
        s.bars[start] = (price, price, price, turnover)
    return s


BTC = series()


def inst(symbol: str, s: Series) -> Instrument:
    return Instrument(symbol, s, BTC)


def unit(symbol: str, d: int = T, sd: float = 0.01, turnover: float = 5e6,
         m: float = 0.1, rank: float = 0.95) -> Member:  # fmt: skip
    return Member(symbol, d, m, sd, turnover, rank)


def test_member_reads_the_week_ending_t_against_btc() -> None:
    funnel: Counter[str] = Counter()
    x = member(inst("UPUSDT", series(drift=0.001)), inst("BTCUSDT", BTC), T, funnel)
    assert x is not None
    assert x.m == pytest.approx(FORMATION_H * math.log(1.001), rel=1e-6)
    assert funnel["defined"] == 1


def test_member_ignores_bars_after_t() -> None:
    s = series(drift=0.001)
    before = member(inst("UPUSDT", s), inst("BTCUSDT", BTC), T, Counter())
    for start in range(T, SPAN_END, HOUR_MS):
        s.bars[start] = (1.0, 1.0, 1.0, 1.0)
    after = member(inst("UPUSDT", s), inst("BTCUSDT", BTC), T, Counter())
    assert before == after


def test_member_needs_liquidity_and_formation() -> None:
    funnel: Counter[str] = Counter()
    assert member(inst("THIN", series(turnover=1_000.0)), inst("BTCUSDT", BTC), T, funnel) is None
    gap = series()
    del gap.bars[T - FORMATION_H * HOUR_MS - HOUR_MS]
    assert member(inst("GAP", gap), inst("BTCUSDT", BTC), T, funnel) is None
    assert funnel["liquidity_illiquid"] == 1
    assert funnel["formation_kline_gap"] == 1


def test_mid_ranks_with_ties() -> None:
    xs = ranked([unit("A", m=0.1), unit("B", m=0.2), unit("C", m=0.2), unit("D", m=0.3)])
    assert [x.rank for x in xs] == [0.125, 0.5, 0.5, 0.875]


def test_thin_cross_section_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    instruments = {f"S{k}USDT": inst(f"S{k}USDT", series(drift=k * 1e-4)) for k in range(5)}
    funnel: Counter[str] = Counter()
    assert cross_section(instruments, inst("BTCUSDT", BTC), T, funnel) is None
    assert funnel["thin_cross_section_days"] == 1
    monkeypatch.setattr(rs, "MIN_CROSS_SECTION", 5)
    members = cross_section(instruments, inst("BTCUSDT", BTC), T, Counter())
    assert members is not None
    assert max(members, key=lambda x: x.rank).symbol == "S4USDT"


def test_match_same_t_bin_caliper_and_nearest_five() -> None:
    e = unit("EVT", sd=0.02)
    pool = [
        unit("A", sd=0.021, rank=0.5),  # ties N4 on the ratio, wins on the symbol
        unit("B", sd=0.026, rank=0.5),  # ln ratio 0.26, outside
        unit("C", sd=0.02, rank=0.5, turnover=2e7),  # another bin
        unit("D", d=T + DAY_MS, sd=0.02, rank=0.5),  # another day
        unit("EVT", sd=0.02, rank=0.5),  # the same instrument
        *[unit(f"N{k}", sd=0.02 * (1 + 0.01 * (k + 1)), rank=0.5) for k in range(5)],
    ]
    assert [c.symbol for c in match([e], pool)[e.key]] == ["N0", "N1", "N2", "N3", "A"]


def test_days_weigh_the_same() -> None:
    events = [unit("A"), unit("B"), unit("C", d=T + DAY_MS)]
    w = weights_a(events)
    assert w[("A", T)] == pytest.approx(0.25)
    assert w[("C", T + DAY_MS)] == pytest.approx(0.5)
    c1, c2 = unit("X", rank=0.5), unit("Y", rank=0.5)
    wb = weights_b(events, {("A", T): [c1, c2], ("B", T): [c1], ("C", T + DAY_MS): []})
    assert sum(v for v in wb.values() if v > 0) == pytest.approx(1.0)
    assert wb[c1.key] == pytest.approx(-0.75)
    assert wb[c2.key] == pytest.approx(-0.25)


def test_balance_spreads_use_the_same_weights() -> None:
    weights = {("E1", T): 0.5, ("E2", T): 0.5, ("A", T): -0.25, ("B", T): -0.25, ("C", T): -0.5}
    values = {("E1", T): 0.03, ("E2", T): 0.0, ("A", T): 0.01, ("B", T): 0.03, ("C", T): 0.0}
    result = balance(weights, {"x": dict(values)})
    assert result is not None
    event_var = 0.015**2
    control_var = 0.25 * 0.0**2 + 0.25 * 0.02**2 + 0.5 * 0.01**2
    expected = 0.005 / math.sqrt((event_var + control_var) / 2)
    assert result["x"]["smd_unrounded"] == pytest.approx(expected)


def test_segments_end_before_the_formation_window() -> None:
    ends = segment_ends(T)
    assert ends == sorted(ends)
    assert ends[-1] == T - FORMATION_H * HOUR_MS
    assert ends[0] == T - FORMATION_H * HOUR_MS - 6 * DAY_MS


def test_portfolio_segments_ignore_the_formation_window_and_after() -> None:
    s = series(drift=0.001)
    instruments = {"UPUSDT": inst("UPUSDT", s)}
    events = [unit("UPUSDT")]
    before = portfolio_segments(instruments, inst("BTCUSDT", BTC), events, None)
    for start in range(T - FORMATION_H * HOUR_MS, SPAN_END, HOUR_MS):
        s.bars[start] = (1.0, 1.0, 1.0, 1.0)
    after = portfolio_segments(instruments, inst("BTCUSDT", BTC), events, None)
    assert before == after
    assert all(v is not None for v in before[T])


def test_b_segments_subtract_the_controls() -> None:
    instruments = {"E": inst("E", series(drift=0.002)), "C": inst("C", series(drift=0.001))}
    e, c = unit("E"), unit("C", rank=0.5)
    out = portfolio_segments(instruments, inst("BTCUSDT", BTC), [e], {e.key: [c]})
    expected = (1.002**24 - 1) - (1.001**24 - 1)
    assert out[T][0] == pytest.approx(expected, rel=1e-3)


def test_day_sigmas_fill_short_days() -> None:
    segments = {
        T: [0.01, -0.01, 0.02, -0.02, 0.0, None, None],
        T + DAY_MS: [0.01, None] * 3 + [None],
    }
    sigmas, missing = day_sigmas(segments)
    assert missing == 1
    assert sigmas[T + DAY_MS] == sigmas[T]


def test_weekly_factor_detects_persistence() -> None:
    alternating = {d: [0.01, -0.01] * 3 + [0.01] for d in range(10)}
    assert weekly_factor(alternating)[0] == 1.0
    persistent = {d: [0.01 * d] * 7 for d in range(10)}  # each day one level
    f, rhos = weekly_factor(persistent)
    assert rhos == pytest.approx([1.0] * 6)
    assert f == pytest.approx(7.0)


def test_mde_matches_the_stated_expectation() -> None:
    sigma = 0.08 * math.sqrt(0.9 / 35 + 0.1)  # 35 units, sd 8%, within-day correlation 0.1
    sigmas = dict.fromkeys(range(350), sigma)
    assert mde(sigmas, 1.5, 1.2, 1.0) == pytest.approx(0.0062, abs=0.0001)


def test_floor_order_data_balance_power() -> None:
    shape = {"days": 300, "symbols": 100, "iso_weeks": 50, "top5_symbol_weight": 0.1}
    controls = {"events": 100, "events_with_control": 90}
    bal = {"ln_sd": {"smd_unrounded": 0.05}}
    powered = {"a_net": {"mde_unrounded": 0.004}, "b_excess": {"mde_unrounded": 0.0049}}
    assert floor(shape, controls, (95, 100), bal, powered)["verdict"] == "pass_to_reading"
    weak = {**powered, "a_net": {"mde_unrounded": 0.0062}}
    assert floor(shape, controls, (95, 100), bal, weak)["verdict"] == "insufficient_power"
    tilted = {"ln_sd": {"smd_unrounded": -0.11}}
    assert floor(shape, controls, (95, 100), tilted, weak)["verdict"] == "insufficient_balance"
    few = {"events": 100, "events_with_control": 79}
    assert floor(shape, few, (95, 100), tilted, weak)["verdict"] == "insufficient_data"


def test_main_refuses_another_features_snapshot() -> None:
    assert FEATURES_SHA256 != "0" * 64
    with pytest.raises(SystemExit, match="pins"):
        rs.main(["counts", "--features", "x", "--features-sha256", "0" * 64,
                 "--output-dir", "out", "--code-revision", "abc",
                 "--no-working-tree-dirty"])  # fmt: skip
