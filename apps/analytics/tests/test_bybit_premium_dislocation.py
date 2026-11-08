from __future__ import annotations

import math
from collections import Counter

import pytest
from schurfer_analytics import bybit_premium_dislocation as pd
from schurfer_analytics import bybit_squeeze_continuation as sq
from schurfer_analytics.bybit_compressed_spring import (
    DAY_MS,
    FIRST_DECISION_MS,
    HOUR_MS,
    WINDOW_FIRST_MS,
    Series,
)
from schurfer_analytics.bybit_premium_dislocation import (
    Hour,
    balance,
    components,
    dislocation,
    first,
    match,
    mde_overlap,
    power,
    render_markdown,
    scan,
)
from schurfer_analytics.bybit_squeeze_continuation import Instrument, control_shape, floor, shape

T = FIRST_DECISION_MS + 5 * DAY_MS  # a decision hour
SPAN_END = T + 3 * DAY_MS


def series(turnover: float = 200_000.0) -> Series:
    """Hourly closes alternating +-0.4%."""
    s = Series(0, SPAN_END)
    price = 100.0
    for k, start in enumerate(range(WINDOW_FIRST_MS, SPAN_END, HOUR_MS)):
        price *= 1.004 if k % 2 else 0.996
        s.bars[start] = (price, price, price, turnover)
    return s


def premium(*deep: int, value: float = -0.008) -> dict[int, float]:
    """Premium closes cycling over 0, +-0.02%, +-0.04%, with value at each bar ending in deep."""
    cycle = (0.0, 0.0002, -0.0002, 0.0004, -0.0004)
    out = {
        start: cycle[k % len(cycle)]
        for k, start in enumerate(range(WINDOW_FIRST_MS, SPAN_END, HOUR_MS))
    }
    for t in deep:
        out[t - HOUR_MS] = value
    return out


def run(prem: dict[int, float], s: Series | None = None) -> tuple[list[Hour], Counter[str]]:
    funnel: Counter[str] = Counter()
    s = s or series()
    inst = Instrument("AAAUSDT", s, series())
    return scan(inst, prem, funnel, first_t=T - DAY_MS, last_t=T + 2 * DAY_MS), funnel


def test_dislocation_statuses() -> None:
    assert dislocation(None, 0.01, "neg") == "premium_gap"
    assert dislocation(-0.004, None, "neg") == "false"
    assert dislocation(-0.006, None, "neg") == "rank_history_short"
    assert dislocation(-0.006, 0.04, "neg") == "true"
    assert dislocation(-0.006, 0.06, "neg") == "false"
    assert dislocation(0.006, 0.96, "pos") == "true"
    assert dislocation(-0.006, 0.01, "pos") == "false"


def test_a_deep_discount_is_an_event_and_the_next_hour_is_not_first() -> None:
    hours, funnel = run(premium(T, T + HOUR_MS))
    events = [h for h in hours if h.role == "event"]
    assert [e.d for e in events] == [T]
    assert funnel["neg_first_false"] == 1
    assert all(h.role == "ordinary" for h in hours if h.role != "event")


def test_the_bar_after_t_is_not_read_at_t() -> None:
    prem = premium()
    prem[T] = -0.009  # the bar [t, t + 1 h) closes after t
    hours, _ = run(prem)
    assert [h.d for h in hours if h.role == "event"] == [T + HOUR_MS]


def test_positive_side_is_v1() -> None:
    hours, _ = run(premium(T, value=0.008))
    assert [h.d for h in hours if h.role == "v1_event"] == [T]
    assert not [h for h in hours if h.role == "event"]


def test_ordinary_pool_excludes_hours_after_a_dislocation() -> None:
    hours, _ = run(premium(T))
    ordinary = {h.d: h for h in hours if h.role == "ordinary"}
    assert not ordinary[T + 24 * HOUR_MS].pool_neg
    assert ordinary[T + 25 * HOUR_MS].pool_neg
    assert ordinary[T + 2 * HOUR_MS].pool_pos


def test_first_is_undefined_after_a_gap() -> None:
    statuses = {T - k * HOUR_MS: "false" for k in range(1, 25)}
    assert first(statuses, T) == "true"
    del statuses[T - 3 * HOUR_MS]
    assert first(statuses, T) == "premium_gap"


def test_liquidity_counts_the_bar_ending_t() -> None:
    s = series(turnover=80_000.0)
    s.bars[T - HOUR_MS] = (*s.bars[T - HOUR_MS][:3], 1_000_000.0)  # the bar [t - 1 h, t)
    inst = Instrument("AAAUSDT", s, s)
    assert inst.turnover_before(T + HOUR_MS) == pytest.approx(23 * 80_000.0 + 1_000_000.0)
    hours, _ = run(premium(T), s)
    assert [h.d for h in hours if h.role == "event"] == [T]


