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
    contract_without,
    fetcher,
    ms,
    rehash,
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


# ---------------------------------------------------------------- a healthy world passes


def test_a_healthy_discovery_year_passes_every_check(tmp_path: Path) -> None:
    world = world_of(105)
    report = evaluate(world, tmp_path / "run")
    assert report["outcome"] == "stage1_pass" and report["stop_codes"] == []
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
    assert report["book"]["counts"]["entry"]["ob500"] == 105
    assert report["book_source_confirmed"] is False
    assert report["m5"]["minute_level_path"] == "not_assessed_at_stage_1"
    assert report["m6"]["price_dependent_part"] == "deferred_to_stage_3_and_4"
    assert report["m3"]["change_known_at_entry"] == "not_observable_at_stage_1"


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


# ---------------------------------------------------------------- M1 and the lifecycle


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


# ---------------------------------------------------------------- M2


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


# ---------------------------------------------------------------- M3


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


# ---------------------------------------------------------------- M4


def test_m4_funding_gaps_exclude_before_entry_and_gate_at_five_percent(tmp_path: Path) -> None:
    world = world_of(20)
    for launch in world.launches[:3]:
        launch.funding_rows = False
    report = evaluate(world, tmp_path / "run")
    assert report["reasons"]["funding_uncovered"] == 3
    assert report["m4"]["share"] == pytest.approx(3 / 20)
    assert report["m4"]["by_launch_month"] and report["m4"]["by_status"]["listed"] == 3
    assert report["m4"]["pass"] is False and "incomplete_coverage" in report["stop_codes"]


def test_m4_paginates_backwards_and_calls_an_exhausted_span_uncovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(s1, "FUNDING_PAGE_LIMIT", 20)
    world = world_of(3)
    covered = evaluate(world, tmp_path / "a")
    assert covered["requests"]["by_class"]["D6"] == 9  # 57 rows: three pages of 20, 20 and 17
    assert covered["m4"]["funding_uncovered"] == 0
    monkeypatch.setattr(s1, "FUNDING_MAX_PAGES", 2)
    exhausted = evaluate(world_of(3), tmp_path / "b")
    assert exhausted["m4"]["funding_uncovered"] == 3


# ---------------------------------------------------------------- M5 and the end-of-archive rule


def test_m5_a_missing_entry_bar_is_entry_unavailable_never_scored(tmp_path: Path) -> None:
    world = world_of(20)
    for launch in world.launches[:3]:
        launch.entry_bar = False
    report = evaluate(world, tmp_path / "run")
    assert report["reasons"]["entry_unavailable"] == 3
    assert report["m5"]["states"]["entry"] == {"absent": 3, "present": 17}
    assert report["m5"]["share"] == 17 / 20 and report["m5"]["pass"] is False
    assert "incomplete_coverage" in report["stop_codes"]


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

    def result(n: int, oldest: int | None, newest: int | None) -> dict[str, Any]:
        return {"outcome": "ok", "facts": {"n": n, "oldest_ms": oldest, "newest_ms": newest}}

    floor = v1._floor_hour
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


def test_path_days_follow_the_calendar_days_the_position_touches() -> None:
    days = v1.path_days(ms("2024-11-08T10:01:00"), ms("2024-11-15T10:01:00"))
    assert days[0] == "2024-11-08" and days[-1] == "2024-11-15" and len(days) == 8
    exact = v1.path_days(ms("2024-11-08T00:00:00"), ms("2024-11-15T00:00:00"))
    assert exact[-1] == "2024-11-14" and len(exact) == 7


# ---------------------------------------------------------------- M6, events, order and censoring


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


def test_the_book_class_is_existence_only_and_never_gates(tmp_path: Path) -> None:
    world = world_of(6)
    world.book_ob500_missing = True  # ob200 answers instead
    report = evaluate(world, tmp_path / "run")
    assert report["book"]["counts"]["entry"] == {"ob500": 0, "ob200": 6, "none": 0}
    assert report["book"]["gates_nothing"] is True
    struck = evaluate(world_of(6), tmp_path / "struck", contract_without(("F",)))
    assert struck["book"] == {"in_go": False}
    assert "F" not in struck["classes_in_go"]


def test_a_stopped_run_is_not_assessed_not_failed(tmp_path: Path) -> None:
    contract = json.loads(json.dumps(s1.build_contract()))
    contract["limits"]["max_attempts"] = 330  # stops inside the bar classes
    f, _ = fetcher(world_of(105), tmp_path / "run", rehash(contract))
    with pytest.raises(s1.BudgetExceededError):
        s1.run(f)
    report = v1.evaluate(v1.load_run(tmp_path / "run"))
    assert report["not_assessed"]  # bars or funding were not finished
    assert report["m4"]["assessed"] is False or report["m5"]["assessed"] is False
    assert "incomplete_coverage" not in report["stop_codes"]  # no verdict from missing evidence
    if not report["stop_codes"]:
        assert report["outcome"] == "stage1_incomplete"


def test_spot_catalog_is_a_diagnostic_that_gates_nothing(tmp_path: Path) -> None:
    report = evaluate(world_of(10), tmp_path / "run")
    assert report["spot"]["available"] is True
    assert report["spot"]["launches_with_spot_symbol"] == 5
    assert report["spot"]["gates_nothing"] is True


def test_every_stop_code_of_stage_1_is_in_the_protocol_table() -> None:
    protocol = (Path(__file__).resolve().parents[3] / s1.PROTOCOL).read_text()
    for code in s1.STOP_CODES:
        assert f"`{code}`" in protocol
    assert re.fullmatch(r"[0-9a-f]{64}", s1.build_contract()["request_list_sha256"])
