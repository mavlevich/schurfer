from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import duckdb
import pytest
from schurfer_analytics.abnormal_flow_replay import DecisionFeatures
from schurfer_analytics.abnormal_flow_snapshots import decision_id
from schurfer_analytics.abnormal_flow_unresolved_taxonomy import (
    REASONS,
    MinuteState,
    PathDiagnosis,
    _load_saved_coverage,
    build_rows,
    diagnose_path,
    first_problem_bucket,
    load_bar_states,
    summarize,
    verify_totals_against_burned_report,
)

if TYPE_CHECKING:
    from pathlib import Path

_DECISION = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
_ENTRY = _DECISION + timedelta(minutes=1)
_HORIZON = 720


def _full_path(**overrides: MinuteState | None) -> dict[datetime, MinuteState]:
    minutes = {
        _ENTRY + timedelta(minutes=offset): MinuteState(True, False)
        for offset in range(_HORIZON + 1)
    }
    for key, state in overrides.items():
        at = _ENTRY + timedelta(minutes=int(key.removeprefix("m")))
        if state is None:
            minutes.pop(at)
        else:
            minutes[at] = state
    return minutes


def _diagnose(
    minutes: dict[datetime, MinuteState] | None, *, resolved: bool = False
) -> PathDiagnosis:
    return diagnose_path(
        minutes, decision_at=_DECISION, outcome_horizon_minutes=_HORIZON, resolved=resolved
    )


def test_complete_path_is_resolved_without_a_reason() -> None:
    diagnosis = _diagnose(_full_path(), resolved=True)
    assert diagnosis.primary_reason is None
    assert diagnosis.first_problem_minute is None


def test_complete_path_that_was_unresolved_is_unexplained_not_folded() -> None:
    """E.g. an empty entry/exit price: invisible to the allowed columns."""
    diagnosis = _diagnose(_full_path())
    assert diagnosis.primary_reason == "unexplained_by_allowed_columns"


@pytest.mark.parametrize(
    ("overrides", "reason", "first_minute"),
    [
        ({"m0": None}, "missing_entry_bar", 0),
        ({"m0": MinuteState(False, False)}, "entry_bar_incomplete", 0),
        ({"m300": None}, "internal_gap", 300),
        ({"m720": None}, "missing_horizon_bar", 720),
        ({"m45": MinuteState(False, False)}, "price_incomplete_inside", 45),
    ],
)
def test_each_path_problem_maps_to_its_reason(
    overrides: dict[str, MinuteState | None], reason: str, first_minute: int
) -> None:
    diagnosis = _diagnose(_full_path(**overrides))
    assert diagnosis.primary_reason == reason
    assert diagnosis.first_problem_minute == first_minute


def test_priority_keeps_one_reason_and_every_flag() -> None:
    diagnosis = _diagnose(
        _full_path(m0=MinuteState(False, False), m100=None, m101=None, m102=None, m720=None)
    )
    assert diagnosis.primary_reason == "entry_bar_incomplete"
    assert diagnosis.internal_gap
    assert diagnosis.missing_horizon_bar
    assert diagnosis.first_problem_minute == 0
    assert diagnosis.first_gap_length == 3
    assert diagnosis.missing_minutes == 4


def test_the_last_path_minute_is_the_horizon_bar_not_an_internal_gap() -> None:
    diagnosis = _diagnose(_full_path(m720=None))
    assert not diagnosis.internal_gap
    assert diagnosis.missing_horizon_bar


def test_a_route_without_any_bar_is_no_route_bars() -> None:
    assert _diagnose(None).primary_reason == "no_route_bars"


def test_collector_gap_marker_is_a_flag_not_a_reason() -> None:
    diagnosis = _diagnose(_full_path(m10=MinuteState(True, True)))
    assert diagnosis.collector_gap_reported
    assert diagnosis.primary_reason == "unexplained_by_allowed_columns"


def test_a_path_problem_on_a_resolved_row_fails_reconciliation() -> None:
    with pytest.raises(ValueError, match="recorded as resolved"):
        _diagnose(_full_path(m5=None), resolved=True)
    with pytest.raises(ValueError, match="cannot have been resolved"):
        _diagnose(None, resolved=True)


def test_first_problem_buckets_cover_the_whole_path() -> None:
    assert [first_problem_bucket(m) for m in (0, 1, 59, 60, 239, 240, 479, 480, 719, 720)] == [
        "0", "1-59", "1-59", "60-239", "60-239", "240-479", "240-479", "480-719", "480-719",
        "720",
    ]  # fmt: skip
    assert first_problem_bucket(None) is None
    with pytest.raises(ValueError, match="outside the path"):
        first_problem_bucket(721)


