"""Post-hoc oracle upper bound: liquidation-cascade maker reversion
(research/liquidation-maker-upper-bound-v1). ROADMAP.md "Near-term
interleaving from 2026-08-31", item 5.

Tests maker-style reversion against the accumulated liquidation-event
history already live in production (`timeseries.liquidation_events`,
Binance + Bybit, `schurfer-liquidation-capture-binance`/`-bybit`) -- not
the 2 live-paper trades this idea originally started from. Independent
episodes, exact venue, a limit level fixed in advance, price merely
touching that level counted only as an optimistic potential fill, never
an actual one, costs, MFE/MAE, adverse selection. A negative result closes
the direction before any L2/shadow-capture infrastructure gets built for
it.

## Frozen parameters (user-specified, not invented -- 2026-09-03)

**Cascade definition.** One episode is a period where a rolling
`CASCADE_TRIGGER_WINDOW_MINUTES`-minute trailing sum of
`estimated_liquidation_notional` for one (exchange, native_market_id,
position_side) crosses `PRIMARY_CASCADE_NOTIONAL_USD`. Overlapping/nearby
trigger minutes are NOT independent observations -- they merge into one
episode, and a new episode is only allowed to start once
`CASCADE_COOLDOWN_MINUTES` has passed with no new qualifying trigger
minute for that same (exchange, native_market_id, position_side).
`SENSITIVITY_CASCADE_NOTIONAL_USD_FAMILY` (100k/250k/500k) is
pre-registered as descriptive context only -- the primary verdict gates on
250k alone; picking whichever threshold looks best after seeing results
would be exactly the multiple-comparisons problem a pre-registered family
exists to prevent.

**Directions are never merged.** `position_side='long'` liquidations
(forced sells, price pushed down) and `position_side='short'` liquidations
(forced buys, price pushed up) are two separate populations, each with its
own episode set, its own evidence floor, and its own verdict -- never
pooled into one sample.

**Entry is a post-hoc optimistic upper bound, not an executable order.**
`entry_price` is the most extreme price actually touched during the
episode itself (the lowest low for a `long`-liquidation episode, the
highest high for a `short`-liquidation episode) -- i.e. exactly the price
a resting limit order AT that level would have needed to be filled at.
This is NOT modeled as "an order placed in advance at that level": the
final extremum is only knowable after the episode has already finished,
so describing this as a real, executable order would be look-ahead bias.
The honest framing is a post-hoc question: "if a passive order had
somehow always been resting exactly at the best price this episode ever
touched, AND it had been filled there, what would the economics have
been?" -- an upper bound on what any real resting-order strategy could
ever achieve, not a claim that this specific strategy is executable.
Touching a price is also not proof of a maker fill on its own: queue
position, available depth at that price, and this strategy's own place in
the book are all unknown and unmodeled. Consequently:

- A NEGATIVE net return under this optimistic upper bound rejects THIS
  specific candidate -- extremum-entry plus a FIXED
  `MAX_POSITION_HOLD_MINUTES`-minute taker-style exit -- not the entire
  maker-reversion idea space. Colleague review, 2026-09-03: an earlier
  version of this docstring said "no real order could plausibly beat an
  upper bound that already loses money", which overclaims -- a causal
  variant with an earlier or dynamic (not fixed-60-minute) exit rule is a
  DIFFERENT candidate this result says nothing about, since it changes the
  very quantity being bounded. What a negative result here does
  conclusively close off is this exact candidate: no real resting order,
  filled at this episode's own best-possible price and held for exactly
  this long before a taker exit, could have done better than what this
  study already measured losing money on.
- A POSITIVE net return is not itself authorization for paper or live
  trading -- it only justifies building the next, causal step: a BBO/L2
  shadow-capture test that can actually observe queue position and fill
  probability, per this item's own ROADMAP text ("only if 5 is positive:
  bounded shadow capture").

**Two separate timers, never conflated.** `order_expiry` (how long an
unfilled resting limit order could realistically wait before being
cancelled) is explicitly NOT modeled here -- this study assumes the fill
already happened at the exact extremum, it does not model waiting for
one. `MAX_POSITION_HOLD_MINUTES` (60, frozen) is the ONLY timer this
module uses: how long the resulting position is held after that assumed
fill, before a taker-style close-out. A future causal/shadow test that
needs to model order waiting requires its own separately frozen
`order_expiry`, not a reuse of this constant.

**Costs.** Maker fee/rebate on entry (`MAKER_ENTRY_FEE_BPS`, 0.0 -- no
rebate assumed, mirrors `maker_entry.py`'s own convention), taker fee
(`schurfer_performance.DEFAULT_COSTS.taker_fee_bps_per_side`) and a
pre-registered conservative slippage assumption
(`EXIT_SLIPPAGE_BPS_ASSUMED`) on the taker-style exit, funding prorated by
actual hold duration via the shared `calculate_performance`. Exact-venue
OHLCV only -- never a different exchange's prices substituted in.

**Evidence floor.** This codebase's usual 100 resolved episodes / 30
distinct clusters / 4 distinct UTC weeks, with the usual 35%/45%
per-cluster/per-week concentration caps, applied per (direction, exchange,
`coverage_kind`) scope -- not merely per direction. Colleague review,
2026-09-03: `timeseries.liquidation_events` captures Binance and Bybit
under genuinely different `coverage_kind` semantics (Bybit:
`complete_stream`, a real full event stream; Binance:
`latest_per_symbol_1000ms`, a lossy periodic sample -- see migration
0022's own CHECK constraint and the two exchanges' `source_contract_
variant` values). Blending both into one denominator/CI would understate
or otherwise distort which minutes cross the cascade threshold in a way
that depends on which feed happened to capture more of the real event
volume, not on the underlying market. Splitting the verdict per (exchange,
coverage_kind) -- currently a 1:1 mapping in practice, so per exchange --
closes that; the honest, disclosed cost is a smaller population per scope,
making the floor genuinely harder to reach than a single pooled
Binance+Bybit population would have been. This is accepted rather than
lowering the floor to compensate: a real per-scope insufficient_data
verdict is more honest than a pooled candidate/reject verdict resting on
two incompatible measurement processes.

**Cluster identity is instrument-level, not verified canonical asset
identity.** The cluster key `formal_verdict`'s `distinct_asset_clusters`
counts is `native_market_id` -- a native contract identifier on ONE
exchange (already scoped to a single exchange by the per-(direction,
exchange, coverage_kind) split above), not a cross-venue canonical asset
identity resolved against an identity registry the way e.g.
`source_lead_forward_cohort.py`'s own `canonical_asset_id` is. Colleague
review, 2026-09-03: calling this an "asset cluster" without that
qualification risks implying a stronger identity guarantee than what is
actually verified here -- there is currently no canonical-identity
registry covering arbitrary liquidated instruments the way the source-lead
registry covers its own curated 14 assets. Scoping per exchange already
removes the most severe risk (a ticker string colliding ACROSS venues);
what remains is a labeling accuracy concern, not a data-mixing one -- treat
`distinct_asset_clusters` here as "distinct native contract IDs on this
scope's one exchange", not a claim of verified cross-venue asset identity.
A real canonical-identity layer would be required before this module could
support any genuine cross-exchange combined verdict, which it does not
attempt.

**Primary metrics.** Resolved/unresolved episode counts, median and mean
net return, profit factor, win rate, MFE, MAE, and drawdown -- broken down
by side, asset, and week. Drawdown here is the chronological ADDITIVE
peak-to-trough decline ACROSS a scope's own ordered sequence of episode
outcomes (`max_sequential_drawdown_pct`, mirrors `virtual_strategy.py`'s
own `max_sequential_drawdown_usd`), a distinct metric from MFE/MAE (the
best/worst excursion WITHIN one episode's own hold window).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from .ohlcv import ONE_MINUTE_MS, ceil_to_timeframe, covers_window_without_gaps

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .ohlcv import Candle

CONTRACT_VERSION = "liquidation_maker_upper_bound_v1"
INTERPRETATION = "post_hoc_oracle_upper_bound_discovery_only_no_trading_authorization"

# Frozen bootstrap parameters -- this codebase's shared cluster bootstrap
# (clustered_inference.py), not a bespoke method: leaving method/seed/
# iterations/confidence level unfrozen would let the eventual report
# choose them after already seeing the data, exactly the failure mode
# this codebase's other frozen contracts (e.g.
# source_lead_forward_cohort.py) already guard against.
BOOTSTRAP_VERSION = CLUSTER_BOOTSTRAP_VERSION
BOOTSTRAP_ITERATIONS = DEFAULT_BOOTSTRAP_ITERATIONS
BOOTSTRAP_SEED = DEFAULT_BOOTSTRAP_SEED
CONFIDENCE_LEVEL = DEFAULT_CONFIDENCE_LEVEL

# --- cascade definition ---------------------------------------------------

CASCADE_TRIGGER_WINDOW_MINUTES = 5
PRIMARY_CASCADE_NOTIONAL_USD = 250_000.0
# Descriptive context only -- see module docstring. Never used to pick a
# "best" threshold after seeing results; the primary verdict gates on
# PRIMARY_CASCADE_NOTIONAL_USD alone.
SENSITIVITY_CASCADE_NOTIONAL_USD_FAMILY = (100_000.0, 250_000.0, 500_000.0)
CASCADE_COOLDOWN_MINUTES = 60

DIRECTIONS = ("long", "short")  # position_side of the LIQUIDATED positions

# --- entry / exit model ---------------------------------------------------

ENTRY_MODEL_VERSION = "post_hoc_optimistic_potential_fill_at_cascade_extremum_v1"
MAKER_ENTRY_FEE_BPS = 0.0
MAX_POSITION_HOLD_MINUTES = 60
EXIT_BAR_TIMEFRAME_MS = ONE_MINUTE_MS
EXIT_PRICE_SOURCE_VERSION = "ohlcv_close_proxy_v1"
EXIT_SLIPPAGE_BPS_ASSUMED = 15.0
REQUIRE_EXIT_SLIPPAGE_SENSITIVITY = True

# The sensitivity family REQUIRE_EXIT_SLIPPAGE_SENSITIVITY commits this
# contract to: 0bps (no slippage at all), the frozen primary assumption
# itself, and 2x that -- pre-registered here, not chosen by the eventual
# report after seeing which one looks best (colleague review, 2026-09-03:
# this frozen constant existed but nothing computed the family it names).
EXIT_SLIPPAGE_SENSITIVITY_BPS = (0.0, EXIT_SLIPPAGE_BPS_ASSUMED, 2 * EXIT_SLIPPAGE_BPS_ASSUMED)
MAX_EXIT_BAR_GAP_MINUTES = 2.0

# A nominal position size; only the resulting *percentage* return is ever
# read out of calculate_performance, so any fixed positive notional gives
# an identical net_return_pct -- this exists only because
# calculate_performance's own contract requires a positive position_usd.
NOMINAL_POSITION_USD = 1_000.0

COSTS: CostParameters = DEFAULT_COSTS

UNRESOLVED_REASONS = frozenset(
    {
        "missing_trigger_window_candles",
        "missing_exit_bar",
        "exit_bar_before_boundary",
        "exit_bar_gap_exceeded",
        "hold_window_has_gaps",
        "invalid_market_data",
    }
)

# --- evidence floor and concentration --------------------------------------

EVIDENCE_FLOOR = {
    "min_resolved_episodes": 100,
    "min_distinct_asset_clusters": 30,
    "min_distinct_utc_weeks": 4,
}
MAX_SINGLE_ASSET_EPISODE_SHARE = 0.35
MAX_SINGLE_WEEK_EPISODE_SHARE = 0.45

VERDICT_REJECT = "reject"
VERDICT_POSITIVE_WARRANTS_SHADOW_TEST = "positive_warrants_shadow_test"
VERDICT_INSUFFICIENT_DATA = "insufficient_data"


# --- pure episode detection -------------------------------------------------


@dataclass(frozen=True)
class LiquidationTriggerMinute:
    """One minute where the trailing CASCADE_TRIGGER_WINDOW_MINUTES-minute
    notional sum for one (exchange, native_market_id, position_side)
    crossed a cascade threshold. Aggregation itself (raw events ->
    per-minute trailing sums) happens in the repository/report layer,
    which has the full raw event stream; this type is this pure module's
    own input contract for declustering."""

    exchange: str
    native_market_id: str
    position_side: str
    bucket_start: datetime


@dataclass(frozen=True)
class CascadeEpisode:
    episode_id: int
    exchange: str
    native_market_id: str
    position_side: str
    first_trigger_at: datetime
    last_trigger_at: datetime


def decluster_cascade_episodes(
    trigger_minutes: tuple[LiquidationTriggerMinute, ...],
    *,
    cooldown_minutes: int = CASCADE_COOLDOWN_MINUTES,
    start_id: int = 1,
) -> tuple[CascadeEpisode, ...]:
    """Group consecutive-in-time trigger minutes for the same (exchange,
    native_market_id, position_side) into one episode, keyed by the FIRST
    such minute. A new episode starts once `cooldown_minutes` has passed
    since the LAST trigger minute for that same group -- mirrors
    `momentum_flow_bidirectional_burst_study.decluster_episodes`'s own
    refractory-window declustering (same shape of problem: a run of
    consecutive extreme minutes is one event, not one-per-minute), adapted
    to this module's own (exchange, native_market_id, position_side)
    grouping key rather than that module's (exchange, symbol). The
    boundary is inclusive (`gap >= cooldown_minutes` starts a new episode,
    not `>`) -- colleague review, 2026-09-03: an earlier version used `>`,
    silently merging two trigger minutes exactly `cooldown_minutes` apart
    into one episode, inconsistent with the `decluster_episodes` precedent
    this docstring already claims to mirror (that function's own refractory
    check is `>=`)."""
    if cooldown_minutes <= 0:
        raise ValueError("cooldown_minutes must be positive")
    if start_id <= 0:
        raise ValueError("start_id must be positive")

    by_group: dict[tuple[str, str, str], list[LiquidationTriggerMinute]] = defaultdict(list)
    for minute in trigger_minutes:
        by_group[(minute.exchange, minute.native_market_id, minute.position_side)].append(minute)

    cooldown = timedelta(minutes=cooldown_minutes)
    episodes: list[CascadeEpisode] = []
    next_id = start_id
    for (exchange, native_market_id, position_side), group_minutes in by_group.items():
        ordered = sorted(group_minutes, key=lambda item: item.bucket_start)
        episode_start: datetime | None = None
        episode_last: datetime | None = None
        for minute in ordered:
            if episode_last is not None and minute.bucket_start - episode_last >= cooldown:
                assert episode_start is not None
                episodes.append(
                    CascadeEpisode(
                        episode_id=next_id,
                        exchange=exchange,
                        native_market_id=native_market_id,
                        position_side=position_side,
                        first_trigger_at=episode_start,
                        last_trigger_at=episode_last,
                    )
                )
                next_id += 1
                episode_start = None
            if episode_start is None:
                episode_start = minute.bucket_start
            episode_last = minute.bucket_start
        if episode_start is not None and episode_last is not None:
            episodes.append(
                CascadeEpisode(
                    episode_id=next_id,
                    exchange=exchange,
                    native_market_id=native_market_id,
                    position_side=position_side,
                    first_trigger_at=episode_start,
                    last_trigger_at=episode_last,
                )
            )
            next_id += 1

    return tuple(sorted(episodes, key=lambda item: (item.first_trigger_at, item.episode_id)))


# --- pure evaluator ----------------------------------------------------
#
# Deliberately pure (no I/O, no DB, no network) so the resolution contract
# itself is frozen and unit-testable with synthetic inputs, matching this
# codebase's other frozen-contract studies.


@dataclass(frozen=True)
class EpisodeInputs:
    """`candles` must span from `episode.first_trigger_at` through at least
    `episode.last_trigger_at` + MAX_POSITION_HOLD_MINUTES +
    MAX_EXIT_BAR_GAP_MINUTES (the repository/report layer's job to fetch;
    resolve_episode only reads what it needs from within that range).
    Native, exact-venue OHLCV -- never a different exchange's candles."""

    episode: CascadeEpisode
    candles: tuple[Candle, ...]


