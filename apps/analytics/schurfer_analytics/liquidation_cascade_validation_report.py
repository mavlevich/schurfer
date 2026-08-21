"""analysis/liquidation-cascade-validation-v2 -- episode-level, chronologically
split validation of the liquidation-cascade entry rule
(apps/execution/schurfer_execution/liquidation_cascade.py), replacing the
recovered `feature/alpha-research` grid search (`583213f`, never merged),
which scored every triggering MINUTE independently (no episode grouping),
had no train/validation/test split, and ignored fees/funding/slippage.

Discovery -> validation -> untouched test, in that order:
  1. Every default entry-threshold cell is scored, purge-classified,
     against the DISCOVERY segment.
  2. The discovery shortlist is re-scored, unchanged, against the
     VALIDATION segment; the single best-on-validation cell becomes the
     candidate.
  3. The CANDIDATE's own economics on the untouched TEST segment -- never
     touched by the sweep or the validation re-score, and re-declustered
     under the candidate's OWN thresholds, not the production reference
     rule -- decide the verdict. A separate reference-rule reading of every
     segment is also reported for context, but the gate never reads from
     it (colleague review, 2026-08-21: an earlier draft ran `_verdict`
     against the production rule's own test-segment economics regardless
     of which thresholds validation had actually selected).

Every entry is modeled as executable only from `trigger_at + 1 minute`
(`replay_from_minute`) -- the trigger bucket's own close/quote is not
knowable until that bucket has closed, so entering AT it is not causal.
This is still a simplification (a real reaction likely takes longer than
one bar), disclosed rather than hidden: it is the earliest defensible
proxy without a captured decision-latency measurement.

Data is fetched via a SINGLE server-side-streamed query
(`LiquidationCascadeRepository.stream_minute_observations`) and processed in
bounded batches of `_SYMBOL_BATCH_SIZE` symbols at a time (`_stream_symbols`,
`_process_batch`), never as one in-memory pass over the whole universe/
window and never as one query per symbol (colleague review, 2026-08-21,
twice: a real 12-hour/full-universe smoke run first found 371,520 rows
fully materialized in Python (35s), then -- after moving to one query per
symbol -- found wall time at 2:59 against only 35s of Python processing,
almost all of it per-symbol round-trip overhead). Only the resulting
episode-level `EpisodeReplay` objects -- not the raw per-minute price/OI
series -- survive past each batch's own processing step;
`build_validation_report` itself is a pure function over that already-
accumulated, already-small data, kept separate from the streaming I/O
specifically so it stays unit-testable without a database.

Data-availability caveat (disclosed, not hidden): `timeseries.
bybit_momentum_bars_1m` has a 35-day retention policy and clean, integrity-
fixed universe capture only since roughly 2026-08-13/17 (ROADMAP.md's own
item-6 canary history). A run against real data today will very likely
report `insufficient_data` for one or more segments rather than a PASS/FAIL
-- that is the correct, honest outcome on a thin sample, not a bug.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import product
from statistics import fmean, median
from typing import TYPE_CHECKING, Any

from .clustered_inference import ClusterObservation, leave_one_cluster_out_means
from .liquidation_cascade_cohort_split import (
    CohortBoundaries,
    Segment,
    classify_episodes,
    load_accepted_cohort,
    resolve_cohort_boundaries,
    resolve_cohort_state_path,
    save_accepted_cohort,
)
from .liquidation_cascade_episodes import CascadeEpisode, decluster_cascade_episodes
from .liquidation_cascade_exit import RUNTIME_EXIT_POLICY, net_return_from_replay, simulate_exit
from .liquidation_cascade_grid_search import (
    DEFAULT_OI_DROP_THRESHOLDS_PCT,
    DEFAULT_PRICE_DROP_THRESHOLDS_PCT,
    MIN_FORMAL_SAMPLE_EPISODES,
    EpisodeReplay,
    GridCell,
    GridSearchResult,
    MinuteObservation,
    episodes_for_threshold_all_segments,
    score_grid_cell,
    shortlist,
    to_minute_states,
)
from .liquidation_cascade_repository import (
    LOOKBACK_MINUTES,
    OI_DROP_TRIGGER_PCT,
    PRICE_DROP_TRIGGER_PCT,
    IdentityLookup,
    LiquidationCascadeRepository,
    OutcomeBar,
    Quote,
)
from .liquidation_cascade_statistics import ShuffledLabelControl, shuffled_label_control
from .reporting import json_ready, normalize_code_revision, parse_utc_datetime, profit_factor

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

REPORT_VERSION = "liquidation_cascade_validation_read_v2"
REPORT_INTERPRETATION = "episode_level_discovery_validation_untouched_test_split"
MIN_DISTINCT_UTC_WEEKS = 4
MIN_FILLABLE_DISTINCT_ASSETS = 5
# A grid search with this many discovery cells produces a real multiple-
# comparisons problem; the candidate's apparent edge must look unusual
# against a label-shuffled null before the gate accepts it, on top of (not
# instead of) the untouched-test-positive and leave-one-out checks below.
SHUFFLED_LABEL_SIGNIFICANCE_THRESHOLD = 0.10
MAX_WINDOW_DAYS = 40
POSITION_USD = 50.0
PROJECTION_NOTIONALS_USD: tuple[float, ...] = (50.0, 100.0, 250.0)
PROJECTION_CAVEAT = (
    "linear extrapolation from the $50 probe's own net_return_pct; no order-book "
    "depth is measured above that size, so $100/$250 assume unchanged fill "
    "quality -- see capacity_above_probe_usd"
)
PROJECTION_UNAVAILABLE_CAVEAT = (
    "unavailable: this segment has not cleared the minimum sample/diversity "
    "evidence floor (MIN_FORMAL_SAMPLE_EPISODES/MIN_DISTINCT_UTC_WEEKS/"
    "MIN_FILLABLE_DISTINCT_ASSETS) -- a projection from too little data is "
    "actively misleading, not just imprecise, so none is published"
)
RECOVERY_PRICE_PCT = 0.02
RECOVERY_OI_PCT = 0.05
COOLDOWN_MINUTES = 30
FEATURE_LOOKBACK_MINUTES = LOOKBACK_MINUTES
OUTCOME_HORIZON_MINUTES = RUNTIME_EXIT_POLICY.max_hold_minutes
# The earliest defensible causal entry instant -- see module doc.
DECISION_LAG_MINUTES = 1
_SCORED_SEGMENTS = (Segment.DISCOVERY, Segment.VALIDATION, Segment.TEST)
_DEFAULT_GRID_CELLS = tuple(
    product(DEFAULT_PRICE_DROP_THRESHOLDS_PCT, DEFAULT_OI_DROP_THRESHOLDS_PCT)
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exchange", type=str, default="bybit")
    parser.add_argument("--since", type=parse_utc_datetime, required=True)
    parser.add_argument("--discovery-end", type=parse_utc_datetime, required=True)
    parser.add_argument("--validation-end", type=parse_utc_datetime, required=True)
    parser.add_argument("--until", type=parse_utc_datetime, required=True)
    parser.add_argument("--accept-new-cohort-boundary", action="store_true")
    parser.add_argument("--cohort-state-path", type=str)
    parser.add_argument("--code-revision", type=str, required=True)
    dirty = parser.add_mutually_exclusive_group(required=True)
    dirty.add_argument("--no-working-tree-dirty", action="store_true")
    dirty.add_argument("--working-tree-dirty", action="store_true")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser.parse_args()


def _max_drawdown_usd(ordered_pnl_usd: Sequence[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in ordered_pnl_usd:
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return max_drawdown


def _worst_losing_streak(ordered_pnl_usd: Sequence[float]) -> int:
    worst = 0
    current = 0
    for pnl in ordered_pnl_usd:
        if pnl < 0:
            current += 1
            worst = max(worst, current)
        else:
            current = 0
    return worst


def _identity_stability(lookup: IdentityLookup, *, since: datetime) -> dict[str, bool]:
    """A symbol is stable only when:

    - a BASELINE observation exists (a relevant snapshot at or before
      `since`) -- a symbol whose earliest evidence starts inside the window
      has no confirmed pre-window identity and is unresolved, not presumed
      stable;
    - the symbol is present in EVERY relevant snapshot (baseline plus every
      snapshot inside `[since, until)`) -- disappearing from one relevant
      snapshot (a temporary delisting) is unresolved, not silently ignored;
    - every observation for that symbol reports `identity_status ==
      "ready"` and they all agree on one `identity_key` and one
      `onboarded_at`.

    `catalog_version` is deliberately NOT used for this comparison
    (colleague review, 2026-08-21): it hashes the WHOLE catalog snapshot, so
    any new listing on either venue changes it for every instrument, which
    would mark nearly every symbol unstable regardless of whether THAT
    symbol itself was ever delisted or relisted. A genuine delisted-and-
    relisted ticker under the same native market id instead changes its OWN
    `onboarded_at` (see momentum_universe_identity_repository.py's own
    module doc). A symbol with NO identity observation at all is not
    presumed stable -- it stays absent from this map, and callers must
    treat that the same as unresolved, never as an implicit pass."""
    relevant_count = len(lookup.relevant_snapshot_timestamps)
    by_symbol: dict[str, list[Any]] = {}
    for observation in lookup.observations:
        by_symbol.setdefault(observation.native_market_id, []).append(observation)

    stability: dict[str, bool] = {}
    for symbol, rows in by_symbol.items():
        has_baseline = any(row.captured_at <= since for row in rows)
        present_in_every_relevant_snapshot = (
            len({row.captured_at for row in rows}) == relevant_count
        )
        all_ready = all(row.identity_status == "ready" for row in rows)
        one_identity_key = len({row.identity_key for row in rows}) == 1
        one_onboarded_at = len({row.onboarded_at for row in rows}) == 1
        stability[symbol] = (
            has_baseline
            and present_in_every_relevant_snapshot
            and all_ready
            and one_identity_key
            and one_onboarded_at
        )
    return stability


def _capital_occupancy(
    episodes: Sequence[CascadeEpisode], *, window_days: float
) -> dict[str, float]:
    """Average concurrently-open episodes, treating each episode as
    occupying capital from `trigger_at` through
    `trigger_at + max_hold_minutes`, regardless of its own resolved exit --
    the same conservative "reserved until proven released" convention the
    plan's own capacity accounting calls for."""
    if not episodes or window_days <= 0:
        return {"mean_concurrent_positions": 0.0, "window_days": window_days}
    horizon = timedelta(minutes=OUTCOME_HORIZON_MINUTES)
    position_days = sum(horizon.total_seconds() / 86400 for _ in episodes)
    return {
        "mean_concurrent_positions": position_days / window_days,
        "window_days": window_days,
    }


