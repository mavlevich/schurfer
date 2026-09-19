"""Pure-layer tests for the HYP-015 hold12h verdict reader.

Covers the mandatory honesty scenarios: watch_id pairing, the common-entry 240m
counterfactual with ACTUAL observed exit times (no nominal-minute look-ahead), ACTUAL
funding sign/boundaries in PERCENT units, denominator preservation with the integrity /
identity / unresolved / accounting classes distinguished, fail-closed funding,
outcome-blind chronological portfolio metrics, the half-open formal window, first-writer
registration, and a row-level fingerprint.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from schurfer_analytics.momentum_flow_hold12h_verdict import HOLD12H_VERDICT_CONTRACT
from schurfer_analytics.momentum_flow_hold12h_verdict_report import (
    ActualFundingSource,
    DuplicateSettlementError,
    FundingCoverage,
    HorizonOutcome,
    InstrumentRoute,
    NoRegisteredFundingSource,
    PairResolution,
    PortfolioEntry,
    PortfolioResult,
    ProbeClass,
    ProbeRecord,
    SettlementEvent,
    WatchDecision,
    assemble_verdict_inputs,
    build_eligible_portfolio,
    classify_and_pair,
    cohort_rows_digest,
    evaluate_cohort,
    filter_to_cohort,
    formal_cohort_start,
    funding_usd_over_interval,
    next_utc_day_boundary,
    replay_fixed_bank,
    resolve_cohort_registration,
    resolve_pair,
    verdict_fingerprint,
)

if TYPE_CHECKING:
    from pathlib import Path

_BASE = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
_ROUTE = InstrumentRoute("bybit", "linear", "FOOUSDT", "FOO/USDT:USDT")


class _StubFunding:
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
    exit_reason: str = "max_hold",
    actual_gross: float | None = 2.0,
    notional: float | None = 50.0,
    max_adverse: float | None = -1.0,
    horizon_240_gross: float | None = 1.0,
    horizon_240_resolved: bool = True,
) -> ProbeRecord:
    entry_at = _BASE
    exit_at = None if exit_minutes is None else entry_at + timedelta(minutes=exit_minutes)
    horizons = {}
    if horizon_240_gross is not None or horizon_240_resolved:
        horizons[240] = HorizonOutcome(
            240,
            resolved=horizon_240_resolved,
            observed_at=entry_at + timedelta(minutes=240) if horizon_240_resolved else None,
            gross_return_pct=horizon_240_gross,
            notional_usd=50.0,
        )
    return ProbeRecord(
        watch_id=watch_id,
        route=_ROUTE,
        entry_at=entry_at,
        entry_ok=entry_ok,
        exit_at=exit_at,
        exit_resolved=exit_resolved,
        exit_reason=exit_reason,
        actual_gross_return_pct=actual_gross,
        actual_notional_usd=notional,
        max_adverse_return_pct=max_adverse,
        horizons=horizons,
    )


# --- funding_usd_over_interval (percent-aware sign + boundaries) ----------------


def test_funding_half_open_interval_and_long_sign() -> None:
    events = (
        SettlementEvent(_BASE, 0.001, "v1"),  # at entry -> EXCLUDED
        SettlementEvent(_BASE + timedelta(hours=4), 0.001, "v1"),  # inside -> long pays
        SettlementEvent(_BASE + timedelta(hours=12), -0.002, "v1"),  # at exit -> long receives
        SettlementEvent(_BASE + timedelta(hours=20), 0.005, "v1"),  # after exit -> EXCLUDED
    )
    cost = funding_usd_over_interval(
        FundingCoverage(events, proven_full_coverage=True),
        entry_at=_BASE,
        exit_at=_BASE + timedelta(hours=12),
        notional_usd=100.0,
    )
    assert cost is not None
    assert abs(cost - (-0.1)) < 1e-9  # 0.001*100 - 0.002*100


def test_funding_unproven_or_nonfinite_is_none() -> None:
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
    nan_event = (SettlementEvent(_BASE + timedelta(hours=1), float("nan"), "v1"),)
    assert (
        funding_usd_over_interval(
            FundingCoverage(nan_event, proven_full_coverage=True),
            entry_at=_BASE,
            exit_at=_BASE + timedelta(hours=2),
            notional_usd=50.0,
        )
        is None
    )


def test_funding_duplicate_settlement_raises_integrity() -> None:
    # A duplicate (settlement_at, source_version) is CORRUPTION -> raises (the caller maps
    # it to INTEGRITY_FAILURE, which fail-closes the verdict), not None/accounting.
    at = _BASE + timedelta(hours=4)
    dup = (SettlementEvent(at, 0.001, "v1"), SettlementEvent(at, 0.001, "v1"))
    with pytest.raises(DuplicateSettlementError):
        funding_usd_over_interval(
            FundingCoverage(dup, proven_full_coverage=True),
            entry_at=_BASE,
            exit_at=_BASE + timedelta(hours=8),
            notional_usd=50.0,
        )
    # But the same timestamp under a DIFFERENT source version is not a duplicate.
    ok = (SettlementEvent(at, 0.001, "v1"), SettlementEvent(at, 0.001, "v2"))
    assert (
        funding_usd_over_interval(
            FundingCoverage(ok, proven_full_coverage=True),
            entry_at=_BASE,
            exit_at=_BASE + timedelta(hours=8),
            notional_usd=50.0,
        )
        is not None
    )


def test_resolve_pair_maps_duplicate_settlement_to_integrity() -> None:
    at = _BASE + timedelta(hours=4)
    funding = _StubFunding((SettlementEvent(at, 0.001, "v1"), SettlementEvent(at, 0.001, "v1")))
    assert resolve_pair(_probe("w1"), funding)[0] is PairResolution.INTEGRITY_FAILURE


# --- resolve_pair (counterfactual, exit semantics, integrity) ------------------


def test_resolve_uses_240_horizon_after_a_full_hold() -> None:
    probe = _probe("w1", actual_gross=2.0, horizon_240_gross=1.0)
    resolution, pair = resolve_pair(probe, _ZERO_FUNDING)
    assert resolution is PairResolution.OK
    assert pair is not None
    assert abs(pair.net_720 - 2.0) < 1e-9
    assert abs(pair.net_240cf - 1.0) < 1e-9
    assert pair.exit_240_at == _BASE + timedelta(minutes=240)


def test_resolve_shared_stop_uses_actual_reason_and_observed_time() -> None:
    # An actual stop_loss at +100m, before the executable 240m mark -> shared exit, the
    # 240m gross is NOT consulted.
    resolution, pair = resolve_pair(
        _probe(
            "w1",
            exit_minutes=100,
            exit_reason="stop_loss",
            actual_gross=-3.0,
            horizon_240_gross=99.0,
        ),
        _ZERO_FUNDING,
    )
    assert resolution is PairResolution.OK
    assert pair is not None
    assert pair.net_720 == pair.net_240cf
    assert abs(pair.net_720 - (-3.0)) < 1e-9
    assert pair.exit_240_at == pair.exit_720_at  # occupancy shares the actual exit


def test_resolve_early_exit_that_is_not_a_stop_still_compares_to_240() -> None:
    # Early exit but reason is not stop_loss -> NOT shared; the 240m counterfactual holds
    # to its own mark (no look-ahead shortcut from the nominal minute).
    resolution, pair = resolve_pair(
        _probe(
            "w1", exit_minutes=100, exit_reason="max_hold", actual_gross=-3.0, horizon_240_gross=1.0
        ),
        _ZERO_FUNDING,
    )
    assert resolution is PairResolution.OK
    assert pair is not None
    assert abs(pair.net_240cf - 1.0) < 1e-9


def test_resolve_missing_240_is_unresolved_not_accounting() -> None:
    # A missing/late 240m mark is a data-resolution problem, never mislabelled funding.
    resolution, _ = resolve_pair(
        _probe("w1", horizon_240_resolved=False, horizon_240_gross=None), _ZERO_FUNDING
    )
    assert resolution is PairResolution.UNRESOLVED


def test_resolve_funding_unproven_is_accounting_incomplete() -> None:
    resolution, _ = resolve_pair(_probe("w1"), NoRegisteredFundingSource())
    assert resolution is PairResolution.ACCOUNTING_INCOMPLETE


def test_resolve_integrity_failure_on_bad_numbers() -> None:
    assert resolve_pair(_probe("w1", actual_gross=float("nan")), _ZERO_FUNDING)[0] is (
        PairResolution.INTEGRITY_FAILURE
    )
    assert resolve_pair(_probe("w1", notional=0.0), _ZERO_FUNDING)[0] is (
        PairResolution.INTEGRITY_FAILURE
    )


def test_resolve_funding_percent_units() -> None:
    # rate 0.01 over $50 notional = $0.5 = 1.0 PERCENT; net = 2.0% - 1.0% = 1.0%.
    funding = _StubFunding((SettlementEvent(_BASE + timedelta(hours=4), 0.01, "v1"),))
    resolution, pair = resolve_pair(_probe("w1", actual_gross=2.0), funding)
    assert resolution is PairResolution.OK
    assert pair is not None
    assert abs(pair.net_720 - 1.0) < 1e-9


# --- classify_and_pair (denominator, pairing, classes) -------------------------


def test_classify_preserves_denominator_and_distinguishes_classes() -> None:
    watches = [
        WatchDecision("w-ok", "FOO", _BASE),
        WatchDecision("w-no-probe", "BAR", _BASE),
        WatchDecision("w-rejected", "BAZ", _BASE),
        WatchDecision("w-unresolved", "QUX", _BASE),
        WatchDecision("w-no-identity", None, _BASE),
    ]
    probes = {
        "w-ok": _probe("w-ok"),
        "w-rejected": _probe("w-rejected", entry_ok=False),
        "w-unresolved": _probe("w-unresolved", exit_resolved=False),
        "w-orphan": _probe("w-orphan"),  # not in the denominator -> ignored
    }
    counts, pairs = classify_and_pair(watches, probes, _ZERO_FUNDING)
    assert sum(counts.values()) == len(watches)  # nothing dropped
    assert counts[ProbeClass.ANALYZABLE] == 1
    assert counts[ProbeClass.REJECTED_STALE] == 2  # missing probe + rejected entry
    assert counts[ProbeClass.UNRESOLVED] == 1
    assert counts[ProbeClass.IDENTITY_UNRESOLVED] == 1
    assert [p.watch_id for p in pairs] == ["w-ok"]
    assert pairs[0].canonical_asset == "FOO"


def test_fail_closed_funding_makes_everything_accounting_incomplete() -> None:
    watches = [WatchDecision(f"w{i}", "FOO", _BASE) for i in range(3)]
    probes = {f"w{i}": _probe(f"w{i}") for i in range(3)}
    counts, pairs = classify_and_pair(watches, probes, NoRegisteredFundingSource())
    assert counts[ProbeClass.ACCOUNTING_INCOMPLETE] == 3
    assert pairs == ()


# --- replay_fixed_bank (outcome-blind, chronological) --------------------------


def _entry(
    tie: str, start_min: int, dur_min: int, pnl: float | None, mae: float | None = 0.0
) -> PortfolioEntry:
    start = _BASE + timedelta(minutes=start_min)
    return PortfolioEntry(tie, "A", start, start + timedelta(minutes=dur_min), pnl, mae)


def test_replay_skips_when_slots_full_using_only_arrival_order() -> None:
    entries = [
        _entry("a", 0, 100, 5.0),
        _entry("b", 10, 100, 5.0),
        _entry("c", 20, 100, 999.0),  # arrives with both slots busy -> skipped regardless of pnl
    ]
    result = replay_fixed_bank(entries, max_slots=2)
    assert result.taken == 2
    assert result.skipped_slots_full == 1
    assert abs(result.window_pnl_usd - 10.0) < 1e-9
    assert result.complete


def test_replay_incomplete_when_a_taken_slot_is_unresolved() -> None:
    result = replay_fixed_bank([_entry("a", 0, 50, None, None)], max_slots=1)
    assert not result.complete
    assert result.window_pnl_usd != result.window_pnl_usd  # NaN


def test_replay_window_and_losing_streak_are_chronological_by_exit() -> None:
    entries = [
        _entry("a", 0, 10, 10.0),
        _entry("b", 1, 10, -4.0),
        _entry("c", 2, 10, -6.0),
        _entry("d", 3, 10, 3.0),
    ]
    result = replay_fixed_bank(entries, max_slots=10)
    assert abs(result.window_pnl_usd - 3.0) < 1e-9
    assert result.longest_losing_streak == 2


def test_adverse_from_entry_is_worst_simultaneous_excursion() -> None:
    # Two positions overlap [0,100) and [50,150); each has from-entry MAE -$5. While both
    # are open the diagnostic is $10; a third, non-overlapping, alone contributes only $5.
    # (Reported only -- the verdict does NOT gate on this; it is not a true drawdown.)
    entries = [
        _entry("a", 0, 100, 1.0, mae=-5.0),
        _entry("b", 50, 100, 1.0, mae=-5.0),
        _entry("c", 500, 10, 1.0, mae=-5.0),
    ]
    result = replay_fixed_bank(entries, max_slots=10)
    assert abs(result.adverse_from_entry_usd - 10.0) < 1e-9


def test_build_eligible_portfolio_includes_unresolved_slots() -> None:
    # An analyzable and an unresolved filled probe: BOTH occupy a slot; the unresolved one
    # has None pnl (so it fails-closed later), never silently excluded from the universe.
    watches = [WatchDecision("w-ok", "FOO", _BASE), WatchDecision("w-unres", "BAR", _BASE)]
    probes = {"w-ok": _probe("w-ok"), "w-unres": _probe("w-unres", exit_resolved=False)}
    entries = build_eligible_portfolio(
        watches, probes, _ZERO_FUNDING, position_usd=50.0, policy_720=True
    )
    by_tie = {e.tie_break: e for e in entries}
    assert set(by_tie) == {"w-ok", "w-unres"}
    assert by_tie["w-ok"].pnl_usd is not None
    assert abs(by_tie["w-ok"].pnl_usd - 1.0) < 1e-9  # net_720 2.0% of $50
    assert by_tie["w-unres"].pnl_usd is None


def test_evaluate_cohort_binds_contract_params_and_enforces_invariants() -> None:
    watches = [WatchDecision("w-ok", "FOO", _BASE), WatchDecision("w-rej", "BAR", _BASE)]
    probes = {"w-ok": _probe("w-ok"), "w-rej": _probe("w-rej", entry_ok=False)}
    ev = evaluate_cohort(HOLD12H_VERDICT_CONTRACT, watches, probes, _ZERO_FUNDING)
    assert sum(ev.funnel.values()) == len(watches)
    assert ev.funnel[ProbeClass.ANALYZABLE] == len(ev.pairs) == 1
    # PnL used the contract's frozen $50 position, not an arbitrary size.
    assert abs(ev.portfolio_720.window_pnl_usd - 1.0) < 1e-9


# --- formal window (both bounds) -----------------------------------------------


def test_filter_to_cohort_is_half_open_on_both_bounds() -> None:
    start = datetime(2026, 10, 2, tzinfo=UTC)
    end = datetime(2026, 10, 5, tzinfo=UTC)
    watches = [
        WatchDecision("pre", "FOO", start - timedelta(minutes=1)),
        WatchDecision("start", "FOO", start),
        WatchDecision("mid", "FOO", datetime(2026, 10, 3, tzinfo=UTC)),
        WatchDecision("end", "FOO", end),  # upper bound EXCLUDED
    ]
    kept = filter_to_cohort(watches, cohort_start=start, decision_prefix_end=end)
    assert [w.watch_id for w in kept] == ["start", "mid"]


def test_formal_cohort_start_rejects_naive_or_non_utc() -> None:
    import dataclasses

    from schurfer_analytics.momentum_flow_hold12h_verdict import Hold12hVerdictContract

    naive = dataclasses.replace(Hold12hVerdictContract(), cohort_start_iso="2026-10-01T00:00:00")
    with pytest.raises(ValueError, match="explicit UTC"):
        formal_cohort_start(naive)
    offset = dataclasses.replace(
        Hold12hVerdictContract(), cohort_start_iso="2026-10-01T00:00:00+02:00"
    )
    with pytest.raises(ValueError, match="explicit UTC"):
        formal_cohort_start(offset)
    ok = dataclasses.replace(Hold12hVerdictContract(), cohort_start_iso="2026-10-01T00:00:00+00:00")
    assert formal_cohort_start(ok) == datetime(2026, 10, 1, tzinfo=UTC)
    assert formal_cohort_start(Hold12hVerdictContract()) is None  # unset -> fail-closed later


# --- registration (atomic first-writer) ----------------------------------------


def test_registration_is_first_writer_and_immutable(tmp_path: Path) -> None:
    state = tmp_path / "reg.json"
    first = resolve_cohort_registration(state, now=datetime(2026, 10, 1, 14, 30, tzinfo=UTC))
    assert first.cohort_start == datetime(2026, 10, 2, tzinfo=UTC)
    assert first.just_registered
    again = resolve_cohort_registration(state, now=datetime(2026, 12, 1, tzinfo=UTC))
    assert again.registered_at == first.registered_at
    assert again.cohort_start == first.cohort_start
    assert not again.just_registered


def test_next_utc_day_boundary_is_strictly_after() -> None:
    oct2 = datetime(2026, 10, 2, tzinfo=UTC)
    assert next_utc_day_boundary(datetime(2026, 10, 1, tzinfo=UTC)) == oct2
    assert next_utc_day_boundary(datetime(2026, 10, 1, 23, 59, tzinfo=UTC)) == oct2


# --- fingerprint (row-level) ---------------------------------------------------


def _fp(rows_digest: str) -> str:
    funnel = dict.fromkeys(ProbeClass, 0)
    inputs = assemble_verdict_inputs(
        HOLD12H_VERDICT_CONTRACT,
        total_watches=1,
        funnel=funnel,
        pairs=(),
        portfolio_720=PortfolioResult(0.0, 0.0, 0, 0, 0, complete=True),
        portfolio_240=PortfolioResult(0.0, 0.0, 0, 0, 0, complete=True),
    )
    return verdict_fingerprint(
        contract_sha256=HOLD12H_VERDICT_CONTRACT.sha256_hex(),
        cohort_start=datetime(2026, 10, 2, tzinfo=UTC),
        decision_prefix_end=datetime(2026, 11, 2, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
        funding_source_id="no_registered_funding_source",
        data_versions={"capture": "v1"},
        rows_digest=rows_digest,
        funnel=funnel,
        inputs=inputs,
    )


def test_fingerprint_is_deterministic_and_row_sensitive() -> None:
    assert _fp("digestA") == _fp("digestA")
    assert _fp("digestA") != _fp("digestB")  # same aggregates, different rows -> differ
    assert len(_fp("digestA")) == 64


def test_rows_digest_distinguishes_different_rows() -> None:
    w = [WatchDecision("w1", "FOO", _BASE)]
    d1 = cohort_rows_digest(w, {"w1": _probe("w1", actual_gross=2.0)})
    d2 = cohort_rows_digest(w, {"w1": _probe("w1", actual_gross=3.0)})
    assert d1 != d2


def test_rows_digest_is_sensitive_to_route_and_funding() -> None:
    import dataclasses

    w = [WatchDecision("w1", "FOO", _BASE)]
    base_probe = _probe("w1")
    rerouted = dataclasses.replace(
        base_probe, route=InstrumentRoute("bybit", "linear", "BBBUSDT", "BBB/USDT:USDT")
    )
    # A re-route with identical metrics must change the digest (P1: route was omitted).
    assert cohort_rows_digest(w, {"w1": base_probe}) != cohort_rows_digest(w, {"w1": rerouted})
    # And the actual funding events used must change it too.
    f1 = _StubFunding((SettlementEvent(_BASE + timedelta(hours=4), 0.001, "v1"),))
    f2 = _StubFunding((SettlementEvent(_BASE + timedelta(hours=4), 0.002, "v1"),))
    assert cohort_rows_digest(w, {"w1": base_probe}, f1) != cohort_rows_digest(
        w, {"w1": base_probe}, f2
    )
