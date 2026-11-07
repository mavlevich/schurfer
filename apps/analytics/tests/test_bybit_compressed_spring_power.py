from __future__ import annotations

import json
import math
from collections import Counter

import pytest
from schurfer_analytics import bybit_compressed_spring_power as pw
from schurfer_analytics.bybit_compressed_spring import (
    DAY_MS,
    FIRST_DECISION_MS,
    HOUR_MS,
    Features,
    Hour,
    Series,
    _week,
)
from schurfer_analytics.bybit_compressed_spring_cond4 import Decision

T0 = FIRST_DECISION_MS + 20 * DAY_MS


def _bars(closes: dict[int, float]) -> Series:
    """Hourly bars keyed by start; close_at(t) reads the bar starting t - 1 h."""
    return Series(bars={t - HOUR_MS: (c, c, c, 1e5) for t, c in closes.items()})


def test_hold_return_and_funding_use_closes_at_t_and_t_plus_h() -> None:
    series = _bars({T0: 100.0, T0 + 2 * HOUR_MS: 110.0, T0 + 4 * HOUR_MS: 120.0})
    series.funding = {T0: -0.01, T0 + 2 * HOUR_MS: -0.001, T0 + 4 * HOUR_MS: 0.002}
    times = sorted(series.funding)
    assert pw.hold_return(series, T0, 4) == pytest.approx(0.20)
    # the settlement at t is before the entry; t + 2 h and t + 4 h are inside the hold
    expected = 0.001 * 110 / 100 - 0.002 * 120 / 100
    assert pw.hold_funding(times, series, T0, 4) == pytest.approx(expected)
    assert pw.net_return(times, series, T0, 4) == pytest.approx(0.20 - 0.003 + expected)
    assert pw.hold_return(series, T0, 12) is None


def test_dispersion_is_the_week_block_error_and_returns_no_mean() -> None:
    values = [0.01, 0.03, -0.02, 0.00]
    weeks = ["W1", "W1", "W2", "W2"]
    d = pw.dispersion(values, weeks)
    assert d is not None
    centre = sum(values) / 4
    blocks = [values[0] + values[1] - 2 * centre, values[2] + values[3] - 2 * centre]
    assert d.se == pytest.approx(math.sqrt(sum(b * b for b in blocks) / 16 * 2))
    assert d.design_effect == pytest.approx(d.se**2 * 4 / d.sd**2)
    assert d.mde == pytest.approx(2.4865 * d.se, rel=1e-4)
    assert not any("mean" in name for name in vars(d))
    assert pw.dispersion([0.1, 0.2], ["W1", "W1"]) is None  # one week


def test_independent_weeks_have_a_design_effect_near_one() -> None:
    rng = pw.seeded(str(1))
    values = [rng.gauss(0, 0.02) for _ in range(4000)]
    weeks = [f"W{i % 40}" for i in range(4000)]
    d = pw.dispersion(values, weeks)
    assert d is not None and 0.7 < d.design_effect < 1.3


def _decision(symbol: str, t: int) -> Decision:
    return Decision(Hour(symbol, t, "liquid", "true", "true", "true"), 0.5, 0.9, 0.01, 0.1, 1e6)


D1 = T0 + DAY_MS


def test_plan_keeps_only_weeks_days_hours_and_symbol_identities() -> None:
    events = [_decision("A", T0), _decision("B", T0), _decision("A", T0 + 2 * HOUR_MS)]
    (week,) = pw.plan(events, 4)
    assert week.week == _week(T0) and T0 in week.days
    i = week.days.index(T0)
    assert week.events == (("A", i, 0), ("B", i, 0), ("A", i, 2 * HOUR_MS))


def _plan_for(events: list[tuple[str, int, int]]) -> pw.WeekPlan:
    days = tuple(T0 + k * DAY_MS for k in range(3))
    return pw.WeekPlan(_week(T0), days, tuple(events))


