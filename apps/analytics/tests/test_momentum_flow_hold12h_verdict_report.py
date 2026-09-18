"""Pure-layer tests for the HYP-015 hold12h verdict reader.

Covers the mandatory honesty scenarios: watch_id pairing, the common-entry 240m
counterfactual without look-ahead, ACTUAL funding sign and half-open boundaries,
denominator preservation, fail-closed funding, and chronological portfolio metrics.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from schurfer_analytics.momentum_flow_hold12h_verdict import HOLD12H_VERDICT_CONTRACT
from schurfer_analytics.momentum_flow_hold12h_verdict_report import (
    ActualFundingSource,
    AnalyzablePair,
    FundingCoverage,
    HorizonOutcome,
    InstrumentRoute,
    NoRegisteredFundingSource,
    PortfolioEntry,
    PortfolioResult,
    ProbeClass,
    ProbeRecord,
    SettlementEvent,
    WatchDecision,
    assemble_verdict_inputs,
    classify_and_pair,
    counterfactual_nets,
    filter_to_cohort,
    funding_usd_over_interval,
    next_utc_day_boundary,
    replay_fixed_bank,
    resolve_cohort_registration,
    verdict_fingerprint,
)

if TYPE_CHECKING:
    from pathlib import Path

_BASE = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
_ROUTE = InstrumentRoute("bybit", "linear", "FOOUSDT", "FOO/USDT:USDT")


class _StubFunding:
    """A funding source that returns fixed events with proven coverage."""

    def __init__(self, events: tuple[SettlementEvent, ...], *, proven: bool = True) -> None:
        self._events = events
        self._proven = proven

    def coverage(
        self, route: InstrumentRoute, entry_at: datetime, exit_at: datetime
    ) -> FundingCoverage | None:
        if not self._proven:
            return None
        return FundingCoverage(events=self._events, proven_full_coverage=True)


_ZERO_FUNDING: ActualFundingSource = _StubFunding(())


def _probe(
    watch_id: str,
    *,
    entry_ok: bool = True,
    exit_minutes: int | None = 720,
    exit_resolved: bool = True,
    actual_gross: float | None = 0.02,
    horizon_240_gross: float | None = 0.01,
) -> ProbeRecord:
    entry_at = _BASE
    exit_at = None if exit_minutes is None else entry_at + timedelta(minutes=exit_minutes)
    horizons = {}
    if horizon_240_gross is not None:
        horizons[240] = HorizonOutcome(
            240, resolved=True, gross_return_pct=horizon_240_gross, notional_usd=50.0
        )
    return ProbeRecord(
        watch_id=watch_id,
        canonical_asset="FOO",
        route=_ROUTE,
        entry_at=entry_at,
        entry_ok=entry_ok,
        exit_at=exit_at,
        exit_resolved=exit_resolved,
        actual_gross_return_pct=actual_gross,
        actual_notional_usd=50.0,
        horizons=horizons,
    )


# --- funding_usd_over_interval -------------------------------------------------


def test_funding_half_open_interval_and_long_sign() -> None:
    events = (
        SettlementEvent(_BASE, 0.001),  # exactly at entry -> EXCLUDED
        SettlementEvent(_BASE + timedelta(hours=4), 0.001),  # inside -> included, long pays
        SettlementEvent(_BASE + timedelta(hours=12), -0.002),  # at exit -> included, long receives
        SettlementEvent(_BASE + timedelta(hours=20), 0.005),  # after exit -> excluded
    )
    exit_at = _BASE + timedelta(hours=12)
    cost = funding_usd_over_interval(
        FundingCoverage(events, proven_full_coverage=True),
        entry_at=_BASE,
        exit_at=exit_at,
        notional_usd=100.0,
    )
    # +0.001*100 (pays) then -0.002*100 (receives) = 0.1 - 0.2 = -0.1
    assert cost is not None
    assert abs(cost - (-0.1)) < 1e-9


def test_funding_unproven_coverage_is_none() -> None:
    assert funding_usd_over_interval(None, entry_at=_BASE, exit_at=_BASE, notional_usd=50.0) is None
    assert (
        funding_usd_over_interval(
            FundingCoverage((), proven_full_coverage=False),
            entry_at=_BASE,
            exit_at=_BASE + timedelta(hours=1),
            notional_usd=50.0,
        )
        is None
    )


# --- counterfactual_nets -------------------------------------------------------


def test_counterfactual_uses_240_horizon_after_a_full_hold() -> None:
    probe = _probe("w1", actual_gross=0.02, horizon_240_gross=0.01)
    nets = counterfactual_nets(probe, _ZERO_FUNDING)
    assert nets is not None
    net_720, net_240cf = nets
    assert abs(net_720 - 0.02) < 1e-9
    assert abs(net_240cf - 0.01) < 1e-9  # the 240m mark on the same entry


def test_counterfactual_pre_240_stop_is_shared_no_lookahead() -> None:
    # The 720m policy actually exited at +100m (a stop). Both policies share it, so the
    # paired difference is exactly zero -- the 240m mark is NOT consulted.
    probe = _probe("w1", exit_minutes=100, actual_gross=-0.03, horizon_240_gross=0.99)
    nets = counterfactual_nets(probe, _ZERO_FUNDING)
    assert nets is not None
    net_720, net_240cf = nets
    assert net_720 == net_240cf
    assert abs(net_720 - (-0.03)) < 1e-9


def test_counterfactual_funding_reduces_long_net() -> None:
    # A positive funding rate inside the hold makes the long PAY, lowering net below gross.
    funding = _StubFunding((SettlementEvent(_BASE + timedelta(hours=4), 0.01),))
    probe = _probe("w1", actual_gross=0.02, exit_minutes=720)
    nets = counterfactual_nets(probe, funding)
    assert nets is not None
    net_720, _ = nets
    # funding cost = 0.01 * 50 = 0.5 usd; net = 0.02 - 0.5/50 = 0.02 - 0.01 = 0.01
    assert abs(net_720 - 0.01) < 1e-9


def test_counterfactual_none_when_funding_unproven() -> None:
    assert counterfactual_nets(_probe("w1"), NoRegisteredFundingSource()) is None


def test_counterfactual_none_when_unresolved_or_bad_entry() -> None:
    assert counterfactual_nets(_probe("w1", entry_ok=False), _ZERO_FUNDING) is None
    assert counterfactual_nets(_probe("w1", exit_resolved=False), _ZERO_FUNDING) is None
    assert counterfactual_nets(_probe("w1", exit_minutes=None), _ZERO_FUNDING) is None


# --- classify_and_pair ---------------------------------------------------------


def test_classify_preserves_the_whole_denominator_and_pairs_by_watch_id() -> None:
    watches = [
        WatchDecision("w-analyzable", "FOO", _BASE),
        WatchDecision("w-no-probe", "BAR", _BASE),
        WatchDecision("w-rejected", "BAZ", _BASE),
        WatchDecision("w-unresolved", "QUX", _BASE),
    ]
    probes = {
        "w-analyzable": _probe("w-analyzable"),
        "w-rejected": _probe("w-rejected", entry_ok=False),
        "w-unresolved": _probe("w-unresolved", exit_resolved=False),
        # NOTE: a probe keyed by a watch_id NOT in the denominator must be ignored.
        "w-orphan": _probe("w-orphan"),
    }
    counts, pairs = classify_and_pair(watches, probes, _ZERO_FUNDING)

    assert sum(counts.values()) == len(watches)  # denominator preserved, nothing dropped
    assert counts[ProbeClass.ANALYZABLE] == 1
    assert counts[ProbeClass.REJECTED_STALE] == 2  # missing probe + rejected entry
    assert counts[ProbeClass.UNRESOLVED] == 1
    assert [p.watch_id for p in pairs] == ["w-analyzable"]


def test_fail_closed_funding_makes_everything_accounting_incomplete() -> None:
    watches = [WatchDecision(f"w{i}", "FOO", _BASE) for i in range(3)]
    probes = {f"w{i}": _probe(f"w{i}") for i in range(3)}
    counts, pairs = classify_and_pair(watches, probes, NoRegisteredFundingSource())
    assert counts[ProbeClass.ACCOUNTING_INCOMPLETE] == 3
    assert counts[ProbeClass.ANALYZABLE] == 0
    assert pairs == ()


# --- replay_fixed_bank ---------------------------------------------------------


def _entry(asset: str, start_min: int, dur_min: int, pnl: float) -> PortfolioEntry:
    start = _BASE + timedelta(minutes=start_min)
    return PortfolioEntry(asset, start, start + timedelta(minutes=dur_min), pnl)


def test_replay_skips_when_slots_full_deterministically() -> None:
    # Two slots; three overlapping entries -> the third is skipped.
    entries = [
        _entry("A", 0, 100, 5.0),
        _entry("B", 10, 100, 5.0),
        _entry("C", 20, 100, 5.0),  # arrives while A and B still hold both slots
    ]
    result = replay_fixed_bank(entries, max_slots=2)
    assert result.taken == 2
    assert result.skipped_slots_full == 1
    assert abs(result.window_pnl_usd - 10.0) < 1e-9


def test_replay_releases_slot_after_exit() -> None:
    # Sequential, non-overlapping -> all taken with one slot.
    entries = [_entry("A", 0, 50, 5.0), _entry("B", 60, 50, -3.0), _entry("C", 120, 50, 2.0)]
    result = replay_fixed_bank(entries, max_slots=1)
    assert result.taken == 3
    assert abs(result.window_pnl_usd - 4.0) < 1e-9


def test_replay_drawdown_and_losing_streak_are_chronological_by_exit() -> None:
    # Exit order: +10, -4, -6, +3 -> equity 10,6,0,3; peak 10; max drawdown 10; streak 2.
    entries = [
        _entry("A", 0, 10, 10.0),
        _entry("B", 1, 10, -4.0),
        _entry("C", 2, 10, -6.0),
        _entry("D", 3, 10, 3.0),
    ]
    result = replay_fixed_bank(entries, max_slots=10)
    assert abs(result.window_pnl_usd - 3.0) < 1e-9
    assert abs(result.drawdown_usd - 10.0) < 1e-9
    assert result.longest_losing_streak == 2


def test_analyzable_pair_dataclass_roundtrip() -> None:
    pair = AnalyzablePair("w1", "FOO", _BASE, net_720=0.02, net_240cf=0.01)
    assert pair.net_720 - pair.net_240cf == 0.01


# --- cohort registration + filtering (pre-freeze barrier) ----------------------


def test_registration_is_first_writer_and_immutable(tmp_path: Path) -> None:
    state = tmp_path / "reg.json"
    first = resolve_cohort_registration(state, now=datetime(2026, 10, 1, 14, 30, tzinfo=UTC))
    # Cohort starts at the next whole UTC-day boundary after registration.
    assert first.cohort_start == datetime(2026, 10, 2, tzinfo=UTC)
    # A later run, even much later, reloads the SAME immutable boundary.
    again = resolve_cohort_registration(state, now=datetime(2026, 12, 1, tzinfo=UTC))
    assert again.registered_at == first.registered_at
    assert again.cohort_start == first.cohort_start


def test_registration_refuses_tampered_state(tmp_path: Path) -> None:
    state = tmp_path / "reg.json"
    # A stored cohort_start that does not match its registered_at (tampered).
    state.write_text(
        '{"registered_at": "2026-10-01T14:30:00+00:00",'
        ' "cohort_start": "2026-11-01T00:00:00+00:00"}'
    )
    with pytest.raises(ValueError, match="re-baseline"):
        resolve_cohort_registration(state, now=datetime(2026, 10, 1, tzinfo=UTC))


def test_next_utc_day_boundary_is_strictly_after() -> None:
    oct2 = datetime(2026, 10, 2, tzinfo=UTC)
    assert next_utc_day_boundary(datetime(2026, 10, 1, tzinfo=UTC)) == oct2
    assert next_utc_day_boundary(datetime(2026, 10, 1, 23, 59, tzinfo=UTC)) == oct2


def test_filter_to_cohort_excludes_pre_start_probes() -> None:
    cohort_start = datetime(2026, 10, 2, tzinfo=UTC)
    watches = [
        WatchDecision("pre", "FOO", datetime(2026, 10, 1, 23, 59, tzinfo=UTC)),
        WatchDecision("boundary", "FOO", cohort_start),
        WatchDecision("post", "FOO", datetime(2026, 10, 3, tzinfo=UTC)),
    ]
    kept = filter_to_cohort(watches, cohort_start)
    assert [w.watch_id for w in kept] == ["boundary", "post"]


# --- deterministic fingerprint -------------------------------------------------


def _fingerprint(inputs_pairs: int = 0) -> str:
    funnel = dict.fromkeys(ProbeClass, 0)
    inputs = assemble_verdict_inputs(
        HOLD12H_VERDICT_CONTRACT,
        total_watches=max(inputs_pairs, 1),
        funnel=funnel,
        pairs=(),
        portfolio_720=PortfolioResult(0.0, 0.0, 0, 0, 0),
        portfolio_240=PortfolioResult(0.0, 0.0, 0, 0, 0),
    )
    return verdict_fingerprint(
        contract_sha256=HOLD12H_VERDICT_CONTRACT.sha256_hex(),
        cohort_start=datetime(2026, 10, 2, tzinfo=UTC),
        decision_prefix_end=datetime(2026, 11, 2, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
        funding_source_id="no_registered_funding_source",
        funnel=funnel,
        inputs=inputs,
    )


def test_fingerprint_is_deterministic() -> None:
    assert _fingerprint() == _fingerprint()
    assert len(_fingerprint()) == 64
