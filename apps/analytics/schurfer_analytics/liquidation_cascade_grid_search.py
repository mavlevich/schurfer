"""Discovery-only grid search over liquidation-cascade entry thresholds.

The recovered `feature/alpha-research` grid search (`583213f`, never merged)
swept `price_drop_thresholds x oi_drop_thresholds` -- the same two axes
reused here, since they are the only free parameters in the causal entry
rule. Exit stays fixed to the runtime's own SL 3% / TP 5% / max-hold 60min
policy (`liquidation_cascade_exit.py`): the live strategy has no tunable
exit today, so grid-searching a second free parameter here would validate a
rule the runtime doesn't actually run.

Every threshold combination is declustered and PURGE-classified against the
full analysis window before scoring (`episodes_for_threshold_segment`,
`classify_episodes`) -- not by pre-slicing `observations` on raw
`bucket_start` before declustering. A raw bucket_start slice would let an
episode whose trigger sits minutes before `discovery_end` borrow outcome
data from the validation segment (colleague review, 2026-08-21): its own
feature-lookback/outcome-horizon footprint would cross the boundary and it
would still be scored as if it were a clean discovery observation. Grid
cells and the validation/test rescoring below all reuse the SAME purge rule
the reference rule's own segment classification already used.

`replay_episodes` is injected (mirrors the `repository=` dependency-
injection convention used throughout this package's report tests) so this
module stays DB-free and independently unit-testable: the caller owns
fetching each episode's bar-by-bar outcome path and running
`liquidation_cascade_exit.simulate_exit`, and owns folding data-quality and
identity eligibility into `EpisodeReplay.unresolved_reason` BEFORE handing
episodes back here -- this module trusts `net_return_pct is not None` as the
single source of truth for "counts toward the mean", so eligibility can
never silently diverge between the grid search and the final reported
economics (colleague review, 2026-08-21).
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from statistics import fmean
from typing import TYPE_CHECKING

from .liquidation_cascade_cohort_split import CohortBoundaries, Segment, classify_episodes
from .liquidation_cascade_episodes import CascadeEpisode, MinuteState, decluster_cascade_episodes
from .reporting import profit_factor

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import datetime

DEFAULT_PRICE_DROP_THRESHOLDS_PCT: tuple[float, ...] = (-0.03, -0.05, -0.07, -0.10, -0.15)
DEFAULT_OI_DROP_THRESHOLDS_PCT: tuple[float, ...] = (0.0, -0.05, -0.10, -0.15, -0.20, -0.25)
# Matches the project's existing N>=10 materiality-floor precedent
# (challenger_inference's minimum_triggered_episodes gate, first applied to
# HYP-013's own MIN_CHANGED_TRADES=10) -- a cell under this floor is
# excluded from shortlisting regardless of how good its mean looks.
MIN_FORMAL_SAMPLE_EPISODES = 10
SHORTLIST_SIZE = 5


@dataclass(frozen=True)
class MinuteObservation:
    """Threshold-independent per-minute state: the raw price/OI-drop ratios
    a grid cell re-thresholds via `to_minute_states`, never baking in one
    fixed trigger combination the way `MinuteState.is_qualifying` does."""

    exchange: str
    symbol: str
    bucket_start: datetime
    price_drop_pct: float
    oi_drop_pct: float
    price_complete: bool
    open_interest_complete: bool


def to_minute_states(
    observations: Sequence[MinuteObservation],
    *,
    price_drop_trigger_pct: float,
    oi_drop_trigger_pct: float,
) -> tuple[MinuteState, ...]:
    return tuple(
        MinuteState(
            exchange=observation.exchange,
            symbol=observation.symbol,
            bucket_start=observation.bucket_start,
            price_drop_pct=observation.price_drop_pct,
            oi_drop_pct=observation.oi_drop_pct,
            is_qualifying=(
                observation.price_drop_pct <= price_drop_trigger_pct
                and observation.oi_drop_pct <= oi_drop_trigger_pct
            ),
            price_complete=observation.price_complete,
            open_interest_complete=observation.open_interest_complete,
        )
        for observation in observations
    )


def episodes_for_threshold_segment(
    observations: Sequence[MinuteObservation],
    *,
    price_drop_trigger_pct: float,
    oi_drop_trigger_pct: float,
    boundaries: CohortBoundaries,
    target_segment: Segment,
    feature_lookback_minutes: int,
    outcome_horizon_minutes: int,
    recovery_price_pct: float,
    recovery_oi_pct: float,
    cooldown_minutes: int,
) -> tuple[CascadeEpisode, ...]:
    """Decluster ONE threshold combination against the FULL analysis window
    (`observations` must span since..until, never a pre-sliced segment),
    then return only the episodes whose complete feature-lookback/outcome-
    horizon footprint lands in `target_segment` -- purge-excluded episodes
    are dropped here, not scored under any segment.

    Declusters once and discards the other segments -- fine for a single
    target segment, but a caller that needs more than one segment for the
    same threshold pair (e.g. streaming all three at once per symbol)
    should call `episodes_for_threshold_all_segments` instead and avoid
    redeclustering the same minutes per segment."""
    by_segment = episodes_for_threshold_all_segments(
        observations,
        price_drop_trigger_pct=price_drop_trigger_pct,
        oi_drop_trigger_pct=oi_drop_trigger_pct,
        boundaries=boundaries,
        feature_lookback_minutes=feature_lookback_minutes,
        outcome_horizon_minutes=outcome_horizon_minutes,
        recovery_price_pct=recovery_price_pct,
        recovery_oi_pct=recovery_oi_pct,
        cooldown_minutes=cooldown_minutes,
    )
    return by_segment[target_segment]


def episodes_for_threshold_all_segments(
    observations: Sequence[MinuteObservation],
    *,
    price_drop_trigger_pct: float,
    oi_drop_trigger_pct: float,
    boundaries: CohortBoundaries,
    feature_lookback_minutes: int,
    outcome_horizon_minutes: int,
    recovery_price_pct: float,
    recovery_oi_pct: float,
    cooldown_minutes: int,
) -> dict[Segment, tuple[CascadeEpisode, ...]]:
    """Same declustering as `episodes_for_threshold_segment`, but returns
    every segment from the ONE declustering pass instead of discarding all
    but one -- a caller that needs discovery, validation, AND test economics
    for the same threshold pair (the streaming per-symbol path in
    `liquidation_cascade_validation_report.py`) must not redecluster the
    same minutes three times per pair."""
    states = to_minute_states(
        observations,
        price_drop_trigger_pct=price_drop_trigger_pct,
        oi_drop_trigger_pct=oi_drop_trigger_pct,
    )
    episodes = decluster_cascade_episodes(
        states,
        recovery_price_pct=recovery_price_pct,
        recovery_oi_pct=recovery_oi_pct,
        cooldown_minutes=cooldown_minutes,
    )
    return classify_episodes(
        episodes,
        boundaries=boundaries,
        feature_lookback_minutes=feature_lookback_minutes,
        outcome_horizon_minutes=outcome_horizon_minutes,
    )


@dataclass(frozen=True)
class EpisodeReplay:
    episode: CascadeEpisode
    net_return_pct: float | None
    unresolved_reason: str | None

    def __post_init__(self) -> None:
        if (self.net_return_pct is None) == (self.unresolved_reason is None):
            raise ValueError("exactly one of net_return_pct/unresolved_reason must be set")

    @property
    def episode_key(self) -> tuple[str, str, datetime]:
        return (self.episode.exchange, self.episode.symbol, self.episode.trigger_at)


@dataclass(frozen=True)
class GridCell:
    price_drop_trigger_pct: float
    oi_drop_trigger_pct: float
    episodes: int
    resolved_episodes: int
    unresolved_episodes: int
    distinct_assets: int
    mean_net_return_pct: float | None
    profit_factor: float | None
    formal_sample_ready: bool


@dataclass(frozen=True)
class GridSearchResult:
    cells: tuple[GridCell, ...]
    # Keyed by (price_drop_trigger_pct, oi_drop_trigger_pct); values are the
    # stable (exchange, symbol, trigger_at) identity of each member episode
    # -- not the raw `episode_id` int, which `decluster_cascade_episodes`
    # restarts at `start_id` on every call and so is not stable across
    # cells. See liquidation_cascade_statistics.py for why this identity is
    # what the shuffled-label control needs.
    cell_membership: Mapping[tuple[float, float], tuple[tuple[str, str, datetime], ...]]
    episode_returns: Mapping[tuple[str, str, datetime], float]


def _ranking_key(cell: GridCell) -> tuple[bool, float, float, float]:
    """`mean_net_return_pct or ...` silently mis-ranks a genuinely zero mean
    as if it were negative infinity, since `0.0` is falsy in Python -- a
    losing cell at -0.001% would then outrank a flat 0.0% cell (colleague
    review, 2026-08-21). Compare against `None` explicitly instead."""
    if cell.mean_net_return_pct is None:
        return (True, 0.0, cell.price_drop_trigger_pct, cell.oi_drop_trigger_pct)
    return (
        False,
        -cell.mean_net_return_pct,
        cell.price_drop_trigger_pct,
        cell.oi_drop_trigger_pct,
    )


def score_grid_cell(
    price_drop_trigger_pct: float,
    oi_drop_trigger_pct: float,
    replays: tuple[EpisodeReplay, ...],
    *,
    min_formal_sample_episodes: int = MIN_FORMAL_SAMPLE_EPISODES,
) -> GridCell:
    resolved = [replay for replay in replays if replay.net_return_pct is not None]
    returns = [replay.net_return_pct for replay in resolved if replay.net_return_pct is not None]
    return GridCell(
        price_drop_trigger_pct=price_drop_trigger_pct,
        oi_drop_trigger_pct=oi_drop_trigger_pct,
        episodes=len(replays),
        resolved_episodes=len(resolved),
        unresolved_episodes=len(replays) - len(resolved),
        distinct_assets=len({replay.episode.symbol for replay in replays}),
        mean_net_return_pct=fmean(returns) if returns else None,
        profit_factor=profit_factor(returns) if returns else None,
        formal_sample_ready=len(resolved) >= min_formal_sample_episodes,
    )


def rescore_cells(
    *,
    observations: Sequence[MinuteObservation],
    cells: Sequence[tuple[float, float]],
    replay_episodes: Callable[[tuple[CascadeEpisode, ...]], tuple[EpisodeReplay, ...]],
    boundaries: CohortBoundaries,
    target_segment: Segment,
    feature_lookback_minutes: int,
    outcome_horizon_minutes: int,
    recovery_price_pct: float,
    recovery_oi_pct: float,
    cooldown_minutes: int,
    min_formal_sample_episodes: int = MIN_FORMAL_SAMPLE_EPISODES,
) -> tuple[GridCell, ...]:
    """Re-score an explicit list of (price, oi) threshold pairs against one
    purge-aware segment -- used for validation-only re-scoring of the
    discovery shortlist, and for scoring the single selected candidate
    against the untouched test segment. Shares `episodes_for_threshold_
    segment` with the discovery sweep so the purge rule is identical on
    every segment, never re-implemented per caller."""
    results = []
    for price_thresh, oi_thresh in cells:
        segment_episodes = episodes_for_threshold_segment(
            observations,
            price_drop_trigger_pct=price_thresh,
            oi_drop_trigger_pct=oi_thresh,
            boundaries=boundaries,
            target_segment=target_segment,
            feature_lookback_minutes=feature_lookback_minutes,
            outcome_horizon_minutes=outcome_horizon_minutes,
            recovery_price_pct=recovery_price_pct,
            recovery_oi_pct=recovery_oi_pct,
            cooldown_minutes=cooldown_minutes,
        )
        replays = replay_episodes(segment_episodes)
        results.append(
            score_grid_cell(
                price_thresh,
                oi_thresh,
                replays,
                min_formal_sample_episodes=min_formal_sample_episodes,
            )
        )
    return tuple(results)


def run_grid_search(
    *,
    observations: Sequence[MinuteObservation],
    replay_episodes: Callable[[tuple[CascadeEpisode, ...]], tuple[EpisodeReplay, ...]],
    boundaries: CohortBoundaries,
    feature_lookback_minutes: int,
    outcome_horizon_minutes: int,
    recovery_price_pct: float,
    recovery_oi_pct: float,
    cooldown_minutes: int,
    price_drop_thresholds_pct: tuple[float, ...] = DEFAULT_PRICE_DROP_THRESHOLDS_PCT,
    oi_drop_thresholds_pct: tuple[float, ...] = DEFAULT_OI_DROP_THRESHOLDS_PCT,
    min_formal_sample_episodes: int = MIN_FORMAL_SAMPLE_EPISODES,
) -> GridSearchResult:
    """`observations` must be the FULL since..until window -- this function
    purge-classifies each cell down to `Segment.DISCOVERY` itself; it must
    never be handed an already-sliced discovery-only observation set (that
    would double-apply, or worse, entirely bypass, the purge rule)."""
    cells: list[GridCell] = []
    membership: dict[tuple[float, float], tuple[tuple[str, str, datetime], ...]] = {}
    episode_returns: dict[tuple[str, str, datetime], float] = {}

    for price_thresh, oi_thresh in product(price_drop_thresholds_pct, oi_drop_thresholds_pct):
        segment_episodes = episodes_for_threshold_segment(
            observations,
            price_drop_trigger_pct=price_thresh,
            oi_drop_trigger_pct=oi_thresh,
            boundaries=boundaries,
            target_segment=Segment.DISCOVERY,
            feature_lookback_minutes=feature_lookback_minutes,
            outcome_horizon_minutes=outcome_horizon_minutes,
            recovery_price_pct=recovery_price_pct,
            recovery_oi_pct=recovery_oi_pct,
            cooldown_minutes=cooldown_minutes,
        )
        replays = replay_episodes(segment_episodes)
        cells.append(
            score_grid_cell(
                price_thresh,
                oi_thresh,
                replays,
                min_formal_sample_episodes=min_formal_sample_episodes,
            )
        )
        keys: list[tuple[str, str, datetime]] = []
        for replay in replays:
            key = replay.episode_key
            keys.append(key)
            if replay.net_return_pct is not None:
                episode_returns[key] = replay.net_return_pct
        membership[(price_thresh, oi_thresh)] = tuple(keys)

    ordered = tuple(sorted(cells, key=_ranking_key))
    return GridSearchResult(
        cells=ordered,
        cell_membership=membership,
        episode_returns=episode_returns,
    )


def shortlist(result: GridSearchResult, *, k: int = SHORTLIST_SIZE) -> tuple[GridCell, ...]:
    """Top-K discovery-only cells eligible to move to validation-only
    re-scoring. Validation never participates in this ranking."""
    eligible = [cell for cell in result.cells if cell.formal_sample_ready]
    ranked = sorted(eligible, key=_ranking_key)
    return tuple(ranked[:k])