def test_transplant_moves_whole_days_and_keeps_groups_and_symbol_days() -> None:
    p = _plan_for([("A", 0, 0), ("B", 0, 0), ("A", 0, 2 * HOUR_MS)])
    pool = {T0 + k * HOUR_MS: ["X", "Y", "Z"] for k in range(72)}
    for seed in range(30):
        out = list(pw.transplant(pw.seeded(str(seed)), [p], pool, lambda s, t: False, 10**15))
        (ca, _, na, pa), (cb, _, nb, pb) = out
        assert ca == ("A", T0) and cb == ("B", T0) and (na, nb) == (2, 1)
        assert pa is not None and pb is not None
        (s1, t1), (s3, t3) = pa
        ((s2, t2),) = pb
        assert t1 == t2 and t3 == t1 + 2 * HOUR_MS
        assert t1 - t1 % DAY_MS != T0  # never the events' own day
        assert s1 == s3 != s2  # one instrument per event symbol-day, distinct in the week


def test_a_symbol_day_is_lost_whole_rather_than_split() -> None:
    p = _plan_for([("A", 0, 0), ("A", 0, 2 * HOUR_MS)])
    pool: dict[int, list[str]] = {}
    for k in (1, 2):  # X only at the first hour, Y only at the second
        pool[T0 + k * DAY_MS] = ["X"]
        pool[T0 + k * DAY_MS + 2 * HOUR_MS] = ["Y"]
    ((cluster, _, count, picks),) = pw.transplant(
        pw.seeded("0"), [p], pool, lambda s, t: False, 10**15
    )
    assert cluster == ("A", T0) and count == 2 and picks is None


def test_two_source_clusters_never_share_one_instrument_in_a_week() -> None:
    p = _plan_for([("A", 0, 0), ("B", 0, 3 * HOUR_MS)])  # same day, different hours
    pool = {T0 + k * DAY_MS + h * HOUR_MS: ["X"] for k in range(3) for h in (0, 3)}
    out = list(pw.transplant(pw.seeded("0"), [p], pool, lambda s, t: False, 10**15))
    assert out[0][3] is not None and out[1][3] is None  # X belongs to A this week
    p = _plan_for([("A", 0, 0), ("A", 1, 0)])  # one symbol on two days keeps X
    out = list(pw.transplant(pw.seeded("0"), [p], pool, lambda s, t: False, 10**15))
    assert [x[3][0][0] for x in out if x[3]] == ["X", "X"]


def test_transplant_gives_none_when_every_instrument_is_blocked_or_used() -> None:
    p = _plan_for([("A", 0, 0), ("B", 0, 0)])
    pool = {T0 + k * DAY_MS: ["X"] for k in range(3)}
    out = list(pw.transplant(pw.seeded("0"), [p], pool, lambda s, t: False, 10**15))
    assert out[0][3] is not None and out[1][3] is None  # X is used once per hour
    out = list(pw.transplant(pw.seeded("0"), [p], pool, lambda s, t: True, 10**15))
    assert [x[3] for x in out] == [None, None]


def test_blocked_keeps_draws_off_every_event_path() -> None:
    u = pw.Universe(Features({}, ()))
    u.event_times = {"A": [T0]}
    assert u.blocked("A", T0 + 23 * HOUR_MS, 4)  # inside the event's 24 h path
    assert u.blocked("A", T0 + 24 * HOUR_MS, 4)  # within the one-hour margin
    assert not u.blocked("A", T0 + 25 * HOUR_MS, 4)
    assert u.blocked("A", T0 - 4 * HOUR_MS, 4)  # its own hold ends within an hour of t_e
    assert not u.blocked("A", T0 - 5 * HOUR_MS, 4)
    assert u.blocked("A", T0 - 20 * HOUR_MS, 24)
    assert not u.blocked("B", T0, 24)


def _universe() -> pw.Universe:
    closes = {D1 + k * HOUR_MS: 100.0 + k for k in range(-90, 40)}
    instruments = {s: _bars(closes) for s in ("A", "B", "C")}
    u = pw.Universe(Features(instruments, ()))
    u.times = {s: [] for s in instruments}
    u.liquid = {D1: ["A"]}
    return u


ONE_DAY_ON = [pw.WeekPlan(_week(T0), (T0, D1), (("C", 0, 0),))]  # always moved onto D1


def test_net_draws_use_the_moved_hour_and_skip_blocked_paths() -> None:
    u = _universe()
    out = pw.net_draws(u, pw.seeded("3"), 4, ONE_DAY_ON)
    assert out.drawn == 1 and out.clusters()[0] == [pytest.approx(104 / 100 - 1 - 0.003)]
    u.event_times = {"A": [D1 + 2 * HOUR_MS]}
    out = pw.net_draws(u, pw.seeded("3"), 4, ONE_DAY_ON)
    assert out.lost == 1 and not out.cells


