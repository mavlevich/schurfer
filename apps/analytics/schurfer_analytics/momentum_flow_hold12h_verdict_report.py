"""HYP-015 hold12h verdict reader -- the pure computation layer.

Turns the raw prospective cohort (WATCH decisions, hold12h probes with their per-
horizon outcome rows, and ACTUAL funding settlements) into the ``VerdictInputs`` the
pure rule (``momentum_flow_hold12h_verdict.decide_verdict``) consumes. Everything here
is pure and DB-free so it can be unit-tested exhaustively; the thin SQL/CLI layer maps
Postgres rows into these dataclasses and persists the artifact.

DRAFT / NOT FROZEN. A ``formal_run`` FAIL-CLOSES unless a registered actual-funding
source is supplied: the default ``NoRegisteredFundingSource`` marks every probe
``accounting_incomplete``, so no return can be laundered into formal evidence through
the fixed 5bps/8h model or through pump-anchored history. The prospective per-instrument
funding capture is a separate prerequisite PR; until it lands, only readiness (outcome-
blind counts and coverage) is meaningful.

Key honesty properties enforced here and covered by tests:
  * pairing is by ``watch_id`` only (never base/ticker matching);
  * the 240m counterfactual uses the SAME entry as the 720m policy (no look-ahead:
    a pre-240m exit is shared by both policies);
  * funding is charged on ACTUAL settlement timestamps in ``(entry_at, exit_at]``, a
    long debited when the rate is positive and credited when negative -- never a fixed
    8h proxy; an interval without proven full coverage is ``accounting_incomplete``;
  * every eligible WATCH stays in the denominator/funnel; no complete-case dropping;
  * portfolio drawdown and losing streak are computed in actual entry/trade order.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from statistics import fmean
from typing import TYPE_CHECKING, Protocol

from .clustered_inference import ClusterObservation, cluster_bootstrap_mean
from .momentum_flow_hold12h_verdict import (
    Hold12hVerdictContract,
    VerdictInputs,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

# The two hold horizons the verdict compares, both recorded on each hold12h probe's
# single entry (see momentum_flow_paper_contract.HOLD12H_PAPER_CONTRACT horizons).
HORIZON_240 = 240
HORIZON_720 = 720
_STOP_LOSS_REASON = "stop_loss"


class ProbeClass(StrEnum):
    """How a shared WATCH decision resolves. Every eligible WATCH gets exactly one;
    the non-analyzable classes stay in the funnel and gate ``insufficient_data``."""

    ANALYZABLE = "analyzable"  # both policies resolvable AND funding complete
    REJECTED_STALE = "rejected_stale"  # no probe / entry rejected / stale / quote fail
    UNRESOLVED = "unresolved"  # opened but the 240m or 720m outcome is not resolved
    ACCOUNTING_INCOMPLETE = "accounting_incomplete"  # resolved but funding not proven
    IDENTITY_UNRESOLVED = "identity_unresolved"  # no point-in-time canonical identity
    INTEGRITY_FAILURE = "integrity_failure"  # NaN/invalid numbers or non-positive notional


@dataclass(frozen=True)
class InstrumentRoute:
    """The immutable identity a funding source resolves against -- exact venue +
    instrument, NEVER a base/ticker string."""

    exchange: str
    market_type: str
    market_id: str  # the venue's canonical instrument id
    unified_symbol: str


@dataclass(frozen=True)
class SettlementEvent:
    settlement_at: datetime
    rate: float  # signed funding rate at that settlement
    source_version: str  # the capture/source version this rate came from


@dataclass(frozen=True)
class FundingCoverage:
    """A funding source's answer for one interval. ``proven_full_coverage`` is the
    honesty gate: an empty ``events`` tuple means zero funding ONLY when coverage is
    proven and no settlement boundary is unaccounted; otherwise the probe is
    ``accounting_incomplete``."""

    events: tuple[SettlementEvent, ...]
    proven_full_coverage: bool


class ActualFundingSource(Protocol):
    """Resolves ACTUAL funding settlements for a route over ``(entry_at, exit_at]``.
    Returns ``None`` when it cannot prove coverage (=> accounting_incomplete)."""

    def coverage(
        self, route: InstrumentRoute, entry_at: datetime, exit_at: datetime
    ) -> FundingCoverage | None: ...


class NoRegisteredFundingSource:
    """The fail-closed default: no registered source, so nothing is ever proven. Every
    probe becomes ``accounting_incomplete`` and a formal run yields no analyzable pairs
    -- returns cannot leak into formal evidence before the funding prerequisite lands."""

    def coverage(
        self, route: InstrumentRoute, entry_at: datetime, exit_at: datetime
    ) -> FundingCoverage | None:
        return None


@dataclass(frozen=True)
class HorizonOutcome:
    """A per-horizon mark on the probe's single entry. ``observed_at`` is the ACTUAL
    quote-observed timestamp (which can be later than the nominal due time), not
    ``entry_at + horizon``; funding and occupancy use it, never the nominal minute."""

    horizon_minutes: int
    resolved: bool
    observed_at: datetime | None  # actual observed exit time for this horizon
    gross_return_pct: float | None  # PERCENT, net of fees, BEFORE actual funding
    notional_usd: float | None


@dataclass(frozen=True)
class ProbeRecord:
    """One hold12h probe: its single entry, its actual (720m-policy) exit, and the per-
    horizon outcome rows on that same entry. ``gross`` returns here are PERCENT, net of
    fees, ex funding; the reader applies ACTUAL funding from the funding source."""

    watch_id: str
    route: InstrumentRoute
    entry_at: datetime
    entry_ok: bool  # a real fill (not rejected/stale/quote-failure)
    # The actual 720m policy exit (a stop/data-end can be earlier than entry+720m).
    exit_at: datetime | None
    exit_resolved: bool
    exit_reason: str | None  # the venue/broker exit rule that fired (e.g. stop_loss)
    actual_gross_return_pct: float | None  # PERCENT, 720m-policy realized, ex funding
    actual_notional_usd: float | None
    # Worst adverse excursion (PERCENT, <= 0) over the hold, for the conservative
    # simultaneous-MAE drawdown proxy. Used as an upper bound for BOTH policies (the 240m
    # policy's true MAE is no worse than the full hold's), so the proxy overstates risk.
    max_adverse_return_pct: float | None = None
    horizons: dict[int, HorizonOutcome] = field(default_factory=dict)


@dataclass(frozen=True)
class WatchDecision:
    """A shared WATCH decision -- the denominator unit. ``decision_at`` sets cohort
    membership; ``canonical_asset`` is the point-in-time identity that clusters the
    bootstrap -- ``None`` means identity could not be resolved (an integrity/missingness
    reason, never a silent drop or a bare-ticker fallback)."""

    watch_id: str
    canonical_asset: str | None
    decision_at: datetime


@dataclass(frozen=True)
class AnalyzablePair:
    watch_id: str
    canonical_asset: str
    entry_at: datetime
    net_720: float  # 720m-policy net (PERCENT) after ACTUAL funding
    net_240cf: float  # 240m counterfactual net (PERCENT) on the SAME entry, after funding
    # Actual occupancy ends (used by the fixed-bank portfolio replay), never nominal.
    exit_720_at: datetime
    exit_240_at: datetime


def _finite(*values: float) -> bool:
    return all(math.isfinite(v) for v in values)


class DuplicateSettlementError(ValueError):
    """A funding settlement appears twice for the same ``(settlement_at, source_version)``.
    This is DATA CORRUPTION, not ordinary missingness, so the caller classifies it as an
    integrity failure (blocking the formal verdict) rather than accounting_incomplete."""


def funding_usd_over_interval(
    coverage: FundingCoverage | None,
    *,
    entry_at: datetime,
    exit_at: datetime,
    notional_usd: float,
) -> float | None:
    """ACTUAL funding a LONG pays over ``(entry_at, exit_at]``, or ``None`` when it
    cannot be proven (=> accounting_incomplete) or any rate is non-finite.

    Half-open interval: a settlement exactly at ``entry_at`` is excluded, one exactly at
    ``exit_at`` is included. A long PAYS when the rate is positive (returns a positive
    cost) and RECEIVES when negative. No fixed 8h assumption -- only the events given.

    RAISES ``DuplicateSettlementError`` on two events sharing ``(settlement_at,
    source_version)`` -- that is corruption (would double-charge), an INTEGRITY failure,
    not the ``None``/accounting_incomplete of proven-absent funding.
    """
    if coverage is None or not coverage.proven_full_coverage:
        return None
    cost = 0.0
    seen: set[tuple[datetime, str]] = set()
    for event in coverage.events:
        if not math.isfinite(event.rate):
            return None
        if entry_at < event.settlement_at <= exit_at:
            key = (event.settlement_at, event.source_version)
            if key in seen:
                raise DuplicateSettlementError(
                    f"duplicate funding settlement at {event.settlement_at} "
                    f"({event.source_version})"
                )
            seen.add(key)
            cost += event.rate * notional_usd
    return cost if math.isfinite(cost) else None


def _net_after_funding(gross_return_pct: float, funding_usd: float, notional_usd: float) -> float:
    """PERCENT return net of ACTUAL funding. ``gross_return_pct`` is already a percent;
    the funding cost is converted to the SAME unit -- ``funding_usd / notional * 100`` --
    so the subtraction is unit-consistent. ``notional_usd`` is guaranteed positive by the
    integrity check upstream."""
    return gross_return_pct - (funding_usd / notional_usd) * 100.0


class PairResolution(StrEnum):
    OK = "ok"
    UNRESOLVED = "unresolved"
    ACCOUNTING_INCOMPLETE = "accounting_incomplete"
    INTEGRITY_FAILURE = "integrity_failure"


def resolve_pair(
    probe: ProbeRecord, funding: ActualFundingSource
) -> tuple[PairResolution, AnalyzablePair | None]:
    """Resolve one FILLED probe into its 720m/240m nets, or the reason it cannot be.

    Distinguishes the failure causes the colleague flagged: a missing/late 240m mark is
    ``UNRESOLVED`` (a data-resolution problem), NOT ``ACCOUNTING_INCOMPLETE`` (funding).
    No look-ahead: the 240m counterfactual shares the 720m policy's single entry, and the
    shared earlier exit applies ONLY to an actual ``stop_loss`` that fired no later than
    the executable 240m exit -- compared on ACTUAL observed timestamps, never the nominal
    ``entry_at + 240m``. Occupancy/funding use the actual observed exit times.
    """
    # Integrity: non-finite numbers or a non-positive notional cannot be turned into a
    # return net of funding; fail-closed rather than silently treat gross as net.
    if (
        probe.exit_at is None
        or not probe.exit_resolved
        or probe.actual_gross_return_pct is None
        or probe.actual_notional_usd is None
    ):
        return PairResolution.UNRESOLVED, None
    if not _finite(probe.actual_gross_return_pct, probe.actual_notional_usd):
        return PairResolution.INTEGRITY_FAILURE, None
    if probe.actual_notional_usd <= 0:
        return PairResolution.INTEGRITY_FAILURE, None

    try:
        funding_720 = funding_usd_over_interval(
            funding.coverage(probe.route, probe.entry_at, probe.exit_at),
            entry_at=probe.entry_at,
            exit_at=probe.exit_at,
            notional_usd=probe.actual_notional_usd,
        )
    except DuplicateSettlementError:
        return PairResolution.INTEGRITY_FAILURE, None
    if funding_720 is None:
        return PairResolution.ACCOUNTING_INCOMPLETE, None
    net_720 = _net_after_funding(
        probe.actual_gross_return_pct, funding_720, probe.actual_notional_usd
    )

    outcome_240 = probe.horizons.get(HORIZON_240)
    if outcome_240 is None or not outcome_240.resolved or outcome_240.observed_at is None:
        # No executable 240m mark to compare against -> unresolved, not a funding fault.
        return PairResolution.UNRESOLVED, None

    # Shared exit only for an ACTUAL stop that fired at or before the executable 240m
    # exit (both observed times). Otherwise the 240m policy exits at its own 240m mark.
    shared_stop = (
        probe.exit_reason == _STOP_LOSS_REASON and probe.exit_at <= outcome_240.observed_at
    )
    if shared_stop:
        return PairResolution.OK, AnalyzablePair(
            watch_id=probe.watch_id,
            canonical_asset="",
            entry_at=probe.entry_at,
            net_720=net_720,
            net_240cf=net_720,
            exit_720_at=probe.exit_at,
            exit_240_at=probe.exit_at,
        )

    if outcome_240.gross_return_pct is None or outcome_240.notional_usd is None:
        return PairResolution.UNRESOLVED, None
    if not _finite(outcome_240.gross_return_pct, outcome_240.notional_usd):
        return PairResolution.INTEGRITY_FAILURE, None
    if outcome_240.notional_usd <= 0:
        return PairResolution.INTEGRITY_FAILURE, None
    try:
        funding_240 = funding_usd_over_interval(
            funding.coverage(probe.route, probe.entry_at, outcome_240.observed_at),
            entry_at=probe.entry_at,
            exit_at=outcome_240.observed_at,
            notional_usd=outcome_240.notional_usd,
        )
    except DuplicateSettlementError:
        return PairResolution.INTEGRITY_FAILURE, None
    if funding_240 is None:
        return PairResolution.ACCOUNTING_INCOMPLETE, None
    net_240cf = _net_after_funding(
        outcome_240.gross_return_pct, funding_240, outcome_240.notional_usd
    )
    return PairResolution.OK, AnalyzablePair(
        watch_id=probe.watch_id,
        canonical_asset="",
        entry_at=probe.entry_at,
        net_720=net_720,
        net_240cf=net_240cf,
        exit_720_at=probe.exit_at,
        exit_240_at=outcome_240.observed_at,
    )


def classify_and_pair(
    watches: Sequence[WatchDecision],
    probes: dict[str, ProbeRecord],
    funding: ActualFundingSource,
) -> tuple[dict[ProbeClass, int], tuple[AnalyzablePair, ...]]:
    """Classify every WATCH into the funnel and return the analyzable pairs. Pairing is
    by ``watch_id`` only (never base/ticker). Every WATCH is counted exactly once and
    nothing is dropped; the classification stays visible for the missingness gates."""
    counts: dict[ProbeClass, int] = dict.fromkeys(ProbeClass, 0)
    pairs: list[AnalyzablePair] = []
    _resolution_to_class = {
        PairResolution.UNRESOLVED: ProbeClass.UNRESOLVED,
        PairResolution.ACCOUNTING_INCOMPLETE: ProbeClass.ACCOUNTING_INCOMPLETE,
        PairResolution.INTEGRITY_FAILURE: ProbeClass.INTEGRITY_FAILURE,
    }
    for watch in watches:
        if watch.canonical_asset is None or not watch.canonical_asset:
            # Identity must be resolved point-in-time; an undefined one is its own
            # missingness reason, not a bare-ticker fallback.
            counts[ProbeClass.IDENTITY_UNRESOLVED] += 1
            continue
        probe = probes.get(watch.watch_id)
        if probe is None or not probe.entry_ok:
            counts[ProbeClass.REJECTED_STALE] += 1
            continue
        resolution, pair = resolve_pair(probe, funding)
        if resolution is not PairResolution.OK or pair is None:
            counts[_resolution_to_class[resolution]] += 1
            continue
        counts[ProbeClass.ANALYZABLE] += 1
        pairs.append(
            AnalyzablePair(
                watch_id=pair.watch_id,
                canonical_asset=watch.canonical_asset,
                entry_at=pair.entry_at,
                net_720=pair.net_720,
                net_240cf=pair.net_240cf,
                exit_720_at=pair.exit_720_at,
                exit_240_at=pair.exit_240_at,
            )
        )
    return counts, tuple(pairs)


@dataclass(frozen=True)
class PortfolioEntry:
    """One position a policy could take. It occupies a slot over ``[entry_at, exit_at)``
    whether or not it later resolves; ``tie_break`` (the watch_id) is a FROZEN, outcome-
    blind key that orders equal arrival times so selection never depends on any return.
    ``pnl_usd`` / ``mae_usd`` are ``None`` for a taken-but-unresolved slot, which makes the
    window non-finite (Gate 0 fail-closes). ``mae_usd`` (<= 0) is the position's worst
    return FROM ENTRY (what the writer stores), used for the reported adverse-excursion
    diagnostic below."""

    tie_break: str
    asset: str
    entry_at: datetime
    exit_at: datetime
    pnl_usd: float | None
    mae_usd: float | None
    # PnL with funding set to ZERO for a slot whose actual funding is not proven. Only a
    # labelled sensitivity, never the verdict value; None when the price leg is unknown.
    pnl_zero_funding_usd: float | None = None


@dataclass(frozen=True)
class PortfolioResult:
    # None when ANY taken slot lacks a complete result: no funding bound is registered,
    # so the window PnL is not determined (Gate E then cannot pass), but it never becomes
    # a NaN that would trip the integrity gate ahead of the negative-EV rejection.
    window_pnl_usd: float | None
    # A REPORTED diagnostic, NOT a gated risk metric and NOT a true drawdown: the worst
    # simultaneous adverse-FROM-ENTRY excursion over taken slots with a known MAE. The
    # writer stores min-return-from-entry, not peak-to-trough, so a 100->130->110 path
    # reads 0 here while losing ~15% from the peak; between-quote moves are also unseen.
    adverse_from_entry_usd: float
    longest_losing_streak: int
    taken: int
    skipped_slots_full: int
    complete: bool  # False when a taken slot has no complete result
    incomplete_taken: int = 0
    # Sum of taken slots' occupied hours; occupancy = slot_hours / (slots * window hours).
    slot_hours: float = 0.0
    # Window PnL with unproven funding set to zero (labelled sensitivity, never gated).
    window_pnl_zero_funding_sensitivity_usd: float | None = None

    @property
    def incomplete_taken_fraction(self) -> float:
        return self.incomplete_taken / self.taken if self.taken else 0.0


def _worst_simultaneous_adverse_from_entry(taken: Sequence[PortfolioEntry]) -> float:
    """The largest total adverse-from-entry excursion of positions open at one instant.
    NOT a conservative drawdown bound: ``mae_usd`` is the worst return FROM ENTRY (the
    only thing the writer stores), so it misses peak-to-trough drawdown and between-quote
    moves. Reported as a diagnostic only; the verdict does NOT gate on it."""
    worst = 0.0
    for pivot in taken:
        total = 0.0
        for other in taken:
            if other.entry_at <= pivot.entry_at < other.exit_at and other.mae_usd is not None:
                total += -other.mae_usd  # mae_usd <= 0 -> add its magnitude
        worst = max(worst, total)
    return worst


def replay_fixed_bank(entries: Sequence[PortfolioEntry], *, max_slots: int) -> PortfolioResult:
    """Deterministic, OUTCOME-BLIND fixed-bank replay of ONE policy over the WATCH stream.

    Selection depends ONLY on decision-time information: entries are considered in
    ``(entry_at, tie_break)`` order (never a function of any return), a position releases
    its slot once ``exit_at <= `` the next arrival, and an arrival that finds no free slot
    is SKIPPED. Every FILLED probe is passed in (resolved or not), so a slot a later-
    unresolved position occupied is present and never freed by hindsight. If a TAKEN slot
    has no complete result (``pnl_usd`` / ``mae_usd`` None) the window PnL is None (not
    determined; no funding bound is registered) and the count is reported. The losing
    streak is over complete closed trades in exit order.
    """
    if max_slots <= 0:
        raise ValueError("max_slots must be positive")
    ordered = sorted(entries, key=lambda e: (e.entry_at, e.tie_break))
    open_exits: list[datetime] = []
    taken: list[PortfolioEntry] = []
    skipped = 0
    for entry in ordered:
        open_exits = [x for x in open_exits if x > entry.entry_at]  # release closed slots
        if len(open_exits) >= max_slots:
            skipped += 1
            continue
        open_exits.append(entry.exit_at)
        taken.append(entry)

    incomplete = [
        e
        for e in taken
        if e.pnl_usd is None
        or e.mae_usd is None
        or not math.isfinite(e.pnl_usd)
        or not math.isfinite(e.mae_usd)
    ]
    incomplete_ids = {e.tie_break for e in incomplete}
    slot_hours = sum((e.exit_at - e.entry_at).total_seconds() / 3600.0 for e in taken)
    zero_funding: float | None = 0.0
    for entry in taken:
        value = (
            entry.pnl_usd if entry.tie_break not in incomplete_ids else entry.pnl_zero_funding_usd
        )
        if value is None or not math.isfinite(value) or zero_funding is None:
            zero_funding = None
        else:
            zero_funding += value

    by_exit = sorted(
        (e for e in taken if e.tie_break not in incomplete_ids),
        key=lambda e: (e.exit_at, e.tie_break),
    )
    running = 0.0
    streak = 0
    longest_streak = 0
    for position in by_exit:
        pnl = position.pnl_usd
        assert pnl is not None  # incomplete slots are excluded above
        running += pnl
        if pnl < 0:
            streak += 1
            longest_streak = max(longest_streak, streak)
        else:
            streak = 0
    return PortfolioResult(
        window_pnl_usd=None if incomplete else running,
        adverse_from_entry_usd=_worst_simultaneous_adverse_from_entry(taken),
        longest_losing_streak=longest_streak,
        taken=len(taken),
        skipped_slots_full=skipped,
        complete=not incomplete,
        incomplete_taken=len(incomplete),
        slot_hours=slot_hours,
        window_pnl_zero_funding_sensitivity_usd=zero_funding,
    )


def build_eligible_portfolio(
    watches: Sequence[WatchDecision],
    probes: dict[str, ProbeRecord],
    funding: ActualFundingSource,
    *,
    position_usd: float,
    policy_720: bool,
) -> tuple[PortfolioEntry, ...]:
    """The OUTCOME-BLIND selection universe: one entry per point-in-time-eligible FILLED
    probe (identity resolved AND a real entry), BEFORE outcome classification. A resolved
    probe carries its pnl/mae; an unresolved one still occupies its slot with ``None`` pnl/
    mae (so the window fail-closes rather than silently free the slot). Occupancy runs to
    the ACTUAL exit when known, else the nominal max-hold bound for that policy."""
    max_hold = HORIZON_720 if policy_720 else HORIZON_240
    entries: list[PortfolioEntry] = []
    for watch in watches:
        if not watch.canonical_asset:
            continue
        probe = probes.get(watch.watch_id)
        if probe is None or not probe.entry_ok:
            continue
        resolution, pair = resolve_pair(probe, funding)
        nominal_exit = probe.entry_at + timedelta(minutes=max_hold)
        if resolution is PairResolution.OK and pair is not None:
            net = pair.net_720 if policy_720 else pair.net_240cf
            exit_at = pair.exit_720_at if policy_720 else pair.exit_240_at
            mae_pct = probe.max_adverse_return_pct
            mae_usd = None if mae_pct is None else (min(mae_pct, 0.0) / 100.0) * position_usd
            entries.append(
                PortfolioEntry(
                    tie_break=watch.watch_id,
                    asset=watch.canonical_asset,
                    entry_at=probe.entry_at,
                    exit_at=exit_at,
                    pnl_usd=(net / 100.0) * position_usd,
                    mae_usd=mae_usd,
                )
            )
        else:
            exit_at, gross_pct = _incomplete_policy_exit(
                probe, policy_720=policy_720, nominal_exit=nominal_exit
            )
            entries.append(
                PortfolioEntry(
                    tie_break=watch.watch_id,
                    asset=watch.canonical_asset,
                    entry_at=probe.entry_at,
                    exit_at=exit_at,
                    pnl_usd=None,
                    mae_usd=None,
                    pnl_zero_funding_usd=None
                    if gross_pct is None or not math.isfinite(gross_pct)
                    else (gross_pct / 100.0) * position_usd,
                )
            )
    return tuple(entries)


def _incomplete_policy_exit(
    probe: ProbeRecord, *, policy_720: bool, nominal_exit: datetime
) -> tuple[datetime, float | None]:
    """Slot release time and ex-funding return (PERCENT) of a filled probe whose pair did
    not resolve. The 240m policy releases at its own observed 240m mark (or an actual stop
    that fired before it), never at the 720m exit; unknown marks fall back to the nominal
    bound of THAT policy. Never used for a pair that resolved."""
    if policy_720:
        return probe.exit_at or nominal_exit, probe.actual_gross_return_pct
    outcome_240 = probe.horizons.get(HORIZON_240)
    mark = outcome_240.observed_at if outcome_240 is not None else None
    stopped_first = (
        probe.exit_reason == _STOP_LOSS_REASON
        and probe.exit_at is not None
        and (mark is None or probe.exit_at <= mark)
        and probe.exit_at <= nominal_exit
    )
    if stopped_first:
        assert probe.exit_at is not None
        return probe.exit_at, probe.actual_gross_return_pct
    if mark is not None:
        return mark, outcome_240.gross_return_pct if outcome_240 is not None else None
    return nominal_exit, None


def _iso_week(moment: datetime) -> tuple[int, int]:
    iso = moment.isocalendar()
    return iso.year, iso.week


def _max_group_fraction(keys: Sequence[object]) -> float:
    if not keys:
        return 0.0
    counts: dict[object, int] = {}
    for key in keys:
        counts[key] = counts.get(key, 0) + 1
    return max(counts.values()) / len(keys)


def assemble_verdict_inputs(
    contract: Hold12hVerdictContract,
    *,
    total_watches: int,
    funnel: dict[ProbeClass, int],
    pairs: Sequence[AnalyzablePair],
    portfolio_720: PortfolioResult,
    portfolio_240: PortfolioResult,
) -> VerdictInputs:
    """Assemble the pure-rule inputs from the classified cohort. The standalone MEAN is
    computed whenever there is >= 1 pair (it needs no clusters), so a mature loser reaches
    Gate B; the cluster-bootstrap CIs are computed only with >= 2 asset clusters (else
    ``ci_computable`` is False and Gate D returns insufficient_evidence)."""
    assets = [p.canonical_asset for p in pairs]
    weeks = [_iso_week(p.entry_at) for p in pairs]
    distinct_clusters = len(set(assets))
    has_pairs = len(pairs) >= 1
    ci_computable = has_pairs and distinct_clusters >= 2

    standalone_mean = fmean(p.net_720 for p in pairs) if has_pairs else 0.0
    standalone_ci_lower = 0.0
    paired_ci_lower = 0.0
    if ci_computable:
        standalone = cluster_bootstrap_mean(
            tuple(ClusterObservation(p.canonical_asset, p.net_720) for p in pairs),
            iterations=contract.bootstrap_iterations,
            seed=contract.bootstrap_seed,
            confidence_level=contract.confidence_level,
        )
        paired = cluster_bootstrap_mean(
            tuple(ClusterObservation(p.canonical_asset, p.net_720 - p.net_240cf) for p in pairs),
            iterations=contract.bootstrap_iterations,
            seed=contract.bootstrap_seed,
            confidence_level=contract.confidence_level,
        )
        standalone_mean = standalone.estimate.point_estimate
        standalone_ci_lower = standalone.estimate.lower_bound
        paired_ci_lower = paired.estimate.lower_bound

    denom = max(total_watches, 1)
    return VerdictInputs(
        analyzable_pairs=len(pairs),
        ci_computable=ci_computable,
        standalone_720_mean_net=standalone_mean,
        standalone_720_ci_lower=standalone_ci_lower,
        paired_diff_ci_lower=paired_ci_lower,
        distinct_asset_clusters=distinct_clusters,
        distinct_utc_weeks=len(set(weeks)),
        max_single_asset_fraction=_max_group_fraction(assets),
        max_single_week_fraction=_max_group_fraction(weeks),
        rejected_stale_fraction=funnel[ProbeClass.REJECTED_STALE] / denom,
        unresolved_fraction=funnel[ProbeClass.UNRESOLVED] / denom,
        accounting_incomplete_fraction=funnel[ProbeClass.ACCOUNTING_INCOMPLETE] / denom,
        identity_unresolved_fraction=funnel[ProbeClass.IDENTITY_UNRESOLVED] / denom,
        integrity_failure_fraction=funnel[ProbeClass.INTEGRITY_FAILURE] / denom,
        portfolio_720_window_pnl_usd=portfolio_720.window_pnl_usd,
        portfolio_240_window_pnl_usd=portfolio_240.window_pnl_usd,
        portfolio_incomplete_slot_fraction=max(
            portfolio_720.incomplete_taken_fraction, portfolio_240.incomplete_taken_fraction
        ),
    )


@dataclass(frozen=True)
class CohortEvaluation:
    inputs: VerdictInputs
    funnel: dict[ProbeClass, int]
    pairs: tuple[AnalyzablePair, ...]
    portfolio_720: PortfolioResult
    portfolio_240: PortfolioResult


def evaluate_cohort(
    contract: Hold12hVerdictContract,
    watches: Sequence[WatchDecision],
    probes: dict[str, ProbeRecord],
    funding: ActualFundingSource,
) -> CohortEvaluation:
    """The single orchestration that turns the cohort into verdict inputs using ONLY the
    contract's frozen parameters (``position_usd``, ``max_concurrent_slots``, bootstrap,
    confidence). The portfolio universe is built ONCE from every point-in-time-eligible
    filled probe (``build_eligible_portfolio``), so a slot an eventually-unresolved
    position occupied is never freed by hindsight. Three invariants are enforced fail-
    closed rather than left to convention: the funnel covers every WATCH, ANALYZABLE
    equals the number of pairs, and every pair's slot appears in the eligible stream."""
    counts, pairs = classify_and_pair(watches, probes, funding)
    if sum(counts.values()) != len(watches):
        raise ValueError("funnel does not account for every WATCH decision")
    if counts[ProbeClass.ANALYZABLE] != len(pairs):
        raise ValueError("ANALYZABLE count does not equal the number of analyzable pairs")

    entries_720 = build_eligible_portfolio(
        watches, probes, funding, position_usd=contract.position_usd, policy_720=True
    )
    entries_240 = build_eligible_portfolio(
        watches, probes, funding, position_usd=contract.position_usd, policy_720=False
    )
    eligible_ids = {e.tie_break for e in entries_720}
    if not all(p.watch_id in eligible_ids for p in pairs):
        raise ValueError("an analyzable pair is missing from the eligible portfolio stream")

    portfolio_720 = replay_fixed_bank(entries_720, max_slots=contract.max_concurrent_slots)
    portfolio_240 = replay_fixed_bank(entries_240, max_slots=contract.max_concurrent_slots)
    inputs = assemble_verdict_inputs(
        contract,
        total_watches=len(watches),
        funnel=counts,
        pairs=pairs,
        portfolio_720=portfolio_720,
        portfolio_240=portfolio_240,
    )
    return CohortEvaluation(inputs, counts, pairs, portfolio_720, portfolio_240)


