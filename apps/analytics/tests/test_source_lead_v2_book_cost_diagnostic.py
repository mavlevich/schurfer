"""Synthetic boundaries for the registered book-cost diagnostic."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from schurfer_analytics import source_lead_v2_book_cost_diagnostic as diagnostic
from schurfer_analytics.source_lead_exit_capture import exit_target_at
from schurfer_analytics.source_lead_multi_source_report import write_once
from schurfer_analytics.source_lead_v2_book_cost_diagnostic import (
    BookCostRow,
    build_report,
)

if TYPE_CHECKING:
    from pathlib import Path

ENTRY = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
SNAPSHOT = {"s": "ABCUSDT", "b": [["1.99", "100"]], "a": [["2.01", "100"]]}
SNAPSHOT_SHA = hashlib.sha256(
    json.dumps(SNAPSHOT, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


def _row(capture_id: int = 1, **changes: Any) -> BookCostRow:
    row = BookCostRow(
        capture_id=capture_id,
        source_first_observed_at=ENTRY - timedelta(seconds=10),
        target_status="sampled",
        entry_at=ENTRY,
        target_identity_key="bybit:swap:ABCUSDT:1",
        capture_contract_size_source="instrument",
        capture_book_age_ms="300",
        capture_spread_bps="20",
        capture_ask_impact_bps="15",
        shadow_version="source_lead_shadow_v1",
        send_identity_key="bybit:swap:ABCUSDT:1",
        send_cost_capture_version="source_lead_send_book_costs_v1",
        attempt_outcome="shadow_recorded",
        attempt_late=False,
        send_book_age_ms=200,
        send_quantity=Decimal("25"),
        send_spread_bps=Decimal("25"),
        send_notional_ask_impact_bps=Decimal("18"),
        send_qty_ask_impact_bps=Decimal("19"),
        exit_version="source_lead_exit_book_v1",
        exit_target_exchange="bybit",
        exit_outcome="sampled",
        exit_timeliness="on_time",
        exit_identity_key="bybit:swap:ABCUSDT:1",
        exit_entry_at=ENTRY,
        exit_target_at=exit_target_at(ENTRY),
        exit_book_age_ms=400,
        exit_contract_size_source="instrument",
        exit_quantity=Decimal("25"),
        exit_filled_quantity=Decimal("25"),
        exit_spread_bps=Decimal("30"),
        exit_impact_bps=Decimal("22"),
        exit_book_snapshot=SNAPSHOT,
        exit_book_sha256=SNAPSHOT_SHA,
    )
    return replace(row, **changes)


def test_full_denominator_and_separate_book_costs() -> None:
    no_attempt = BookCostRow(
        capture_id=2,
        source_first_observed_at=ENTRY,
        target_status="sampled",
        entry_at=ENTRY,
        target_identity_key="bybit:swap:ABCUSDT:1",
        capture_contract_size_source="instrument",
        capture_book_age_ms="250",
        capture_spread_bps="20",
        capture_ask_impact_bps="15",
    )
    report = build_report([_row(), no_attempt])
    assert report["eligible"] == 2
    assert report["attempt_outcome"] == {"no_attempt": 1, "shadow_recorded": 1}
    assert report["missingness"]["no_attempt"] == 1
    assert report["missingness"]["no_exit"] == 1
    assert report["paired_on_time"] == 1
    assert report["paired_same_quantity"] == 1
    costs = report["book_cost_bps"]
    assert costs["capture_ask_impact_bps"]["n"] == 2
    assert costs["send_on_time_notional_ask_impact_bps"]["mean"] == 18
    assert costs["send_on_time_qty_ask_impact_bps"]["mean"] == 19
    assert costs["exit_on_time_bid_impact_bps"]["mean"] == 22
    assert not any("round_trip" in key or "net" in key for key in costs)


def test_late_and_unfresh_books_never_enter_principal_distribution() -> None:
    late = _row(1, attempt_late=True, exit_timeliness="late")
    stale = _row(2, send_book_age_ms=2100, exit_book_age_ms=-1001)
    report = build_report([late, stale])
    assert report["book_cost_bps"]["send_on_time_notional_ask_impact_bps"]["n"] == 0
    assert report["book_cost_bps"]["send_late_notional_ask_impact_bps"]["n"] == 1
    assert report["book_cost_bps"]["exit_on_time_bid_impact_bps"]["n"] == 0
    assert report["missingness"]["send_unfresh_book"] == 1
    assert report["missingness"]["exit_timeliness:late"] == 1
    assert report["missingness"]["exit_unfresh_book"] == 1


def test_identity_depth_and_quantity_are_not_silently_mixed() -> None:
    changed_quantity = _row(1, exit_quantity=Decimal("24"), exit_filled_quantity=Decimal("24"))
    wrong_identity = _row(2, exit_identity_key="bybit:swap:OTHERUSDT:1")
    thin = _row(3, exit_filled_quantity=Decimal("24"))
    wrong_send = _row(4, send_identity_key="bybit:swap:OTHERUSDT:1")
    report = build_report([changed_quantity, wrong_identity, thin, wrong_send])
    assert report["paired_on_time"] == 1
    assert report["paired_same_quantity"] == 0
    assert report["missingness"]["exit_identity_or_entry_mismatch"] == 1
    assert report["missingness"]["exit_depth_or_quantity_missing"] == 1
    assert report["missingness"]["send_identity_or_capture_mismatch"] == 1


def test_sampled_exit_snapshot_corruption_publishes_no_costs_even_when_late() -> None:
    for row, reason in (
        (
            _row(exit_timeliness="late", exit_book_snapshot={"b": []}),
            "sampled_exit_snapshot_hash_mismatch",
        ),
        (_row(exit_book_sha256=None), "sampled_exit_snapshot_missing"),
    ):
        report = build_report([_row(2), row])
        assert report["status"] == "integrity_failed"
        assert report["integrity_failures"] == {reason: 1}
        assert report["rows_read"] == 2
        assert "book_cost_bps" not in report
        assert "book metric" not in diagnostic.render_markdown(
            {
                **report,
                "database_now_utc": ENTRY.isoformat(),
                "code_revision": "test",
                "working_tree_dirty": False,
                "row_snapshot_sha256": "a" * 64,
            }
        )


def test_duplicate_and_unknown_versions_publish_integrity_failures() -> None:
    for rows, reason in (
        ([_row(), _row()], "duplicate_qualified_capture"),
        ([_row(send_cost_capture_version="unknown_v2")], "unexpected_send_cost_version"),
        ([_row(exit_version="unknown_v2")], "unexpected_exit_version"),
    ):
        report = build_report(rows)
        assert report["status"] == "integrity_failed"
        assert report["integrity_failures"] == {reason: 1}
        assert "book_cost_bps" not in report


def test_pre_version_attempt_is_counted_without_costs() -> None:
    report = build_report([_row(send_cost_capture_version=None)])
    assert report["send_cost_version"] == {"pre_version_or_none": 1}
    assert report["book_cost_bps"]["send_on_time_notional_ask_impact_bps"]["n"] == 0
    assert report["missingness"]["send_pre_version"] == 1


def test_nonfinite_and_negative_costs_are_not_reported() -> None:
    report = build_report(
        [
            _row(
                capture_ask_impact_bps="NaN",
                send_spread_bps=Decimal("NaN"),
                exit_impact_bps=Decimal("-1"),
            )
        ]
    )
    assert report["missingness"]["capture_cost_missing_or_invalid"] == 1
    assert report["missingness"]["send_cost_missing_or_invalid"] == 1
    assert report["missingness"]["exit_cost_missing_or_invalid"] == 1
    assert report["paired_on_time"] == 0


@pytest.mark.asyncio
async def test_published_artifact_is_reused_without_second_database_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def fake_load_rows(_db_url: str) -> tuple[datetime, list[BookCostRow]]:
        nonlocal calls
        calls += 1
        return datetime(2026, 10, 15, tzinfo=UTC), [_row()]

    monkeypatch.setattr(diagnostic, "load_rows", fake_load_rows)
    first = await diagnostic.report_once(
        "unused", artifact_dir=tmp_path, code_revision="abc", dirty=False
    )
    second = await diagnostic.report_once(
        "unused", artifact_dir=tmp_path, code_revision="different", dirty=True
    )
    assert calls == 1
    assert second == first
    assert first["code_revision"] == "abc"
    artifact = tmp_path / diagnostic.ARTIFACT_NAME
    assert artifact.exists()
    assert (tmp_path / f"{diagnostic.ARTIFACT_NAME}.sha256").exists()


@pytest.mark.asyncio
async def test_integrity_failed_artifact_is_one_time_and_does_not_reload_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def fake_load_rows(_db_url: str) -> tuple[datetime, list[BookCostRow]]:
        nonlocal calls
        calls += 1
        return datetime(2026, 10, 15, tzinfo=UTC), [_row(exit_book_sha256=None)]

    monkeypatch.setattr(diagnostic, "load_rows", fake_load_rows)
    first = await diagnostic.report_once(
        "unused", artifact_dir=tmp_path, code_revision="abc", dirty=False
    )
    second = await diagnostic.report_once(
        "unused", artifact_dir=tmp_path, code_revision="different", dirty=True
    )
    assert calls == 1
    assert second == first
    assert first["status"] == "integrity_failed"
    assert first["integrity_failures"] == {"sampled_exit_snapshot_missing": 1}
    assert "book_cost_bps" not in first


def test_cli_reports_integrity_failure_with_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_run(_args: object) -> tuple[str, bool]:
        return "Status: integrity_failed.\n", True

    monkeypatch.setattr(diagnostic, "_run", fake_run)
    monkeypatch.setattr(sys, "argv", ["source-lead-v2-book-cost-diagnostic"])
    with pytest.raises(SystemExit) as exc:
        diagnostic.main()
    assert exc.value.code == 2
    assert capsys.readouterr().out == "Status: integrity_failed.\n"


def test_saved_artifact_rejects_a_different_registered_version(tmp_path: Path) -> None:
    path = tmp_path / diagnostic.ARTIFACT_NAME
    report = build_report([_row()])
    report["exit_version"] = "other_exit_v2"
    write_once(path, report)
    with pytest.raises(ValueError, match="unexpected exit_version"):
        diagnostic._load_artifact(path)