def test_excess_is_own_return_minus_the_unblocked_controls_mean() -> None:
    u = _universe()
    u.compressed = {D1: {s: Hour(s, D1, "liquid", "true", "false", "true") for s in "AB"}}
    u.rising = {D1: ["A"]}
    wobble = {D1 + k * HOUR_MS: 1000.0 + k + (k % 2) * 0.5 for k in range(-400, 1)}
    after_a = {D1 + k * HOUR_MS: 1000.0 + k for k in range(1, 40)}
    after_b = {D1 + k * HOUR_MS: 1000.0 + 2 * k for k in range(1, 40)}
    u.features.instruments["A"] = _bars(wobble | after_a)
    u.features.instruments["B"] = _bars(wobble | after_b)  # same price state before t
    out = pw.excess_draws(u, pw.seeded("0"), 4, ONE_DAY_ON)
    assert out.drawn == 1 and out.no_control == 0
    assert out.clusters()[0] == [pytest.approx(1004 / 1000 - 1008 / 1000)]
    u.event_times = {"B": [D1 - 10 * HOUR_MS]}  # the control's path meets an event's
    out = pw.excess_draws(u, pw.seeded("0"), 4, ONE_DAY_ON)
    assert out.no_control == 1 and not out.cells


def test_draws_average_within_a_source_cluster_first() -> None:
    d = pw.Draws()
    week = _week(T0)
    d.add(("A", T0), week, 0.01)
    d.add(("A", T0), week, 0.03)
    d.add(("B", T0), week, 0.10)
    assert d.clusters() == ([pytest.approx(0.02), 0.10], [week, week])


def _runs(n_runs: int, clusters: int) -> list[pw.Draws]:
    rng = pw.seeded("2")
    runs = []
    for _ in range(n_runs):
        d = pw.Draws(drawn=clusters)
        for i in range(clusters):
            t = T0 + (i % 30) * 7 * DAY_MS
            d.add((f"S{i}", t), _week(t), rng.gauss(0.01, 0.02))
        runs.append(d)
    return runs


def test_a_nearly_empty_leg_is_not_estimable() -> None:
    runs = _runs(19, 2) + _runs(1, 300)  # one full replicate, the rest almost empty
    out = pw.summarize(runs, event_clusters=300, usable_share=0.9)
    assert out["usable_replicates"] == 1 and out["estimable"] is False
    full = pw.summarize(_runs(20, 300), event_clusters=300, usable_share=0.9)
    assert full["estimable"] is True and full["mde"] > 0
    assert not any("mean" in k for k in full) and "mean" not in json.dumps(full)


def _h(floor: bool, a: float | None, b: float | None, est: bool = True) -> dict[str, object]:
    return {
        "floor_met": floor,
        "a_net": {"estimable": est, "mde": a},
        "b_excess": {"estimable": est, "mde": b},
    }


def test_choose_h_takes_the_shortest_testable_horizon_unrounded() -> None:
    assert pw.choose_h({"4h": _h(True, 0.004, 0.003), "12h": _h(True, 0.003, 0.003)}) == "4h"
    assert pw.choose_h({"4h": _h(True, 0.004, 0.0056), "12h": _h(True, 0.004, 0.002)}) == "12h"
    assert pw.choose_h({"4h": _h(True, 0.001, 0.001, est=False)}) == "insufficient_power"
    assert pw.choose_h({"4h": _h(True, 0.006, 0.004), "12h": _h(False, 0.003, 0.003)}) == (
        "insufficient_power"
    )
    assert pw.choose_h({"4h": _h(False, 0.001, 0.001)}) == "insufficient_data"


