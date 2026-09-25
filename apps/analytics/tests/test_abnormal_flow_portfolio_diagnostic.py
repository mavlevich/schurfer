from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import duckdb
import pytest
from schurfer_analytics import abnormal_flow_portfolio_diagnostic as diagnostic_module
from schurfer_analytics import abnormal_flow_replay as replay_module
from schurfer_analytics.abnormal_flow_portfolio_diagnostic import (
    _parse_k_values,
    _read_outcomes_once,
    _write_bundle,
    build_outcome_rows,
    build_positions,
    bundle_fingerprint,
    compare_to_baseline,
    outcomes_fingerprint,
    peak_planned_concurrency,
    portfolio_frontier,
    run_diagnostic,
    scenario_frontiers,
    write_outcome_rows,
)
from schurfer_analytics.abnormal_flow_portfolio_rescore import rescore_bundle
from schurfer_analytics.abnormal_flow_replay import (
    DecisionFeatures,
    Outcome,
    priced_proxy_path_times,
)
from schurfer_analytics.abnormal_flow_screen import AbnormalFlowContract
from schurfer_analytics.portfolio_engine_v2 import PositionSizingPolicy

if TYPE_CHECKING:
    from pathlib import Path


def _contract() -> AbnormalFlowContract:
    return AbnormalFlowContract(
        min_oi_growth_pct=10.0,
        min_buy_pressure_ratio=0.6,
        max_price_containment=0.05,
        min_oi_notional_usd=100.0,
        oi_usd_conversion_rule="bybit_native_value_binance_amount_x_decision_price_v1",
        position_usd=300.0,
        max_participation_frac=0.1,
        entry_execution_window_minutes=1,
        oi_freshness_limit_seconds_bybit=120,
        oi_freshness_limit_seconds_binance=300,
        calibration_rule="fixed_percentiles_on_prestart_window_v1",
        calibration_window_days=14,
        scan_lag_minutes=2,
        entry_reference="next_bar_open_priced_proxy_v1",
        exit_reference="horizon_bar_close_priced_proxy_v1",
        matching_rule="same_venue_regime_liquidity_pricemove_band_v1",
        controls_per_episode=1,
        portfolio_bank_usd=300.0,
        portfolio_max_slots=1,
        inference_rule="student_t_df_weeks_minus_one_v1",
        window_start_utc="2026-01-01T00:00:00+00:00",
        window_end_utc="2026-01-02T00:00:00+00:00",
        input_fingerprint="sha256:" + "a" * 64,
        taker_fee_bps=10.0,
        entry_slippage_bps=15.0,
        exit_slippage_bps=15.0,
        funding_bps_720m_binance=6.0,
        funding_bps_720m_bybit=5.0,
        min_resolved_episodes=1,
        max_missing_fraction=1.0,
        min_excess_over_control_pct=0.0,
        min_distinct_assets=1,
        min_utc_weeks=1,
        max_episodes_per_asset_frac=1.0,
        max_episodes_per_week_frac=1.0,
        min_control_coverage_frac=1.0,
    )


def _decision(symbol: str, minute: int) -> DecisionFeatures:
    return DecisionFeatures(
        exchange="bybit",
        market_type="linear",
        native_market_id=symbol,
        capture_version="v1",
        symbol=symbol,
        canonical_asset=f"asset:{symbol}",
        decision_at=datetime(2026, 1, 1, 0, minute, tzinfo=UTC),
        oi_growth_pct=20.0,
        buy_pressure=0.8,
        containment=0.01,
        oi_native_amount=100.0,
        oi_native_value_usd=10_000.0,
        decision_price=100.0,
        pre_decision_turnover_usd=10_000.0,
        iso_week="2026-W01",
    )


def _outcome(decision: DecisionFeatures, exit_price: float) -> Outcome:
    return Outcome(
        exchange=decision.exchange,
        market_type=decision.market_type,
        native_market_id=decision.native_market_id,
        capture_version=decision.capture_version,
        symbol=decision.symbol,
        decision_at=decision.decision_at,
        entry_price=100.0,
        exit_price=exit_price,
    )


