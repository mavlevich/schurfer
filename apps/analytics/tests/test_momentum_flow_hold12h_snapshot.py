"""The HYP-015 formal input snapshot pins every verdict input (no database needed)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from schurfer_analytics.momentum_flow_hold12h_funding import CoverageRun, StoredFundingSource
from schurfer_analytics.momentum_flow_hold12h_snapshot import (
    holdings_window,
    inputs_from_snapshot,
    publish_once,
    publish_once_or_same,
    snapshot_bytes,
    snapshot_digest,
)
from schurfer_analytics.momentum_flow_hold12h_verdict_report import (
    HorizonOutcome,
    InstrumentRoute,
    ProbeRecord,
    SettlementEvent,
    WatchDecision,
    cohort_rows_digest,
    funding_usd_over_interval,
)

if TYPE_CHECKING:
    from pathlib import Path

T0 = datetime(2026, 10, 6, 12, tzinfo=UTC)
ROUTE = InstrumentRoute("bybit", "swap", "ABCUSDT", "ABC/USDT:USDT")
V = "hold12h_actual_funding_v2"


def _inputs() -> tuple[tuple[WatchDecision, ...], dict[str, ProbeRecord], StoredFundingSource]:
    watches = (WatchDecision("w-1", "abc", T0),)
    probe = ProbeRecord(
        watch_id="w-1",
        route=ROUTE,
        entry_at=T0,
        entry_ok=True,
        exit_at=T0 + timedelta(hours=12),
        exit_resolved=True,
        exit_reason="max_hold",
        actual_gross_return_pct=1.25,
        actual_notional_usd=50.0,
        max_adverse_return_pct=-2.5,
        horizons={
            240: HorizonOutcome(240, True, T0 + timedelta(hours=4), 0.5, 50.0),
            720: HorizonOutcome(720, True, T0 + timedelta(hours=12), 1.25, 50.0),
        },
    )
    key = ("bybit", "ABCUSDT")
    funding = StoredFundingSource(
        {
            key: (
                SettlementEvent(T0 - timedelta(days=30), 0.001, V),  # long before the cohort
                SettlementEvent(T0 + timedelta(hours=4), 0.0001, V),
                SettlementEvent(T0 + timedelta(hours=12), 0.0002, V),
            )
        },
        {
            key: (
                CoverageRun(T0 - timedelta(days=31), T0 - timedelta(days=29), "complete", V),
                CoverageRun(T0 - timedelta(hours=1), T0 + timedelta(hours=13), "complete", V),
            )
        },
        source_version=V,
    )
    return watches, {"w-1": probe}, funding


def _body(**probe_changes: Any) -> bytes:
    watches, probes, funding = _inputs()
    if probe_changes:
        probes = {"w-1": dataclasses.replace(probes["w-1"], **probe_changes)}
    return snapshot_bytes(watches, probes, funding, window=holdings_window(probes))


def test_the_snapshot_round_trips_every_input_and_funding_answer() -> None:
    watches, probes, funding = _inputs()
    body = _body()
    again_watches, again_probes, again_funding = inputs_from_snapshot(body)
    assert again_watches == watches
    assert again_probes == probes
    for since, until in (
        (T0, T0 + timedelta(hours=12)),
        (T0, T0 + timedelta(hours=4)),
    ):
        # coverage() returns the route's events unfiltered; the verdict applies the
        # (entry, exit] filter in funding_usd_over_interval, which is what must not change.
        paid = [
            funding_usd_over_interval(
                source.coverage(ROUTE, since, until),
                entry_at=since,
                exit_at=until,
                notional_usd=50.0,
            )
            for source in (funding, again_funding)
        ]
        assert paid[0] == paid[1] is not None
    window = holdings_window(probes)
    assert snapshot_bytes(again_watches, again_probes, again_funding, window=window) == body


@pytest.mark.parametrize(
    "change",
    [
        {"exit_resolved": False},  # the reviewer's reproduction
        {"max_adverse_return_pct": -9.0},
        {"exit_reason": "stop_loss"},
        {"entry_ok": False},
    ],
)
def test_every_verdict_input_changes_the_digest(change: dict[str, Any]) -> None:
    assert snapshot_digest(_body(**change)) != snapshot_digest(_body())
    watches, probes, funding = _inputs()
    changed = {"w-1": dataclasses.replace(probes["w-1"], **change)}
    assert cohort_rows_digest(watches, changed, funding) != cohort_rows_digest(
        watches, probes, funding
    )


def test_captures_outside_the_holdings_do_not_change_the_snapshot() -> None:
    watches, probes, funding = _inputs()
    later = StoredFundingSource(
        {
            ("bybit", "ABCUSDT"): (
                SettlementEvent(T0 + timedelta(hours=4), 0.0001, V),
                SettlementEvent(T0 + timedelta(hours=12), 0.0002, V),
                SettlementEvent(T0 + timedelta(days=20), 0.003, V),  # captured weeks later
            )
        },
        {
            ("bybit", "ABCUSDT"): (
                CoverageRun(T0 - timedelta(hours=1), T0 + timedelta(hours=13), "complete", V),
                CoverageRun(T0 + timedelta(days=19), T0 + timedelta(days=21), "complete", V),
            )
        },
        source_version=V,
    )
    window = holdings_window(probes)
    assert snapshot_bytes(watches, probes, later, window=window) == snapshot_bytes(
        watches, probes, funding, window=window
    )


def test_files_are_published_once_whole_and_never_overwritten(tmp_path: Path) -> None:
    target = tmp_path / "hold12h_verdict.json"
    publish_once(target, b"first")
    with pytest.raises(FileExistsError):
        publish_once(target, b"second")
    publish_once_or_same(target, b"first")
    with pytest.raises(FileExistsError):
        publish_once_or_same(target, b"other")
    assert target.read_bytes() == b"first"
    assert [p.name for p in tmp_path.iterdir()] == ["hold12h_verdict.json"]