def test_a_shorter_h_near_50_bps_holds_the_choice_for_the_minute_check() -> None:
    near_above = _h(True, 0.0030, 0.0052)  # 52 bps: minute bars could bring it under
    assert pw.near_threshold(near_above) is True
    assert pw.choose_h({"4h": near_above, "12h": _h(True, 0.004, 0.003)}) == (
        "pending_minute_check"
    )
    assert pw.choose_h({"4h": _h(True, 0.0048, 0.003)}) == "pending_minute_check"
    far = _h(True, 0.0080, 0.0052)  # one leg far above: the check cannot change it
    assert pw.near_threshold(far) is False
    assert pw.choose_h({"4h": far, "12h": _h(True, 0.004, 0.003)}) == "12h"
    assert pw.near_threshold(_h(True, 0.0049, 0.001, est=False)) is False
    assert pw.near_threshold(_h(True, 0.0045, 0.001)) is True  # both bounds included
    assert pw.near_threshold(_h(True, 0.0055, 0.001)) is True
    assert pw.near_threshold(_h(True, 0.00449, 0.001)) is False
    assert pw.near_threshold(_h(True, 0.00551, 0.001)) is False


def test_the_usable_share_is_set_by_the_rule() -> None:
    runs = _runs(20, 255)  # 85% of 300 source clusters
    assert pw.summarize(runs, 300, pw.RULES["original"].usable_b)["estimable"] is False
    assert pw.summarize(runs, 300, pw.RULES["amended-2026-11-07"].usable_b)["estimable"] is True
    assert pw.RULES["amended-2026-11-07"].usable_a == 0.90


def test_loss_concentration_on_symbols_and_weeks() -> None:
    weeks = [T0 + k * 7 * DAY_MS for k in range(10)]
    source = [(f"S{i}", weeks[i % 10]) for i in range(100)]
    spread = pw.Draws()
    for i, cluster in enumerate(source):
        if i % 10 != 0:  # one lost per week, ten symbols
            spread.add(cluster, _week(cluster[1]), 0.0)
    out = pw.loss_concentration([spread], source)
    assert out["lost_cluster_replicates"] == 10 and out["concentrated"] is True  # 5 of 10 = 50%
    many = [pw.Draws() for _ in range(1)]
    for i, cluster in enumerate(source):
        if i >= 40:
            many[0].add(cluster, _week(cluster[1]), 0.0)
    out = pw.loss_concentration(many, source)  # 40 lost over 40 symbols and 10 weeks
    assert out["top5_symbols_share"] == 0.125 and out["top5_weeks_share"] == 0.5
    assert out["concentrated"] is True
    entry = {
        "floor_met": True,
        "loss_check": True,
        "b_loss": out,
        "a_net": {"estimable": True, "mde": 0.001},
        "b_excess": {"estimable": True, "mde": 0.001},
    }
    assert pw.verdict(entry) == "loss_concentrated"
    assert pw.verdict(entry | {"loss_check": False}) == "testable"


def test_week_weights_restore_each_weeks_event_share() -> None:
    weeks = ["W1", "W1", "W2"]
    w = pw.week_weights(weeks, Counter({"W1": 2, "W2": 4}))
    assert w == [1.0, 1.0, 4.0]  # W2 kept 1 of its 4 clusters


def test_weighted_dispersion_equals_equal_weights_when_nothing_is_lost() -> None:
    values = [0.01, 0.03, -0.02, 0.00, 0.05, -0.01]
    weeks = ["W1", "W1", "W2", "W2", "W3", "W3"]
    plain = pw.dispersion(values, weeks)
    same = pw.dispersion(values, weeks, pw.week_weights(weeks, Counter(weeks)))
    assert plain is not None and same is not None and same.se == pytest.approx(plain.se)
    heavy = pw.dispersion(values, weeks, [1, 1, 1, 1, 5, 5])
    assert heavy is not None and heavy.se != pytest.approx(plain.se)


def test_the_corrected_rule_decides_b_on_its_weighted_effect() -> None:
    rule = pw.RULES["corrected-2026-11-07"]
    assert (rule.usable_a, rule.usable_b, rule.loss_check, rule.reweight) == (
        0.90, 0.80, False, True,
    )  # fmt: skip
    runs = _runs(20, 300)
    out = pw.summarize(
        runs, 300, rule.usable_b, Counter(_week(T0 + (i % 30) * 7 * DAY_MS) for i in range(300))
    )
    assert out["mde_weighted"] == pytest.approx(out["mde"])  # nothing lost: same effect
    assert out["event_weeks_without_value_median"] == 0
    assert "mde_weighted" not in pw.summarize(runs, 300, rule.usable_b)