def _decision(symbol: str, week: str = "2026-W36") -> DecisionFeatures:
    return DecisionFeatures(
        exchange="bybit",
        market_type="linear",
        native_market_id=symbol,
        capture_version="v1",
        symbol=symbol,
        canonical_asset=f"asset:{symbol}",
        decision_at=_DECISION,
        oi_growth_pct=10.0,
        buy_pressure=0.7,
        containment=0.02,
        oi_native_amount=100.0,
        oi_native_value_usd=10_000.0,
        decision_price=100.0,
        pre_decision_turnover_usd=1_000.0,
        iso_week=week,
    )


def test_rows_reconcile_and_reasons_add_up_per_role() -> None:
    ok, gap = _decision("OK"), _decision("GAP")
    control_ok, control_missing = _decision("C1"), _decision("C2")
    primary_ok_id, primary_gap_id = decision_id(ok), decision_id(gap)
    resolved = {
        ("primary", None, primary_ok_id): True,
        ("primary", None, primary_gap_id): False,
        ("control", primary_ok_id, decision_id(control_ok)): True,
        ("control", primary_gap_id, decision_id(control_missing)): False,
    }
    bars = {
        ("bybit", "linear", "OK", "v1"): _full_path(),
        ("bybit", "linear", "GAP", "v1"): _full_path(m200=None),
        ("bybit", "linear", "C1", "v1"): _full_path(),
    }
    rows = build_rows(
        [ok, gap],
        {primary_ok_id: [control_ok], primary_gap_id: [control_missing]},
        resolved,
        bars,
        outcome_horizon_minutes=_HORIZON,
    )
    summary = summarize(rows)
    assert summary["primary"]["primary_reason"]["internal_gap"] == 1
    assert summary["control"]["primary_reason"]["no_route_bars"] == 1
    assert set(summary["primary"]["primary_reason"]) == set(REASONS)
    assert summary["control_comparison"]["paired_to_primary"] == {
        "primary_resolved|control_resolved": 1,
        "primary_unresolved|control_unresolved": 1,
    }
    assert summary["listing_age"].startswith("unknown")


def test_rows_must_match_the_saved_coverage_rows_exactly() -> None:
    ok = _decision("OK")
    with pytest.raises(ValueError, match="does not match the saved coverage rows"):
        build_rows([ok], {}, {}, {}, outcome_horizon_minutes=_HORIZON)


def test_bars_elsewhere_in_the_window_do_not_hide_an_empty_path() -> None:
    """Review P1: the route has bars, but none inside THIS episode's 721-minute path."""
    elsewhere = {_ENTRY + timedelta(days=2): MinuteState(True, False)}
    diagnosis = _diagnose(elsewhere)
    assert diagnosis.primary_reason == "no_route_bars"
    assert diagnosis.missing_minutes == _HORIZON + 1
    assert diagnosis.first_gap_length == _HORIZON + 1
    assert diagnosis.first_problem_minute == 0
    none_at_all = _diagnose(None)
    assert none_at_all.missing_minutes == _HORIZON + 1


def test_entry_exit_flag_marks_rows_a_two_bar_rule_would_resolve() -> None:
    inside_only = _diagnose(_full_path(m300=MinuteState(False, False), m400=None))
    assert inside_only.entry_exit_bars_complete
    bad_exit = _diagnose(_full_path(m720=MinuteState(False, False)))
    assert bad_exit.exit_bar_incomplete
    assert bad_exit.primary_reason == "price_incomplete_inside"
    assert not bad_exit.entry_exit_bars_complete
    assert not _diagnose(_full_path(m0=None)).entry_exit_bars_complete


def _bundle(tmp_path: Path, provenance: dict[str, str]) -> Path:
    bundle = tmp_path / "coverage"
    bundle.mkdir()
    rows_path = bundle / "coverage_rows.parquet"
    with duckdb.connect(":memory:") as db:
        db.execute(
            "COPY (SELECT 'primary' AS role, NULL::VARCHAR AS primary_decision_id, "
            "'d1' AS decision_id, false AS resolved) TO ? (FORMAT PARQUET)",
            [str(rows_path)],
        )
    digest = "sha256:" + hashlib.sha256(rows_path.read_bytes()).hexdigest()
    report = bundle / "coverage_diagnostic.json"
    report.write_text(
        json.dumps({"provenance": provenance, "artifacts": {"coverage_rows.parquet": digest}})
    )
    report_digest = "sha256:" + hashlib.sha256(report.read_bytes()).hexdigest()
    (bundle / "coverage_diagnostic.sha256").write_text(report_digest + "\n")
    return bundle