@dataclass(frozen=True)
class EpisodeResult:
    episode_id: int
    native_market_id: str
    position_side: str
    direction: str | None  # "buy" (long-liquidation reversion) or "sell" (short-liquidation)
    resolved: bool
    unresolved_reason: str | None
    entry_at: datetime | None
    entry_price: float | None
    net_return_pct: float | None
    mfe_pct: float | None
    mae_pct: float | None


def _direction_for(position_side: str) -> str:
    # Long liquidations are forced SELLS (price pushed down) -- the
    # reversion bet is a BUY. Short liquidations are forced BUYS (price
    # pushed up) -- the reversion bet is a SELL.
    return "buy" if position_side == "long" else "sell"


def _expected_exit_boundary_ms(entry_at_ms: int) -> int:
    target_ms = entry_at_ms + MAX_POSITION_HOLD_MINUTES * 60_000
    return ceil_to_timeframe(target_ms, EXIT_BAR_TIMEFRAME_MS)


def resolve_episode(
    inputs: EpisodeInputs,
    *,
    costs: CostParameters = COSTS,
    exit_slippage_bps: float = EXIT_SLIPPAGE_BPS_ASSUMED,
) -> EpisodeResult:
    episode = inputs.episode
    direction = _direction_for(episode.position_side)

    trigger_start_ms = int(episode.first_trigger_at.timestamp() * 1000)
    trigger_end_ms = int(episode.last_trigger_at.timestamp() * 1000)
    trigger_candles = [
        candle for candle in inputs.candles if trigger_start_ms <= candle.ts_ms <= trigger_end_ms
    ]
    if not trigger_candles:
        return EpisodeResult(
            episode.episode_id,
            episode.native_market_id,
            episode.position_side,
            direction,
            False,
            "missing_trigger_window_candles",
            None,
            None,
            None,
            None,
            None,
        )

    # Post-hoc optimistic extremum: the lowest low (long-liquidation /
    # buy reversion) or the highest high (short-liquidation / sell
    # reversion) actually touched during the episode. See module
    # docstring's "Entry is a post-hoc optimistic upper bound" section.
    if direction == "buy":
        extremum_candle = min(trigger_candles, key=lambda candle: candle.low)
        entry_price = extremum_candle.low
    else:
        extremum_candle = max(trigger_candles, key=lambda candle: candle.high)
        entry_price = extremum_candle.high
    entry_at_ms = extremum_candle.ts_ms

    if not (entry_price > 0):
        return EpisodeResult(
            episode.episode_id,
            episode.native_market_id,
            episode.position_side,
            direction,
            False,
            "invalid_market_data",
            None,
            None,
            None,
            None,
            None,
        )

    boundary_ms = _expected_exit_boundary_ms(entry_at_ms)

    exit_bar = None
    gap_minutes: float | None = None
    for candle in inputs.candles:
        if candle.ts_ms < boundary_ms:
            continue
        gap = (candle.ts_ms - boundary_ms) / 60_000
        if exit_bar is None or candle.ts_ms < exit_bar.ts_ms:
            exit_bar = candle
            gap_minutes = gap
    if exit_bar is None:
        return EpisodeResult(
            episode.episode_id,
            episode.native_market_id,
            episode.position_side,
            direction,
            False,
            "missing_exit_bar",
            None,
            None,
            None,
            None,
            None,
        )
    assert gap_minutes is not None
    if gap_minutes > MAX_EXIT_BAR_GAP_MINUTES:
        return EpisodeResult(
            episode.episode_id,
            episode.native_market_id,
            episode.position_side,
            direction,
            False,
            "exit_bar_gap_exceeded",
            None,
            None,
            None,
            None,
            None,
        )

    hold_candles = sorted(
        (candle for candle in inputs.candles if entry_at_ms <= candle.ts_ms <= exit_bar.ts_ms),
        key=lambda candle: candle.ts_ms,
    )
    if not covers_window_without_gaps(
        hold_candles, entry_at_ms, exit_bar.ts_ms + EXIT_BAR_TIMEFRAME_MS, EXIT_BAR_TIMEFRAME_MS
    ):
        return EpisodeResult(
            episode.episode_id,
            episode.native_market_id,
            episode.position_side,
            direction,
            False,
            "hold_window_has_gaps",
            None,
            None,
            None,
            None,
            None,
        )

    duration_minutes = (exit_bar.ts_ms - entry_at_ms) / 60_000
    try:
        result = calculate_performance(
            position_usd=NOMINAL_POSITION_USD,
            entry_price=entry_price,
            exit_price=exit_bar.close,
            side="long" if direction == "buy" else "short",
            duration_minutes=duration_minutes,
            entry_slippage_bps=0.0,  # the assumed fill IS the extremum -- no slippage by definition
            exit_slippage_bps=exit_slippage_bps,
            entry_fee_bps=MAKER_ENTRY_FEE_BPS,
            costs=costs,
        )
    except ValueError:
        return EpisodeResult(
            episode.episode_id,
            episode.native_market_id,
            episode.position_side,
            direction,
            False,
            "invalid_market_data",
            None,
            None,
            None,
            None,
            None,
        )
    if result.net_return_pct is None:
        return EpisodeResult(
            episode.episode_id,
            episode.native_market_id,
            episode.position_side,
            direction,
            False,
            "invalid_market_data",
            None,
            None,
            None,
            None,
            None,
        )

    if direction == "buy":
        mfe_pct = max((candle.high - entry_price) / entry_price * 100 for candle in hold_candles)
        mae_pct = min((candle.low - entry_price) / entry_price * 100 for candle in hold_candles)
    else:
        mfe_pct = max((entry_price - candle.low) / entry_price * 100 for candle in hold_candles)
        mae_pct = min((entry_price - candle.high) / entry_price * 100 for candle in hold_candles)

    return EpisodeResult(
        episode.episode_id,
        episode.native_market_id,
        episode.position_side,
        direction,
        True,
        None,
        _candle_datetime(extremum_candle),
        round(entry_price, 10),
        round(result.net_return_pct, 6),
        round(mfe_pct, 6),
        round(mae_pct, 6),
    )


