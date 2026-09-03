"""Frozen prospective forward cohort for source-lead qualified capture v3.

research/gate-source-lead-forward-cohort-v1. Registers the untouched forward
read the 3-PR registry-activation sequence
(research/source-lead-derivative-market-evidence-v1,
research/gate-source-lead-registry-activation-v3 PR 2/PR 3) was built to
answer: for identity- and route-verified assets, does an immediate long
entry on the selected target exchange, the moment Gate shows a leading
source-lead capture, carry a real after-cost edge over the following half
hour?

## Colleague review (2026-08-30/31): this is NOT a same-methodology
## confirmation of HYP-012

`docs/research/discovery-ledger.md` HYP-012's frozen primary metric is a
**paired delta** (early-entry net return minus confirmation-entry net
return), cluster-bootstrapped and **Holm-corrected across 4 source/execution
routes** (gate/mexc -> binance/bybit). Its own "Next" instruction calls for
re-running that exact paired, 4-route, Holm-corrected family on an untouched
identity-verified forward cohort before any promotion decision.

The identity- and route-verified registry (v3, 14 canonical assets) covers
exactly **one** of those four routes -- gate -> binance -- so replicating
the full 4-route Holm family is not possible here. Claiming this cohort
confirms HYP-012 in that original sense would be dishonest about what a
single, currently-unconfirmable-on-3-of-4-routes registry can actually test.

This contract is registered as its own narrower estimand instead
(`ESTIMAND_VERSION` below): standalone after-cost net return on the
qualified, verified gate -> binance route alone. That is honestly a
*different*, arguably more directly money-relevant question than HYP-012's
original "does early beat waiting" claim -- and it does not need a
multiple-testing correction, because there is exactly one primary test, not
four. Where a confirming second-exchange source does appear (the same
signal `SourceLeadProgress.confirmed_within_hour` already surfaces from
`app.pump_event_sources`), the original paired early-vs-confirmed delta is
still computed and reported as a **secondary diagnostic** -- kept for
continuity with HYP-012's design, useful for telling "informational lead"
apart from "pumps go up regardless" -- but it never gates the verdict.

## What is frozen now vs. what waits

Colleague review, three rounds: freezing only the cohort boundary and
leaving exit-price mechanics, unresolved semantics, and the verdict rule to
be decided once real data exists reverses this codebase's own prospective-
research discipline -- whoever writes the evaluator later would be choosing
resolution mechanics with the outcome already in view. `resolve_episode`
and `formal_verdict` below are pure functions (no I/O, no DB, no network),
exercised by synthetic-input unit tests, precisely so the resolution
contract is frozen and testable *before* a single real qualified capture
exists. Only the DB-fetching, CLI, and Markdown-rendering plumbing around
these two functions waits for real data to shape it against -- there is
nothing prospective-research-sensitive about that plumbing itself.

Third round specifically closed: `resolve_episode` now derives the exit-bar
boundary and gap itself from `entry_at` (never trusts a pre-computed gap a
future caller could get wrong), reuses this codebase's shared
`calculate_performance` (packages/performance/schurfer_performance) for
fees/funding/validation instead of a partial hand-rolled fee calculation
(a 30-minute hold can, contrary to an earlier version of this docstring,
cross an 8h funding settlement if entry happens shortly before one --
`calculate_performance` prorates this correctly), and the primary
sensitivity's cluster-bootstrap method/seed/iterations/confidence level are
frozen via this codebase's shared `clustered_inference` module rather than
left for the eventual evaluator to pick after seeing the cohort.

See docs/research/source-lead-forward-cohort-v1.md for the full frozen
contract and the small-universe evidence-floor rationale.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from schurfer_performance import DEFAULT_COSTS, CostParameters, calculate_performance

from .clustered_inference import (
    CLUSTER_BOOTSTRAP_VERSION,
    DEFAULT_BOOTSTRAP_ITERATIONS,
    DEFAULT_BOOTSTRAP_SEED,
    DEFAULT_CONFIDENCE_LEVEL,
    ClusterObservation,
    cluster_bootstrap_mean,
)
from .ohlcv import ONE_MINUTE_MS, ceil_to_timeframe
from .source_lead_contract import IDENTITY_REGISTRY_V3_START

if TYPE_CHECKING:
    from datetime import datetime

    from .ohlcv import Candle

CONTRACT_VERSION = "source_lead_forward_cohort_v1"

# Frozen strictly at the qualification cutover: no v3-qualified capture can
# exist before this instant (qualify_source_lead's own early-exit check
# against IDENTITY_REGISTRY_V3_START), so there is no reason to start this
# cohort's clock any earlier -- and starting it later would mean discarding
# real qualified data while we waited. Aliased, not copied, so the two
# constants can never drift apart.
SOURCE_LEAD_FORWARD_COHORT_START: datetime = IDENTITY_REGISTRY_V3_START

# --- estimand -----------------------------------------------------------

# See the module docstring's "colleague review" section above for the full
# reasoning behind registering a narrower estimand than HYP-012's original
# 4-route paired family, rather than claiming to replicate it.
ESTIMAND_VERSION = "standalone_early_entry_net_return_v1"
HYPOTHESIS_ORIGIN = "HYP-012"  # discovery-ledger.md -- motivation, not a claimed replication
SECONDARY_DIAGNOSTIC_VERSION = "paired_early_minus_confirmed_entry_delta_v1"

# --- candidate set --------------------------------------------------------

# Every app.source_lead_qualifications row with this exact (status,
# qualification_version) pair whose capture's source_first_observed_at is
# at or after SOURCE_LEAD_FORWARD_COHORT_START. No manual asset selection --
# exactly whatever the already-live, identity- and route-verified capture
# pipeline produces going forward. Duplicated here as literals (not
# imported from source_lead_qualification.py) so a future bump of those
# live constants cannot silently widen this frozen cohort's candidate set
# without a deliberate, reviewed change to this file too.
QUALIFICATION_STATUS = "qualified"
QUALIFICATION_VERSION = "source_lead_qualified_capture_v3"

# --- entry -----------------------------------------------------------------

# Frozen at 0m, no artificial delay, no re-fetch. entry_price is the
# qualification result's own selected TargetObservation.liquidity["ask_vwap"]
# (buy side; this contract only tests longs) -- the exact executable VWAP
# qualify_source_lead itself already proved fillable against real
# order-book depth. entry_notional_usd is that same observation's own
# requested_notional_usd (the real captured value, currently
# SOURCE_LEAD_NOTIONAL_USD=$50 by env default) -- not a hardcoded constant
# this contract would otherwise have to keep in sync with that env var by
# hand.
ENTRY_TIME_SOURCE = "target_observation_observed_at"
ENTRY_PRICE_SOURCE = "target_observation_liquidity_ask_vwap"
ENTRY_NOTIONAL_SOURCE = "target_observation_requested_notional_usd"
ENTRY_DELAY_MINUTES = 0

# --- outcome / exit ----------------------------------------------------

OUTCOME_HORIZON_MINUTES = 30

# Colleague review: nothing in the live capture pipeline fetches a fresh,
# executable quote 30 minutes after entry -- TargetObservation is captured
# once, at qualification time. Building that (a new live capture worker,
# mirroring trade_exit_liquidity_observations) is a bigger lift than
# registering a cohort should require. OHLCV close is used instead,
# explicitly labeled as a proxy, never claimed to be an executable quote.
#
# EXIT_SLIPPAGE_BPS_ASSUMED is a pre-registered, deliberately conservative
# proxy assumption charged against that close -- not a guarantee that a
# real fill could never be worse (colleague review, third round: an
# earlier version of this comment overclaimed "never"). The eventual
# report must include a sensitivity read against this assumption (e.g. 0
# bps and 2x this value), not just the frozen primary number.
EXIT_PRICE_SOURCE_VERSION = "ohlcv_close_proxy_v1"
EXIT_BAR_TIMEFRAME_MS = ONE_MINUTE_MS
EXIT_SLIPPAGE_BPS_ASSUMED = 15.0
REQUIRE_EXIT_SLIPPAGE_SENSITIVITY = True

# Unresolved (not a synthetic worst-case fill) if the nearest usable bar is
# farther than this from the ideal exit boundary -- a real gap in captured
# OHLCV history, not something to paper over with a guess.
MAX_EXIT_BAR_GAP_MINUTES = 2.0

UNRESOLVED_REASONS = frozenset(
    {
        "missing_exit_bar",
        "exit_bar_before_boundary",
        "exit_bar_gap_exceeded",
        "invalid_market_data",
        "market_path_unavailable",
        "delisted_before_exit",
    }
)

# Episode definition: one app.source_lead_qualifications row with
# status='qualified' under this contract's QUALIFICATION_VERSION is exactly
# one episode. No additional cooldown/dedup logic -- unlike a continuous
# activity stream, each Gate pump event is already a discrete, upstream-
# deduplicated unit (app.pump_events), so capture_id already is the natural
# episode key.

# --- costs ---------------------------------------------------------------

# This codebase's shared conservative cost model and accounting function
# (packages/performance/schurfer_performance), not a bespoke one --
# calculate_performance validates entry/exit prices (finite, positive),
# charges both-side taker fees, and prorates funding_cost_bps_per_8h by
# duration_minutes/480. A 30-minute hold CAN cross an 8h funding
# settlement if entry happens shortly before one (colleague review, third
# round: an earlier version of this module claimed it never could) --
# calculate_performance's proration is this codebase's existing, already
# accepted answer to exactly that, not something resolve_episode needs to
# detect itself.
COSTS: CostParameters = DEFAULT_COSTS

# --- primary sensitivity: frozen bootstrap parameters -----------------

# This codebase's shared cluster bootstrap (clustered_inference.py), not a
# bespoke method -- colleague review, third round: leaving bootstrap
# method/seed/iterations/confidence level unfrozen would let the eventual
# evaluator choose them after already seeing the cohort. primary_sensitivity_ci
# below is the only entry point the eventual report may use to turn
# per-episode results into the CI formal_verdict gates on.
BOOTSTRAP_VERSION = CLUSTER_BOOTSTRAP_VERSION
BOOTSTRAP_ITERATIONS = DEFAULT_BOOTSTRAP_ITERATIONS
BOOTSTRAP_SEED = DEFAULT_BOOTSTRAP_SEED
CONFIDENCE_LEVEL = DEFAULT_CONFIDENCE_LEVEL

# --- evidence floor and concentration --------------------------------------

# Colleague review, second round: the entire identity- and route-verified
# universe is 14 canonical assets (source_lead_identity_registry_v3.json),
# so this codebase's usual 30-distinct-cluster floor is unreachable here by
# construction, forever -- registering it anyway would mean this cohort can
# never produce any verdict other than insufficient_data. This is
# registered honestly as a SMALL-UNIVERSE study instead: a lower, reachable
# cluster floor, but with explicit per-asset/per-week concentration caps
# doing the job the 30-cluster floor exists to do elsewhere (stopping one
# asset's idiosyncrasy from carrying the whole verdict) -- and a verdict
# reached under this floor does NOT by itself authorize paper or live
# execution (see SMALL_UNIVERSE_PROMOTION_NOTE).
EVIDENCE_FLOOR = {
    "min_resolved_episodes": 100,
    "min_distinct_asset_clusters": 7,
    "min_distinct_utc_weeks": 4,
}
MAX_SINGLE_ASSET_EPISODE_SHARE = 0.35
MAX_SINGLE_WEEK_EPISODE_SHARE = 0.45

SMALL_UNIVERSE_PROMOTION_NOTE = (
    "A 'candidate' verdict reached under this cohort's small-universe "
    "evidence floor (7 clusters, not this codebase's usual 30) authorizes "
    "only registering a broader confirmatory cohort once the identity "
    "registry covers more assets and/or exchanges -- it does not by "
    "itself authorize paper or live execution."
)

# Checkpoint / stopping rule, made unambiguous (colleague review, third
# round -- an earlier version named only the episode/week floors here while
# formal_verdict also gated on clusters/concentration, leaving it unclear
# whether the cohort could be re-peeked if those lagged behind): evaluate
# formal_verdict exactly ONCE, at the earliest point where
# min_resolved_episodes AND min_distinct_utc_weeks are BOTH met. If the
# cluster or concentration gates are not satisfied at that single
# checkpoint, the verdict is insufficient_data PERMANENTLY for this
# contract -- it is not re-run later on the same growing window (that would
# be exactly the incremental re-peeking this codebase's other registered
# contracts also forbid). The only sanctioned next step after an
# insufficient_data verdict here is registering a new, broader cohort (more
# weeks and/or a wider identity registry), not re-evaluating this one.
STOPPING_RULE = (
    "Evaluate exactly once, at the earliest point where "
    "min_resolved_episodes and min_distinct_utc_weeks are both met. A "
    "cluster/concentration failure at that single checkpoint is "
    "insufficient_data permanently for this contract, never re-peeked."
)

VERDICT_CANDIDATE = "candidate"
VERDICT_FAIL = "fail"
VERDICT_INSUFFICIENT_DATA = "insufficient_data"


# --- pure evaluator ----------------------------------------------------
#
# Deliberately pure (no I/O, no DB, no network) so the resolution contract
# itself is frozen and unit-testable with synthetic inputs before any real
# qualified capture exists -- see the module docstring's "what is frozen
# now" section. DB-fetching, CLI, and Markdown-rendering are separate,
# later plumbing around these functions.


@dataclass(frozen=True)
class EpisodeInputs:
    """Already-fetched inputs for one episode -- no fetching happens inside
    resolve_episode itself. entry_at/exit_bar are the raw, unvalidated
    values a future DB-fetch step would produce; resolve_episode computes
    and validates the exit boundary and gap itself (colleague review, third
    round: an earlier version accepted a pre-computed gap directly, which a
    buggy caller could get wrong -- e.g. a floor-aligned bar, the wrong
    venue/path, or a zero timestamp -- and still have it silently resolve)."""

    base: str
    entry_at: datetime
    entry_price: float
    entry_notional_usd: float
    exit_bar: Candle | None


@dataclass(frozen=True)
class EpisodeResult:
    base: str
    resolved: bool
    unresolved_reason: str | None
    net_return_pct: float | None


def expected_exit_boundary_ms(entry_at: datetime) -> int:
    """First fully-closed EXIT_BAR_TIMEFRAME_MS bar at or after
    entry_at + OUTCOME_HORIZON_MINUTES -- ceil, never floor, so the exit
    bar is never inspected before it closes. Ordering (exit strictly after
    entry) follows automatically: the target is always
    OUTCOME_HORIZON_MINUTES in the future before it is even rounded
    forward.

    Public (no leading underscore) so the DB-fetch plumbing this module's
    own docstring defers to later (research/source-lead-forward-cohort-
    plumbing-v1) can pick the exact same exit candle `resolve_episode`
    itself will validate, via this one function, instead of reimplementing
    the ceil formula a second time and risking the two drifting apart --
    same reasoning as this codebase's own `instruments.onboarded_at_ms`
    precedent."""
    entry_ms = int(entry_at.timestamp() * 1000)
    target_ms = entry_ms + OUTCOME_HORIZON_MINUTES * 60_000
    return ceil_to_timeframe(target_ms, EXIT_BAR_TIMEFRAME_MS)


def resolve_episode(inputs: EpisodeInputs, *, costs: CostParameters = COSTS) -> EpisodeResult:
    """One episode's resolved net return, or its unresolved reason. Pure --
    see the section header above. Computes and validates the exit-bar
    boundary/gap itself from entry_at rather than trusting a pre-computed
    value, and delegates price validation/fees/funding to this codebase's
    shared calculate_performance rather than a partial hand-rolled
    calculation."""
    if inputs.exit_bar is None:
        return EpisodeResult(inputs.base, False, "missing_exit_bar", None)

    expected_boundary_ms = expected_exit_boundary_ms(inputs.entry_at)
    if inputs.exit_bar.ts_ms < expected_boundary_ms:
        # Never accept a bar that closes before the frozen ceil boundary --
        # would mean inspecting the outcome before it is fully known yet.
        return EpisodeResult(inputs.base, False, "exit_bar_before_boundary", None)
    gap_minutes = (inputs.exit_bar.ts_ms - expected_boundary_ms) / 60_000
    if gap_minutes > MAX_EXIT_BAR_GAP_MINUTES:
        return EpisodeResult(inputs.base, False, "exit_bar_gap_exceeded", None)

    try:
        result = calculate_performance(
            position_usd=inputs.entry_notional_usd,
            entry_price=inputs.entry_price,
            exit_price=inputs.exit_bar.close,
            side="long",
            duration_minutes=OUTCOME_HORIZON_MINUTES,
            entry_slippage_bps=0.0,  # ask_vwap already reflects real captured impact
            exit_slippage_bps=EXIT_SLIPPAGE_BPS_ASSUMED,
            costs=costs,
        )
    except ValueError:
        # calculate_performance fails closed on non-finite/non-positive
        # prices or notional -- a real market-data problem, not a
        # programming error this function should crash on (colleague
        # review, third round).
        return EpisodeResult(inputs.base, False, "invalid_market_data", None)

    assert result.net_return_pct is not None  # entry/exit slippage are both always provided above
    return EpisodeResult(inputs.base, True, None, round(result.net_return_pct, 6))


def primary_sensitivity_ci(observations: tuple[ClusterObservation, ...]) -> float:
    """The only entry point the eventual report may use to turn per-episode
    net returns into the lower CI bound formal_verdict gates on -- frozen
    bootstrap method/seed/iterations/confidence level, this codebase's
    shared clustered_inference module, not a choice left for later."""
    computation = cluster_bootstrap_mean(
        observations,
        iterations=BOOTSTRAP_ITERATIONS,
        seed=BOOTSTRAP_SEED,
        confidence_level=CONFIDENCE_LEVEL,
    )
    return computation.estimate.lower_bound


def formal_verdict(
    *,
    resolved_episodes: int,
    distinct_asset_clusters: int,
    distinct_utc_weeks: int,
    max_single_asset_share: float,
    max_single_week_share: float,
    ci_lower_bound_pct: float | None,
) -> str:
    """Frozen verdict rule over already-aggregated statistics -- aggregation
    itself belongs to the eventual report, not here. Pure -- see the
    section header above. Called exactly once, per STOPPING_RULE; a
    cluster/concentration failure here is final for this contract, not a
    reason to call this again later on the same growing window."""
    floor_met = (
        resolved_episodes >= EVIDENCE_FLOOR["min_resolved_episodes"]
        and distinct_asset_clusters >= EVIDENCE_FLOOR["min_distinct_asset_clusters"]
        and distinct_utc_weeks >= EVIDENCE_FLOOR["min_distinct_utc_weeks"]
    )
    if not floor_met:
        return VERDICT_INSUFFICIENT_DATA
    if max_single_asset_share > MAX_SINGLE_ASSET_EPISODE_SHARE:
        return VERDICT_INSUFFICIENT_DATA
    if max_single_week_share > MAX_SINGLE_WEEK_EPISODE_SHARE:
        return VERDICT_INSUFFICIENT_DATA
    if ci_lower_bound_pct is None or not math.isfinite(ci_lower_bound_pct):
        return VERDICT_INSUFFICIENT_DATA
    return VERDICT_CANDIDATE if ci_lower_bound_pct > 0 else VERDICT_FAIL


__all__ = [
    "BOOTSTRAP_ITERATIONS",
    "BOOTSTRAP_SEED",
    "BOOTSTRAP_VERSION",
    "CONFIDENCE_LEVEL",
    "CONTRACT_VERSION",
    "COSTS",
    "ENTRY_DELAY_MINUTES",
    "ENTRY_NOTIONAL_SOURCE",
    "ENTRY_PRICE_SOURCE",
    "ENTRY_TIME_SOURCE",
    "ESTIMAND_VERSION",
    "EVIDENCE_FLOOR",
    "EXIT_BAR_TIMEFRAME_MS",
    "EXIT_PRICE_SOURCE_VERSION",
    "EXIT_SLIPPAGE_BPS_ASSUMED",
    "HYPOTHESIS_ORIGIN",
    "MAX_EXIT_BAR_GAP_MINUTES",
    "MAX_SINGLE_ASSET_EPISODE_SHARE",
    "MAX_SINGLE_WEEK_EPISODE_SHARE",
    "OUTCOME_HORIZON_MINUTES",
    "QUALIFICATION_STATUS",
    "QUALIFICATION_VERSION",
    "REQUIRE_EXIT_SLIPPAGE_SENSITIVITY",
    "SECONDARY_DIAGNOSTIC_VERSION",
    "SMALL_UNIVERSE_PROMOTION_NOTE",
    "SOURCE_LEAD_FORWARD_COHORT_START",
    "STOPPING_RULE",
    "UNRESOLVED_REASONS",
    "VERDICT_CANDIDATE",
    "VERDICT_FAIL",
    "VERDICT_INSUFFICIENT_DATA",
    "EpisodeInputs",
    "EpisodeResult",
    "expected_exit_boundary_ms",
    "formal_verdict",
    "primary_sensitivity_ci",
    "resolve_episode",
]