def test_build_positions_preserves_resolved_and_unresolved() -> None:
    resolved = _decision("WIN", 0)
    missing = _decision("MISSING", 1)
    positions = build_positions(
        _contract(),
        [resolved, missing],
        {resolved.route_key(): _outcome(resolved, 110.0)},
    )

    assert positions[0].gross_return == pytest.approx(0.1)
    assert positions[0].net_return == pytest.approx(0.0945)
    assert positions[0].fees_bps == 20.0
    # Exit is the close of the horizon bar that starts at entry + 720m.
    assert positions[0].entry_at == datetime(2026, 1, 1, 0, 1, tzinfo=UTC)
    assert positions[0].exit_at == datetime(2026, 1, 1, 12, 2, tzinfo=UTC)
    assert positions[1].exit_at is None
    assert positions[1].unresolved_reason == "missing_or_incomplete_priced_proxy_path"


def test_frontier_includes_large_k_without_selecting_a_winner() -> None:
    decision = _decision("WIN", 0)
    positions = build_positions(
        _contract(),
        [decision],
        {decision.route_key(): _outcome(decision, 110.0)},
    )
    frontier = portfolio_frontier(
        positions,
        initial_capital=300.0,
        k_values=(1, 10, 15, 20),
        max_positions_per_asset=1,
        sizing_policy=PositionSizingPolicy.CURRENT_EQUITY_EQUAL_WEIGHT,
    )

    assert [row["k_slots"] for row in frontier] == [1, 10, 15, 20]
    assert [row["initial_position_usd"] for row in frontier] == [300.0, 30.0, 20.0, 15.0]
    assert all("selected" not in row for row in frontier)


def test_outcome_capability_is_reset_when_reader_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_reader(*_args: Any, **_kwargs: Any) -> Any:
        def fail(_episodes: Any) -> Any:
            assert replay_module.BURNED_DIAGNOSTIC_RETURNS_RUN_ENABLED is True
            assert replay_module.FORMAL_RETURNS_RUN_ENABLED is False
            raise RuntimeError("boom")

        return fail

    monkeypatch.setattr(diagnostic_module, "parquet_outcome_reader", fake_reader)
    replay_module.BURNED_DIAGNOSTIC_RETURNS_RUN_ENABLED = False
    with pytest.raises(RuntimeError, match="boom"):
        _read_outcomes_once([], [_decision("A", 0)], outcome_horizon_minutes=720)
    assert replay_module.BURNED_DIAGNOSTIC_RETURNS_RUN_ENABLED is False


def test_run_requires_explicit_burned_window_confirmation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="--burned-window-diagnostic"):
        run_diagnostic(
            snapshot_dir=tmp_path / "missing-snapshot",
            contract_path=tmp_path / "missing-contract",
            evaluation_manifest_path=tmp_path / "missing-evaluation-manifest",
            scan_manifest_path=tmp_path / "missing-scan-manifest",
            cold_bars_dir=tmp_path / "missing-bars",
            output_dir=tmp_path / "output",
        )


def test_bundle_is_immutable_and_contains_reusable_positions(tmp_path: Path) -> None:
    decision = _decision("WIN", 0)
    positions = build_positions(
        _contract(),
        [decision],
        {decision.route_key(): _outcome(decision, 110.0)},
    )
    report: dict[str, Any] = scenario_frontiers(
        positions,
        initial_capital=300.0,
        k_values=(1, 20),
        max_positions_per_asset=1,
        sizing_policy=PositionSizingPolicy.CURRENT_EQUITY_EQUAL_WEIGHT,
    )
    output = tmp_path / "result"
    rows = build_outcome_rows(
        _contract(), [decision], {decision.route_key(): _outcome(decision, 110.0)}
    )
    _write_bundle(
        output, write_positions=lambda path: write_outcome_rows(path, rows), report=report
    )

    assert json.loads((output / "diagnostic_report.json").read_text())["artifacts"]
    with duckdb.connect(":memory:") as db:
        row = db.execute(
            "SELECT count(*), min(net_return) FROM read_parquet(?)",
            [str(output / "portfolio_positions.parquet")],
        ).fetchone()
    assert row == pytest.approx((1, 0.0945))
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        _write_bundle(
            output, write_positions=lambda path: write_outcome_rows(path, rows), report=report
        )


