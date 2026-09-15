"""Coverage for the gated-deletion orchestration (PR 1, dry-run).

Impure Borg/DB work is faked, so these tests prove the runner assembles evidence
correctly, honours dry-run (drops nothing), and in a live run drops exactly the
cleared oldest-first prefix -- never anything the pure gate blocked or held.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from schurfer_analytics.cold_bar_gated_deletion import DropReceipt
from schurfer_analytics.cold_bar_gated_deletion_job import (
    ChunkCandidate,
    ExtractedOffsite,
    read_receipt,
    render_plan,
    run_gated_deletion,
    validate_chunks,
    write_receipt,
)

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)  # cutoff at 40d = 2026-08-05


def _receipt(day: str) -> DropReceipt:
    return DropReceipt(
        day=day,
        archive_name=f"bars-{day}T04:00:00",
        parquet_path=f"bars-{day}.parquet",
        parquet_sha256="pq",
        manifest_sha256="mf",
        row_count=1_000_000,
        schema_version="cold_bars_v1",
        source_fingerprint="cbfp_v1:fp",
        file_fingerprint="cbfp_v1:fp",
        fidelity_verified=True,
    )


class FakeCollectors:
    """Everything passing by default; a test flips one thing to see a day block."""

    def __init__(self, days: list[str]) -> None:
        # 1-day chunks whose range is derived from the day itself (as Timescale reports)
        self._chunks = tuple(
            ChunkCandidate(
                day=day,
                range_start=datetime.fromisoformat(day).replace(tzinfo=UTC),
                range_end=datetime.fromisoformat(day).replace(tzinfo=UTC) + timedelta(days=1),
            )
            for day in days
        )
        self.dropped: list[str] = []
        self.manifest_ok = set(days)
        self.receipt_offsite_ok = set(days)
        self.archive_ok = True
        self.gathered: list[str] = []

    def list_chunks(self) -> tuple[ChunkCandidate, ...]:
        return self._chunks

    def manifest_present(self, day: str) -> bool:
        return day in self.manifest_ok

    def receipt_offsite_confirmed(self, day: str) -> bool:
        return day in self.receipt_offsite_ok

    def archive_present(self, archive_name: str) -> bool:
        return self.archive_ok

    def extract_offsite(self, receipt: DropReceipt) -> ExtractedOffsite | None:
        return ExtractedOffsite(
            parquet_sha256="pq", manifest_sha256="mf", file_fingerprint="cbfp_v1:fp"
        )

    def recompute_source_fingerprint(self, day: str) -> str | None:
        self.gathered.append(day)
        return "cbfp_v1:fp"

    def drop_chunk(self, candidate: ChunkCandidate) -> None:
        self.dropped.append(candidate.day)


def _days(base: str, n: int) -> list[str]:
    d0 = datetime.fromisoformat(base)
    return [(d0 + timedelta(days=i)).date().isoformat() for i in range(n)]


# ---------- receipt I/O ----------


def test_receipt_round_trip(tmp_path: Path) -> None:
    r = _receipt("2026-07-20")
    write_receipt(tmp_path, r)
    assert read_receipt(tmp_path, "2026-07-20") == r
    assert read_receipt(tmp_path, "2026-07-21") is None


def test_receipt_is_immutable(tmp_path: Path) -> None:
    write_receipt(tmp_path, _receipt("2026-07-20"))
    with pytest.raises(FileExistsError):
        write_receipt(tmp_path, _receipt("2026-07-20"))


# ---------- dry-run vs live ----------


def test_dry_run_drops_nothing(tmp_path: Path) -> None:
    days = _days("2026-07-20", 3)
    for d in days:
        write_receipt(tmp_path, _receipt(d))
    c = FakeCollectors(days)
    result = run_gated_deletion(
        now=NOW, cutoff_days=40, receipts_dir=tmp_path, collectors=c, dry_run=True
    )
    assert result.to_drop == tuple(days)  # all would drop
    assert result.dropped == ()  # but nothing was
    assert c.dropped == []


def test_live_run_drops_the_cleared_prefix(tmp_path: Path) -> None:
    days = _days("2026-07-20", 3)
    for d in days:
        write_receipt(tmp_path, _receipt(d))
    c = FakeCollectors(days)
    result = run_gated_deletion(
        now=NOW, cutoff_days=40, receipts_dir=tmp_path, collectors=c, dry_run=False
    )
    assert result.dropped == tuple(days)
    assert c.dropped == days


def test_a_blocked_day_halts_the_frontier_and_stops_drops(tmp_path: Path) -> None:
    days = _days("2026-07-20", 3)
    for d in days:
        write_receipt(tmp_path, _receipt(d))
    c = FakeCollectors(days)
    c.manifest_ok.discard(days[1])  # middle day blocks (missing manifest)
    result = run_gated_deletion(
        now=NOW, cutoff_days=40, receipts_dir=tmp_path, collectors=c, dry_run=False
    )
    assert result.dropped == (days[0],)  # only the day before the block
    assert result.blocked_at is not None
    assert result.blocked_at[0] == days[1]
    assert c.dropped == [days[0]]


def test_missing_receipt_blocks_that_day(tmp_path: Path) -> None:
    days = _days("2026-07-20", 2)
    write_receipt(tmp_path, _receipt(days[0]))  # only the first has a receipt
    c = FakeCollectors(days)
    result = run_gated_deletion(
        now=NOW, cutoff_days=40, receipts_dir=tmp_path, collectors=c, dry_run=True
    )
    assert result.to_drop == (days[0],)
    assert result.blocked_at[0] == days[1]  # type: ignore[index]


def test_ineligible_recent_chunks_are_not_candidates(tmp_path: Path) -> None:
    # a chunk from yesterday is well inside the 40-day buffer -> never a candidate
    recent = (NOW - timedelta(days=1)).date().isoformat()
    write_receipt(tmp_path, _receipt(recent))
    c = FakeCollectors([recent])
    result = run_gated_deletion(
        now=NOW, cutoff_days=40, receipts_dir=tmp_path, collectors=c, dry_run=True
    )
    assert result.to_drop == ()
    assert result.blocked_at is None  # no eligible candidates at all


def test_a_raising_collector_blocks_its_day_without_crashing(tmp_path: Path) -> None:
    days = _days("2026-07-20", 2)
    for d in days:
        write_receipt(tmp_path, _receipt(d))
    c = FakeCollectors(days)

    def boom(day: str) -> str | None:
        raise RuntimeError("db exploded")

    c.recompute_source_fingerprint = boom  # type: ignore[method-assign]
    # The run must complete and block the first day with the gather error, not raise.
    result = run_gated_deletion(
        now=NOW, cutoff_days=40, receipts_dir=tmp_path, collectors=c, dry_run=True
    )
    assert result.to_drop == ()
    assert result.blocked_at is not None
    assert result.blocked_at[0] == days[0]
    assert "could not gather evidence" in result.blocked_at[1]


def test_render_plan_mentions_counts_and_halt(tmp_path: Path) -> None:
    days = _days("2026-07-20", 2)
    write_receipt(tmp_path, _receipt(days[0]))
    c = FakeCollectors(days)
    result = run_gated_deletion(
        now=NOW, cutoff_days=40, receipts_dir=tmp_path, collectors=c, dry_run=True
    )
    text = render_plan(result)
    assert "dry_run=True" in text
    assert "would drop 1 day" in text
    assert "halted at" in text


# ---------- lazy evaluation, gaps, bounded, chunk validation ----------


def _chunk(day: str) -> ChunkCandidate:
    start = datetime.fromisoformat(day).replace(tzinfo=UTC)
    return ChunkCandidate(day=day, range_start=start, range_end=start + timedelta(days=1))


def test_evidence_is_not_gathered_past_the_first_block(tmp_path: Path) -> None:
    days = _days("2026-07-20", 4)
    for d in days:
        write_receipt(tmp_path, _receipt(d))
    c = FakeCollectors(days)
    c.manifest_ok.discard(days[1])  # block at the 2nd day
    run_gated_deletion(now=NOW, cutoff_days=40, receipts_dir=tmp_path, collectors=c, dry_run=True)
    # only the cleared day and the blocking day are gathered; days 2 and 3 are not
    assert c.gathered == [days[0], days[1]]


def test_calendar_gap_halts_the_frontier(tmp_path: Path) -> None:
    days = ["2026-07-20", "2026-07-22"]  # 07-21 missing
    for d in days:
        write_receipt(tmp_path, _receipt(d))
    c = FakeCollectors(days)
    result = run_gated_deletion(
        now=NOW, cutoff_days=40, receipts_dir=tmp_path, collectors=c, dry_run=True
    )
    assert result.to_drop == ("2026-07-20",)
    assert result.blocked_at is not None
    assert "calendar gap" in result.blocked_at[1]


def test_max_eval_days_bounds_reconciliation(tmp_path: Path) -> None:
    days = _days("2026-07-20", 5)
    for d in days:
        write_receipt(tmp_path, _receipt(d))
    c = FakeCollectors(days)
    result = run_gated_deletion(
        now=NOW, cutoff_days=40, receipts_dir=tmp_path, collectors=c, dry_run=True, max_eval_days=2
    )
    assert c.gathered == days[:2]  # stopped after 2
    assert result.bounded_at == 2
    assert len(result.held) == 3


def test_validate_chunks_rejects_offset_chunk() -> None:
    bad = ChunkCandidate(
        day="2026-07-20",
        range_start=datetime(2026, 7, 20, 1, 0, tzinfo=UTC),
        range_end=datetime(2026, 7, 21, 1, 0, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="not a single UTC day"):
        validate_chunks((bad,))


def test_validate_chunks_rejects_multiday_chunk() -> None:
    start = datetime(2026, 7, 20, tzinfo=UTC)
    bad = ChunkCandidate(day="2026-07-20", range_start=start, range_end=start + timedelta(days=2))
    with pytest.raises(ValueError, match="not a single UTC day"):
        validate_chunks((bad,))


def test_validate_chunks_rejects_duplicate_day() -> None:
    with pytest.raises(ValueError, match="same day"):
        validate_chunks((_chunk("2026-07-20"), _chunk("2026-07-20")))