@dataclass(frozen=True)
class SegmentEconomics:
    segment: str
    episodes: int
    fillable_episodes: int
    unresolved_episodes: int
    distinct_assets: int
    distinct_utc_weeks: int
    fillable_distinct_assets: int
    fillable_distinct_utc_weeks: int
    window_days: float
    opportunities_per_day: float | None
    fillable_opportunities_per_day: float | None
    mean_net_return_pct: float | None
    median_net_return_pct: float | None
    profit_factor: float | None
    max_drawdown_usd_at_position: float | None
    worst_losing_streak: int
    capital_occupancy: dict[str, float]
    projected_monthly_pnl_usd: dict[str, float | None]
    projected_monthly_pnl_caveat: str
    capacity_above_probe_usd: None
    sensitivity: dict[str, Any]


def _segment_economics(
    segment: Segment,
    replays: tuple[EpisodeReplay, ...],
    *,
    since: datetime,
    until: datetime,
) -> SegmentEconomics:
    """`replays` must already carry eligibility (data-quality and identity)
    baked into `net_return_pct`/`unresolved_reason` by the caller (see
    `_replays_from_cache`) -- this function trusts `net_return_pct is not
    None` as the single fillable test, the same trust boundary
    `liquidation_cascade_grid_search.py` relies on, so the grid search and
    this final reporting can never disagree about which episodes count."""
    fillable = [replay for replay in replays if replay.net_return_pct is not None]
    resolved_returns = [
        replay.net_return_pct for replay in fillable if replay.net_return_pct is not None
    ]
    ordered_pnl = [
        replay.net_return_pct * POSITION_USD / 100
        for replay in sorted(fillable, key=lambda r: r.episode.trigger_at)
        if replay.net_return_pct is not None
    ]
    window_days = max((until - since).total_seconds() / 86400, 0.0)
    distinct_assets = len({replay.episode.symbol for replay in replays})
    distinct_weeks = len({replay.episode.week_key for replay in replays})
    fillable_distinct_assets = len({replay.episode.symbol for replay in fillable})
    fillable_distinct_weeks = len({replay.episode.week_key for replay in fillable})

    week_observations = tuple(
        ClusterObservation(replay.episode.week_key, replay.net_return_pct)
        for replay in fillable
        if replay.net_return_pct is not None
    )
    asset_observations = tuple(
        ClusterObservation(replay.episode.symbol, replay.net_return_pct)
        for replay in fillable
        if replay.net_return_pct is not None
    )
    week_keys = tuple(sorted({obs.cluster_key for obs in week_observations}))
    asset_keys = tuple(sorted({obs.cluster_key for obs in asset_observations}))
    # Leave-one-cluster-out is undefined (and clustered_inference.py itself
    # raises) once excluding the only cluster would remove every
    # observation -- a single distinct week/asset reports empty sensitivity
    # rather than crashing; `_verdict` already treats fewer than the
    # diversity floors as insufficient_data before it would ever rely on
    # this being non-empty.
    sensitivity = {
        "leave_one_week_out": (
            leave_one_cluster_out_means(week_observations, week_keys) if len(week_keys) >= 2 else ()
        ),
        "leave_one_asset_out": (
            leave_one_cluster_out_means(asset_observations, asset_keys)
            if len(asset_keys) >= 2
            else ()
        ),
    }

    mean_return = fmean(resolved_returns) if resolved_returns else None
    # A linear extrapolation from a handful of episodes and a few hours of
    # window is actively misleading, not merely imprecise (colleague
    # review, 2026-08-21: a real smoke run's 4-hour/1-week test sample
    # projected to "$1,776/month" -- a number with no real portfolio
    # constraints behind it, published as if it meant something). The
    # projection is only computed once the segment clears the SAME sample/
    # diversity floors the verdict gate itself requires; short of that it
    # is explicitly unavailable, never a number computed from too little.
    meets_evidence_floor = (
        len(fillable) >= MIN_FORMAL_SAMPLE_EPISODES
        and fillable_distinct_weeks >= MIN_DISTINCT_UTC_WEEKS
        and fillable_distinct_assets >= MIN_FILLABLE_DISTINCT_ASSETS
    )
    projected_monthly: dict[str, float | None]
    if meets_evidence_floor and mean_return is not None and window_days > 0:
        projected_monthly = {
            str(int(notional)): mean_return * notional / 100 * (len(fillable) / window_days) * 30
            for notional in PROJECTION_NOTIONALS_USD
        }
        projection_caveat = PROJECTION_CAVEAT
    else:
        projected_monthly = {str(int(notional)): None for notional in PROJECTION_NOTIONALS_USD}
        projection_caveat = PROJECTION_UNAVAILABLE_CAVEAT

    return SegmentEconomics(
        segment=segment.value,
        episodes=len(replays),
        fillable_episodes=len(fillable),
        unresolved_episodes=len(replays) - len(fillable),
        distinct_assets=distinct_assets,
        distinct_utc_weeks=distinct_weeks,
        fillable_distinct_assets=fillable_distinct_assets,
        fillable_distinct_utc_weeks=fillable_distinct_weeks,
        window_days=window_days,
        opportunities_per_day=len(replays) / window_days if window_days > 0 else None,
        fillable_opportunities_per_day=len(fillable) / window_days if window_days > 0 else None,
        mean_net_return_pct=mean_return,
        median_net_return_pct=median(resolved_returns) if resolved_returns else None,
        profit_factor=profit_factor(resolved_returns) if resolved_returns else None,
        max_drawdown_usd_at_position=_max_drawdown_usd(ordered_pnl) if ordered_pnl else None,
        worst_losing_streak=_worst_losing_streak(ordered_pnl),
        capital_occupancy=_capital_occupancy(
            [r.episode for r in fillable], window_days=window_days
        ),
        projected_monthly_pnl_usd=projected_monthly,
        projected_monthly_pnl_caveat=projection_caveat,
        capacity_above_probe_usd=None,
        sensitivity=sensitivity,
    )