def test_rescore_uses_saved_positions_without_market_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "schurfer_analytics.abnormal_flow_portfolio_rescore._run_code_state",
        lambda: {"code_revision": "test", "working_tree_dirty": False},
    )
    first = _decision("LOSS", 0)
    second = _decision("WIN", 1)
    rows = build_outcome_rows(
        _contract(),
        [first, second],
        {
            first.route_key(): _outcome(first, 90.0),
            second.route_key(): _outcome(second, 110.0),
        },
    )
    positions = [row.position for row in rows]
    source = tmp_path / "source"
    source_report: dict[str, Any] = {
        "policy": {"initial_capital": 300.0, "max_positions_per_asset": 1},
        "coverage": {"episodes": 2, "resolved": 2, "unresolved": 0},
        "provenance": {"snapshot_fingerprint": "a" * 64},
        **scenario_frontiers(
            positions,
            initial_capital=300.0,
            k_values=(1,),
            max_positions_per_asset=1,
            sizing_policy=PositionSizingPolicy.FIXED_INITIAL_EQUITY,
        ),
    }
    _write_bundle(
        source, write_positions=lambda path: write_outcome_rows(path, rows), report=source_report
    )

    output = tmp_path / "rescored"
    report = rescore_bundle(source, output, k_values=(1, 2))

    assert report["policy"]["sizing"] == "fixed_initial_equity"
    assert report["resolved_only_sensitivity"]["positions"] == 2
    assert (output / "diagnostic_report.json").is_file()
    # The provenance-bearing position artifact is carried over byte for byte.
    assert (output / "portfolio_positions.parquet").read_bytes() == (
        source / "portfolio_positions.parquet"
    ).read_bytes()


def test_path_times_match_the_bars_the_reader_consumes() -> None:
    decision_at = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    entry_at, exit_bar_start, exit_at = priced_proxy_path_times(decision_at, 720)

    assert entry_at == decision_at + timedelta(minutes=1)
    # 721 one-minute bars: [entry_at, exit_bar_start]; exit is that last bar's close.
    assert (exit_bar_start - entry_at) // timedelta(minutes=1) + 1 == 721
    assert exit_at == exit_bar_start + timedelta(minutes=1)


def test_outcome_rows_are_one_per_episode_with_route_and_prices(tmp_path: Path) -> None:
    resolved = _decision("WIN", 0)
    missing = _decision("MISSING", 1)
    rows = build_outcome_rows(
        _contract(), [resolved, missing], {resolved.route_key(): _outcome(resolved, 110.0)}
    )
    assert len(rows) == 2
    assert {row.position.decision_id for row in rows} == {
        rows[0].position.decision_id,
        rows[1].position.decision_id,
    }
    assert rows[0].native_market_id == "WIN"
    assert (rows[0].entry_price, rows[0].exit_price) == (100.0, 110.0)
    assert (rows[1].entry_price, rows[1].exit_price) == (None, None)
    assert rows[0].path_provenance == (
        "next_bar_open_priced_proxy_v1|horizon_bar_close_priced_proxy_v1"
    )

    path = tmp_path / "rows.parquet"
    write_outcome_rows(path, rows)
    with duckdb.connect(":memory:") as db:
        persisted = db.execute(
            "SELECT native_market_id, entry_price, exit_price, unresolved_reason "
            "FROM read_parquet(?) ORDER BY decision_at",
            [str(path)],
        ).fetchall()
    assert persisted == [
        ("WIN", 100.0, 110.0, None),
        ("MISSING", None, None, "missing_or_incomplete_priced_proxy_path"),
    ]


def test_duplicate_episode_routes_are_rejected() -> None:
    decision = _decision("DUP", 0)
    with pytest.raises(ValueError, match="duplicate route keys"):
        build_outcome_rows(_contract(), [decision, decision], {})


def test_fingerprints_are_stable_across_reruns_and_order() -> None:
    first = _decision("A", 0)
    second = _decision("B", 1)
    outcomes = {first.route_key(): _outcome(first, 105.0)}
    forward = build_outcome_rows(_contract(), [first, second], outcomes)
    backward = build_outcome_rows(_contract(), [second, first], outcomes)
    assert outcomes_fingerprint(forward) == outcomes_fingerprint(backward)

    report = {"generated_at": "t1", "coverage": {"episodes": 2}}
    rerun = {"generated_at": "t2", "coverage": {"episodes": 2}}
    assert bundle_fingerprint(report) == bundle_fingerprint(rerun)
    assert bundle_fingerprint(report) != bundle_fingerprint({"coverage": {"episodes": 3}})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1-20", tuple(range(1, 21))), ("1,2,10,15,20", (1, 2, 10, 15, 20))],
)
def test_parse_k_values(raw: str, expected: tuple[int, ...]) -> None:
    assert _parse_k_values(raw) == expected


