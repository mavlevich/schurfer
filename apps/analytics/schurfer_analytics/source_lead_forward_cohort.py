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
(`ESTIMAND` below): standalone after-cost net return on the qualified,
verified gate -> binance route alone. That is honestly a *different*,
arguably more directly money-relevant question than HYP-012's original
"does early beat waiting" claim -- and it does not need a multiple-testing
correction, because there is exactly one primary test, not four. Where a
confirming second-exchange source does appear (the same signal
`SourceLeadProgress.confirmed_within_hour` already surfaces from
`app.pump_event_sources`), the original paired early-vs-confirmed delta is
still computed and reported as a **secondary diagnostic** -- kept for
continuity with HYP-012's design, useful for telling "informational lead"
apart from "pumps go up regardless" -- but it never gates the verdict.

## What is frozen now vs. what waits

Colleague review, second round: freezing only the cohort boundary and
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

See docs/research/source-lead-forward-cohort-v1.md for the full frozen
contract and the small-universe evidence-floor rationale.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from schurfer_performance import DEFAULT_COSTS, CostParameters

from .ohlcv import ONE_MINUTE_MS
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

# Frozen at 0m, no artificial delay, no re-fetch. Uses the qualification
# result's own selected TargetObservation -- entry_price is that
# observation's liquidity["ask_vwap"] (buy side; this contract only tests
# longs), the exact executable VWAP qualify_source_lead itself already
# proved was fillable against real order-book depth, on the fixed
# SOURCE_LEAD_NOTIONAL_USD ($50) quote. Not an assumption -- the same fill
# qualify_source_lead used to select this route.
ENTRY_TIME_SOURCE = "target_observation_observed_at"
ENTRY_PRICE_SOURCE = "target_observation_liquidity_ask_vwap"
ENTRY_DELAY_MINUTES = 0

# --- outcome / exit ----------------------------------------------------

OUTCOME_HORIZON_MINUTES = 30

# Colleague review: nothing in the live capture pipeline fetches a fresh,
# executable quote 30 minutes after entry -- TargetObservation is captured
# once, at qualification time. Building that (a new live capture worker,
# mirroring trade_exit_liquidity_observations) is a bigger lift than
# registering a cohort should require. OHLCV close is used instead,
# explicitly labeled as a proxy, never claimed to be an executable quote --
# and EXIT_SLIPPAGE_BPS_ASSUMED is charged against it specifically so an
# unrealistically clean proxy fill can never manufacture an edge that would
# not survive a real one.
EXIT_PRICE_SOURCE_VERSION = "ohlcv_close_proxy_v1"
EXIT_BAR_TIMEFRAME_MS = ONE_MINUTE_MS
# First fully-closed 1m bar at or after (entry_at + OUTCOME_HORIZON_MINUTES)
# -- ceil, never floor, so the exit bar is never inspected before it closes.
EXIT_BAR_ALIGNMENT = "ceil"
# Unresolved (not a synthetic worst-case fill) if the nearest usable bar is
# farther than this from the ideal exit boundary -- a real gap in captured
# OHLCV history, not something to paper over with a guess.
MAX_EXIT_BAR_GAP_MINUTES = 2.0
# Fixed, deliberately conservative haircut against the OHLCV close proxy.
# Chosen to exceed typical entry-side ask_impact_bps already observed on
# this notional in real TargetObservation captures, specifically so the
# proxy is never more favorable than a real fill would be -- biases toward
# fail, never toward candidate.
EXIT_SLIPPAGE_BPS_ASSUMED = 15.0

