from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from schurfer_analytics.orderflow_pilot_report import (
    ORDERFLOW_COHORT_START,
    build_orderflow_report,
    build_parser,
    load_capture_episodes,
    render_json,
    render_markdown,
)

if TYPE_CHECKING:
    from pathlib import Path

    from schurfer_analytics.orderflow_pilot_report import CaptureEpisode


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
    offsets: tuple[int, ...] | None = None,
) -> Path:
    selected_offsets = offsets or (-1_700, -800, -200, -30, -1, 59, 299, 899, 3_599)
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
        for offset in selected_offsets
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


def _complete_capture(root: Path, first_observed: datetime) -> None:
    _write_subject(
        root,
        observed_symbol="PUMPUSDT",
        role="event",
        first_observed=first_observed,
        buy_notional=800,
        sell_notional=200,
    )
    for index, symbol in enumerate(("BTCUSDT", "ETHUSDT", "SOLUSDT"), start=1):
        _write_subject(
            root,
            observed_symbol=symbol,
            role="control",
            first_observed=first_observed,
            buy_notional=500 + index,
            sell_notional=500,
        )


def _load(
    root: Path,
    first_observed: datetime,
) -> tuple[tuple[CaptureEpisode, ...], int, int, str]:
    return load_capture_episodes(
        root,
        since=ORDERFLOW_COHORT_START,
        until=first_observed + timedelta(hours=2),
    )


def test_report_builds_three_separate_discovery_lanes(tmp_path: Path) -> None:
    first_observed = ORDERFLOW_COHORT_START + timedelta(hours=1)
    _complete_capture(tmp_path, first_observed)
    episodes, files, records, fingerprint = _load(tmp_path, first_observed)
    report = build_orderflow_report(
        episodes,
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

    assert report.capture_episodes == 1
    assert report.complete_matched_episodes == 1
    assert report.clusters == 1
    assert report.market_days == 1
    assert report.readiness == "collecting"
    assert files == 4
    assert records == 36
    assert {row.lane for row in report.lanes} == {
        "early_long",
        "squeeze_avoidance",
        "delayed_short",
    }
    assert all(row.matched_episodes == 1 for row in report.lanes)
    assert report.lead_features[-1].median_imbalance_lift is not None
    assert report.lead_features[-1].median_imbalance_lift > 0
    assert "Economic interpretation is withheld" in render_markdown(report)
    assert json.loads(render_json(report))["manifest"]["interpretation"] == (
        "discovery_only_no_strategy_change"
    )


def test_report_excludes_right_censored_and_incomplete_controls(tmp_path: Path) -> None:
    first_observed = ORDERFLOW_COHORT_START + timedelta(hours=1)
    _complete_capture(tmp_path, first_observed)
    episodes, files, records, fingerprint = _load(tmp_path, first_observed)

    right_censored = build_orderflow_report(
        episodes,
        files=files,
        records=records,
        input_fingerprint=fingerprint,
        root=tmp_path,
        since=ORDERFLOW_COHORT_START,
        until=first_observed + timedelta(minutes=30),
        generated_at=first_observed + timedelta(minutes=30),
        code_revision="abc123",
        working_tree_dirty=True,
    )
    assert right_censored.complete_matched_episodes == 0
    assert right_censored.exclusion_reasons[0].name == "right_censored"

    event_only_root = tmp_path / "event-only"
    _write_subject(
        event_only_root,
        observed_symbol="PUMPUSDT",
        role="event",
        first_observed=first_observed,
        buy_notional=800,
        sell_notional=200,
    )
    event_only, event_files, event_records, event_fingerprint = _load(
        event_only_root,
        first_observed,
    )
    incomplete = build_orderflow_report(
        event_only,
        files=event_files,
        records=event_records,
        input_fingerprint=event_fingerprint,
        root=event_only_root,
        since=ORDERFLOW_COHORT_START,
        until=first_observed + timedelta(hours=2),
        generated_at=first_observed + timedelta(hours=2),
        code_revision="abc123",
        working_tree_dirty=False,
    )
    assert incomplete.exclusion_reasons[0].name == "insufficient_controls"


def test_loader_rejects_activation_bucket_and_non_monotonic_rows(tmp_path: Path) -> None:
    first_observed = ORDERFLOW_COHORT_START + timedelta(hours=1)
    _write_subject(
        tmp_path,
        observed_symbol="PUMPUSDT",
        role="event",
        first_observed=first_observed,
        buy_notional=800,
        sell_notional=200,
        offsets=(0,),
    )
    with pytest.raises(ValueError, match="activation boundary bucket"):
        _load(tmp_path, first_observed)

    duplicate_root = tmp_path / "duplicate"
    _write_subject(
        duplicate_root,
        observed_symbol="PUMPUSDT",
        role="event",
        first_observed=first_observed,
        buy_notional=800,
        sell_notional=200,
        offsets=(-1, -1),
    )
    with pytest.raises(ValueError, match="buckets must be unique and ordered"):
        _load(duplicate_root, first_observed)


def test_input_fingerprint_is_independent_of_root_location(tmp_path: Path) -> None:
    first_observed = ORDERFLOW_COHORT_START + timedelta(hours=1)
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    _complete_capture(first_root, first_observed)
    _complete_capture(second_root, first_observed)

    assert _load(first_root, first_observed)[3] == _load(second_root, first_observed)[3]


def test_parser_requires_explicit_dirty_state() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--code-revision", "abc123"])
    args = parser.parse_args(["--code-revision", "abc123", "--no-working-tree-dirty"])
    assert args.since == ORDERFLOW_COHORT_START
    assert args.working_tree_dirty is False
