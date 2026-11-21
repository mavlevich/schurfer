"""I25 Stage 1b body verdicts: thresholds at their exact boundaries and the stop-code order.

The verdict module reads sealed metadata facts and receipts only. These tests build both by
hand (no network, no mock Bybit) so that each boundary of protocol sections 4 and 8 is hit
exactly: 10% against 10% plus one launch, 5% against 5% plus one, 95%, 100 episodes, 26 weeks.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from schurfer_analytics import new_perp_stage1 as s
from schurfer_analytics import new_perp_stage1b_body as body
from schurfer_analytics import new_perp_stage1b_body_verdict as v
from schurfer_analytics.new_perp_window import HOLDOUT

from .new_perp_body_world import holdout_world, sealed_row, slot_of
from .new_perp_world import ms

HOUR = s.HOUR_MS
EIGHT = 8 * HOUR


def report_stub(
    n: int, *, share: float | None = 0.0, near: int = 0, **extra: Any
) -> dict[str, Any]:
    return {
        "catalog_count": n,
        "m2_i_no_file_near_launch": near,
        "m2_i_unproven": 0,
        "m2ii": {
            "share": share,
            "archive_only_in_window": 0,
            "archive_only_pre_window": 0,
            "unverified_directories": 0,
            "collision_directories": 0,
        },
        **extra,
    }


def ok_first(slot: str, row: dict[str, Any], delay_ns: int = 600_000_000_000) -> dict[str, Any]:
    t0 = row["launch_ms"] * 1_000_000 + delay_ns
    facts = {"rows": 5, "min_ns": t0, "max_ns": t0 + 5_000_000_000}
    return {"outcome": "ok", "facts": facts, "meta": {"slot": slot}}


def hourly(c: body.Candidate, **change: Any) -> dict[str, Any]:
    first = (c.entry_ms // HOUR) * HOUR - 3 * HOUR
    last = ((c.exit_ms - 1) // HOUR) * HOUR
    facts = {
        "n": (last - first) // HOUR + 1,
        "oldest_ms": first,
        "newest_ms": last,
        "runs": [],
        "runs_truncated": False,
        "full": False,
        **change,
    }
    return {"outcome": "ok", "facts": facts, "meta": {"slot": c.slot}}


def entry(c: body.Candidate, *, present: bool = True) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "n": 1 if present else 0,
        "oldest_ms": c.entry_ms if present else None,
        "newest_ms": c.entry_ms if present else None,
        "runs": [],
        "runs_truncated": False,
        "full": False,
    }
    return {"outcome": "ok", "facts": facts, "meta": {"slot": c.slot}}


def funding_page(c: body.Candidate, **change: Any) -> dict[str, Any]:
    start, end = HOLDOUT.funding_span(c.t0_ms)
    facts = {
        "n": 54,
        "oldest_ms": start,
        "newest_ms": end,
        "max_gap_ms": EIGHT,
        "full": False,
        **change,
    }
    return {"outcome": "ok", "facts": facts, "meta": {"slot": c.slot, "page": "1"}}


def make(n: int = 130, *, final: bool = True) -> v.BodyData:
    """A clean synthetic holdout: n launches two days apart, every check covered."""
    world = holdout_world(n)
    rows = {slot_of(x.symbol): sealed_row(x) for x in world.launches}
    data = v.BodyData(report=report_stub(n), rows=rows)
    for slot, row in rows.items():
        data.firsts[slot] = ok_first(slot, row)
    if final:
        add_bars(data)
    return data


def add_bars(data: v.BodyData) -> None:
    queued = {slot for slot, r in data.rows.items() if body.has_size(r)}
    for c in body.entry_candidates(data.rows, data.firsts, queued):
        data.entry_bar[c.slot] = entry(c)
        data.mark_hourly[c.slot] = hourly(c)
        data.trade_hourly[c.slot] = hourly(c)
        data.funding[c.slot] = [funding_page(c)]


def slots(data: v.BodyData) -> list[str]:
    return sorted(data.rows, key=lambda slot: data.rows[slot]["launch_ms"])


def evaluate(data: v.BodyData, phase: str = "final") -> dict[str, Any]:
    return v.evaluate(copy.deepcopy(data), phase)


def test_a_clean_holdout_is_a_partial_coverage_pass_and_says_so() -> None:
    report = evaluate(make())
    assert report["outcome"] == "stage1b_pass_partial" and report["stop_codes"] == []
    assert report["not_assessed"] == [] and report["n_primary"] == 130
    assert report["reasons"]["primary"] == 130 and sum(report["reasons"].values()) == 130
    assert "not an economic result" in report["statement"]
    assert report["m4"]["status"] == "partial_pass" and report["m6"]["status"] == "partial_pass"
    assert len(report["partial_checks"]) >= 6


def test_go_a_report_decides_only_what_no_later_request_can_change() -> None:
    report = evaluate(make(final=False), "a")
    assert report["outcome"] == "stage1b_a_complete" and report["go_b_allowed"] is True
    assert report["n_primary"] is None and report["m4"] is None and report["m5"] is None
    assert report["entry_candidates"] == 130 and report["m6"]["provisional"] is True
    assert {"m4_funding_go_b", "m5_bars_go_b", "n_primary_go_b"} <= set(report["not_assessed"])


@pytest.mark.parametrize(("ambiguous", "passes"), [(2, True), (3, False)])
def test_m1_ambiguous_share_is_exactly_ten_percent(ambiguous: int, passes: bool) -> None:
    """10% passes, 10% plus one launch fails: 2 of 20 against 3 of 20."""
    data = make(20, final=False)
    for slot in slots(data)[:ambiguous]:
        data.firsts[slot] = ok_first(slot, data.rows[slot], delay_ns=30 * 3_600_000_000_000)
    m1 = evaluate(data, "a")["m1"]
    assert m1["pass"] is passes and m1["fail"] is (not passes)
    assert m1["classes"]["premarket_or_ambiguous"] == ambiguous


@pytest.mark.parametrize(("unreadable", "passes"), [(2, True), (3, False)])
def test_m1_readable_and_lifecycle_shares(unreadable: int, passes: bool) -> None:
    """Unreadable first files lower the readable share: 90% is exact, one more fails."""
    data = make(20, final=False)
    for slot in slots(data)[:unreadable]:
        data.firsts[slot] = {"outcome": "unavailable", "facts": None, "meta": {"slot": slot}}
    report = evaluate(data, "a")
    assert report["m1"]["pass"] is passes
    assert report["m1"]["shares"]["lifecycle_unrebuildable"] == unreadable / 20
    assert ("not_reconstructible" in report["stop_codes"]) is (not passes)


def test_m1_unreadable_metadata_states_count_as_lifecycle_losses() -> None:
    data = make(20, final=False)
    states = ("no_archive", "no_pre_cut_file", "t0_pre_window_unreadable")
    for slot, status in zip(slots(data), states, strict=False):
        data.rows[slot] = {**data.rows[slot], "t0_status": status}
    report = evaluate(data, "a")
    assert report["m1"]["classes"]["no_archive"] == 1
    assert report["m1"]["classes"]["t0_pre_window_unreadable"] == 1
    assert report["m1"]["fail"] is True


def test_m1_pending_launches_never_mask_a_known_failure_or_invent_one() -> None:
    data = make(20, final=False)
    for slot in slots(data)[:3]:
        data.firsts[slot] = ok_first(slot, data.rows[slot], delay_ns=30 * 3_600_000_000_000)
    for slot in slots(data)[3:10]:
        del data.firsts[slot]  # not run
    report = evaluate(data, "a")
    assert report["m1"]["fail"] is True and report["m1"]["pending"] == 7
    assert report["outcome"] == "not_reconstructible"
    clean = make(20, final=False)
    for slot in slots(clean)[:10]:
        del clean.firsts[slot]
    again = evaluate(clean, "a")
    assert again["m1"]["fail"] is False and again["m1"]["pass"] is False
    assert again["outcome"] == "stage1b_incomplete" and again["go_b_allowed"] is False
    assert "insufficient_events" not in again["stop_codes"]


@pytest.mark.parametrize(("missing", "passes"), [(2, True), (3, False)])
def test_m2_no_file_near_launch_is_exactly_five_percent(missing: int, passes: bool) -> None:
    """2 of 40 launches without a file within a day is 5%; 3 of 40 fails."""
    data = make(40, final=False)
    for slot in slots(data)[:missing]:
        row = dict(data.rows[slot])
        row.update(first_file_within_1d_of_launch=False, t0_status="no_pre_cut_file")
        row.pop("first_file_day", None)
        row.pop("head", None)
        data.rows[slot] = row
    m2 = evaluate(data, "a")["m2"]
    assert m2["counts"]["no_file_near_launch"] == missing
    assert m2["fail"] is (not passes)


def test_m2_unproven_catalog_folders_and_a_missing_share_are_not_assessed() -> None:
    data = make(130, final=False)
    slot = slots(data)[0]
    data.rows[slot] = {**data.rows[slot], "filename_identity_unverified": True}
    report = evaluate(data, "a")
    assert "m2_i_unproven_catalog_folders" in report["not_assessed"]
    assert report["m2"]["pass"] is False and report["outcome"] == "stage1b_incomplete"
    data2 = make(130, final=False)
    data2.report = report_stub(130, share=None)
    report2 = evaluate(data2, "a")
    assert "m2_ii_share_unavailable" in report2["not_assessed"]
    assert report2["m2"]["archive_only_proven"] is False and report2["m2"]["fail"] is False


@pytest.mark.parametrize(("share", "fails"), [(0.05, False), (0.0500001, True)])
def test_m2_archive_only_share_comes_from_the_metadata_report(share: float, fails: bool) -> None:
    data = make(20, final=False)
    data.report = report_stub(20, share=share)
    report = evaluate(data, "a")
    assert report["m2"]["fail"] is fails
    assert ("incomplete_coverage" in report["stop_codes"]) is fails


@pytest.mark.parametrize(("bad", "fails"), [(1, False), (2, True)])
def test_m2_path_days_missing_is_five_percent_of_candidates(bad: int, fails: bool) -> None:
    """A candidate whose listing lacks more than 5% of its path days is bad: 1 of 20 passes."""
    data = make(20, final=False)
    for slot in slots(data)[:bad]:
        row = dict(data.rows[slot])
        row["days_in_window"] = []
        data.rows[slot] = row
    m2 = evaluate(data, "a")["m2"]
    assert m2["counts"]["path_days_missing_launches"] == bad
    assert m2["fail"] is fails


def test_m3_boundaries_and_the_episode_floor() -> None:
    data = make(130, final=False)
    ordered = slots(data)
    for slot in ordered[:13]:  # 13 of 130 is exactly 10%
        data.rows[slot] = {**data.rows[slot], "catalog_identity": "relist"}
    m3 = evaluate(data, "a")["m3"]
    assert m3["shares"]["contradicting"] == 0.1 and m3["episodes_remaining"] == 117
    assert m3["pass"] is True
    data.rows[ordered[13]] = {**data.rows[ordered[13]], "ticker_reuse_suspected": True}
    report = evaluate(data, "a")
    assert report["m3"]["pass"] is False and "identity_unresolved" in report["stop_codes"]
    unverified = make(130, final=False)
    for slot in slots(unverified)[:26]:  # 20% exactly
        unverified.rows[slot] = {**unverified.rows[slot], "listing_state": "absent"}
    assert evaluate(unverified, "a")["m3"]["shares"]["identity_unverifiable"] == 0.2
    assert evaluate(unverified, "a")["m3"]["pass"] is True
    unverified.rows[slots(unverified)[26]]["listing_state"] = "absent"
    assert evaluate(unverified, "a")["m3"]["pass"] is False
    few = evaluate(make(99, final=False), "a")
    assert few["m3"]["episodes_remaining"] == 99 and few["m3"]["pass"] is False


def test_m3_holds_identity_statuses_apart_and_never_claims_change_known_at_entry() -> None:
    data = make(130, final=False)
    ordered = slots(data)
    data.rows[ordered[0]] = {**data.rows[ordered[0]], "catalog_identity": "multiplier_ambiguous"}
    data.rows[ordered[1]] = {**data.rows[ordered[1]], "catalog_identity": "relist"}
    data.rows[ordered[2]] = {**data.rows[ordered[2]], "ticker_reuse_suspected": True}
    m3 = evaluate(data, "a")["m3"]
    assert m3["statuses"] == {"relist": 1, "multiplier_ambiguous": 1, "ticker_reuse_suspected": 1}
    assert m3["change_known_at_entry"] == "not_observable"


@pytest.mark.parametrize(("uncovered", "passes"), [(5, True), (6, False)])
def test_m4_funding_uncovered_is_exactly_five_percent(uncovered: int, passes: bool) -> None:
    """The M4 share is of the eligible episodes: 5 of 100 passes, 6 of 100 fails."""
    data = make(100)
    for slot in slots(data)[:uncovered]:
        data.funding[slot] = [funding_page(_candidate(data, slot), max_gap_ms=EIGHT + 1)]
    report = evaluate(data)
    assert report["m4"]["funding_uncovered"] == uncovered and report["m4"]["eligible"] == 100
    assert report["m4"]["pass"] is passes
    assert ("incomplete_coverage" in report["stop_codes"]) is (not passes)
    assert report["m4"]["by_reason"] == {"gap_inside": uncovered}
    assert report["reasons"]["funding_uncovered"] == uncovered


def _candidate(data: v.BodyData, slot: str) -> body.Candidate:
    queued = {slot for slot, r in data.rows.items() if body.has_size(r)}
    return next(c for c in body.entry_candidates(data.rows, data.firsts, queued) if c.slot == slot)


def test_m4_partial_pass_counts_launches_whose_interval_snapshot_is_not_eight_hours() -> None:
    data = make(100)
    for slot in slots(data)[:4]:
        data.rows[slot] = {**data.rows[slot], "funding_interval_min": 60}
    data.rows[slots(data)[4]].pop("funding_interval_min")
    m4 = evaluate(data)["m4"]
    assert m4["covered_by_interval_snapshot"] == {
        "snapshot_is_8h": 95,
        "snapshot_shorter_or_unknown": 5,
    }
    assert m4["status"] == "partial_pass" and m4["partial"] is True
    assert "8 h" in m4["rule"] and "snapshot" in m4["interval_note"]


def test_m4_reports_launch_quarters_not_months_and_delivery_state() -> None:
    data = make(100)
    for slot in slots(data)[:3]:
        data.funding[slot] = [funding_page(_candidate(data, slot), max_gap_ms=EIGHT + 1)]
    data.rows[slots(data)[0]] = {
        **data.rows[slots(data)[0]],
        "delivery_ms": ms("2023-05-01T00:00:00"),
    }
    m4 = evaluate(data)["m4"]
    assert set(m4["by_launch_quarter"]) == {"2023-Q1"}
    assert m4["by_delivery"] == {"delivered_in_window": 1, "not_delivered_in_window": 2}
    assert "by_launch_month" not in m4


@pytest.mark.parametrize(("bad", "passes"), [(5, True), (6, False)])
def test_m5_bars_share_is_exactly_ninety_five_percent(bad: int, passes: bool) -> None:
    data = make(100)
    for slot in slots(data)[:bad]:
        c = _candidate(data, slot)
        data.mark_hourly[slot] = hourly(c, runs=[[c.entry_ms // HOUR * HOUR + 3 * HOUR, 1]])
    report = evaluate(data)
    assert report["m5"]["with_bars"] == 100 - bad and report["m5"]["pass"] is passes
    assert report["m5"]["states"]["mark"]["interior_gap"] == bad
    assert ("incomplete_coverage" in report["stop_codes"]) is (not passes)


def test_m5_states_for_every_kind_of_gap_and_never_green_when_unproven() -> None:
    data = make(130)
    ordered = slots(data)
    c0, c1, c2, _c3, c4 = (_candidate(data, slot) for slot in ordered[:5])
    data.entry_bar[ordered[0]] = entry(c0, present=False)
    data.trade_hourly[ordered[1]] = hourly(c1, oldest_ms=c1.entry_ms + 10 * HOUR)
    data.trade_hourly[ordered[2]] = hourly(c2, newest_ms=c2.exit_ms - 20 * HOUR)
    data.mark_hourly[ordered[3]] = {"outcome": "unavailable", "facts": None, "meta": {}}
    data.mark_hourly[ordered[4]] = hourly(c4, full=True)
    report = evaluate(data)
    states = report["m5"]["states"]
    assert states["entry"]["absent"] == 1 and states["trade"]["starts_late"] == 1
    assert states["trade"]["ends_early"] == 1 and states["mark"]["unavailable"] == 1
    assert states["mark"]["unproven"] == 1
    assert report["m5"]["assessed"] is False and report["outcome"] == "stage1b_incomplete"
    assert "m5_series_unproven" in report["not_assessed"]
    assert report["reasons"]["entry_unavailable"] == 1


def test_m5_end_of_archive_is_a_gap_unless_the_catalog_confirms_a_delivery() -> None:
    data = make(100)
    slot = slots(data)[0]
    c = _candidate(data, slot)
    early = c.exit_ms - 20 * HOUR
    data.trade_hourly[slot] = hourly(c, newest_ms=early)
    assert evaluate(data)["m5"]["states"]["trade"]["ends_early"] == 1
    data.rows[slot] = {**data.rows[slot], "delivery_ms": early + HOUR}
    states = evaluate(data)["m5"]["states"]["trade"]
    assert states.get("confirmed_event") == 1 and "ends_early" not in states


def test_m6_is_static_feasibility_and_its_share_is_exactly_ninety_percent() -> None:
    data = make(100)
    for slot in slots(data)[:10]:
        data.rows[slot] = {**data.rows[slot], "min_notional": "50.01"}
    m6 = evaluate(data)["m6"]
    assert m6["counts"] == {"feasible": 90, "infeasible": 10, "unknown": 0} and m6["pass"] is True
    data.rows[slots(data)[10]] = {**data.rows[slots(data)[10]], "max_leverage": "1.5"}
    assert evaluate(data)["m6"]["pass"] is False
    assert "capital_infeasible" in evaluate(data)["stop_codes"]
    unknown = make(100)
    unknown.rows[slots(unknown)[0]] = {**unknown.rows[slots(unknown)[0]], "qty_step": None}
    assert evaluate(unknown)["m6"]["counts"]["unknown"] == 1
    assert "depth at entry" in evaluate(unknown)["m6"]["not_checked"]


def test_floor_is_one_hundred_primary_episodes_and_twenty_six_iso_weeks() -> None:
    assert evaluate(make(100))["n_primary"] == 100 and evaluate(make(100))["stop_codes"] == []
    assert "insufficient_events" in evaluate(make(99))["stop_codes"]
    # 100 launches one day apart span 100 days, 15 ISO weeks: too few weeks
    world = holdout_world(100, step_days=1)
    rows = {slot_of(x.symbol): sealed_row(x) for x in world.launches}
    data = v.BodyData(report=report_stub(100), rows=rows)
    for slot, row in rows.items():
        data.firsts[slot] = ok_first(slot, row)
    add_bars(data)
    report = evaluate(data)
    assert report["iso_weeks"] < 26 and report["n_primary"] == 100
    assert "insufficient_events" in report["stop_codes"]
    # the Go A bound uses the same floors on the candidates it knows
    assert evaluate(make(99, final=False), "a")["go_b_allowed"] is False
    assert evaluate(make(100, final=False), "a")["go_b_allowed"] is True


def test_stop_codes_follow_the_protocol_order() -> None:
    """Protocol section 8: not_reconstructible, incomplete_coverage, identity_unresolved,
    insufficient_events, capital_infeasible. The first one is the outcome."""
    data = make(60)
    for slot in slots(data)[:12]:
        data.firsts[slot] = ok_first(slot, data.rows[slot], delay_ns=30 * 3_600_000_000_000)
    data.report = report_stub(60, share=0.5)
    for slot in slots(data)[12:20]:
        data.rows[slot] = {**data.rows[slot], "catalog_identity": "relist"}
    for slot in slots(data)[20:40]:
        data.rows[slot] = {**data.rows[slot], "min_notional": "99"}
    add_bars(data)
    report = evaluate(data)
    assert report["stop_codes"] == [
        "not_reconstructible",
        "incomplete_coverage",
        "identity_unresolved",
        "insufficient_events",
        "capital_infeasible",
    ]
    assert report["outcome"] == "not_reconstructible"


def test_a_known_failure_is_never_erased_by_a_later_stop() -> None:
    data = make(100)
    data.report = report_stub(100, share=0.5)
    data.stop = {"go": 2, "reason": "D4: HTTP 403"}
    report = evaluate(data)
    assert report["outcome"] == "incomplete_coverage" and report["stop"]["reason"] == "D4: HTTP 403"
    clean = make(100)
    clean.stop = {"go": 2, "reason": "D4: HTTP 403"}
    assert evaluate(clean)["outcome"] == "stage1b_stopped"


def test_incomplete_funding_or_bars_are_not_assessed_and_never_a_pass() -> None:
    data = make(100)
    slot = slots(data)[0]
    del data.funding[slot]
    del data.entry_bar[slot]
    report = evaluate(data)
    assert report["outcome"] == "stage1b_incomplete"
    assert {"m4_funding_not_run", "m5_entry_bar_not_run"} <= set(report["not_assessed"])
    assert report["m4"]["status"] == "not_assessed" and report["m4"]["pass"] is False


def test_the_report_is_aggregate_only() -> None:
    data = make(100)
    text = repr(evaluate(data))
    for slot, row in data.rows.items():
        assert slot not in text and row["symbol"] not in text
        assert str(row["launch_ms"]) not in text
    assert "USDT" not in text


def test_every_launch_gets_exactly_one_reason_in_the_discovery_order() -> None:
    data = make(100)
    ordered = slots(data)
    data.rows[ordered[0]] = {**data.rows[ordered[0]], "t0_status": "no_archive"}
    data.firsts[ordered[1]] = ok_first(ordered[1], data.rows[ordered[1]], 30 * 3_600_000_000_000)
    data.rows[ordered[2]] = {**data.rows[ordered[2]], "catalog_identity": "relist"}
    data.rows[ordered[3]] = {**data.rows[ordered[3]], "ticker_reuse_suspected": True}
    # an ambiguous T0 that is also a relist is ambiguous first
    data.rows[ordered[1]] = {**data.rows[ordered[1]], "catalog_identity": "relist"}
    add_bars(data)
    reasons = evaluate(data)["reasons"]
    assert reasons["t0_unavailable"] == 1 and reasons["t0_ambiguous"] == 1
    assert reasons["identity_excluded"] == 2 and reasons["primary"] == 96
    assert sum(reasons.values()) == 100
    assert list(reasons) == list(v.REASONS)