def _verdict(
    *,
    best_validation_cell: GridCell | None,
    candidate_test_economics: SegmentEconomics | None,
    shuffled_control: ShuffledLabelControl,
) -> tuple[str, list[str]]:
    """A negative-mean validation cell is never promoted to candidate (see
    `build_validation_report`'s own candidate-selection logic) -- but the
    verdict must still say WHY, and the shuffled-label-control result is
    always evaluated and appended alongside whatever other reason applies,
    never gated behind the other checks passing first (colleague review,
    2026-08-21: a real smoke run's validation-selected cell was negative
    (mean -2.29%, PF 0.12) with a discovery shuffle p=0.692 -- pure noise
    by both measures -- while its own untouched test segment happened to
    look positive on four hours of data; that positive test number must
    never be treated as a legitimate gate input for a candidate validation
    already rejected)."""
    shuffle_failed = (
        shuffled_control.empirical_p_value is None
        or shuffled_control.empirical_p_value >= SHUFFLED_LABEL_SIGNIFICANCE_THRESHOLD
    )

    if best_validation_cell is None:
        reasons = ["no_validation_selected_candidate"]
        if shuffle_failed:
            reasons.append("shuffled_label_control_not_significant")
        return "insufficient_data", reasons

    if (
        best_validation_cell.mean_net_return_pct is None
        or best_validation_cell.mean_net_return_pct <= 0
    ):
        reasons = ["validation_net_ev_non_positive"]
        if shuffle_failed:
            reasons.append("shuffled_label_control_not_significant")
        return "FAIL", reasons

    # A positive-validation cell was promoted to candidate; its own test
    # economics must have been computed (see build_validation_report).
    assert candidate_test_economics is not None

    data_reasons: list[str] = []
    if candidate_test_economics.fillable_episodes < MIN_FORMAL_SAMPLE_EPISODES:
        data_reasons.append("insufficient_test_sample")
    if candidate_test_economics.fillable_distinct_utc_weeks < MIN_DISTINCT_UTC_WEEKS:
        data_reasons.append("fewer_than_four_distinct_utc_weeks")
    if candidate_test_economics.fillable_distinct_assets < MIN_FILLABLE_DISTINCT_ASSETS:
        data_reasons.append("fewer_than_min_fillable_assets")
    if data_reasons:
        if shuffle_failed:
            data_reasons.append("shuffled_label_control_not_significant")
        return "insufficient_data", data_reasons

    fail_reasons: list[str] = []
    if (
        candidate_test_economics.mean_net_return_pct is None
        or candidate_test_economics.mean_net_return_pct <= 0
    ):
        fail_reasons.append("test_net_ev_non_positive")
    if any(value <= 0 for _, value in candidate_test_economics.sensitivity["leave_one_week_out"]):
        fail_reasons.append("fails_leave_one_week_out")
    if any(value <= 0 for _, value in candidate_test_economics.sensitivity["leave_one_asset_out"]):
        fail_reasons.append("fails_leave_one_asset_out")
    if shuffle_failed:
        fail_reasons.append("shuffled_label_control_not_significant")
    if fail_reasons:
        return "FAIL", fail_reasons
    return "PASS", []