def test_a_coverage_bundle_from_another_run_is_rejected(tmp_path: Path) -> None:
    """Review P1: an intact but foreign bundle must not supply the resolved labels."""
    bundle = _bundle(tmp_path, {"snapshot_fingerprint": "other", "contract_hash": "c"})
    with pytest.raises(ValueError, match="not from this burned run"):
        _load_saved_coverage(
            bundle, expected_provenance={"snapshot_fingerprint": "this", "contract_hash": "c"}
        )
    resolved, _hash, _report = _load_saved_coverage(
        bundle, expected_provenance={"snapshot_fingerprint": "other", "contract_hash": "c"}
    )
    assert resolved == {("primary", None, "d1"): False}


def test_totals_must_match_the_burned_report() -> None:
    ok = _decision("OK")
    rows = build_rows(
        [ok],
        {},
        {("primary", None, decision_id(ok)): True},
        {("bybit", "linear", "OK", "v1"): _full_path()},
        outcome_horizon_minutes=_HORIZON,
    )
    report = {
        "resolved_episodes": 1,
        "unresolved_episodes": 0,
        "resolved_controls": 0,
        "unresolved_controls": 0,
    }
    verify_totals_against_burned_report(rows, report)
    with pytest.raises(ValueError, match="differ from the burned report"):
        verify_totals_against_burned_report(rows, {**report, "unresolved_episodes": 1})


def test_summary_has_asset_split_and_paired_reasons() -> None:
    gap = _decision("GAP")
    control = _decision("C1")
    rows = build_rows(
        [gap],
        {decision_id(gap): [control]},
        {
            ("primary", None, decision_id(gap)): False,
            ("control", decision_id(gap), decision_id(control)): False,
        },
        {
            ("bybit", "linear", "GAP", "v1"): _full_path(m200=None),
            ("bybit", "linear", "C1", "v1"): _full_path(m50=MinuteState(False, False)),
        },
        outcome_horizon_minutes=_HORIZON,
    )
    summary = summarize(rows)
    assert summary["primary"]["by_asset"] == [
        {"group": ["asset:GAP"], "total": 1, "internal_gap": 1}
    ]
    assert summary["control_comparison"]["paired_reasons"] == [
        {
            "primary_reason": "internal_gap",
            "control_reason": "price_incomplete_inside",
            "control_rows": 1,
        }
    ]


def test_load_bar_states_reads_only_requested_routes_and_merges_duplicates(
    tmp_path: Path,
) -> None:
    """The real DuckDB query on a Parquet file with the cold-bar column names."""
    path = tmp_path / "bars.parquet"
    minute = _ENTRY
    with duckdb.connect(":memory:") as db:
        db.execute(
            """
            CREATE TABLE bars (exchange VARCHAR, market_type VARCHAR, symbol VARCHAR,
                capture_version VARCHAR, bucket_start TIMESTAMPTZ, price_complete BOOLEAN,
                unbackfilled_gap_minutes INTEGER, close_price DOUBLE)
            """
        )
        db.executemany(
            "INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("bybit", "linear", "A", "v1", minute, True, 0, 1.0),
                ("bybit", "linear", "A", "v1", minute, False, 0, 1.0),  # duplicate copy
                ("bybit", "linear", "A", "v1", minute + timedelta(minutes=1), True, 3, 1.0),
                ("bybit", "linear", "A", "v2", minute, True, 0, 1.0),  # other capture
                ("bybit", "linear", "B", "v1", minute, True, 0, 1.0),  # not requested
                ("bybit", "linear", "A", "v1", minute + timedelta(days=5), True, 0, 1.0),
            ],
        )
        db.execute("COPY bars TO ? (FORMAT PARQUET)", [str(path)])
    states = load_bar_states(
        [str(path)],
        [("bybit", "linear", "A", "v1")],
        window_start=minute,
        window_end=minute + timedelta(days=1),
    )
    assert set(states) == {("bybit", "linear", "A", "v1")}
    route = states[("bybit", "linear", "A", "v1")]
    assert route == {
        minute: MinuteState(price_complete=False, collector_gap=False),
        minute + timedelta(minutes=1): MinuteState(price_complete=True, collector_gap=True),
    }