def hour(symbol: str, d: int, z4: float = -2.0, r1: float = -0.01, turnover: float = 5e6,
         role: str = "ordinary") -> Hour:  # fmt: skip
    return Hour(symbol, d, role, r1, 0.0, z4, turnover, pool_neg=True)


def test_match_applies_caliper_direction_week_window_and_bin() -> None:
    event = hour("EVT", T, role="event")
    pool = [
        hour("A", T + HOUR_MS),  # nearest
        hour("B", T, z4=-2.4),
        hour("C", T, z4=-2.6),  # outside the caliper
        hour("D", T, r1=0.002),  # the last hour was not down
        hour("E", T, turnover=2e7),  # another turnover bin
        hour("EVT", T + 5 * HOUR_MS),  # the same instrument
        hour("F", T + 4 * DAY_MS),  # beyond 72 hours
    ]
    assert [c.symbol for c in match([event], pool)[event.key]] == ["A", "B"]


def test_match_keeps_five_nearest() -> None:
    event = hour("EVT", T, role="event")
    pool = [hour(f"S{k}", T, z4=-2.0 - 0.05 * k) for k in range(8)]
    assert [c.symbol for c in match([event], pool)[event.key]] == [f"S{k}" for k in range(5)]


def test_components_chain_overlapping_holds() -> None:
    units = [("X", T), ("X", T + 3 * HOUR_MS), ("X", T + 6 * HOUR_MS), ("X", T + 10 * HOUR_MS),
             ("Y", T)]  # fmt: skip
    groups = components(units)
    assert sorted(len(g) for g in groups) == [1, 1, 3]


def test_overlap_bound_sums_weights_inside_a_component() -> None:
    sigma = 0.01
    single = {("X", T): 0.5, ("Y", T): -0.5}
    scales = dict.fromkeys(single, sigma)
    assert mde_overlap(single, scales, 1.5, 1.2) == pytest.approx(sq.mde(single, scales, 1.5, 1.2))
    shared = {("E", T): 1.0, ("X", T): -0.5, ("X", T + HOUR_MS): -0.5}
    scales = dict.fromkeys(shared, sigma)
    expected = sq.Z_POWER * math.sqrt(((1.0 * 1.5 * sigma) ** 2 + (1.5 * sigma) ** 2) * 1.2)
    assert mde_overlap(shared, scales, 1.5, 1.2) == pytest.approx(expected)
    assert mde_overlap(shared, scales, 1.5, 1.2) > sq.mde(shared, scales, 1.5, 1.2)


def test_balance_weighs_controls_by_their_pairs() -> None:
    e = Hour("EVT", T, "event", -0.02, -0.04, -2.0, 5e6)
    c1 = Hour("A", T, "ordinary", -0.01, -0.03, -2.0, 5e6)
    c2 = Hour("B", T, "ordinary", -0.03, -0.05, -2.0, 5e6)
    result = balance([e], {e.key: [c1, c2]})
    assert result is not None
    assert result["r1"]["event_mean_bps"] == -200.0
    assert result["r1"]["control_mean_bps"] == -200.0
    assert result["r4"]["smd"] == 0.0


def test_no_controls_renders_not_estimable() -> None:
    s = series()
    inst = Instrument("AAAUSDT", s, s)
    events = [Hour("AAAUSDT", T, "event", -0.01, -0.02, -2.0, 5e6)]
    pairs: dict[tuple[str, int], list[Hour]] = {events[0].key: []}
    mdes = power({"AAAUSDT": inst}, events, pairs)
    assert mdes["b_excess"] is None
    events_shape, controls = shape(events), control_shape(events, pairs)
    part = {"events": events_shape, "controls": controls, "balance": None, "power": mdes}
    report = {
        "funnel": {"grid": 1},
        "primary": part,
        "v1": part,
        "floor": floor(events_shape, controls, (1, 1), mdes),
        "premium_determinable_share": 1.0,
    }
    text = render_markdown(report)
    assert "not estimable" in text
    assert "insufficient_data" in text


def test_counts_refuse_an_unpinned_premium_snapshot() -> None:
    with pytest.raises(SystemExit, match="pinned in code"):
        pd.main(["counts", "--features", "x", "--features-sha256", sq.FEATURES_SHA256,
                 "--premium", "y", "--premium-sha256", "0" * 64, "--output-dir", "out",
                 "--code-revision", "abc", "--no-working-tree-dirty"])  # fmt: skip


def test_main_refuses_another_features_snapshot() -> None:
    with pytest.raises(SystemExit, match="pins"):
        pd.main(["fetch-premium", "--features", "x", "--features-sha256", "0" * 64,
                 "--snapshot", "out.jsonl.gz"])  # fmt: skip
