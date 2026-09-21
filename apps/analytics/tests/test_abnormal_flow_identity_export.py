"""Tests for the pure core of the point-in-time identity export (no DB).

Pins PERSIST-UNTIL-CHANGED semantics: a route's interval ends only at the next record
FOR THAT ROUTE whose (identity_key, market_type, status) differs; absence from a partial
snapshot never delists; nothing is backfilled before a route's first appearance; an
identity change closes the prior interval.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from schurfer_analytics.abnormal_flow_identity_export import (
    SnapshotInstrumentRow,
    build_identity_records,
)

_WS = datetime(2026, 8, 16, tzinfo=UTC)
_WE = datetime(2026, 9, 19, tzinfo=UTC)


def _row(
    exchange: str,
    mid: str,
    key: str,
    captured: datetime,
    *,
    market_type: str = "linear_usdt_perpetual",
    status: str = "ready",
) -> SnapshotInstrumentRow:
    return SnapshotInstrumentRow(
        exchange=exchange,
        market_type=market_type,
        native_market_id=mid,
        identity_key=key,
        identity_status=status,
        captured_at=captured,
    )


def _intervals(records: list[dict[str, Any]], mid: str) -> list[tuple[str, str | None, str]]:
    return [
        (r["valid_from"], r["valid_to"], r["canonical_asset"])
        for r in records
        if r["native_market_id"] == mid
    ]


def test_partial_snapshot_does_not_break_other_routes() -> None:
    # 08-18 capture warmup: 525 -> 6 -> 50 -> 150, dropping most routes for ~9 days.
    s1 = datetime(2026, 8, 15, 18, 9, tzinfo=UTC)  # full
    s2 = datetime(2026, 8, 18, 17, 51, tzinfo=UTC)  # partial: only AAA present
    s3 = datetime(2026, 8, 27, 19, 48, tzinfo=UTC)  # full again
    rows = [
        _row("binance", "AAAUSDT", "aaa", s1),
        _row("binance", "BBBUSDT", "bbb", s1),
        _row("binance", "AAAUSDT", "aaa", s2),  # BBB absent from the partial snapshot
        _row("binance", "AAAUSDT", "aaa", s3),
        _row("binance", "BBBUSDT", "bbb", s3),
    ]
    records = build_identity_records(rows, window_start=_WS, window_end=_WE)
    # BBB persists across the partial-snapshot gap: one open-ended interval from s1.
    assert _intervals(records, "BBBUSDT") == [(s1.isoformat(), None, "bbb")]
    assert _intervals(records, "AAAUSDT") == [(s1.isoformat(), None, "aaa")]


def test_new_listing_has_no_identity_before_its_first_snapshot() -> None:
    s1 = datetime(2026, 8, 15, 18, 9, tzinfo=UTC)
    s3 = datetime(2026, 8, 27, 19, 48, tzinfo=UTC)
    rows = [
        _row("binance", "AAAUSDT", "aaa", s1),
        _row("binance", "NEWUSDT", "new", s3),  # first appears at s3
    ]
    records = build_identity_records(rows, window_start=_WS, window_end=_WE)
    # NEW is only valid from s3 onward; nothing is backfilled before it.
    assert _intervals(records, "NEWUSDT") == [(s3.isoformat(), None, "new")]


def test_identity_change_closes_the_previous_interval() -> None:
    s1 = datetime(2026, 8, 15, 18, 9, tzinfo=UTC)
    s3 = datetime(2026, 8, 27, 19, 48, tzinfo=UTC)
    rows = [
        _row("binance", "EEEUSDT", "e1", s1),
        _row("binance", "EEEUSDT", "e2", s3),  # re-onboarded with a new identity_key
    ]
    records = build_identity_records(rows, window_start=_WS, window_end=_WE)
    assert _intervals(records, "EEEUSDT") == [
        (s1.isoformat(), s3.isoformat(), "e1"),
        (s3.isoformat(), None, "e2"),
    ]


def test_status_or_market_type_change_starts_a_new_interval() -> None:
    s1 = datetime(2026, 8, 15, tzinfo=UTC)
    s2 = datetime(2026, 8, 20, tzinfo=UTC)
    rows = [
        _row("bybit", "XUSDT", "x", s1, status="ready"),
        _row("bybit", "XUSDT", "x", s2, status="delisting"),  # status change
    ]
    records = build_identity_records(rows, window_start=_WS, window_end=_WE)
    assert _intervals(records, "XUSDT") == [
        (s1.isoformat(), s2.isoformat(), "x"),
        (s2.isoformat(), None, "x"),
    ]


def test_intervals_after_window_end_are_dropped() -> None:
    before = datetime(2026, 8, 16, tzinfo=UTC)
    after = datetime(2026, 9, 20, tzinfo=UTC)  # change starting after window_end
    rows = [
        _row("bybit", "AAAUSDT", "aaa", before),
        _row("bybit", "AAAUSDT", "aaa2", after),
    ]
    records = build_identity_records(rows, window_start=_WS, window_end=_WE)
    assert _intervals(records, "AAAUSDT") == [(before.isoformat(), after.isoformat(), "aaa")]