def _candle_datetime(candle: Candle) -> datetime:
    return datetime.fromtimestamp(candle.ts_ms / 1000, tz=UTC)


def primary_sensitivity_ci(observations: tuple[ClusterObservation, ...]) -> tuple[float, float]:
    """The only entry point the report layer may use to turn per-episode
    net returns into the (lower, upper) CI `formal_verdict` gates on --
    frozen bootstrap method/seed/iterations/confidence level, this
    codebase's shared clustered_inference module, not a choice left for
    later."""
    computation = cluster_bootstrap_mean(
        observations,
        iterations=BOOTSTRAP_ITERATIONS,
        seed=BOOTSTRAP_SEED,
        confidence_level=CONFIDENCE_LEVEL,
    )
    return computation.estimate.lower_bound, computation.estimate.upper_bound


def max_sequential_drawdown_pct(ordered_net_return_pct: Sequence[float]) -> float | None:
    """Chronological (caller-ordered, by entry_at) ADDITIVE drawdown across
    a scope's own resolved episodes -- mirrors `virtual_strategy.py`'s own
    `max_sequential_drawdown_usd` (additive, not compounding, this
    codebase's established convention for a simple, order-sensitive
    peak-to-trough proxy across a sequence of independent trades), applied
    to `net_return_pct` instead of USD P&L. Distinct from per-episode
    MFE/MAE (the best/worst excursion WITHIN one episode's own hold
    window): this is the worst peak-to-trough decline ACROSS the ordered
    sequence of episode outcomes themselves, the frozen contract's own
    "Primary metrics" list names alongside MFE/MAE."""
    if not ordered_net_return_pct:
        return None
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in ordered_net_return_pct:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return round(drawdown, 6)


