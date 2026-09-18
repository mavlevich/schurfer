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
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
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


class ProbeClass(StrEnum):
    """How a shared WATCH decision resolves. Every eligible WATCH gets exactly one;
    the missingness classes stay in the funnel and gate ``insufficient_data``."""

    ANALYZABLE = "analyzable"  # both policies resolvable AND funding complete
    REJECTED_STALE = "rejected_stale"  # no probe / entry rejected / stale / quote fail
    UNRESOLVED = "unresolved"  # opened but the 240m or 720m outcome is not resolved
    ACCOUNTING_INCOMPLETE = "accounting_incomplete"  # resolved but funding not proven


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
    horizon_minutes: int
    resolved: bool
    gross_return_pct: float | None  # net of fees but BEFORE actual funding is applied
    notional_usd: float | None


@dataclass(frozen=True)
class ProbeRecord:
    """One hold12h probe: its single entry, its actual (720m-policy) exit, and the per-
    horizon outcome rows on that same entry. ``gross`` returns here exclude funding; the
    reader applies ACTUAL funding from the funding source."""

    watch_id: str
    canonical_asset: str
    route: InstrumentRoute
    entry_at: datetime
    entry_ok: bool  # a real fill (not rejected/stale/quote-failure)
    # The actual 720m policy exit (a stop/data-end can be earlier than entry+720m).
    exit_at: datetime | None
    exit_resolved: bool
    actual_gross_return_pct: float | None  # 720m-policy realized, ex funding
    actual_notional_usd: float | None
    horizons: dict[int, HorizonOutcome] = field(default_factory=dict)


@dataclass(frozen=True)
class WatchDecision:
    """A shared WATCH decision -- the denominator unit. ``decision_at`` sets cohort
    membership; ``canonical_asset`` clusters the bootstrap."""

    watch_id: str
    canonical_asset: str
    decision_at: datetime


@dataclass(frozen=True)
class AnalyzablePair:
    watch_id: str
    canonical_asset: str
    entry_at: datetime
    net_720: float  # 720m-policy net after ACTUAL funding
    net_240cf: float  # 240m counterfactual net on the SAME entry, after ACTUAL funding


def funding_usd_over_interval(
    coverage: FundingCoverage | None,
    *,
    entry_at: datetime,
    exit_at: datetime,
    notional_usd: float,
) -> float | None:
    """ACTUAL funding a LONG pays over ``(entry_at, exit_at]``, or ``None`` when it
    cannot be proven (=> accounting_incomplete).

    Half-open interval: a settlement exactly at ``entry_at`` is excluded, one exactly at
    ``exit_at`` is included. A long PAYS when the rate is positive (returns a positive
    cost) and RECEIVES when negative. No fixed 8h assumption -- only the events given.
    """
    if coverage is None or not coverage.proven_full_coverage:
        return None
    cost = 0.0
    for event in coverage.events:
        if entry_at < event.settlement_at <= exit_at:
            cost += event.rate * notional_usd
    return cost


def counterfactual_nets(
    probe: ProbeRecord,
    funding: ActualFundingSource,
) -> tuple[float, float] | None:
    """``(net_720, net_240cf)`` after ACTUAL funding for one probe, or ``None`` when the
    probe is not an analyzable pair (bad entry, unresolved horizon, or funding not
    proven -- the caller classifies which).

    No look-ahead: the 240m counterfactual shares the 720m policy's single entry. If the
    720m policy actually exited at or before entry+240m (a stop or data end), BOTH
    policies realize that identical earlier exit. Otherwise the 240m policy exits at the
    probe's own recorded 240m horizon mark.
    """
    if not probe.entry_ok or probe.exit_at is None or not probe.exit_resolved:
        return None
    if probe.actual_gross_return_pct is None or probe.actual_notional_usd is None:
        return None

    coverage = funding.coverage(probe.route, probe.entry_at, probe.exit_at)
    funding_720 = funding_usd_over_interval(
        coverage,
        entry_at=probe.entry_at,
        exit_at=probe.exit_at,
        notional_usd=probe.actual_notional_usd,
    )
    if funding_720 is None:
        return None
    net_720 = _net_after_funding(
        probe.actual_gross_return_pct, funding_720, probe.actual_notional_usd
    )

    shared_stop = probe.exit_at <= probe.entry_at + timedelta(minutes=HORIZON_240)
    if shared_stop:
        # Both policies share the identical earlier exit -> the paired difference is 0.
        return net_720, net_720

    outcome_240 = probe.horizons.get(HORIZON_240)
    if outcome_240 is None or not outcome_240.resolved:
        return None
    if outcome_240.gross_return_pct is None or outcome_240.notional_usd is None:
        return None
    exit_240_at = probe.entry_at + timedelta(minutes=HORIZON_240)
    coverage_240 = funding.coverage(probe.route, probe.entry_at, exit_240_at)
    funding_240 = funding_usd_over_interval(
        coverage_240,
        entry_at=probe.entry_at,
        exit_at=exit_240_at,
        notional_usd=outcome_240.notional_usd,
    )
    if funding_240 is None:
        return None
    net_240cf = _net_after_funding(
        outcome_240.gross_return_pct, funding_240, outcome_240.notional_usd
    )
    return net_720, net_240cf