def test_scenarios_bracket_unresolved_and_take_every_signal() -> None:
    win = _decision("WIN", 0)
    loss = _decision("LOSS", 1)
    missing = _decision("MISSING", 2)
    positions = build_positions(
        _contract(),
        [win, loss, missing],
        {win.route_key(): _outcome(win, 110.0), loss.route_key(): _outcome(loss, 80.0)},
    )
    assert positions[0].exit_at is not None
    assert positions[2].planned_exit_at == positions[0].exit_at + timedelta(minutes=2)

    result = scenario_frontiers(
        positions,
        initial_capital=300.0,
        k_values=(1, 3),
        max_positions_per_asset=1,
        sizing_policy=PositionSizingPolicy.FIXED_INITIAL_EQUITY,
    )
    scenarios = result["scenarios"]
    assert list(scenarios) == [
        "hold_to_end",
        "planned_exit_worst_resolved",
        "planned_exit_zero_gross",
        "planned_exit_mean_resolved",
    ]
    assert scenarios["planned_exit_worst_resolved"]["unresolved_gross_return"] == pytest.approx(
        -0.2
    )
    assert scenarios["planned_exit_mean_resolved"]["unresolved_gross_return"] == pytest.approx(
        -0.05
    )
    # Three overlapping 721-minute paths: every signal needs three slots of $100.
    assert result["take_every_signal_k"] == 3
    take_all = result["take_every_signal"]["planned_exit_zero_gross"]
    assert take_all["accepted_entries"] == 3
    assert take_all["rejection_counts"] == {}
    assert take_all["initial_position_usd"] == pytest.approx(100.0)
    # Worst case books the unresolved leg at the worst resolved return, after costs.
    worst = result["take_every_signal"]["planned_exit_worst_resolved"]
    assert worst["unresolved_assumed_net_pnl"] == pytest.approx(100.0 * (-0.2 - 0.0055))
    assert worst["accounting_complete"] is False


def test_peak_concurrency_releases_before_entries_at_the_same_instant() -> None:
    decision = _decision("A", 0)
    (first,) = build_positions(
        _contract(), [decision], {decision.route_key(): _outcome(decision, 100.0)}
    )
    assert first.exit_at is not None
    later = replace(
        first,
        decision_id="B",
        entry_at=first.exit_at,
        exit_at=first.exit_at + timedelta(hours=1),
    )
    assert peak_planned_concurrency([first, later]) == 1


def _report_for(positions: list[Any], *, path_rule: str) -> dict[str, Any]:
    resolved = sum(p.exit_at is not None for p in positions)
    return {
        "provenance": {
            "snapshot_fingerprint": "snap",
            "contract_hash": "c",
            "scan_manifest_hash": "s",
        },
        "policy": {
            "initial_capital": 300.0,
            "k_values": [1, 2],
            "max_positions_per_asset": 1,
            "sizing": "fixed_initial_equity",
            "path_rule": path_rule,
        },
        "coverage": {"episodes": len(positions), "resolved": resolved},
        **scenario_frontiers(
            positions,
            initial_capital=300.0,
            k_values=(1, 2),
            max_positions_per_asset=1,
            sizing_policy=PositionSizingPolicy.FIXED_INITIAL_EQUITY,
        ),
    }


def _compare(
    report: dict[str, Any], baseline: dict[str, Any], positions: Any, baseline_positions: Any
) -> dict[str, Any]:
    return compare_to_baseline(
        report,
        baseline,
        positions,
        baseline_positions,
        max_positions_per_asset=1,
        sizing_policy=PositionSizingPolicy.FIXED_INITIAL_EQUITY,
    )


def _strict_and_relaxed() -> tuple[list[Any], list[Any]]:
    win, loss, lost = _decision("WIN", 0), _decision("LOSS", 1), _decision("LOST", 2)
    known = {win.route_key(): _outcome(win, 110.0), loss.route_key(): _outcome(loss, 95.0)}
    strict = build_positions(_contract(), [win, loss, lost], known)
    relaxed = build_positions(
        _contract(), [win, loss, lost], {**known, lost.route_key(): _outcome(lost, 104.0)}
    )
    return strict, relaxed