def replay_from_minute(
    *,
    symbol: str,
    trigger_at: datetime,
    bars: tuple[OutcomeBar, ...],
    quotes: Mapping[tuple[str, datetime], Quote],
    position_usd: float,
) -> tuple[float | None, str | None]:
    """Pure -- no I/O. `bars`/`quotes` must already be the bulk-fetched
    series for `symbol` (see `_stream_symbols`). Entry is modeled at
    `trigger_at + DECISION_LAG_MINUTES`, not at `trigger_at` itself -- see
    this module's own doc comment on why entering at the trigger bucket's
    own close is not causal."""
    decision_at = trigger_at + timedelta(minutes=DECISION_LAG_MINUTES)
    by_bucket = {bar.bucket_start: bar for bar in bars}
    entry_bar = by_bucket.get(decision_at)
    if entry_bar is None or entry_bar.close_price is None or not entry_bar.complete:
        return None, "unresolved_entry_bar"
    entry_price = entry_bar.close_price
    exit_result = simulate_exit(
        entry_at=decision_at, entry_price=entry_price, bars=bars, policy=RUNTIME_EXIT_POLICY
    )
    if exit_result is None:
        return None, "immature_outcome"
    entry_quote = quotes.get((symbol, decision_at))
    exit_quote = quotes.get((symbol, exit_result.exit_at))
    accounting = net_return_from_replay(
        entry_price=entry_price,
        exit_result=exit_result,
        entry_quote=entry_quote,
        exit_quote=exit_quote,
        position_usd=position_usd,
    )
    if accounting.status != "complete" or accounting.net_return_pct is None:
        return None, f"unresolved_costs:{accounting.error or 'incomplete'}"
    return accounting.net_return_pct, None


def _replays_from_cache(
    episodes: tuple[CascadeEpisode, ...],
    cache: Mapping[tuple[str, str, datetime], tuple[float | None, str | None]],
    *,
    identity_stable: Mapping[str, bool],
) -> tuple[EpisodeReplay, ...]:
    """The single eligibility chokepoint: every caller that needs episode
    outcomes -- the reference-rule segments, the discovery sweep, the
    validation re-score, and the candidate's own test economics -- goes
    through this function, so data-quality and identity exclusions can
    never silently diverge between the grid search's own selection and the
    final reported economics (colleague review, 2026-08-21)."""
    results = []
    for episode in episodes:
        if episode.data_quality_unresolved:
            results.append(
                EpisodeReplay(
                    episode=episode,
                    net_return_pct=None,
                    unresolved_reason="data_quality_unresolved",
                )
            )
            continue
        if not identity_stable.get(episode.symbol, False):
            results.append(
                EpisodeReplay(
                    episode=episode, net_return_pct=None, unresolved_reason="identity_unresolved"
                )
            )
            continue
        key = (episode.exchange, episode.symbol, episode.trigger_at)
        cached = cache.get(key)
        if cached is None:
            results.append(
                EpisodeReplay(
                    episode=episode, net_return_pct=None, unresolved_reason="not_in_replay_cache"
                )
            )
            continue
        net_return_pct, unresolved_reason = cached
        results.append(
            EpisodeReplay(
                episode=episode,
                net_return_pct=net_return_pct,
                unresolved_reason=unresolved_reason if net_return_pct is None else None,
            )
        )
    return tuple(results)


def _candidate_ranking_key(cell: GridCell) -> tuple[bool, float]:
    """Same explicit-None-check fix as grid_search._ranking_key -- `mean or
    -inf` would mis-rank a genuinely zero-mean cell as worse than a losing
    one (colleague review, 2026-08-21)."""
    if cell.mean_net_return_pct is None:
        return (True, 0.0)
    return (False, -cell.mean_net_return_pct)


