"""I25 stage 1 verdicts: M1 to M6 and the stop codes, from receipts a run really wrote.

Each case fetches a synthetic world through the real fetcher into a temp root and
evaluates the receipts from disk, so the whole path (request, reduction, receipt,
verdict) is exercised, not only the verdict on hand-built rows.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from schurfer_analytics import new_perp_stage1 as s1
from schurfer_analytics import new_perp_stage1_verdict as v1

from .new_perp_world import (
    DAY,
    SENTINELS,
    Launch,
    World,
    fetcher,
    ms,
    run_go,
    run_world,
)

HOUR = s1.HOUR_MS
CODES = list(s1.STOP_CODES)


def evaluate(world: World, root: Path, contract: dict[str, Any] | None = None) -> dict[str, Any]:
    _, report = run_world(world, root, contract)
    return report


def ordered(codes: list[str]) -> bool:
    return codes == [c for c in CODES if c in codes]


def world_of(count: int, **kw: Any) -> World:
    world = World.evenly(count, **kw)
    world.prior = [("OLDUSDT", "OLD", ms("2024-06-01T00:00:00"))]
    return world


# a healthy world passes


def test_a_healthy_discovery_year_passes_every_check_as_a_partial_pass(tmp_path: Path) -> None:
    world = world_of(105)
    report = evaluate(world, tmp_path / "run")
    assert report["outcome"] == "stage1_pass_partial" and report["stop_codes"] == []
    assert report["not_assessed"] == []
    assert report["launches"] == report["n_primary"] == 105
    assert report["iso_weeks"] >= 26
    assert report["reasons"]["primary"] == 105
    assert all(report[m]["pass"] for m in ("m1", "m2", "m3", "m4", "m5", "m6"))
    assert report["m1"]["classes"] == {"aligned": 105}
    assert report["m5"]["states"] == {
        "entry": {"present": 105},
        "mark": {"covered": 105},
        "trade": {"covered": 105},
    }
    assert report["m5"]["minute_level_path"] == "not_assessed_at_stage_1"
    assert report["m3"]["change_known_at_entry"] == "not_observable_at_stage_1"
    # M6 and the minute-level path are named partial checks, never proof that an order opens
    assert report["m6"]["partial"] is True and report["m6"]["status"] == "partial_pass"
    assert "risk limit and position cap" in report["m6"]["not_checked"]
    assert len(report["partial_checks"]) == 2 and report["stop"] is None
    assert "book" not in report and "spot" not in report


def test_the_report_holds_no_price_like_field_or_sentinel(tmp_path: Path) -> None:
    report = evaluate(world_of(8), tmp_path / "run")
    text = json.dumps(report)
    for sentinel in SENTINELS:
        assert sentinel not in text
    keys: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                keys.add(str(key))
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(report)
    banned = {"price", "open", "high", "low", "close", "volume", "turnover", "funding_rate", "rate"}
    assert not keys & banned


def test_the_report_is_written_once_and_the_command_line_runs(tmp_path: Path) -> None:
    root = tmp_path / "run"
    evaluate(world_of(6), root)
    out = tmp_path / "report.json"
    assert v1.main(["--root", str(root), "--out", str(out)]) == 0
    assert json.loads(out.read_text())["version"] == v1.REPORT_VERSION
    with pytest.raises(FileExistsError):
        v1.main(["--root", str(root), "--out", str(out)])


# M1 and the lifecycle


def test_m1_counts_the_three_classes_and_fails_on_ambiguity(tmp_path: Path) -> None:
    world = world_of(20)
    for launch in world.launches[:3]:  # the archive starts two days after launchTime
        launch.first_file_offset_days = 2
        launch.first_trade_delay_ms = 2 * DAY + 10 * s1.MINUTE_MS
    world.launches[3].first_trade_delay_ms = 5 * HOUR  # delayed open, still readable
    report = evaluate(world, tmp_path / "run")
    assert report["m1"]["classes"] == {
        "aligned": 16,
        "delayed_open": 1,
        "premarket_or_ambiguous": 3,
    }
    assert report["m1"]["pass"] is False  # 15% ambiguous
    assert report["stop_codes"][0] == "not_reconstructible"
    assert report["reasons"]["t0_ambiguous"] == 3
    # a first file two days from launchTime is decided by its name: never downloaded
    assert (
        report["requests"]["by_class"]["D2h"] == 17 and report["requests"]["by_class"]["D2"] == 17
    )


def test_a_missing_archive_or_unreadable_first_file_is_a_lifecycle_gap(tmp_path: Path) -> None:
    world = world_of(20)
    world.launches[0].listing_days = 0  # no archive file before the cut at all
    world.launches[1].first_file_missing = True  # listed, but the file answers 404
    world.launches[2].first_file_missing = True
    world.launches[3].first_file_missing = True
    report = evaluate(world, tmp_path / "run")
    assert report["m1"]["classes"] == {"aligned": 16, "no_archive": 1, "t0_unreadable": 3}
    assert report["m1"]["lifecycle_unrebuildable_share"] == 0.2  # above 15%
    assert report["stop_codes"][0] == "not_reconstructible"
    assert report["reasons"]["t0_unavailable"] == 4


# M2


def test_m2_three_shares(tmp_path: Path) -> None:
    world = world_of(20)
    world.archive_only = ["GHOSTAUSDT", "GHOSTBUSDT", "GHOSTCUSDT"]  # absent from the catalog
    world.launches[0].listing_days = 0  # catalog launch with no file within a day of launchTime
    report = evaluate(world, tmp_path / "run")
    m2 = report["m2"]
    assert m2["counts"]["archive_only_in_window"] == 3
    assert m2["shares"]["archive_only"] == pytest.approx(3 / 23)
    assert m2["shares"]["no_file_near_launch"] == 1 / 20
    assert m2["pass"] is False and "incomplete_coverage" in report["stop_codes"]


def test_m2_counts_an_unlisted_archive_candidate_in_the_worst_case(tmp_path: Path) -> None:
    world = world_of(20)
    world.archive_only = ["GHOSTUSDT"]
    world.fail["public.bybit.com/trading/GHOSTUSDT/"] = [500, 500, 500]
    report = evaluate(world, tmp_path / "run")
    assert report["m2"]["counts"]["archive_only_unlisted"] == 1
    assert report["m2"]["shares"]["archive_only"] == pytest.approx(1 / 21)


def test_m2_path_days_missing(tmp_path: Path) -> None:
    world = world_of(20)
    world.launches[0].listing_days = 11  # the file list ends before the 14 day V1 path does
    report = evaluate(world, tmp_path / "run")
    assert report["m2"]["counts"]["path_days_missing_launches"] == 1
    assert report["m2"]["shares"]["path_days_missing"] == 1 / 20


# M3


def test_m3_statuses_come_from_the_catalog_and_the_file_list(tmp_path: Path) -> None:
    world = world_of(20)
    far = ms("2024-01-01T00:00:00")
    world.prior += [
        (
            f"1000{world.launches[0].base}USDT",
            world.launches[0].base,
            far,
        ),  # same base, delivered before the window
        (
            f"1000{world.launches[1].base}USDT",
            world.launches[1].base,
            None,
        ),  # same base, still live: two symbols
    ]
    world.launches[2].listing_days = 20
    world.launches[3].first_file_offset_days = 0
    report = evaluate(world, tmp_path / "run")
    m3 = report["m3"]
    assert m3["statuses"]["relist"] == 1 and m3["statuses"]["multiplier_ambiguous"] == 1
    assert report["reasons"]["identity_excluded"] == 2
    assert m3["episodes_remaining"] == 18 and m3["pass"] is False  # fewer than 100 episodes
    assert "identity_unresolved" in report["stop_codes"]
    # an excluded launch gets no bar, mark or funding request
    assert report["requests"]["by_class"]["D3"] == 18


def test_ticker_reuse_is_read_from_a_gap_in_the_file_list(tmp_path: Path) -> None:
    world = world_of(8)
    # launched at 00:02, first trade 23:58 the evening before; then a 39 day hole in the files
    reused = Launch("REUSEDUSDT", "REUSED", ms("2024-11-02T00:02:00"))
    reused.first_trade_delay_ms = -4 * s1.MINUTE_MS
    reused.first_file_offset_days = -1
    reused.listing_days = 60
    reused.listing_gap = (1, 40)
    world.launches.append(reused)
    report = evaluate(world, tmp_path / "run")
    assert report["m3"]["statuses"]["ticker_reuse_suspected"] == 1
    assert report["reasons"]["identity_excluded"] == 1


def test_a_first_file_months_before_launch_time_is_caught_by_m1_first(tmp_path: Path) -> None:
    world = world_of(8)
    world.launches[0].first_file_offset_days = -70
    report = evaluate(world, tmp_path / "run")
    assert report["m1"]["classes"].get("premarket_or_ambiguous") == 1
    assert report["m3"]["statuses"]["ticker_reuse_suspected"] == 0


def test_identity_unverifiable_stays_in_the_sample_and_is_counted(tmp_path: Path) -> None:
    world = world_of(10)
    world.launches[0].base_coin = None
    world.launches[1].base_coin = "WRONG"
    report = evaluate(world, tmp_path / "run")
    assert report["m3"]["identity_unverifiable"] == 2
    assert report["reasons"]["primary"] == 10  # unverifiable launches are still traded


# M4


def test_m4_funding_gaps_exclude_before_entry_and_gate_at_five_percent(tmp_path: Path) -> None:
    world = world_of(20)
    for launch in world.launches[:3]:
        launch.funding_rows = False
    report = evaluate(world, tmp_path / "run")
    assert report["reasons"]["funding_uncovered"] == 3
    assert report["m4"]["share"] == pytest.approx(3 / 20)
    assert report["m4"]["by_launch_month"] and report["m4"]["by_status"]["listed"] == 3
    assert report["m4"]["pass"] is False and "incomplete_coverage" in report["stop_codes"]


def test_m4_paginates_backwards_and_proves_the_pages_join(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(s1, "FUNDING_PAGE_LIMIT", 20)
    world = world_of(3)
    covered = evaluate(world, tmp_path / "a")
    assert covered["requests"]["by_class"]["D6"] == 9  # 57 rows: pages of 20, 20 and 17
    assert covered["m4"]["funding_uncovered"] == 0
    monkeypatch.setattr(s1, "FUNDING_MAX_PAGES", 2)
    exhausted = evaluate(world_of(3), tmp_path / "b")
    assert exhausted["m4"]["funding_uncovered"] == 3
    assert exhausted["m4"]["by_reason"] == {"pagination_exhausted": 3}


def test_m4_a_finished_pagination_is_not_proof_of_coverage(tmp_path: Path) -> None:
    """P1-2 regression: a short non-empty page used to cover the whole interval."""
    world = world_of(20)
    for launch in world.launches[:3]:
        launch.funding_mode = "sparse"  # one settlement over 19 days
    for launch in world.launches[3:5]:
        launch.funding_mode = "hole"  # a 24 hour hole inside an otherwise dense page
    report = evaluate(world, tmp_path / "run")
    assert report["m4"]["by_reason"] == {"gap_inside": 2, "starts_late": 3}
    assert report["reasons"]["funding_uncovered"] == 5
    assert report["m4"]["pass"] is False and "incomplete_coverage" in report["stop_codes"]


def test_the_p1_2_repro_two_hourly_points_and_one_funding_row_is_never_a_pass(
    tmp_path: Path,
) -> None:
    """140 episodes, two hourly points over the whole range, one funding row: was a pass."""
    world = world_of(105)
    for launch in world.launches:
        launch.hourly_gap_hours = tuple(range(1, 455))
        launch.funding_mode = "sparse"
    report = evaluate(world, tmp_path / "run")
    assert report["outcome"] != "stage1_pass_partial"
    assert report["m4"]["pass"] is False and report["m5"]["pass"] is False
    assert report["stop_codes"][0] == "incomplete_coverage"


# M5 and the end-of-archive rule


def test_m5_a_missing_entry_bar_is_entry_unavailable_never_scored(tmp_path: Path) -> None:
    world = world_of(20)
    for launch in world.launches[:3]:
        launch.entry_bar = False
    report = evaluate(world, tmp_path / "run")
    assert report["reasons"]["entry_unavailable"] == 3
    assert report["m5"]["states"]["entry"] == {"absent": 3, "present": 17}
    assert report["m5"]["share"] == 17 / 20 and report["m5"]["pass"] is False
    assert "incomplete_coverage" in report["stop_codes"]


def test_m5_an_interior_gap_in_the_path_is_a_gap_and_one_outside_it_is_not(
    tmp_path: Path,
) -> None:
    world = world_of(20)
    for launch in world.launches[:3]:
        launch.hourly_gap_hours = (150,)  # day 9: inside the V1 path (days 7 to 14)
    for launch in world.launches[3:6]:
        launch.hourly_gap_hours = (10,)  # day 3: before the entry, outside the V1 path
    report = evaluate(world, tmp_path / "run")
    assert report["m5"]["states"]["mark"] == {"covered": 17, "interior_gap": 3}
    assert report["m5"]["states"]["trade"] == {"covered": 17, "interior_gap": 3}
    assert report["m5"]["pass"] is False  # 85% have their bars


def test_m5_end_of_archive_is_a_gap_unless_the_catalog_confirms_the_end(tmp_path: Path) -> None:
    world = world_of(20)
    for launch in world.launches[:4]:
        launch.hourly_end_days = 10.0  # the series stops 4 days before the planned exit
    gap = evaluate(world, tmp_path / "gap")
    assert gap["m5"]["states"]["mark"] == {"covered": 16, "ends_early": 4}
    assert gap["m5"]["pass"] is False and "incomplete_coverage" in gap["stop_codes"]

    confirmed = world_of(20)
    for launch in confirmed.launches[:4]:
        launch.hourly_end_days = 10.0
        launch.status = "Closed"
        launch.delivery_ms = launch.launch_ms + 9 * DAY  # dated at or before the first gap
    done = evaluate(confirmed, tmp_path / "confirmed")
    assert done["m5"]["states"]["mark"] == {"confirmed_event": 4, "covered": 16}
    assert done["m5"]["pass"] is True

    late = world_of(20)
    for launch in late.launches[:4]:
        launch.hourly_end_days = 10.0
        launch.status = "Closed"
        launch.delivery_ms = launch.launch_ms + 12 * DAY  # dated after the gap began
    after = evaluate(late, tmp_path / "late")
    assert after["m5"]["states"]["mark"] == {"covered": 16, "ends_early": 4}


def test_bar_state_unit_cases() -> None:
    entry, exit_ = ms("2024-11-08T10:01:00"), ms("2024-11-15T10:01:00")
    floor = v1._floor_hour

    def result(
        n: int,
        oldest: int | None,
        newest: int | None,
        runs: list[list[int]] | None = None,
        *,
        full: bool = False,
        truncated: bool = False,
    ) -> dict[str, Any]:
        facts = {
            "n": n,
            "oldest_ms": oldest,
            "newest_ms": newest,
            "runs": runs or [],
            "runs_truncated": truncated,
            "full": full,
        }
        return {"outcome": "ok", "facts": facts}

    assert v1.bar_state(None, entry, exit_, None) == "not_run"
    assert v1.bar_state({"outcome": "unavailable"}, entry, exit_, None) == "unavailable"
    assert v1.bar_state(result(0, None, None), entry, exit_, None) == "empty"
    late_start = result(5, floor(entry) + HOUR, floor(exit_ - 1))
    assert v1.bar_state(late_start, entry, exit_, None) == "starts_late"
    full = result(170, floor(entry), floor(exit_ - 1))
    assert v1.bar_state(full, entry, exit_, None) == "covered"
    short = result(100, floor(entry), floor(exit_ - 1) - HOUR)
    assert v1.bar_state(short, entry, exit_, None) == "ends_early"
    first_missing = floor(exit_ - 1) - HOUR + HOUR
    assert v1.bar_state(short, entry, exit_, first_missing) == "confirmed_event"
    assert v1.bar_state(short, entry, exit_, first_missing + 1) == "ends_early"


def test_bar_state_checks_continuity_not_only_the_two_ends() -> None:
    """P1-2 regression: two points at the ends of the range used to read as `covered`."""
    entry, exit_ = ms("2024-11-08T10:01:00"), ms("2024-11-15T10:01:00")
    floor = v1._floor_hour
    ends = {
        "n": 2,
        "oldest_ms": floor(entry) - 96 * HOUR,
        "newest_ms": floor(exit_ - 1),
        "runs": [[floor(entry) - 95 * HOUR, 455]],
        "runs_truncated": False,
        "full": False,
    }
    assert v1.bar_state({"outcome": "ok", "facts": ends}, entry, exit_, None) == "interior_gap"
    # a hole before the entry hour does not touch the V1 path; one after it does
    before = {**ends, "runs": [[floor(entry) - 50 * HOUR, 3]]}
    assert v1.bar_state({"outcome": "ok", "facts": before}, entry, exit_, None) == "covered"
    inside = {**ends, "runs": [[floor(entry) + 40 * HOUR, 1]]}
    assert v1.bar_state({"outcome": "ok", "facts": inside}, entry, exit_, None) == "interior_gap"
    edge = {**ends, "runs": [[floor(exit_ - 1) - HOUR, 1]]}
    assert v1.bar_state({"outcome": "ok", "facts": edge}, entry, exit_, None) == "interior_gap"
    # a page that may be cut by its limit, or whose gap list was cut, proves nothing
    cut = {**ends, "runs": [], "full": True}
    assert v1.bar_state({"outcome": "ok", "facts": cut}, entry, exit_, None) == "unproven"
    many = {**ends, "runs": [], "runs_truncated": True}
    assert v1.bar_state({"outcome": "ok", "facts": many}, entry, exit_, None) == "unproven"


def test_path_days_follow_the_calendar_days_the_position_touches() -> None:
    days = v1.path_days(ms("2024-11-08T10:01:00"), ms("2024-11-15T10:01:00"))
    assert days[0] == "2024-11-08" and days[-1] == "2024-11-15" and len(days) == 8
    exact = v1.path_days(ms("2024-11-08T00:00:00"), ms("2024-11-15T00:00:00"))
    assert exact[-1] == "2024-11-14" and len(exact) == 7


# M6, events, order and censoring


def test_m6_static_order_uses_catalog_fields_only(tmp_path: Path) -> None:
    world = world_of(20)
    for launch in world.launches[:2]:
        launch.min_notional = "100"  # above the USD 50 slot
    world.launches[2].max_leverage = "1"  # below the 2x isolated margin
    world.launches[3].max_leverage = None  # unknown fields count against feasibility
    report = evaluate(world, tmp_path / "run")
    assert report["m6"]["counts"] == {"feasible": 16, "infeasible": 3, "unknown": 1}
    assert report["m6"]["pass"] is False and "capital_infeasible" in report["stop_codes"]


def test_static_order_unit_cases() -> None:
    base = {"min_notional": "5", "max_leverage": "25", "qty_step": "1", "min_order_qty": "1"}
    assert v1.static_order(base) == "feasible"
    assert v1.static_order({**base, "min_notional": "50"}) == "feasible"
    assert v1.static_order({**base, "min_notional": "50.01"}) == "infeasible"
    assert v1.static_order({**base, "max_leverage": "2"}) == "feasible"
    assert v1.static_order({**base, "max_leverage": "1.99"}) == "infeasible"
    assert v1.static_order({**base, "qty_step": None}) == "unknown"
    assert v1.static_order({**base, "min_notional": "n/a"}) == "unknown"


def test_censored_launches_are_counted_and_never_requested_for_bars(tmp_path: Path) -> None:
    world = World(
        [
            Launch(
                "EARLYUSDT", "EARLY", ms("2024-10-10T10:00:00")
            ),  # E - 30 d is before the window
            Launch("NORMALUSDT", "NORMAL", ms("2024-12-10T10:00:00")),
            Launch("LATEUSDT", "LATE", ms("2025-09-25T10:00:00")),  # X is after the cut
        ]
    )
    report = evaluate(world, tmp_path / "run")
    assert report["reasons"]["lookback_censored"] == 1
    assert report["reasons"]["window_censored"] == 1
    assert report["reasons"]["primary"] == 1
    assert report["requests"]["by_class"]["D3"] == 1
    assert not any("LATEUSDT" in str(r.url) and "kline" in str(r.url) for r in world.requests)


def test_too_few_launches_or_weeks_is_insufficient_events(tmp_path: Path) -> None:
    few = evaluate(world_of(60), tmp_path / "few")
    assert few["n_primary"] == 60
    assert few["stop_codes"] == ["identity_unresolved", "insufficient_events"]  # < 100 remain
    # 105 launches, one a day: 105 days is 16 ISO weeks, below the floor of 26
    dense = World.evenly(105, step_days=1)
    dense.prior = []
    report = evaluate(dense, tmp_path / "dense")
    assert report["n_primary"] == 105 and report["iso_weeks"] < 26
    assert report["stop_codes"] == ["insufficient_events"]


def test_stop_codes_come_in_the_order_of_section_8(tmp_path: Path) -> None:
    world = world_of(20)
    for launch in world.launches[:4]:
        launch.first_file_offset_days = 3
        launch.first_trade_delay_ms = 3 * DAY
    for launch in world.launches[4:7]:
        launch.funding_rows = False
    for launch in world.launches[7:10]:
        launch.min_notional = "100"
    world.prior += [
        (f"1000{world.launches[10 + i].base}USDT", world.launches[10 + i].base, None)
        for i in range(3)
    ]
    report = evaluate(world, tmp_path / "run")
    assert report["stop_codes"] == list(CODES)
    assert report["outcome"] == "not_reconstructible"
    assert ordered(report["stop_codes"])


def test_a_run_stopped_by_a_cap_is_stopped_not_passed_and_not_failed(tmp_path: Path) -> None:
    world = world_of(105)
    root = tmp_path / "run"
    run_go(world, root, 1)
    f, _ = fetcher(world, root, go=2)
    f.contract["go_limits"]["2"]["max_attempts"] = 330  # the cap runs out inside the bar classes
    with pytest.raises(s1.BudgetExceededError):
        s1.run_go2(f, s1.manifest_sha256(root))
    report = v1.evaluate(v1.load_run(root))
    assert report["not_assessed"]  # bars or funding were not finished
    assert "incomplete_coverage" not in report["stop_codes"]  # no verdict from missing evidence
    assert report["outcome"] == "stage1_stopped" and report["stop"]["reason"]


def test_the_go_1_report_is_what_the_owner_reviews_before_go_2(tmp_path: Path) -> None:
    world = world_of(20)
    for launch in world.launches[:3]:  # the archive starts three days after launchTime
        launch.first_file_offset_days = 3
        launch.first_trade_delay_ms = 3 * DAY
    root = tmp_path / "run"
    summary = run_go(world, root, 1)
    report = v1.evaluate(v1.load_run(root))
    assert report["report"] == "go1" and report["provisional"] is True
    assert report["m1_by_name"] == {"premarket_or_ambiguous": 3, "first_file_to_read": 17}
    # 15% ambiguous by name alone, and those launches have no file within a day of launchTime
    assert report["stop_codes"] == ["not_reconstructible", "incomplete_coverage"]
    assert report["manifest"] == summary["manifest"] and report["manifest"]["d2_requests"] == 17
    assert "m4_funding" in report["not_assessed"] and report["m6"]["partial"] is True
    assert "n_primary_and_weeks" in report["not_assessed"]
    healthy = world_of(20)
    clean = tmp_path / "clean"
    run_go(healthy, clean, 1)
    assert v1.evaluate(v1.load_run(clean))["outcome"] == "go1_complete"


def test_m2_archive_only_is_not_assessed_when_the_page_carries_no_dates() -> None:
    phase0 = {
        "catalog": {"rows": [], "prior": []},
        "archive": {"candidates": [], "modified_dates_present": False},
    }
    m2 = v1._m2(v1.RunData(phase0=phase0), [], {})
    assert m2["archive_only_assessed"] is False and m2["shares"]["archive_only"] is None
    assert "last file inside the window" in m2["archive_only_scope"]


def test_an_added_post_cut_directory_changes_nothing_the_run_records(tmp_path: Path) -> None:
    """P1-1 regression: the directory counts moved when a post-cut directory was added."""
    plain, more = world_of(6), world_of(6)
    more.extra_dirs = [("NEWAUSDT", "2026-02-01"), ("NEWBUSDT", None), ("NEWCUSDT", "2025-10-01")]
    run_go(plain, tmp_path / "a", 1)
    run_go(more, tmp_path / "b", 1)
    first = json.loads((tmp_path / "a" / "phase0.json").read_text())
    second = json.loads((tmp_path / "b" / "phase0.json").read_text())
    assert first == second
    assert "NEW" not in (tmp_path / "b" / "phase0.json").read_text()
    assert not any("NEW" in str(r.url) for r in more.requests)  # and none was listed


def test_every_stop_code_of_stage_1_is_in_the_protocol_table() -> None:
    protocol = (Path(__file__).resolve().parents[3] / s1.PROTOCOL).read_text()
    for code in s1.STOP_CODES:
        assert f"`{code}`" in protocol
    assert re.fullmatch(r"[0-9a-f]{64}", s1.build_contract()["request_list_sha256"])