def _net_after_funding(gross_return_pct: float, funding_usd: float, notional_usd: float) -> float:
    """Return-fraction net of ACTUAL funding: subtract the funding cost (positive = a
    payment) as a fraction of the position notional."""
    if notional_usd <= 0:
        return gross_return_pct
    return gross_return_pct - funding_usd / notional_usd


def classify_and_pair(
    watches: Sequence[WatchDecision],
    probes: dict[str, ProbeRecord],
    funding: ActualFundingSource,
) -> tuple[dict[ProbeClass, int], tuple[AnalyzablePair, ...]]:
    """Classify every WATCH into the funnel and return the analyzable pairs. Pairing is
    by ``watch_id`` only. Every WATCH is counted exactly once; nothing is dropped."""
    counts: dict[ProbeClass, int] = dict.fromkeys(ProbeClass, 0)
    pairs: list[AnalyzablePair] = []
    for watch in watches:
        probe = probes.get(watch.watch_id)
        if probe is None or not probe.entry_ok:
            counts[ProbeClass.REJECTED_STALE] += 1
            continue
        if probe.exit_at is None or not probe.exit_resolved:
            counts[ProbeClass.UNRESOLVED] += 1
            continue
        nets = counterfactual_nets(probe, funding)
        if nets is None:
            # Resolved but funding not proven (or the 240m mark is missing) -> keep it in
            # the funnel as accounting_incomplete, never silently dropped.
            counts[ProbeClass.ACCOUNTING_INCOMPLETE] += 1
            continue
        net_720, net_240cf = nets
        counts[ProbeClass.ANALYZABLE] += 1
        pairs.append(
            AnalyzablePair(
                watch_id=watch.watch_id,
                canonical_asset=watch.canonical_asset,
                entry_at=probe.entry_at,
                net_720=net_720,
                net_240cf=net_240cf,
            )
        )
    return counts, tuple(pairs)


@dataclass(frozen=True)
class PortfolioEntry:
    """One position a policy could take: it occupies a slot over ``[entry_at, exit_at)``
    and realizes ``pnl_usd`` at ``exit_at``."""

    asset: str
    entry_at: datetime
    exit_at: datetime
    pnl_usd: float


@dataclass(frozen=True)
class PortfolioResult:
    window_pnl_usd: float
    drawdown_usd: float  # conservative realized-equity proxy (labelled, not exact float)
    longest_losing_streak: int
    taken: int
    skipped_slots_full: int


def replay_fixed_bank(entries: Sequence[PortfolioEntry], *, max_slots: int) -> PortfolioResult:
    """Deterministic fixed-bank replay of ONE policy over the actual WATCH stream.

    Frozen selection policy: entries are considered in ``(entry_at, asset)`` order (the
    real arrival order, ties broken by asset for determinism); a position releases its
    slot once ``exit_at <= `` the next entry's time; if all slots are occupied when an
    entry arrives it is SKIPPED (never queued). Drawdown is a conservative realized-
    equity proxy -- the running cumulative realized PnL in EXIT order, peak-to-trough --
    NOT an exact floating mark (paper storage lacks a synchronous cross-position mark),
    and the losing streak is over CLOSED trades in exit order.
    """
    if max_slots <= 0:
        raise ValueError("max_slots must be positive")
    ordered = sorted(entries, key=lambda e: (e.entry_at, e.asset))
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

    by_exit = sorted(taken, key=lambda e: (e.exit_at, e.asset))
    running = 0.0
    peak = 0.0
    max_drawdown = 0.0
    streak = 0
    longest_streak = 0
    for position in by_exit:
        running += position.pnl_usd
        peak = max(peak, running)
        max_drawdown = max(max_drawdown, peak - running)
        if position.pnl_usd < 0:
            streak += 1
            longest_streak = max(longest_streak, streak)
        else:
            streak = 0
    return PortfolioResult(
        window_pnl_usd=running,
        drawdown_usd=max_drawdown,
        longest_losing_streak=longest_streak,
        taken=len(taken),
        skipped_slots_full=skipped,
    )


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
    """Assemble the pure-rule inputs from the classified cohort. The bootstrap CI is
    clustered by canonical asset; it is only computable with >= 2 clusters and >= 1 pair
    (else ``ci_computable`` is False and Gate A fail-closes to insufficient_data)."""
    assets = [p.canonical_asset for p in pairs]
    weeks = [_iso_week(p.entry_at) for p in pairs]
    distinct_clusters = len(set(assets))
    ci_computable = len(pairs) >= 1 and distinct_clusters >= 2

    if ci_computable:
        standalone = cluster_bootstrap_mean(
            tuple(ClusterObservation(p.canonical_asset, p.net_720) for p in pairs),
            confidence_level=contract.confidence_level,
        )
        paired = cluster_bootstrap_mean(
            tuple(ClusterObservation(p.canonical_asset, p.net_720 - p.net_240cf) for p in pairs),
            confidence_level=contract.confidence_level,
        )
        standalone_mean = standalone.estimate.point_estimate
        standalone_ci_lower = standalone.estimate.lower_bound
        paired_ci_lower = paired.estimate.lower_bound
    else:
        standalone_mean = 0.0
        standalone_ci_lower = 0.0
        paired_ci_lower = 0.0

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
        portfolio_720_window_pnl_usd=portfolio_720.window_pnl_usd,
        portfolio_240_window_pnl_usd=portfolio_240.window_pnl_usd,
        portfolio_720_drawdown_usd=portfolio_720.drawdown_usd,
        portfolio_240_drawdown_usd=portfolio_240.drawdown_usd,
    )