@dataclass
class Diagnostics:
    """Surfaced in the report so a thin/broken run is visibly diagnosable
    instead of silently producing a mathematically-honest-but-uninformative
    zero (colleague review, 2026-08-21: the first real smoke run's zero
    reference-rule result was correct arithmetic over a genuinely empty
    identity lookup, and nothing in the report itself said so)."""

    symbols_in_window: int = 0
    symbols_with_data: int = 0
    input_rows: int = 0
    clean_lookback_rows: int = 0
    loose_qualifying_minutes: int = 0
    reference_qualifying_minutes: int = 0
    reference_purge_excluded_episodes: int = 0
    identity_ready_symbols: int = 0
    identity_unresolved_symbols: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "symbols_in_window": self.symbols_in_window,
            "symbols_with_data": self.symbols_with_data,
            "input_rows": self.input_rows,
            "clean_lookback_rows": self.clean_lookback_rows,
            "loose_qualifying_minutes": self.loose_qualifying_minutes,
            "reference_qualifying_minutes": self.reference_qualifying_minutes,
            "reference_purge_excluded_episodes": self.reference_purge_excluded_episodes,
            "identity_ready_symbols": self.identity_ready_symbols,
            "identity_unresolved_symbols": self.identity_unresolved_symbols,
        }


@dataclass
class _StreamedData:
    reference_replays: dict[Segment, list[EpisodeReplay]] = field(
        default_factory=lambda: {segment: [] for segment in _SCORED_SEGMENTS}
    )
    grid_replays: dict[tuple[float, float], dict[Segment, list[EpisodeReplay]]] = field(
        default_factory=lambda: {
            cell: {segment: [] for segment in _SCORED_SEGMENTS} for cell in _DEFAULT_GRID_CELLS
        }
    )
    diagnostics: Diagnostics = field(default_factory=Diagnostics)


# Symbols per batch: bounds peak memory to roughly this many symbols' own
# raw minute series at once (colleague review, 2026-08-21: 25-50 is the
# suggested range) while cutting the bars/quotes round-trip count from one
# pair PER TRIGGERING SYMBOL to one pair per batch. Known, disclosed
# tradeoff: a batch's combined bars/quotes fetch window spans from its
# earliest to its latest qualifying trigger, so a symbol whose own triggers
# are narrow can still pull back a wider row set than it alone needed if it
# shares a batch with a symbol triggering far away in time -- acceptable
# because the ROWS returned are still exactly-scoped per symbol (no cross-
# symbol leakage), only the fetched TIME RANGE is shared.
_SYMBOL_BATCH_SIZE = 50


async def _process_batch(
    repository: LiquidationCascadeRepository,
    batch: list[tuple[str, tuple[MinuteObservation, ...]]],
    *,
    exchange: str,
    boundaries: CohortBoundaries,
    identity_stable: Mapping[str, bool],
    position_usd: float,
    accumulator: _StreamedData,
) -> None:
    """Processes up to `_SYMBOL_BATCH_SIZE` already-streamed symbols
    together -- only the small `EpisodeReplay` objects it appends to
    `accumulator` survive once this call returns; `batch`'s own raw
    observations are released with it. See the module doc comment."""
    diagnostics = accumulator.diagnostics
    loosest_price = max(DEFAULT_PRICE_DROP_THRESHOLDS_PCT)
    loosest_oi = max(DEFAULT_OI_DROP_THRESHOLDS_PCT)

    # Pass 1: per symbol, find which minutes need a bar-by-bar replay at
    # all (loosest combo -- every stricter combination, reference rule
    # included, is a strict subset). No I/O yet.
    per_symbol_qualifying: dict[str, list[Any]] = {}
    for symbol, observations in batch:
        diagnostics.symbols_with_data += 1
        diagnostics.input_rows += len(observations)
        diagnostics.clean_lookback_rows += sum(
            1 for o in observations if o.price_complete and o.open_interest_complete
        )
        loose_states = to_minute_states(
            observations, price_drop_trigger_pct=loosest_price, oi_drop_trigger_pct=loosest_oi
        )
        qualifying = [minute for minute in loose_states if minute.is_qualifying]
        diagnostics.loose_qualifying_minutes += len(qualifying)
        if qualifying:
            per_symbol_qualifying[symbol] = qualifying

    # Pass 2: ONE combined bars fetch and ONE combined quotes fetch for
    # every symbol in this batch that has at least one qualifying minute --
    # not one pair per symbol.
    replay_cache: dict[tuple[str, str, datetime], tuple[float | None, str | None]] = {}
    if per_symbol_qualifying:
        trigger_symbols = list(per_symbol_qualifying)
        earliest_trigger = min(
            minute.bucket_start for minutes in per_symbol_qualifying.values() for minute in minutes
        )
        latest_trigger = max(
            minute.bucket_start for minutes in per_symbol_qualifying.values() for minute in minutes
        )
        fetch_since = earliest_trigger + timedelta(minutes=DECISION_LAG_MINUTES)
        fetch_until = latest_trigger + timedelta(
            minutes=DECISION_LAG_MINUTES + RUNTIME_EXIT_POLICY.max_hold_minutes + 5
        )
        bars_by_symbol = await repository.fetch_bars_for_symbols(
            exchange=exchange, symbols=trigger_symbols, since=fetch_since, until=fetch_until
        )
        quotes = await repository.fetch_quotes_for_symbols(
            exchange=exchange, symbols=trigger_symbols, since=fetch_since, until=fetch_until
        )
        for symbol, minutes in per_symbol_qualifying.items():
            bars = bars_by_symbol.get(symbol, ())
            for minute in minutes:
                key = (minute.exchange, minute.symbol, minute.bucket_start)
                replay_cache[key] = replay_from_minute(
                    symbol=symbol,
                    trigger_at=minute.bucket_start,
                    bars=bars,
                    quotes=quotes,
                    position_usd=position_usd,
                )

    # Pass 3: per symbol, decluster reference + every grid cell, replay via
    # the batch-wide cache, and accumulate.
    for symbol, observations in batch:
        symbol_identity_stable = {symbol: identity_stable.get(symbol, False)}

        def _replay_lookup(
            episodes: tuple[CascadeEpisode, ...],
            *,
            _stable: Mapping[str, bool] = symbol_identity_stable,
        ) -> tuple[EpisodeReplay, ...]:
            # Default-arg capture, not a bare closure over the loop
            # variable -- called synchronously within this same iteration
            # either way, but this keeps it correct even if that ever
            # changes (ruff B023).
            return _replays_from_cache(episodes, replay_cache, identity_stable=_stable)

        reference_states = to_minute_states(
            observations,
            price_drop_trigger_pct=PRICE_DROP_TRIGGER_PCT,
            oi_drop_trigger_pct=OI_DROP_TRIGGER_PCT,
        )
        diagnostics.reference_qualifying_minutes += sum(
            1 for state in reference_states if state.is_qualifying
        )
        reference_episodes = decluster_cascade_episodes(
            reference_states,
            recovery_price_pct=RECOVERY_PRICE_PCT,
            recovery_oi_pct=RECOVERY_OI_PCT,
            cooldown_minutes=COOLDOWN_MINUTES,
        )
        reference_by_segment = classify_episodes(
            reference_episodes,
            boundaries=boundaries,
            feature_lookback_minutes=FEATURE_LOOKBACK_MINUTES,
            outcome_horizon_minutes=OUTCOME_HORIZON_MINUTES,
        )
        diagnostics.reference_purge_excluded_episodes += len(
            reference_by_segment[Segment.EXCLUDED_PURGE]
        )
        for segment in _SCORED_SEGMENTS:
            accumulator.reference_replays[segment].extend(
                _replay_lookup(reference_by_segment[segment])
            )

        for price_thresh, oi_thresh in _DEFAULT_GRID_CELLS:
            cell_by_segment = episodes_for_threshold_all_segments(
                observations,
                price_drop_trigger_pct=price_thresh,
                oi_drop_trigger_pct=oi_thresh,
                boundaries=boundaries,
                feature_lookback_minutes=FEATURE_LOOKBACK_MINUTES,
                outcome_horizon_minutes=OUTCOME_HORIZON_MINUTES,
                recovery_price_pct=RECOVERY_PRICE_PCT,
                recovery_oi_pct=RECOVERY_OI_PCT,
                cooldown_minutes=COOLDOWN_MINUTES,
            )
            cell_accumulator = accumulator.grid_replays[(price_thresh, oi_thresh)]
            for segment in _SCORED_SEGMENTS:
                cell_accumulator[segment].extend(_replay_lookup(cell_by_segment[segment]))
    # `batch`, `per_symbol_qualifying`, and `replay_cache` all go out of
    # scope when this function returns -- only the EpisodeReplay objects
    # already appended to `accumulator` above survive.


