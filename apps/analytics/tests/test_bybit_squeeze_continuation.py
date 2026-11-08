from __future__ import annotations

import math
from collections import Counter
from typing import Any

import pytest
from schurfer_analytics import bybit_squeeze_continuation as sq
from schurfer_analytics.bybit_compressed_spring import (
    DAY_MS,
    FIRST_DECISION_MS,
    HOUR_MS,
    WINDOW_FIRST_MS,
    Series,
)
from schurfer_analytics.bybit_squeeze_continuation import (
    Impulse,
    Instrument,
    control_shape,
    filled,
    floor,
    match,
    mde,
    oi_change,
    power,
    render_markdown,
    scan,
    shape,
    weights_excess,
    weights_net,
)

T = FIRST_DECISION_MS + 5 * DAY_MS  # an impulse bar ends here; d = T + 1 h
D = T + HOUR_MS
SPAN_END = T + 3 * DAY_MS


def series(impulses: tuple[int, ...] = (T,), turnover: float = 200_000.0) -> Series:
    """Hourly closes alternating +-0.4%, a +5% bar ending at each impulse time,
    flat OI except a rise at the record stamped T, one negative settlement before D."""
    s = Series(0, SPAN_END)
    price = 100.0
    for k, start in enumerate(range(WINDOW_FIRST_MS, SPAN_END, HOUR_MS)):
        end = start + HOUR_MS
        price *= 1.05 if end in impulses else (1.004 if k % 2 else 0.996)
        s.bars[start] = (price, price, price, turnover)
        s.oi[start] = 100.0
    s.oi[T] = 120.0
    s.funding[D - 3 * HOUR_MS] = -0.0001
    return s


def run(s: Series, symbol: str = "AAAUSDT") -> tuple[list[Impulse], Counter[str]]:
    funnel: Counter[str] = Counter()
    inst = Instrument(symbol, s, series())
    return scan(inst, funnel, first_d=T - DAY_MS, last_d=T + 2 * DAY_MS), funnel


def test_first_impulse_with_funding_and_oi_is_an_event() -> None:
    impulses, funnel = run(series())
    events = [i for i in impulses if i.cell == "event"]
    assert [e.d for e in events] == [D]
    assert events[0].z1 > sq.IMPULSE_MIN_Z
    assert funnel["first_true"] == 1
    assert funnel["fg_determinable"] == 1


def test_a_second_impulse_inside_24_hours_is_not_first() -> None:
    impulses, funnel = run(series((T, T + 10 * HOUR_MS)))
    assert [i.d for i in impulses] == [D]
    assert funnel["trigger_true"] == 2
    assert funnel["first_false"] == 1


def test_small_absolute_move_is_not_a_trigger() -> None:
    s = series(impulses=())
    inst = Instrument("AAAUSDT", s, s)
    assert inst.trigger(T) == "false"


def test_g_uses_the_records_stamped_t_and_t_minus_2h() -> None:
    s = series()
    assert oi_change(s, D) == pytest.approx(0.2)
    s.oi[T] = 100.0
    s.oi[D] = 150.0  # the record stamped t_d is never read at t_d
    assert oi_change(s, D) == 0.0
    impulses, _ = run(s)
    assert [i.g for i in impulses] == ["false"]


def test_funding_settled_at_d_is_not_known_at_d() -> None:
    s = series()
    s.funding = {D - 3 * HOUR_MS: 0.0001, D: -0.001}
    impulses, _ = run(s)
    assert [(i.f, i.cell) for i in impulses] == [("false", "g_only")]


def test_illiquid_impulse_is_counted_and_dropped() -> None:
    impulses, funnel = run(series(turnover=50_000.0))
    assert impulses == []
    assert funnel["liquidity_illiquid"] > 0


def test_turnover_before_ignores_the_impulse_bar() -> None:
    s = series()
    s.bars[T - HOUR_MS] = (*s.bars[T - HOUR_MS][:3], 10**12)
    inst = Instrument("AAAUSDT", s, s)
    assert inst.turnover_before(T) == pytest.approx(24 * 200_000.0)
    del s.bars[T - 3 * HOUR_MS]
    assert Instrument("AAAUSDT", s, s).turnover_before(T) is None


def imp(symbol: str, d: int, z1: float = 5.0, zw: float = 0.0, turnover: float = 5e6,
        f: str = "false", g: str = "true") -> Impulse:  # fmt: skip
    return Impulse(symbol, d, z1, zw, turnover, f, g)


def test_match_applies_calipers_week_window_and_bin() -> None:
    event = imp("EVT", D, f="true")
    pool = [
        imp("A", D + HOUR_MS),  # nearest
        imp("B", D, z1=5.4),
        imp("C", D, zw=0.6),  # outside the waiting-hour caliper
        imp("D", D, z1=5.6),  # outside the impulse caliper
        imp("E", D, turnover=2e7),  # another turnover bin
        imp("EVT", D + 5 * HOUR_MS),  # the same instrument
        imp("F", D + 4 * DAY_MS),  # beyond 72 hours
    ]
    pairs = match([event], pool)
    assert [c.symbol for c in pairs[event.key]] == ["A", "B"]


