"""analysis/liquidation-cascade-validation-v2 -- episode-level, chronologically
split validation of the liquidation-cascade entry rule
(apps/execution/schurfer_execution/liquidation_cascade.py), replacing the
recovered `feature/alpha-research` grid search (`583213f`, never merged),
which scored every triggering MINUTE independently (no episode grouping),
had no train/validation/test split, and ignored fees/funding/slippage.

Discovery -> validation -> untouched test, in that order:
  1. `liquidation_cascade_grid_search.run_grid_search` sweeps entry
     thresholds, purge-classified against the DISCOVERY segment.
  2. The discovery shortlist is re-scored, unchanged, against the
     VALIDATION segment (`rescore_cells`); the single best-on-validation
     cell becomes the candidate.
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
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
    MinuteObservation,
    episodes_for_threshold_segment,
    rescore_cells,
    run_grid_search,
    shortlist,
    to_minute_states,
)
from .liquidation_cascade_repository import (
    LOOKBACK_MINUTES,
    OI_DROP_TRIGGER_PCT,
    PRICE_DROP_TRIGGER_PCT,
    IdentityObservation,
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
RECOVERY_PRICE_PCT = 0.02
RECOVERY_OI_PCT = 0.05
COOLDOWN_MINUTES = 30
FEATURE_LOOKBACK_MINUTES = LOOKBACK_MINUTES
OUTCOME_HORIZON_MINUTES = RUNTIME_EXIT_POLICY.max_hold_minutes
# The earliest defensible causal entry instant -- see module doc.
DECISION_LAG_MINUTES = 1


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


def _identity_stability(observations: tuple[IdentityObservation, ...]) -> dict[str, bool]:
    """A symbol is stable only when EVERY identity observation seen for
    that ONE symbol across the analysis window reports `identity_status ==
    "ready"` and they all agree on one `identity_key` and one
    `onboarded_at`. `catalog_version` is deliberately NOT used for this
    comparison (colleague review, 2026-08-21): it hashes the WHOLE catalog
    snapshot, so any new listing on either venue changes it for every
    instrument, which would mark nearly every symbol unstable regardless of
    whether THAT symbol itself was ever delisted or relisted. A genuine
    delisted-and-relisted ticker under the same native market id instead
    changes its OWN `onboarded_at` (see momentum_universe_identity_
    repository.py's own module doc). A symbol with NO identity observation
    at all is not presumed stable -- it stays absent from this map, and
    callers must treat that the same as unresolved, never as an implicit
    pass."""
    by_symbol: dict[str, list[IdentityObservation]] = defaultdict(list)
    for observation in observations:
        by_symbol[observation.native_market_id].append(observation)
    return {
        symbol: (
            all(row.identity_status == "ready" for row in rows)
            and len({row.identity_key for row in rows}) == 1
            and len({row.onboarded_at for row in rows}) == 1
        )
        for symbol, rows in by_symbol.items()
    }


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
    projected_monthly = {
        str(int(notional)): (
            mean_return * notional / 100 * (len(fillable) / window_days) * 30
            if mean_return is not None and window_days > 0
            else None
        )
        for notional in PROJECTION_NOTIONALS_USD
    }

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
        projected_monthly_pnl_caveat=PROJECTION_CAVEAT,
        capacity_above_probe_usd=None,
        sensitivity=sensitivity,
    )


def _verdict(
    candidate_test_economics: SegmentEconomics | None,
    shuffled_control: ShuffledLabelControl,
) -> tuple[str, list[str]]:
    if candidate_test_economics is None:
        return "insufficient_data", ["no_validation_selected_candidate"]
    reasons: list[str] = []
    if candidate_test_economics.fillable_episodes < MIN_FORMAL_SAMPLE_EPISODES:
        reasons.append("insufficient_test_sample")
    if candidate_test_economics.fillable_distinct_utc_weeks < MIN_DISTINCT_UTC_WEEKS:
        reasons.append("fewer_than_four_distinct_utc_weeks")
    if candidate_test_economics.fillable_distinct_assets < MIN_FILLABLE_DISTINCT_ASSETS:
        reasons.append("fewer_than_min_fillable_assets")
    if reasons:
        return "insufficient_data", reasons
    if (
        candidate_test_economics.mean_net_return_pct is None
        or candidate_test_economics.mean_net_return_pct <= 0
    ):
        return "FAIL", ["test_net_ev_non_positive"]
    if any(value <= 0 for _, value in candidate_test_economics.sensitivity["leave_one_week_out"]):
        return "FAIL", ["fails_leave_one_week_out"]
    if any(value <= 0 for _, value in candidate_test_economics.sensitivity["leave_one_asset_out"]):
        return "FAIL", ["fails_leave_one_asset_out"]
    if (
        shuffled_control.empirical_p_value is None
        or shuffled_control.empirical_p_value >= SHUFFLED_LABEL_SIGNIFICANCE_THRESHOLD
    ):
        return "FAIL", ["shuffled_label_control_not_significant"]
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
    series for `symbol` (see `_build_replay_cache`). Entry is modeled at
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


async def _build_replay_cache(
    repository: LiquidationCascadeRepository,
    observations: tuple[MinuteObservation, ...],
    *,
    position_usd: float,
) -> dict[tuple[str, str, datetime], tuple[float | None, str | None]]:
    """One BULK bars fetch and one BULK quotes fetch covering every symbol
    that has at least one qualifying minute under the LOOSEST grid
    threshold combination, instead of up to three round trips PER
    qualifying minute (colleague review, 2026-08-21: the original per-
    minute fetch could reach thousands of round trips across a full
    analysis window). Every stricter combination's qualifying set --
    including the production reference combo and every grid cell -- is a
    strict subset of the loosest combo's qualifying set, so caching by
    (exchange, symbol, bucket_start) here guarantees a cache hit for every
    episode's own `trigger_at` that any cell or the reference rule could
    ever produce."""
    loosest_price = max(DEFAULT_PRICE_DROP_THRESHOLDS_PCT)
    loosest_oi = max(DEFAULT_OI_DROP_THRESHOLDS_PCT)
    loose_states = to_minute_states(
        observations, price_drop_trigger_pct=loosest_price, oi_drop_trigger_pct=loosest_oi
    )
    qualifying = [minute for minute in loose_states if minute.is_qualifying]
    if not qualifying:
        return {}

    exchange = qualifying[0].exchange
    symbols = sorted({minute.symbol for minute in qualifying})
    earliest_trigger = min(minute.bucket_start for minute in qualifying)
    latest_trigger = max(minute.bucket_start for minute in qualifying)
    fetch_since = earliest_trigger + timedelta(minutes=DECISION_LAG_MINUTES)
    fetch_until = latest_trigger + timedelta(
        minutes=DECISION_LAG_MINUTES + RUNTIME_EXIT_POLICY.max_hold_minutes + 5
    )

    bars_by_symbol = await repository.fetch_bars_for_symbols(
        exchange=exchange, symbols=symbols, since=fetch_since, until=fetch_until
    )
    quotes = await repository.fetch_quotes_for_symbols(
        exchange=exchange, symbols=symbols, since=fetch_since, until=fetch_until
    )

    cache: dict[tuple[str, str, datetime], tuple[float | None, str | None]] = {}
    for minute in qualifying:
        key = (minute.exchange, minute.symbol, minute.bucket_start)
        cache[key] = replay_from_minute(
            symbol=minute.symbol,
            trigger_at=minute.bucket_start,
            bars=bars_by_symbol.get(minute.symbol, ()),
            quotes=quotes,
            position_usd=position_usd,
        )
    return cache


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


def build_validation_report(
    *,
    observations: tuple[MinuteObservation, ...],
    replay_cache: Mapping[tuple[str, str, datetime], tuple[float | None, str | None]],
    identity_stable: Mapping[str, bool],
    boundaries: CohortBoundaries,
    since: datetime,
    until: datetime,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
) -> dict[str, Any]:
    def _replay_lookup(episodes: tuple[CascadeEpisode, ...]) -> tuple[EpisodeReplay, ...]:
        return _replays_from_cache(episodes, replay_cache, identity_stable=identity_stable)

    # Reference rule: descriptive context only, computed once over the FULL
    # window then purge-classified -- never the input to `_verdict`.
    reference_states = to_minute_states(
        observations,
        price_drop_trigger_pct=PRICE_DROP_TRIGGER_PCT,
        oi_drop_trigger_pct=OI_DROP_TRIGGER_PCT,
    )
    reference_episodes = decluster_cascade_episodes(
        reference_states,
        recovery_price_pct=RECOVERY_PRICE_PCT,
        recovery_oi_pct=RECOVERY_OI_PCT,
        cooldown_minutes=COOLDOWN_MINUTES,
    )
    by_segment = classify_episodes(
        reference_episodes,
        boundaries=boundaries,
        feature_lookback_minutes=FEATURE_LOOKBACK_MINUTES,
        outcome_horizon_minutes=OUTCOME_HORIZON_MINUTES,
    )
    segment_windows = {
        Segment.DISCOVERY: (since, boundaries.discovery_end),
        Segment.VALIDATION: (boundaries.discovery_end, boundaries.validation_end),
        Segment.TEST: (boundaries.validation_end, until),
    }
    reference_segment_economics = {
        segment.value: _segment_economics(
            segment, _replay_lookup(by_segment[segment]), since=window[0], until=window[1]
        )
        for segment, window in segment_windows.items()
    }

    grid_result = run_grid_search(
        observations=observations,
        replay_episodes=_replay_lookup,
        boundaries=boundaries,
        feature_lookback_minutes=FEATURE_LOOKBACK_MINUTES,
        outcome_horizon_minutes=OUTCOME_HORIZON_MINUTES,
        recovery_price_pct=RECOVERY_PRICE_PCT,
        recovery_oi_pct=RECOVERY_OI_PCT,
        cooldown_minutes=COOLDOWN_MINUTES,
    )
    discovery_shortlist = shortlist(grid_result)

    validation_cells = rescore_cells(
        observations=observations,
        cells=[
            (cell.price_drop_trigger_pct, cell.oi_drop_trigger_pct) for cell in discovery_shortlist
        ],
        replay_episodes=_replay_lookup,
        boundaries=boundaries,
        target_segment=Segment.VALIDATION,
        feature_lookback_minutes=FEATURE_LOOKBACK_MINUTES,
        outcome_horizon_minutes=OUTCOME_HORIZON_MINUTES,
        recovery_price_pct=RECOVERY_PRICE_PCT,
        recovery_oi_pct=RECOVERY_OI_PCT,
        cooldown_minutes=COOLDOWN_MINUTES,
    )
    validation_ready = [cell for cell in validation_cells if cell.formal_sample_ready]
    candidate = min(validation_ready, key=_candidate_ranking_key) if validation_ready else None

    candidate_test_economics: SegmentEconomics | None = None
    if candidate is not None:
        candidate_test_episodes = episodes_for_threshold_segment(
            observations,
            price_drop_trigger_pct=candidate.price_drop_trigger_pct,
            oi_drop_trigger_pct=candidate.oi_drop_trigger_pct,
            boundaries=boundaries,
            target_segment=Segment.TEST,
            feature_lookback_minutes=FEATURE_LOOKBACK_MINUTES,
            outcome_horizon_minutes=OUTCOME_HORIZON_MINUTES,
            recovery_price_pct=RECOVERY_PRICE_PCT,
            recovery_oi_pct=RECOVERY_OI_PCT,
            cooldown_minutes=COOLDOWN_MINUTES,
        )
        candidate_test_economics = _segment_economics(
            Segment.TEST,
            _replay_lookup(candidate_test_episodes),
            since=boundaries.validation_end,
            until=until,
        )

    shuffled_control = shuffled_label_control(
        grid_result, min_formal_sample_episodes=MIN_FORMAL_SAMPLE_EPISODES
    )

    verdict, verdict_reasons = _verdict(candidate_test_economics, shuffled_control)

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
        "reference_rule_segments": {
            key: _asdict_segment(value) for key, value in reference_segment_economics.items()
        },
        "grid_search": {
            "discovery_cells": len(grid_result.cells),
            "discovery_leaderboard_top10": [_asdict_cell(cell) for cell in grid_result.cells[:10]],
            "discovery_shortlist": [_asdict_cell(cell) for cell in discovery_shortlist],
            "validation_rescoring": [_asdict_cell(cell) for cell in validation_cells],
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
        "## Reference-rule segments (context only, not the gate)",
        "",
    ]
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
        f"## Candidate (validation-selected): {report['grid_search']['candidate']}",
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
        observations = await repo.fetch_minute_observations(
            exchange=args.exchange, since=args.since, until=args.until
        )
        replay_cache = await _build_replay_cache(repo, observations, position_usd=POSITION_USD)
        symbols = sorted({observation.symbol for observation in observations})
        identity_observations = await repo.fetch_identity_observations(
            exchange=args.exchange, symbols=symbols, since=args.since, until=args.until
        )
        identity_stable = _identity_stability(identity_observations)
        report = build_validation_report(
            observations=observations,
            replay_cache=replay_cache,
            identity_stable=identity_stable,
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