async def _stream_symbols(
    repository: LiquidationCascadeRepository,
    *,
    exchange: str,
    since: datetime,
    until: datetime,
    boundaries: CohortBoundaries,
    identity_stable: Mapping[str, bool],
    position_usd: float,
    symbols_in_window: int,
) -> _StreamedData:
    """`symbols_in_window` is the count from the caller's own upfront
    `fetch_symbols_in_window` call (needed there already, to size the
    identity lookup before this -- much larger -- stream starts); this
    function does not re-query it."""
    accumulator = _StreamedData()
    accumulator.diagnostics.symbols_in_window = symbols_in_window
    accumulator.diagnostics.identity_ready_symbols = sum(
        1 for stable in identity_stable.values() if stable
    )
    accumulator.diagnostics.identity_unresolved_symbols = (
        symbols_in_window - accumulator.diagnostics.identity_ready_symbols
    )

    batch: list[tuple[str, tuple[MinuteObservation, ...]]] = []
    async for symbol, observations in repository.stream_minute_observations(
        exchange=exchange, since=since, until=until
    ):
        if not observations:
            continue
        batch.append((symbol, observations))
        if len(batch) >= _SYMBOL_BATCH_SIZE:
            await _process_batch(
                repository,
                batch,
                exchange=exchange,
                boundaries=boundaries,
                identity_stable=identity_stable,
                position_usd=position_usd,
                accumulator=accumulator,
            )
            batch = []
    if batch:
        await _process_batch(
            repository,
            batch,
            exchange=exchange,
            boundaries=boundaries,
            identity_stable=identity_stable,
            position_usd=position_usd,
            accumulator=accumulator,
        )
    return accumulator