# --- cohort registration (first-writer-wins) -----------------------------------


@dataclass(frozen=True)
class CohortRegistration:
    """The DRAFT/readiness information barrier. ``registered_at`` is stamped on the FIRST
    run and never changes; the readiness cohort starts at the next whole UTC-day boundary
    after it. ``just_registered`` is True only on the run that created it -- that run is
    registration-ONLY and must exit before reading any return. NOTE: a formal run does NOT
    use this; it uses the LITERAL ``cohort_start_iso`` frozen in the contract (see
    ``formal_cohort_start``). This runtime state is a convenience for the pre-freeze
    readiness phase, never a substitute for the frozen literal."""

    registered_at: datetime
    cohort_start: datetime
    just_registered: bool


def next_utc_day_boundary(moment: datetime) -> datetime:
    """The first UTC midnight strictly after ``moment``."""
    moment = moment.astimezone(UTC)
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(days=1)


def resolve_cohort_registration(
    state_path: Path, *, now: datetime, allow_rebaseline: bool = False
) -> CohortRegistration:
    """First-writer-wins via ATOMIC exclusive creation, so two concurrent first runs
    cannot both win: the file is created with ``O_CREAT | O_EXCL``; whoever loses the race
    (or any later run) reloads the stored value instead of overwriting it. A stored value
    inconsistent with its ``registered_at`` is refused unless ``allow_rebaseline`` is set
    (a logged, deliberate re-baseline)."""
    registered_at = now.astimezone(UTC)
    cohort_start = next_utc_day_boundary(registered_at)
    payload = json.dumps(
        {"registered_at": registered_at.isoformat(), "cohort_start": cohort_start.isoformat()},
        sort_keys=True,
    )
    try:
        fd = os.open(state_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        fd = None
    if fd is not None:
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
        return CohortRegistration(registered_at, cohort_start, just_registered=True)

    stored = json.loads(state_path.read_text())
    stored_registered = datetime.fromisoformat(stored["registered_at"])
    stored_cohort_start = next_utc_day_boundary(stored_registered)
    if not allow_rebaseline and stored.get("cohort_start") != stored_cohort_start.isoformat():
        raise ValueError(
            "cohort registration is inconsistent with the stored registered_at; "
            "refusing to run without an explicit re-baseline"
        )
    return CohortRegistration(stored_registered, stored_cohort_start, just_registered=False)


def formal_cohort_start(contract: Hold12hVerdictContract) -> datetime | None:
    """The LITERAL frozen formal boundary, or ``None`` when the contract is not yet
    registered (so a formal run fail-closes).

    The boundary MUST carry an explicit UTC offset: a naive timestamp like
    ``2026-10-01T00:00:00`` would be interpreted in the host's local timezone (shifting
    the cohort by the host offset), so it is rejected. Only an explicit +00:00 / Z is
    accepted."""
    if contract.cohort_start_iso is None:
        return None
    moment = datetime.fromisoformat(contract.cohort_start_iso)
    if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
        raise ValueError(
            "cohort_start_iso must be an explicit UTC instant (offset +00:00), "
            f"got {contract.cohort_start_iso!r}"
        )
    return moment.astimezone(UTC)


def formal_decision_prefix_end(contract: Hold12hVerdictContract) -> datetime | None:
    """The ONE frozen decision-time prefix, or ``None`` when not registered. Same explicit-
    UTC rule as ``formal_cohort_start``."""
    if contract.decision_prefix_end_iso is None:
        return None
    moment = datetime.fromisoformat(contract.decision_prefix_end_iso)
    if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
        raise ValueError(
            "decision_prefix_end_iso must be an explicit UTC instant (offset +00:00), "
            f"got {contract.decision_prefix_end_iso!r}"
        )
    return moment.astimezone(UTC)


def filter_to_cohort(
    watches: Sequence[WatchDecision], *, cohort_start: datetime, decision_prefix_end: datetime
) -> tuple[WatchDecision, ...]:
    """The formal window is the HALF-OPEN interval ``[cohort_start, decision_prefix_end)``
    -- both bounds enforced (the fingerprint declares both). This keeps already-accrued
    (and any already-inspected) probes out of the formal denominator and pins the upper
    decision-time prefix at which the verdict is evaluated exactly once."""
    if decision_prefix_end <= cohort_start:
        raise ValueError("decision_prefix_end must be after cohort_start")
    return tuple(w for w in watches if cohort_start <= w.decision_at < decision_prefix_end)


# --- deterministic fingerprint -------------------------------------------------


def _iso_or_none(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(UTC).isoformat()


def _route_tuple(route: InstrumentRoute) -> tuple[str, str, str, str]:
    return (route.exchange, route.market_type, route.market_id, route.unified_symbol)


def cohort_rows_digest(
    watches: Sequence[WatchDecision],
    probes: dict[str, ProbeRecord],
    funding: ActualFundingSource | None = None,
) -> str:
    """A ROW-LEVEL sha over the raw cohort so two different row sets that happen to
    produce the same aggregate metrics get DIFFERENT fingerprints. It covers each WATCH,
    its probe's exact ``InstrumentRoute`` (so a re-route AAAUSDT->BBBUSDT changes the
    digest), entry/exit/returns and every horizon mark, AND -- when a funding source is
    given -- the actual settlement events it returns for the 720m interval (so the funding
    inputs are pinned, not just the funding source name)."""
    rows: list[tuple[object, ...]] = []
    for watch in sorted(watches, key=lambda w: w.watch_id):
        probe = probes.get(watch.watch_id)
        horizon_rows: tuple[tuple[object, ...], ...] = ()
        funding_rows: tuple[tuple[object, ...], ...] | None = None
        route_row: tuple[str, str, str, str] | None = None
        if probe is not None:
            route_row = _route_tuple(probe.route)
            horizon_rows = tuple(
                (
                    h.horizon_minutes,
                    h.resolved,
                    _iso_or_none(h.observed_at),
                    h.gross_return_pct,
                    h.notional_usd,
                )
                for h in sorted(probe.horizons.values(), key=lambda h: h.horizon_minutes)
            )
            if funding is not None and probe.exit_at is not None:
                coverage = funding.coverage(probe.route, probe.entry_at, probe.exit_at)
                if coverage is not None:
                    funding_rows = tuple(
                        (e.settlement_at.astimezone(UTC).isoformat(), e.rate, e.source_version)
                        for e in sorted(coverage.events, key=lambda e: (e.settlement_at, e.rate))
                    )
        rows.append(
            (
                watch.watch_id,
                watch.canonical_asset,
                watch.decision_at.astimezone(UTC).isoformat(),
                route_row,
                None if probe is None else probe.entry_ok,
                None if probe is None else _iso_or_none(probe.entry_at),
                None if probe is None else _iso_or_none(probe.exit_at),
                None if probe is None else probe.exit_reason,
                None if probe is None else probe.actual_gross_return_pct,
                None if probe is None else probe.actual_notional_usd,
                horizon_rows,
                funding_rows,
            )
        )
    return sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def verdict_fingerprint(
    *,
    contract_sha256: str,
    cohort_start: datetime,
    decision_prefix_end: datetime,
    code_revision: str,
    working_tree_dirty: bool,
    funding_source_id: str,
    data_versions: dict[str, str],
    rows_digest: str,
    funnel: dict[ProbeClass, int],
    inputs: VerdictInputs,
) -> str:
    """A reproducible sha over everything that determines the verdict: the contract, the
    window bounds, the code revision + dirty flag, which funding source produced the
    economics, the capture/data/bootstrap versions, a ROW-LEVEL digest of the raw inputs
    (so identical aggregates over different rows do not collide), the funnel, and the
    computed inputs."""
    from dataclasses import asdict

    payload = {
        "contract_sha256": contract_sha256,
        "cohort_start": cohort_start.astimezone(UTC).isoformat(),
        "decision_prefix_end": decision_prefix_end.astimezone(UTC).isoformat(),
        "code_revision": code_revision,
        "working_tree_dirty": working_tree_dirty,
        "funding_source_id": funding_source_id,
        "data_versions": dict(sorted(data_versions.items())),
        "rows_digest": rows_digest,
        "funnel": {cls.value: funnel[cls] for cls in ProbeClass},
        "inputs": asdict(inputs),
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