def formal_verdict(
    *,
    resolved_episodes: int,
    distinct_asset_clusters: int,
    distinct_utc_weeks: int,
    max_single_asset_share: float,
    max_single_week_share: float,
    ci_upper_bound_pct: float | None,
) -> str:
    """Frozen verdict rule over already-aggregated statistics -- aggregation
    itself belongs to the report layer, not here. `ci_upper_bound_pct` is
    the cluster-bootstrap CI's own UPPER bound deliberately, not the
    lower: this study models an optimistic UPPER BOUND on what a real
    resting order could achieve, so the honest reject condition is "even
    the optimistic upper bound's own confidence interval never crosses
    into positive territory" -- gating on the CI's lower bound (this
    codebase's usual convention for a confirmatory/candidate verdict)
    would be the wrong direction of caution for an upper-bound study.

    VERDICT_REJECT means exactly this candidate is rejected -- extremum
    entry plus a fixed MAX_POSITION_HOLD_MINUTES taker-style exit -- not
    that no maker-reversion variant could ever work; see the module
    docstring's own "Entry is a post-hoc optimistic upper bound" section
    (colleague review, 2026-09-03) for why a causal, differently-timed exit
    is a different, unaddressed candidate."""
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
    if ci_upper_bound_pct is None:
        return VERDICT_INSUFFICIENT_DATA
    return VERDICT_POSITIVE_WARRANTS_SHADOW_TEST if ci_upper_bound_pct > 0 else VERDICT_REJECT