# --- cohort registration (first-writer-wins) -----------------------------------


@dataclass(frozen=True)
class CohortRegistration:
    """The immutable information barrier. ``registered_at`` is stamped on the FIRST run
    and never changes; the formal cohort starts at the next whole UTC-day boundary after
    it. Probes decided before that boundary -- including all pre-registration history --
    are operational/readiness only, never formal evidence."""

    registered_at: datetime
    cohort_start: datetime


def next_utc_day_boundary(moment: datetime) -> datetime:
    """The first UTC midnight strictly after ``moment``."""
    moment = moment.astimezone(UTC)
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(days=1)


def resolve_cohort_registration(
    state_path: Path, *, now: datetime, allow_rebaseline: bool = False
) -> CohortRegistration:
    """First-writer-wins: persist ``registered_at`` on the first run and never move it.

    A merge timestamp cannot be frozen inside its own commit, so the boundary is stamped
    by the first execution and stored immutably. A later run reloads the stored value; a
    request to change it is refused unless ``allow_rebaseline`` is explicitly set (a
    logged, deliberate re-baseline).
    """
    if state_path.exists():
        stored = json.loads(state_path.read_text())
        registered_at = datetime.fromisoformat(stored["registered_at"])
        cohort_start = next_utc_day_boundary(registered_at)
        if not allow_rebaseline and stored.get("cohort_start") != cohort_start.isoformat():
            raise ValueError(
                "cohort registration is inconsistent with the stored registered_at; "
                "refusing to run without an explicit re-baseline"
            )
        return CohortRegistration(registered_at, cohort_start)

    registered_at = now.astimezone(UTC)
    cohort_start = next_utc_day_boundary(registered_at)
    state_path.write_text(
        json.dumps(
            {"registered_at": registered_at.isoformat(), "cohort_start": cohort_start.isoformat()},
            sort_keys=True,
        )
    )
    return CohortRegistration(registered_at, cohort_start)


def filter_to_cohort(
    watches: Sequence[WatchDecision], cohort_start: datetime
) -> tuple[WatchDecision, ...]:
    """Only decisions on/after the formal cohort start are evidence. This is the
    barrier that keeps already-accrued (and any already-inspected) probes out of the
    formal denominator."""
    return tuple(w for w in watches if w.decision_at >= cohort_start)


# --- deterministic fingerprint -------------------------------------------------


def verdict_fingerprint(
    *,
    contract_sha256: str,
    cohort_start: datetime,
    decision_prefix_end: datetime,
    code_revision: str,
    working_tree_dirty: bool,
    funding_source_id: str,
    funnel: dict[ProbeClass, int],
    inputs: VerdictInputs,
) -> str:
    """A reproducible sha over everything that determines the verdict: the contract, the
    window bounds, the code revision + dirty flag, which funding source produced the
    economics, the funnel, and the computed inputs. Same inputs => same fingerprint."""
    from dataclasses import asdict

    payload = {
        "contract_sha256": contract_sha256,
        "cohort_start": cohort_start.astimezone(UTC).isoformat(),
        "decision_prefix_end": decision_prefix_end.astimezone(UTC).isoformat(),
        "code_revision": code_revision,
        "working_tree_dirty": working_tree_dirty,
        "funding_source_id": funding_source_id,
        "funnel": {cls.value: funnel[cls] for cls in ProbeClass},
        "inputs": asdict(inputs),
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
