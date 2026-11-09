from __future__ import annotations

import math

import pytest
from schurfer_analytics import bybit_absorbed_buying as v1
from schurfer_analytics import bybit_absorbed_buying_match_check as mc
from schurfer_analytics.bybit_absorbed_buying import Member, State
from schurfer_analytics.bybit_compressed_spring import Features

from .test_bybit_absorbed_buying import T, member, series, state, tape


def test_the_check_rule_is_fixed_as_in_the_note() -> None:
    assert mc.RULES["check"] == mc.Rule((0.15, 0.85), 0.25, by_tercile=False)
    assert mc.RULES["v1"] == mc.Rule((0.25, 0.75), 0.5, by_tercile=True)
    assert mc.BALANCED == ("z1", "z4", "ln_sigma1", "ln_turnover")


def test_rule_admits_the_wider_band_only() -> None:
    check, old = mc.RULES["check"], mc.RULES["v1"]
    assert check.admits(state(qb=0.16)) and not old.admits(state(qb=0.16))
    assert check.admits(state(qb=0.85)) and not check.admits(state(qb=0.86))
    assert not check.admits(state(qb=0.5, qv=0.4))
    assert not check.admits(state(qb=0.5, z1=1.1))
    undefined = State("E", T, None, None, None, None, None, None, None, "tape_missing")
    assert not check.admits(undefined)
    assert mc.wide(state(qb=0.15)) and not mc.wide(state(qb=0.9))


def test_match_across_terciles_with_the_tighter_z1_caliper() -> None:
    event = member("E", tercile=0, z1=0.3, z4=0.1)
    near_other_tercile = member("A", tercile=2, qb=0.5, z1=0.45, z4=0.1)
    far_same_tercile = member("B", tercile=0, qb=0.5, z1=0.75, z4=0.1)
    old = v1.match([event], [near_other_tercile, far_same_tercile])
    assert [c.symbol for c in old[event.key]] == ["B"]
    new = v1.match([event], [near_other_tercile, far_same_tercile], z1_caliper=0.25,
                   by_tercile=False)  # fmt: skip
    assert [c.symbol for c in new[event.key]] == ["A"]


def test_smd_matches_v1_balance() -> None:
    e, c1, c2 = member("E", z1=0.4), member("A", qb=0.5, z1=0.1), member("B", qb=0.5, z1=0.0)
    weights = {e.key: 1.0, c1.key: -0.5, c2.key: -0.5}
    members: dict[tuple[str, int], Member] = {m.key: m for m in (e, c1, c2)}
    old = v1.balance(weights, members)["z1"]["smd_unrounded"]
    assert mc.smd(weights, lambda k: members[k].state.z1 or 0.0) == pytest.approx(old)


def test_reuse_counts_distinct_controls_and_the_largest_share() -> None:
    out = mc.reuse({("E", 1): 1.0, ("A", 1): -0.75, ("B", 1): -0.25}, slots=3)
    assert out == {"distinct_controls": 2, "pair_slots": 3, "slots_per_distinct_control": 1.5,
                   "largest_control_weight_share": 0.75}  # fmt: skip


def test_build_runs_both_rules_on_blocks() -> None:
    instruments = {"BTCUSDT": series(), **{f"S{k}": series(phase=k) for k in range(4)}}
    features = Features(instruments, ())
    tapes = {f"S{k}": tape(buy=1.0 + k) for k in range(4)}
    report, frozen = mc.build(features, tapes)
    assert report["v1_reproduced"] is False and report["verdict"] == "v1_not_reproduced"
    assert set(report["rules"]) == {"v1", "check"}
    assert report["rules"]["check"]["candidates"] >= report["rules"]["v1"]["candidates"]
    assert len(frozen["events"]) == report["events"]
    turnover = mc.turnovers(features, v1.pools_of(features))
    assert all(math.isfinite(v) and v > 0 for v in turnover.values())


def test_different_means_with_no_spread_never_pass_the_balance_gate() -> None:
    e, c = member("E", z1=0.4), member("A", qb=0.5, z1=0.0)
    weights = {e.key: 1.0, c.key: -1.0}
    assert mc.smd(weights, lambda k: 0.4 if k == e.key else 0.0) is None
    assert mc.smd(weights, lambda k: 0.2) == 0.0
    out = v1.balance(weights, {e.key: e, c.key: c})
    assert out["z1"]["smd"] is None and not v1.balance_passes(out)
    turnover = {(v1.month_of(e.d), "E"): 1e6, (v1.month_of(c.d), "A"): 1e6}
    bal = mc.balance(weights, {e.key: e, c.key: c}, turnover)
    assert bal is not None and bal["z1"] is None and bal["passes"] is False


def test_two_constant_groups_are_decided_before_weighting() -> None:
    weights = {("E", 1): 0.1, ("F", 1): 0.9, ("A", 1): -0.2, ("B", 1): -0.8}
    assert mc.smd(weights, lambda k: 0.1) == 0.0
    assert mc.smd(weights, lambda k: 0.1 if k[0] in "EF" else 0.2) is None