def build_validation_report(
    *,
    reference_replays: Mapping[Segment, tuple[EpisodeReplay, ...]],
    grid_replays: Mapping[tuple[float, float], Mapping[Segment, tuple[EpisodeReplay, ...]]],
    diagnostics: Diagnostics,
    boundaries: CohortBoundaries,
    since: datetime,
    until: datetime,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
) -> dict[str, Any]:
    """Pure -- no I/O. `reference_replays`/`grid_replays` must already be
    fully accumulated and eligibility-resolved (see `_stream_symbols`,
    `_replays_from_cache`); this function only aggregates and selects.
    Kept pure specifically so it stays testable with small, hand-built
    inputs instead of a live database."""
    segment_windows = {
        Segment.DISCOVERY: (since, boundaries.discovery_end),
        Segment.VALIDATION: (boundaries.discovery_end, boundaries.validation_end),
        Segment.TEST: (boundaries.validation_end, until),
    }
    reference_segment_economics = {
        segment.value: _segment_economics(
            segment, reference_replays.get(segment, ()), since=window[0], until=window[1]
        )
        for segment, window in segment_windows.items()
    }

    discovery_cells = tuple(
        score_grid_cell(price, oi, grid_replays.get((price, oi), {}).get(Segment.DISCOVERY, ()))
        for price, oi in _DEFAULT_GRID_CELLS
    )
    cell_membership = {
        (price, oi): tuple(
            replay.episode_key
            for replay in grid_replays.get((price, oi), {}).get(Segment.DISCOVERY, ())
        )
        for price, oi in _DEFAULT_GRID_CELLS
    }
    episode_returns: dict[tuple[str, str, datetime], float] = {}
    for price, oi in _DEFAULT_GRID_CELLS:
        for replay in grid_replays.get((price, oi), {}).get(Segment.DISCOVERY, ()):
            if replay.net_return_pct is not None:
                episode_returns[replay.episode_key] = replay.net_return_pct
    grid_result = GridSearchResult(
        cells=discovery_cells, cell_membership=cell_membership, episode_returns=episode_returns
    )
    discovery_shortlist = shortlist(grid_result)

    validation_cells = tuple(
        score_grid_cell(
            cell.price_drop_trigger_pct,
            cell.oi_drop_trigger_pct,
            grid_replays.get((cell.price_drop_trigger_pct, cell.oi_drop_trigger_pct), {}).get(
                Segment.VALIDATION, ()
            ),
        )
        for cell in discovery_shortlist
    )
    validation_ready = [cell for cell in validation_cells if cell.formal_sample_ready]
    best_validation_cell = (
        min(validation_ready, key=_candidate_ranking_key) if validation_ready else None
    )
    # A negative (or zero) validation mean is never promoted to candidate --
    # `best_validation_cell` stays visible in the report for transparency,
    # but only a POSITIVE validation result ever reaches the untouched test
    # segment (colleague review, 2026-08-21: promoting a negative-
    # validation cell anyway let its own test-segment economics look like a
    # legitimate PASS input on a real smoke run).
    candidate = (
        best_validation_cell
        if best_validation_cell is not None
        and best_validation_cell.mean_net_return_pct is not None
        and best_validation_cell.mean_net_return_pct > 0
        else None
    )

    candidate_test_economics: SegmentEconomics | None = None
    if candidate is not None:
        candidate_test_replays = grid_replays.get(
            (candidate.price_drop_trigger_pct, candidate.oi_drop_trigger_pct), {}
        ).get(Segment.TEST, ())
        candidate_test_economics = _segment_economics(
            Segment.TEST,
            candidate_test_replays,
            since=boundaries.validation_end,
            until=until,
        )

    shuffled_control = shuffled_label_control(
        grid_result, min_formal_sample_episodes=MIN_FORMAL_SAMPLE_EPISODES
    )

    verdict, verdict_reasons = _verdict(
        best_validation_cell=best_validation_cell,
        candidate_test_economics=candidate_test_economics,
        shuffled_control=shuffled_control,
    )

    return {
        "report_version": REPORT_VERSION,
        "interpretation": REPORT_INTERPRETATION,
        "code_revision": normalize_code_revision(code_revision),
        "working_tree_dirty": working_tree_dirty,
        "generated_at": generated_at,
        "window": {"since": since, "until_exclusive": until},
        "cohort_boundaries": {
            "discovery_end": boundaries.discovery_end,
            "validation_end": boundaries.validation_end,
        },
        "reference_rule": {
            "price_drop_trigger_pct": PRICE_DROP_TRIGGER_PCT,
            "oi_drop_trigger_pct": OI_DROP_TRIGGER_PCT,
            "note": "must track apps/execution/schurfer_execution/liquidation_cascade.py",
        },
        "position_usd": POSITION_USD,
        "diagnostics": diagnostics.as_dict(),
        "reference_rule_segments": {
            key: _asdict_segment(value) for key, value in reference_segment_economics.items()
        },
        "grid_search": {
            "discovery_cells": len(grid_result.cells),
            "discovery_leaderboard_top10": [_asdict_cell(cell) for cell in grid_result.cells[:10]],
            "discovery_shortlist": [_asdict_cell(cell) for cell in discovery_shortlist],
            "validation_rescoring": [_asdict_cell(cell) for cell in validation_cells],
            # The best validation-segment cell regardless of sign -- shown
            # for transparency even when it was rejected below.
            "best_validation_cell": (
                _asdict_cell(best_validation_cell) if best_validation_cell is not None else None
            ),
            # Only set when best_validation_cell's own mean was positive --
            # never a cell promoted despite a non-positive validation mean.
            "candidate_promoted": candidate is not None,
            "candidate": _asdict_cell(candidate) if candidate is not None else None,
            "candidate_test_economics": (
                _asdict_segment(candidate_test_economics)
                if candidate_test_economics is not None
                else None
            ),
        },
        "shuffled_label_control": {
            "observed_best_mean_net_return_pct": shuffled_control.observed_best_mean_net_return_pct,
            "iterations": shuffled_control.iterations,
            "shuffled_at_or_above_observed": shuffled_control.shuffled_at_or_above_observed,
            "empirical_p_value": shuffled_control.empirical_p_value,
            "significance_threshold": SHUFFLED_LABEL_SIGNIFICANCE_THRESHOLD,
        },
        "verdict": verdict,
        "verdict_reasons": verdict_reasons,
    }


def _asdict_segment(economics: SegmentEconomics) -> dict[str, Any]:
    return {
        "segment": economics.segment,
        "episodes": economics.episodes,
        "fillable_episodes": economics.fillable_episodes,
        "unresolved_episodes": economics.unresolved_episodes,
        "distinct_assets": economics.distinct_assets,
        "distinct_utc_weeks": economics.distinct_utc_weeks,
        "fillable_distinct_assets": economics.fillable_distinct_assets,
        "fillable_distinct_utc_weeks": economics.fillable_distinct_utc_weeks,
        "window_days": economics.window_days,
        "opportunities_per_day": economics.opportunities_per_day,
        "fillable_opportunities_per_day": economics.fillable_opportunities_per_day,
        "mean_net_return_pct": economics.mean_net_return_pct,
        "median_net_return_pct": economics.median_net_return_pct,
        "profit_factor": economics.profit_factor,
        "max_drawdown_usd_at_position": economics.max_drawdown_usd_at_position,
        "worst_losing_streak": economics.worst_losing_streak,
        "capital_occupancy": economics.capital_occupancy,
        "projected_monthly_pnl_usd": economics.projected_monthly_pnl_usd,
        "projected_monthly_pnl_caveat": economics.projected_monthly_pnl_caveat,
        "capacity_above_probe_usd": economics.capacity_above_probe_usd,
        "sensitivity": economics.sensitivity,
    }