def test_match_stays_inside_the_iso_week() -> None:
    monday = FIRST_DECISION_MS + 5 * DAY_MS  # 2024-10-21, a Monday
    event = imp("EVT", monday + HOUR_MS, f="true")
    pairs = match([event], [imp("A", monday - HOUR_MS), imp("B", monday + 2 * HOUR_MS)])
    assert [c.symbol for c in pairs[event.key]] == ["B"]


def test_match_keeps_five_nearest() -> None:
    event = imp("EVT", D, f="true")
    pool = [imp(f"S{k}", D, z1=5.0 + 0.05 * k) for k in range(8)]
    assert [c.symbol for c in match([event], pool)[event.key]] == [f"S{k}" for k in range(5)]


def test_shared_control_weight_sums_over_its_pairs() -> None:
    e1, e2 = imp("EVT", D, f="true"), imp("EVT", D + 2 * HOUR_MS, f="true")
    other = imp("OTH", D, f="true")
    control = imp("CTL", D)
    pairs = {e1.key: [control], e2.key: [control], other.key: []}
    weights = weights_excess([e1, e2, other], pairs)
    assert weights == {e1.key: 0.5, e2.key: 0.5, control.key: -1.0}
    sigma = dict.fromkeys(weights, 0.01)
    expected = sq.Z_POWER * math.sqrt((0.25 + 0.25 + 1.0) * (1.5 * 0.01) ** 2 * 1.2)
    assert mde(weights, sigma, 1.5, 1.2) == pytest.approx(expected)


def test_net_weights_are_equal_per_cluster() -> None:
    e1, e2, e3 = imp("A", D), imp("A", D + HOUR_MS), imp("B", D)
    assert weights_net([e1, e2, e3]) == {e1.key: 0.25, e2.key: 0.25, e3.key: 0.5}


def test_undefined_scale_takes_the_90th_percentile() -> None:
    scales = {("A", k): float(k) for k in range(1, 11)} | {("B", 0): None}
    out, missing = filled(scales)
    assert missing == 1
    assert out[("B", 0)] == 9.0


def test_scale_uses_only_returns_before_the_impulse() -> None:
    s = series()
    inst = Instrument("AAAUSDT", s, s)
    before = inst.scale(T, relative=False)
    s.bars[T + HOUR_MS] = (1e6, 1e6, 1e6, 1.0)
    assert Instrument("AAAUSDT", s, s).scale(T, relative=False) == before
    assert Instrument("AAAUSDT", s, s).scale(T, relative=True) is None  # zero relative spread


def shape_ok() -> dict[str, Any]:
    return {"clusters": 200, "symbols": 50, "iso_weeks": 40, "top5_symbol_clusters": 40}


CONTROLS_OK = {"events": 200, "events_with_control": 180}
POWERED = {"a_net": {"mde_unrounded": 0.004}, "b_excess": {"mde_unrounded": 0.0049}}


def test_floor_verdicts() -> None:
    assert floor(shape_ok(), CONTROLS_OK, (95, 100), POWERED)["verdict"] == "pass_to_reading"
    weak = POWERED | {"b_excess": {"mde_unrounded": 0.0051}}
    assert floor(shape_ok(), CONTROLS_OK, (95, 100), weak)["verdict"] == "insufficient_power"
    few = shape_ok() | {"clusters": 149}
    assert floor(few, CONTROLS_OK, (95, 100), POWERED)["verdict"] == "insufficient_data"
    no_b = POWERED | {"b_excess": None}
    assert floor(shape_ok(), CONTROLS_OK, (95, 100), no_b)["verdict"] == "insufficient_power"


def test_floor_compares_unrounded_fractions() -> None:
    # 11,050 / 12,278 = 89.9984%, which rounds to 90% at four places
    result = floor(shape_ok(), CONTROLS_OK, (11_050, 12_278), POWERED)
    assert result["data"]["determinable"] is False
    assert result["verdict"] == "insufficient_data"
    almost = {"events": 12_278, "events_with_control": 9_822}  # 79.9967%
    assert floor(shape_ok(), almost, (95, 100), POWERED)["data"]["control_share"] is False
    over = shape_ok() | {"clusters": 12_278, "top5_symbol_clusters": 3_684}  # 30.0049%
    assert floor(over, CONTROLS_OK, (95, 100), POWERED)["data"]["top5_symbols"] is False


def test_no_controls_renders_not_estimable() -> None:
    s = series()
    inst = Instrument("AAAUSDT", s, s)
    events = [imp("AAAUSDT", D, f="true")]
    pairs: dict[tuple[str, int], list[Impulse]] = {events[0].key: []}
    mdes = power({"AAAUSDT": inst}, events, pairs)
    assert mdes["b_excess"] is None
    controls = control_shape(events, pairs)
    report = {
        "funnel": {"first_true": 1},
        "cells": {"event": shape(events)},
        "controls": controls,
        "power": mdes,
        "floor": floor(shape(events), controls, (1, 1), mdes),
        "fg_determinable_share": 1.0,
    }
    text = render_markdown(report)
    assert "not estimable" in text
    assert "insufficient_data" in text


def test_main_refuses_another_snapshot() -> None:
    with pytest.raises(SystemExit, match="pins"):
        sq.main(["counts", "--features", "x", "--features-sha256", "0" * 64,
                 "--output-dir", "out", "--code-revision", "abc",
                 "--no-working-tree-dirty"])  # fmt: skip
