from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest
import schurfer_analytics.orderflow_pilot_report as pilot_report
from schurfer_analytics.orderflow_endpoint_sensitivity_report import (
    ORDERFLOW_SENSITIVITY_CANDIDATE_BOUNDS_MS,
    ORDERFLOW_SENSITIVITY_DIAGNOSTIC_BOUND_MS,
    BoundResult,
    OrderflowSensitivityReport,
    build_parser,
    build_sensitivity_report,
    load_raw_capture_episodes,
    render_json,
    render_markdown,
)
from schurfer_analytics.orderflow_pilot_report import (
    ORDERFLOW_COHORT_START,
    ORDERFLOW_MAX_ENDPOINT_STALENESS_MS,
)

if TYPE_CHECKING:
    from pathlib import Path

    from schurfer_analytics.orderflow_endpoint_sensitivity_report import RawCaptureEpisode


def _record(
    *,
    event_id: int,
    event_base: str,
    event_symbol: str,
    observed_symbol: str,
    role: str,
    first_observed: datetime,
    offset_seconds: int,
    buy_notional: float,
    sell_notional: float,
    price: float,
) -> dict[str, object]:
    start_ms = int((first_observed + timedelta(seconds=offset_seconds)).timestamp() * 1_000)
    return {
        "contract_version": "bybit_orderflow_pilot_v1",
        "pump_event_id": event_id,
        "event_base": event_base,
        "event_symbol": event_symbol,
        "observed_symbol": observed_symbol,
        "role": role,
        "first_observed_at_ms": int(first_observed.timestamp() * 1_000),
        "capture_expires_at_ms": int((first_observed + timedelta(hours=1)).timestamp() * 1_000),
        "bucket": {
            "schema_version": 1,
            "exchange": "bybit",
            "symbol": observed_symbol,
            "bucket_start_ms": start_ms,
            "first_event_at_ms": start_ms + 100,
            "last_event_at_ms": start_ms + 200,
            "last_received_at_ms": start_ms + 210,
            "open": price,
            "high": price * 1.001,
            "low": price * 0.999,
            "close": price,
            "buy_notional": buy_notional,
            "sell_notional": sell_notional,
            "buy_quantity": buy_notional / price,
            "sell_quantity": sell_notional / price,
            "buy_trades": 2,
            "sell_trades": 1,
            "max_lag_ms": 10,
        },
    }


