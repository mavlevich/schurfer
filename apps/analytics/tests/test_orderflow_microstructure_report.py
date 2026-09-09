"""Report-orchestration coverage: build_report, rendering, and the held-out
cutoff guard. No database -- rows are hand built."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.orderflow_microstructure import (
    HELD_OUT_START,
    VERDICT_CANDIDATE,
    ResolvedDecisionRow,
)
from schurfer_analytics.orderflow_microstructure_report import (
    OrderflowReport,
    _guard_window,
    build_report,
    render_json,
    render_markdown,
)
from schurfer_analytics.orderflow_microstructure_repository import HeldOutWindowError

_DB_NOW = datetime(2026, 8, 26, tzinfo=UTC)
_COHORT_START = datetime(2026, 8, 10, tzinfo=UTC)
_COHORT_END = HELD_OUT_START


def _rows() -> tuple[ResolvedDecisionRow, ...]:
    rows: list[ResolvedDecisionRow] = []
    nets = [0.0, 0.5, 1.0, 1.5, 2.1]
    running = 0
    for q, base_net in enumerate(nets):
        for j in range(160):
            feature = float(q * 160 + j)
            rows.append(
                ResolvedDecisionRow(
                    decision_id=f"d{running:05d}",
                    base=f"A{running % 40}",
                    exchange="bybit" if running % 2 == 0 else "binance",
                    ts=datetime(2026, 8, 12, tzinfo=UTC) + timedelta(minutes=running),
                    short_return_pct=base_net,
                    mfe_pct=1.0,
                    mae_pct=-1.0,
                    match_count=1,
                    native_market_id=f"A{running % 40}USDT",
                    market_type="linear",
                    bars_10m=10,
                    bars_5m=5,
                    bars_20m=20,
                    imbalance_10m=feature,
                    imbalance_5m=feature,
                    imbalance_20m=feature,
                )
            )
            running += 1
    # A coverage-lost decision that must never become a measured episode.
    rows.append(
        ResolvedDecisionRow(
            decision_id="unresolved",
            base="ZZZ",
            exchange="kraken",
            ts=datetime(2026, 8, 12, tzinfo=UTC),
            short_return_pct=99.0,
            mfe_pct=None,
            mae_pct=None,
            match_count=0,
            native_market_id=None,
            market_type=None,
            bars_10m=0,
            bars_5m=0,
            bars_20m=0,
            imbalance_10m=None,
            imbalance_5m=None,
            imbalance_20m=None,
        )
    )
    return tuple(rows)


def _build() -> OrderflowReport:
    return build_report(
        rows=_rows(),
        db_snapshot_at=_DB_NOW,
        cohort_start=_COHORT_START,
        cohort_end=_COHORT_END,
        code_revision="abc123",
        working_tree_dirty=False,
    )


def test_build_report_produces_a_candidate_and_excludes_coverage_loss() -> None:
    report = _build()
    assert report.verdict.verdict == VERDICT_CANDIDATE
    assert report.total_cohort_decisions == 801
    assert report.measured_episodes == 800
    assert report.formal_run is True
    # kraken appears only as coverage loss, never as a measured episode.
    kraken = next(c for c in report.coverage_by_exchange if c.exchange == "kraken")
    assert kraken.measured_episodes == 0
    assert kraken.unresolved_identity == 1


def test_context_windows_are_present_but_separate_from_the_primary() -> None:
    report = _build()
    assert report.primary.lookback_minutes == 10
    assert {a.lookback_minutes for a in report.context} == {5, 20}


def test_fingerprint_is_deterministic_across_two_builds() -> None:
    assert _build().dataset_fingerprint == _build().dataset_fingerprint


def test_dirty_tree_is_not_a_formal_run() -> None:
    report = build_report(
        rows=_rows(),
        db_snapshot_at=_DB_NOW,
        cohort_start=_COHORT_START,
        cohort_end=_COHORT_END,
        code_revision="abc123",
        working_tree_dirty=True,
    )
    assert report.formal_run is False
    assert "NOT A FORMAL RUN" in render_markdown(report)


def test_renderers_do_not_raise() -> None:
    report = _build()
    assert report.hypothesis_id in render_markdown(report)
    parsed = json.loads(render_json(report))
    assert parsed["verdict"]["verdict"] == VERDICT_CANDIDATE


def test_guard_refuses_reading_the_held_out_window() -> None:
    with pytest.raises(HeldOutWindowError):
        _guard_window(cohort_start=_COHORT_START, cohort_end=HELD_OUT_START + timedelta(days=1))


def test_guard_refuses_a_start_before_the_frozen_discovery_start() -> None:
    with pytest.raises(ValueError, match="predates the frozen discovery start"):
        _guard_window(cohort_start=_COHORT_START - timedelta(days=1), cohort_end=_COHORT_END)