__all__ = [
    "BOOTSTRAP_ITERATIONS",
    "BOOTSTRAP_SEED",
    "BOOTSTRAP_VERSION",
    "CASCADE_COOLDOWN_MINUTES",
    "CASCADE_TRIGGER_WINDOW_MINUTES",
    "CONFIDENCE_LEVEL",
    "CONTRACT_VERSION",
    "COSTS",
    "DIRECTIONS",
    "ENTRY_MODEL_VERSION",
    "EVIDENCE_FLOOR",
    "EXIT_BAR_TIMEFRAME_MS",
    "EXIT_PRICE_SOURCE_VERSION",
    "EXIT_SLIPPAGE_BPS_ASSUMED",
    "EXIT_SLIPPAGE_SENSITIVITY_BPS",
    "INTERPRETATION",
    "MAKER_ENTRY_FEE_BPS",
    "MAX_EXIT_BAR_GAP_MINUTES",
    "MAX_POSITION_HOLD_MINUTES",
    "MAX_SINGLE_ASSET_EPISODE_SHARE",
    "MAX_SINGLE_WEEK_EPISODE_SHARE",
    "NOMINAL_POSITION_USD",
    "PRIMARY_CASCADE_NOTIONAL_USD",
    "REQUIRE_EXIT_SLIPPAGE_SENSITIVITY",
    "SENSITIVITY_CASCADE_NOTIONAL_USD_FAMILY",
    "UNRESOLVED_REASONS",
    "VERDICT_INSUFFICIENT_DATA",
    "VERDICT_POSITIVE_WARRANTS_SHADOW_TEST",
    "VERDICT_REJECT",
    "CascadeEpisode",
    "EpisodeInputs",
    "EpisodeResult",
    "LiquidationTriggerMinute",
    "decluster_cascade_episodes",
    "formal_verdict",
    "max_sequential_drawdown_pct",
    "primary_sensitivity_ci",
    "resolve_episode",
]
