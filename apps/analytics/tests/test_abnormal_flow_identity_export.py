"""Tests for the pure core of the point-in-time identity export (no DB).

Pins the per-route interval logic: valid_to = the venue's next snapshot boundary, a
route absent from a later snapshot is delisted at that boundary, window filtering keeps
the carry-in snapshot, and single-venue instruments still get a record.
"""

from __future__ import annotations

from datetime import UTC, datetime

from schurfer_analytics.abnormal_flow_identity_export import (
    SnapshotInstrumentRow,
    build_identity_records,
)


def _row(exchange: str, mid: str, key: str, captured: datetime) -> SnapshotInstrumentRow:
    return SnapshotInstrumentRow(
        exchange=exchange,
        market_type="linear",
        native_market_id=mid,
        identity_key=key,
        identity_status="ready",
        captured_at=captured,
    )


def test_build_identity_records_intervals_delisting_and_window() -> None:
    t1 = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)  # carry-in (before window_start)
    t2 = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
    e0 = datetime(2026, 8, 1, tzinfo=UTC)  # fully before window
    e1 = datetime(2026, 8, 5, tzinfo=UTC)  # carry-in for binance
    rows = [
        _row("bybit", "AAAUSDT", "aaa", t1),
        _row("bybit", "BBBUSDT", "bbb", t1),
        _row("bybit", "AAAUSDT", "aaa", t2),  # AAA persists; BBB absent at t2 => delisted
        _row("binance", "CCCUSDT", "ccc", e0),
        _row("binance", "CCCUSDT", "ccc2", e1),  # single-venue; e0 interval ends at e1
    ]
    window_start = datetime(2026, 8, 16, tzinfo=UTC)
    window_end = datetime(2026, 9, 18, tzinfo=UTC)
    records = build_identity_records(rows, window_start=window_start, window_end=window_end)

    by = [
        (r["exchange"], r["native_market_id"], r["canonical_asset"], r["valid_from"], r["valid_to"])
        for r in records
    ]
    # AAA has two intervals (carry-in t1..t2, then t2..open).
    assert ("bybit", "AAAUSDT", "aaa", t1.isoformat(), t2.isoformat()) in by
    assert ("bybit", "AAAUSDT", "aaa", t2.isoformat(), None) in by
    # BBB only until t2 (delisted at t2), never open-ended.
    bbb = [r for r in records if r["native_market_id"] == "BBBUSDT"]
    assert len(bbb) == 1 and bbb[0]["valid_to"] == t2.isoformat()
    # Binance single-venue: only the carry-in e1 interval; e0 (ends at e1 <= window_start) dropped.
    ccc = [r for r in records if r["native_market_id"] == "CCCUSDT"]
    assert len(ccc) == 1
    assert ccc[0]["canonical_asset"] == "ccc2" and ccc[0]["valid_to"] is None
    # snapshot_captured_at is carried for snapshot-age measurement.
    assert all("snapshot_captured_at" in r for r in records)


def test_build_identity_records_drops_intervals_after_window_end() -> None:
    before = datetime(2026, 8, 16, tzinfo=UTC)
    after = datetime(2026, 9, 20, tzinfo=UTC)  # a snapshot starting after window_end
    rows = [
        _row("bybit", "AAAUSDT", "aaa", before),
        _row("bybit", "AAAUSDT", "aaa2", after),
    ]
    records = build_identity_records(
        rows,
        window_start=datetime(2026, 8, 16, tzinfo=UTC),
        window_end=datetime(2026, 9, 18, tzinfo=UTC),
    )
    # The 'before' interval is kept (valid_to = after); the 'after' interval is dropped.
    keys = {r["canonical_asset"] for r in records}
    assert keys == {"aaa"}
    assert records[0]["valid_to"] == after.isoformat()