def _write_subject(
    root: Path,
    *,
    event_id: int = 42,
    event_base: str = "PUMP",
    event_symbol: str = "PUMPUSDT",
    observed_symbol: str,
    role: str,
    first_observed: datetime,
    buy_notional: float,
    sell_notional: float,
    anchor_offset_seconds: int,
) -> Path:
    # -1_700/-800/-200 fill the 30m/15m/5m pre-trigger windows; anchor_offset_seconds
    # is the single bucket immediately before first_observed (the anchor) — the only
    # variable under test; 59/299/899/3_599 are comfortably fresh post-horizon
    # endpoints at every candidate bound, isolating the anchor as the one thing that
    # flips completeness between bounds.
    offsets = (-1_700, -800, -200, anchor_offset_seconds, 59, 299, 899, 3_599)
    rows = [
        _record(
            event_id=event_id,
            event_base=event_base,
            event_symbol=event_symbol,
            observed_symbol=observed_symbol,
            role=role,
            first_observed=first_observed,
            offset_seconds=offset,
            buy_notional=buy_notional,
            sell_notional=sell_notional,
            price=100 + offset / 10_000,
        )
        for offset in offsets
    ]
    path = (
        root
        / first_observed.date().isoformat()
        / f"event-{event_id}"
        / f"{role}-{observed_symbol}.jsonl.gz"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")
    return path


def _capture(root: Path, first_observed: datetime, *, anchor_offset_seconds: int) -> None:
    _write_subject(
        root,
        observed_symbol="PUMPUSDT",
        role="event",
        first_observed=first_observed,
        buy_notional=800,
        sell_notional=200,
        anchor_offset_seconds=anchor_offset_seconds,
    )
    for index, symbol in enumerate(("BTCUSDT", "ETHUSDT", "SOLUSDT"), start=1):
        _write_subject(
            root,
            observed_symbol=symbol,
            role="control",
            first_observed=first_observed,
            buy_notional=500 + index,
            sell_notional=500,
            anchor_offset_seconds=anchor_offset_seconds,
        )


def _load(
    root: Path,
    first_observed: datetime,
) -> tuple[tuple[RawCaptureEpisode, ...], int, int, str]:
    return load_raw_capture_episodes(
        root,
        since=ORDERFLOW_COHORT_START,
        until=first_observed + timedelta(hours=2),
    )


def _bound(report: OrderflowSensitivityReport, bound_ms: int) -> BoundResult:
    return next(item for item in report.bounds if item.bound_ms == bound_ms)


def test_report_recovers_episode_at_a_wider_bound_without_reparsing(tmp_path: Path) -> None:
    """The exact mechanism found in the 2026-08-05/06 diagnosis: an anchor bucket
    that is real but ~8s stale fails the registered 5000ms bound (excluded as
    stale_or_missing_anchor) but passes every bound from 10s upward — recovered
    from the SAME single parse, not a re-parse per bound."""
    first_observed = ORDERFLOW_COHORT_START + timedelta(hours=1)
    _capture(tmp_path, first_observed, anchor_offset_seconds=-8)
    raw_episodes, files, records, fingerprint = _load(tmp_path, first_observed)

    report = build_sensitivity_report(
        raw_episodes,
        files=files,
        records=records,
        input_fingerprint=fingerprint,
        root=tmp_path,
        since=ORDERFLOW_COHORT_START,
        until=first_observed + timedelta(hours=2),
        generated_at=first_observed + timedelta(hours=2),
        code_revision="abc123",
        working_tree_dirty=False,
    )

    assert _bound(report, 5_000).complete_episodes == 0
    assert _bound(report, 5_000).exclusion_reasons[0].name == "stale_or_missing_anchor"
    for bound_ms in (10_000, 15_000, 20_000, 30_000, 60_000):
        assert _bound(report, bound_ms).complete_episodes == 1
        assert _bound(report, bound_ms).clusters == 1


def test_manifest_exposes_bounds_without_mutating_the_registered_v1_constant(
    tmp_path: Path,
) -> None:
    """Regression: this report must never monkeypatch or otherwise mutate v1's
    module-level ORDERFLOW_MAX_ENDPOINT_STALENESS_MS — it re-derives validity from
    its own raw, unconditional parse instead."""
    first_observed = ORDERFLOW_COHORT_START + timedelta(hours=1)
    _capture(tmp_path, first_observed, anchor_offset_seconds=-1)
    raw_episodes, files, records, fingerprint = _load(tmp_path, first_observed)

    report = build_sensitivity_report(
        raw_episodes,
        files=files,
        records=records,
        input_fingerprint=fingerprint,
        root=tmp_path,
        since=ORDERFLOW_COHORT_START,
        until=first_observed + timedelta(hours=2),
        generated_at=first_observed + timedelta(hours=2),
        code_revision="abc123",
        working_tree_dirty=False,
    )

    assert report.manifest.registered_v1_bound_ms == 5_000
    assert report.manifest.candidate_bounds_ms == ORDERFLOW_SENSITIVITY_CANDIDATE_BOUNDS_MS
    assert report.manifest.diagnostic_only_bound_ms == ORDERFLOW_SENSITIVITY_DIAGNOSTIC_BOUND_MS
    assert [bound.bound_ms for bound in report.bounds] == [
        5_000,
        10_000,
        15_000,
        20_000,
        30_000,
        60_000,
    ]
    assert [bound.is_diagnostic_only for bound in report.bounds] == [
        False,
        False,
        False,
        False,
        False,
        True,
    ]
    # The actual module constant v1's own report reads from must be untouched.
    assert pilot_report.ORDERFLOW_MAX_ENDPOINT_STALENESS_MS == ORDERFLOW_MAX_ENDPOINT_STALENESS_MS
    assert pilot_report.ORDERFLOW_MAX_ENDPOINT_STALENESS_MS == 5_000


def test_lane_rows_reflect_completeness_per_bound(tmp_path: Path) -> None:
    first_observed = ORDERFLOW_COHORT_START + timedelta(hours=1)
    _capture(tmp_path, first_observed, anchor_offset_seconds=-8)
    raw_episodes, files, records, fingerprint = _load(tmp_path, first_observed)

    report = build_sensitivity_report(
        raw_episodes,
        files=files,
        records=records,
        input_fingerprint=fingerprint,
        root=tmp_path,
        since=ORDERFLOW_COHORT_START,
        until=first_observed + timedelta(hours=2),
        generated_at=first_observed + timedelta(hours=2),
        code_revision="abc123",
        working_tree_dirty=False,
    )

    excluded = _bound(report, 5_000)
    assert all(row.matched_episodes == 0 for row in excluded.lanes)

    included = _bound(report, 15_000)
    assert all(row.matched_episodes == 1 for row in included.lanes)
    early_long_row = next(
        row
        for row in included.lanes
        if row.lane == "early_long" and row.feature == "5m_to_1m_imbalance_lift"
    )
    # The event is skewed buy-heavy (800/200) against balanced controls (~500/500);
    # the imbalance lift must be positive, same shape as the pilot's own test.
    assert early_long_row.median_feature is not None
    assert early_long_row.median_feature > 0


def test_markdown_flags_the_diagnostic_bound_and_never_endorses_it(tmp_path: Path) -> None:
    first_observed = ORDERFLOW_COHORT_START + timedelta(hours=1)
    _capture(tmp_path, first_observed, anchor_offset_seconds=-1)
    raw_episodes, files, records, fingerprint = _load(tmp_path, first_observed)

    report = build_sensitivity_report(
        raw_episodes,
        files=files,
        records=records,
        input_fingerprint=fingerprint,
        root=tmp_path,
        since=ORDERFLOW_COHORT_START,
        until=first_observed + timedelta(hours=2),
        generated_at=first_observed + timedelta(hours=2),
        code_revision="abc123",
        working_tree_dirty=False,
    )
    markdown = render_markdown(report)
    payload = json.loads(render_json(report))

    assert "60s (diagnostic only)" in markdown
    assert "only turns positive at 60s" in markdown
    assert payload["manifest"]["report_version"] == "bybit_orderflow_endpoint_sensitivity_v1"
    assert "Discovery-only diagnostic" in markdown
    assert "never modifies its contract" in markdown


def test_parser_defaults_match_the_pilot_cohort() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--code-revision", "abc123"])
    args = parser.parse_args(["--code-revision", "abc123", "--no-working-tree-dirty"])
    assert args.since == ORDERFLOW_COHORT_START
    assert args.working_tree_dirty is False