def test_comparison_holds_the_baseline_assumptions_fixed() -> None:
    """Review P1: worst/mean assumptions are recomputed from the resolved rows, so a raw
    delta would mix the rule change with an assumption change."""
    strict, relaxed = _strict_and_relaxed()
    baseline = _report_for(strict, path_rule="every_minute_v1")
    report = _report_for(relaxed, path_rule="entry_exit_bars_v1")
    comparison = _compare(report, baseline, relaxed, strict)

    worst = comparison["scenarios"]["planned_exit_worst_resolved"]
    assert worst["fixed_unresolved_gross_return"] == pytest.approx(-0.05)
    assert worst["this_run_recomputed_gross_return"] == pytest.approx(-0.05)
    mean = comparison["scenarios"]["planned_exit_mean_resolved"]
    assert mean["fixed_unresolved_gross_return"] == pytest.approx(0.025)
    assert mean["this_run_recomputed_gross_return"] == pytest.approx(0.03)
    k2 = comparison["scenarios"]["hold_to_end"]["by_k"][1]
    assert k2["k_slots"] == 2
    pnl = k2["total_net_pnl_incl_assumed"]
    assert pnl["delta"] == pytest.approx(pnl["this_run"] - pnl["baseline"])
    assert comparison["coverage"]["this_run"]["resolved"] == 3


def test_comparison_refuses_a_baseline_that_differs_in_more_than_the_rule() -> None:
    strict, relaxed = _strict_and_relaxed()
    report = _report_for(relaxed, path_rule="entry_exit_bars_v1")
    other_snapshot = _report_for(strict, path_rule="every_minute_v1")
    other_snapshot["provenance"]["snapshot_fingerprint"] = "other"
    with pytest.raises(ValueError, match="snapshot_fingerprint"):
        _compare(report, other_snapshot, relaxed, strict)
    other_sizing = _report_for(strict, path_rule="every_minute_v1")
    other_sizing["policy"]["sizing"] = "current_equity_equal_weight"
    with pytest.raises(ValueError, match="policy sizing"):
        _compare(report, other_sizing, relaxed, strict)


def test_comparison_requires_the_same_scenarios_and_k_values() -> None:
    """Review P1: a missing scenario or K must fail, not be skipped silently."""
    strict, relaxed = _strict_and_relaxed()
    report = _report_for(relaxed, path_rule="entry_exit_bars_v1")
    fewer = _report_for(strict, path_rule="every_minute_v1")
    del fewer["scenarios"]["planned_exit_zero_gross"]
    with pytest.raises(ValueError, match="different scenarios"):
        _compare(report, fewer, relaxed, strict)
    short_k = _report_for(strict, path_rule="every_minute_v1")
    short_k["scenarios"]["hold_to_end"]["frontier"] = short_k["scenarios"]["hold_to_end"][
        "frontier"
    ][:1]
    with pytest.raises(ValueError, match="different K values"):
        _compare(report, short_k, relaxed, strict)


def test_comparison_refuses_a_changed_baseline_resolved_position() -> None:
    strict, relaxed = _strict_and_relaxed()
    report = _report_for(relaxed, path_rule="entry_exit_bars_v1")
    baseline = _report_for(strict, path_rule="every_minute_v1")
    tampered = [replace(relaxed[0], net_return=0.5, gross_return=0.5055), *relaxed[1:]]
    with pytest.raises(ValueError, match="baseline-resolved position"):
        _compare(report, baseline, tampered, strict)


def test_baseline_must_be_the_registered_report(tmp_path: Path) -> None:
    bundle = tmp_path / "baseline"
    bundle.mkdir()
    (bundle / "diagnostic_report.json").write_text("{}")
    digest = "sha256:" + hashlib.sha256(b"{}").hexdigest()
    (bundle / "diagnostic_report.sha256").write_text(digest + "\n")
    assert diagnostic_module._load_verified_report(bundle, expected_sha256=digest) == {}
    with pytest.raises(ValueError, match="is not the registered"):
        diagnostic_module._load_verified_report(bundle, expected_sha256="sha256:" + "0" * 64)


def test_the_burned_flag_is_checked_before_any_baseline_is_opened(tmp_path: Path) -> None:
    """Review P2: the baseline carries PnL, so it must not be read without the flag."""
    with pytest.raises(ValueError, match="--burned-window-diagnostic"):
        run_diagnostic(
            snapshot_dir=tmp_path / "missing",
            contract_path=tmp_path / "missing",
            evaluation_manifest_path=tmp_path / "missing",
            scan_manifest_path=tmp_path / "missing",
            cold_bars_dir=tmp_path / "missing",
            output_dir=tmp_path / "out",
            baseline_bundle=tmp_path / "no-such-baseline",
            baseline_sha256="sha256:" + "0" * 64,
        )