UNRESOLVED_REASONS = frozenset(
    {
        "missing_exit_bar",
        "exit_bar_gap_exceeded",
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

# This codebase's shared conservative cost model
# (packages/performance/schurfer_performance), not a bespoke one. Entry
# slippage/impact is not modeled separately -- it is the real, order-book-
# derived ask_vwap TargetObservation captured. A 30-minute hold never
# crosses an 8h funding settlement, so funding_cost_bps_per_8h is carried
# for completeness but never actually charged by resolve_episode below.
COSTS: CostParameters = DEFAULT_COSTS

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

# Checkpoint / stopping rule: evaluate once, at the earliest point where
# BOTH min_resolved_episodes and min_distinct_utc_weeks are met (the same
# "earliest maturity prefix" convention this codebase's other registered
# contracts use, e.g. liquid_taker_report.py) -- never re-peeked at
# incrementally. An early look that fails the floor is insufficient_data,
# not a result, and is not logged as one.

VERDICT_CANDIDATE = "candidate"
VERDICT_FAIL = "fail"
VERDICT_INSUFFICIENT_DATA = "insufficient_data"


# --- pure evaluator ----------------------------------------------------
#
# Deliberately pure (no I/O, no DB, no network) so the resolution contract
# itself is frozen and unit-testable with synthetic inputs before any real
# qualified capture exists -- see the module docstring's "what is frozen
# now" section. DB-fetching, CLI, and Markdown-rendering are separate,
# later plumbing around these two functions.


@dataclass(frozen=True)
class EpisodeInputs:
    """Already-fetched inputs for one episode -- no fetching happens inside
    resolve_episode itself."""

    base: str
    entry_price: float
    exit_bar: Candle | None
    exit_bar_gap_minutes: float | None


@dataclass(frozen=True)
class EpisodeResult:
    base: str
    resolved: bool
    unresolved_reason: str | None
    net_return_pct: float | None


def resolve_episode(inputs: EpisodeInputs, *, costs: CostParameters = COSTS) -> EpisodeResult:
    """One episode's resolved net return, or its unresolved reason. Pure --
    see the section header above."""
    if inputs.exit_bar is None:
        return EpisodeResult(inputs.base, False, "missing_exit_bar", None)
    gap = inputs.exit_bar_gap_minutes
    if gap is None or gap > MAX_EXIT_BAR_GAP_MINUTES:
        return EpisodeResult(inputs.base, False, "exit_bar_gap_exceeded", None)
    exit_price = inputs.exit_bar.close * (1 - EXIT_SLIPPAGE_BPS_ASSUMED / 10_000)
    gross_return_pct = (exit_price - inputs.entry_price) / inputs.entry_price * 100
    # Entry + exit taker fee, both sides at the shared conservative rate.
    # No funding: a 30-minute hold never crosses an 8h settlement.
    fee_cost_pct = costs.taker_fee_bps_per_side * 2 / 100
    return EpisodeResult(inputs.base, True, None, round(gross_return_pct - fee_cost_pct, 6))


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
    section header above."""
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
    if ci_lower_bound_pct is None:
        return VERDICT_INSUFFICIENT_DATA
    return VERDICT_CANDIDATE if ci_lower_bound_pct > 0 else VERDICT_FAIL


__all__ = [
    "CONTRACT_VERSION",
    "COSTS",
    "ENTRY_DELAY_MINUTES",
    "ENTRY_PRICE_SOURCE",
    "ENTRY_TIME_SOURCE",
    "ESTIMAND_VERSION",
    "EVIDENCE_FLOOR",
    "EXIT_BAR_ALIGNMENT",
    "EXIT_BAR_TIMEFRAME_MS",
    "EXIT_PRICE_SOURCE_VERSION",
    "EXIT_SLIPPAGE_BPS_ASSUMED",
    "HYPOTHESIS_ORIGIN",
    "MAX_EXIT_BAR_GAP_MINUTES",
    "MAX_SINGLE_ASSET_EPISODE_SHARE",
    "MAX_SINGLE_WEEK_EPISODE_SHARE",
    "QUALIFICATION_STATUS",
    "QUALIFICATION_VERSION",
    "SECONDARY_DIAGNOSTIC_VERSION",
    "SMALL_UNIVERSE_PROMOTION_NOTE",
    "SOURCE_LEAD_FORWARD_COHORT_START",
    "UNRESOLVED_REASONS",
    "VERDICT_CANDIDATE",
    "VERDICT_FAIL",
    "VERDICT_INSUFFICIENT_DATA",
    "EpisodeInputs",
    "EpisodeResult",
    "formal_verdict",
    "resolve_episode",
]