def _asdict_cell(cell: GridCell) -> dict[str, Any]:
    return {
        "price_drop_trigger_pct": cell.price_drop_trigger_pct,
        "oi_drop_trigger_pct": cell.oi_drop_trigger_pct,
        "episodes": cell.episodes,
        "resolved_episodes": cell.resolved_episodes,
        "unresolved_episodes": cell.unresolved_episodes,
        "distinct_assets": cell.distinct_assets,
        "mean_net_return_pct": cell.mean_net_return_pct,
        "profit_factor": cell.profit_factor,
        "formal_sample_ready": cell.formal_sample_ready,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Liquidation Cascade Validation ({report['report_version']})",
        "",
        f"Generated: {report['generated_at']}",
        f"Verdict: **{report['verdict']}** ({', '.join(report['verdict_reasons']) or 'clean'})",
        "",
        "## Diagnostics",
        "",
    ]
    diagnostics = report["diagnostics"]
    lines.append(
        f"- symbols in window: {diagnostics['symbols_in_window']} "
        f"({diagnostics['symbols_with_data']} with data)"
    )
    lines.append(
        f"- input rows: {diagnostics['input_rows']}, "
        f"clean lookbacks: {diagnostics['clean_lookback_rows']}"
    )
    lines.append(
        f"- loose triggers: {diagnostics['loose_qualifying_minutes']}, "
        f"reference triggers: {diagnostics['reference_qualifying_minutes']}"
    )
    lines.append(
        f"- identity ready: {diagnostics['identity_ready_symbols']}, "
        f"identity unresolved: {diagnostics['identity_unresolved_symbols']}"
    )
    lines.append(
        f"- reference episodes excluded by purge: "
        f"{diagnostics['reference_purge_excluded_episodes']}"
    )
    lines += ["", "## Reference-rule segments (context only, not the gate)", ""]
    for key in ("discovery", "validation", "test"):
        segment = report["reference_rule_segments"][key]
        lines.append(
            f"- **{key}**: {segment['episodes']} episodes "
            f"({segment['fillable_episodes']} fillable, "
            f"{segment['unresolved_episodes']} unresolved), "
            f"{segment['fillable_distinct_assets']} fillable assets, "
            f"{segment['fillable_distinct_utc_weeks']} fillable UTC weeks, "
            f"mean net {segment['mean_net_return_pct']!r}%, "
            f"profit factor {segment['profit_factor']!r}"
        )
    lines += [
        "",
        f"## Best validation cell (regardless of sign): "
        f"{report['grid_search']['best_validation_cell']}",
        "",
        f"## Candidate (promoted only if validation mean > 0): "
        f"{report['grid_search']['candidate']}",
        "",
        f"## Candidate test-segment economics (the actual gate input): "
        f"{report['grid_search']['candidate_test_economics']}",
        "",
        f"## Shuffled-label control: {report['shuffled_label_control']}",
    ]
    return "\n".join(lines) + "\n"


def render_json(report: dict[str, Any]) -> str:
    return json.dumps(json_ready(report), indent=2, sort_keys=True)


async def _run(
    args: argparse.Namespace,
    *,
    repository: LiquidationCascadeRepository | None = None,
    now: datetime | None = None,
) -> str:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for liquidation-cascade-validation-report")
    if not (args.since < args.discovery_end < args.validation_end < args.until):
        raise ValueError("require since < discovery_end < validation_end < until")
    for name, value in (
        ("since", args.since),
        ("discovery_end", args.discovery_end),
        ("validation_end", args.validation_end),
        ("until", args.until),
    ):
        if value.utcoffset() != timedelta(0):
            raise ValueError(f"{name} must be UTC")
    if args.until - args.since > timedelta(days=MAX_WINDOW_DAYS):
        raise ValueError(f"maximum window width is {MAX_WINDOW_DAYS} days")
    if bool(args.working_tree_dirty) == bool(args.no_working_tree_dirty):
        raise ValueError("exactly one working-tree dirty flag is required")

    generated_at = now or datetime.now(UTC)
    maturity_cutoff = generated_at - timedelta(
        minutes=DECISION_LAG_MINUTES + OUTCOME_HORIZON_MINUTES
    )
    if args.until > maturity_cutoff:
        raise ValueError("outcome window is not fully mature")

    state_path = resolve_cohort_state_path(args.cohort_state_path, env=os.environ)
    accepted = load_accepted_cohort(state_path)
    requested = CohortBoundaries(
        discovery_end=args.discovery_end, validation_end=args.validation_end
    )
    boundaries, acceptance, changed = resolve_cohort_boundaries(
        requested=requested,
        accepted=accepted,
        accept_new_cohort=args.accept_new_cohort_boundary,
        now=generated_at,
    )

    owned_repository = repository is None
    repo = repository or LiquidationCascadeRepository.from_url(db_url)
    try:
        symbols = await repo.fetch_symbols_in_window(
            exchange=args.exchange, since=args.since, until=args.until
        )
        identity_lookup = await repo.fetch_identity_lookup(
            exchange=args.exchange, symbols=symbols, since=args.since, until=args.until
        )
        identity_stable = _identity_stability(identity_lookup, since=args.since)
        streamed = await _stream_symbols(
            repo,
            exchange=args.exchange,
            since=args.since,
            until=args.until,
            boundaries=boundaries,
            identity_stable=identity_stable,
            position_usd=POSITION_USD,
            symbols_in_window=len(symbols),
        )
        report = build_validation_report(
            reference_replays={
                segment: tuple(replays) for segment, replays in streamed.reference_replays.items()
            },
            grid_replays={
                cell: {segment: tuple(replays) for segment, replays in by_segment.items()}
                for cell, by_segment in streamed.grid_replays.items()
            },
            diagnostics=streamed.diagnostics,
            boundaries=boundaries,
            since=args.since,
            until=args.until,
            generated_at=generated_at,
            code_revision=args.code_revision,
            working_tree_dirty=args.working_tree_dirty,
        )
        payload = render_markdown(report) if args.format == "markdown" else render_json(report)
    finally:
        if owned_repository:
            await repo.close()

    if changed:
        save_accepted_cohort(state_path, acceptance)
    return payload


def main() -> None:
    args = _parse_args()
    try:
        sys.stdout.write(asyncio.run(_run(args)) + "\n")
    except ValueError as error:
        sys.stderr.write(f"ERROR: {error}\n")
        raise SystemExit(1) from error
    except Exception as error:
        sys.stderr.write(f"ERROR: {error}\n")
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
