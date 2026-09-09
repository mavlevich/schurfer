"""research/orderflow-microstructure-v1 (HYP-024) -- pure quintile/tie/verdict
logic for the single registered taker-imbalance statistic.

No I/O here. The repository (`orderflow_microstructure_repository.py`) does
the SQL-side aggregation and identity resolution; the report CLI
(`orderflow_microstructure_report.py`) enforces the held-out cutoff and
renders. Every function in this module is a pure transformation over
already-fetched, per-decision rows, so the quintile/tie/decision-rule logic is
fully unit-testable without a database.

Frozen contract, transcribed from docs/research/orderflow-microstructure-v1.md
and its 2026-09-08/2026-09-09 amendments. Nothing here may be retuned after a
result is seen: a parameter change is a new hypothesis id with an untouched
held-out window, not an edit to this file.

- ONE statistic, chosen before looking: taker imbalance summed over the ten
  1-minute bars BEFORE the decision, per bar `(sell - buy) / (sell + buy)`.
  Positive means sellers dominated. The 5- and 20-minute variants exist ONLY
  as context and never replace the registered 10-minute measure.
- Primary metric: the spread in MEDIAN net short return between the TOP and
  BOTTOM quintile of the 10-minute taker imbalance, net of the shared cost
  model, at the 60-minute horizon.
- Decision rule, evaluated in this exact order:
    1. Sufficiency floor binds FIRST. Below 150 episodes in either compared
       quintile, or below 30 asset clusters across the compared quintiles,
       the verdict is `inconclusive` -- never a rejection.
    2. `candidate` if the top-minus-bottom spread exceeds 1.5 percentage
       points AND the five quintile medians are monotone AND (Rule 6) every
       adjacent pair of compared buckets is actually separated in the feature.
    3. `rejected` if the absolute spread is under 0.5 points and the floor is
       met.
    4. `inconclusive` otherwise.
- Rule 6 (2026-09-09 amendment): a quintile boundary falling inside a tied
  value splits equal measurements by sort order. A candidate requires every
  adjacent pair of compared buckets to differ in the feature, and every report
  states the distinct-value count and the largest tied group.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from statistics import median
from typing import Any

from schurfer_performance import COST_MODEL_VERSION, DEFAULT_COSTS, CostParameters

# --- Frozen contract ----------------------------------------------------

REPORT_VERSION = "orderflow_microstructure_v1"
HYPOTHESIS_ID = "HYP-024"

# pump_short identifies itself through strategy_version alone (see
# pump_short_reentry_audit_report.REENTRY_AUDIT_STRATEGY_VERSIONS and the
# decision.py note that side/strategy_id/trading_mode are NULL on every
# pump_short row). The short direction is intrinsic to the strategy and is
# read through the outcome's short_return_pct, never through a side filter.
STRATEGY_VERSION = "pump_short_v1_market_quality"

# The strategy-agnostic forward resolver that writes short_return_pct/mfe/mae
# (schurfer_analytics.outcomes.RESOLVER_VERSION). Pinned so a decision with
# outcome rows under more than one resolver version can never be double
# counted in the join.
RESOLVER_VERSION = "forward_v1"
HORIZON_MINUTES = 60

# Discovery window, inclusive start / exclusive end. The held-out window
# (2026-08-25 onward) MUST NOT be read in this pass; the report refuses a
# --cohort-end past HELD_OUT_START.
DISCOVERY_START = datetime(2026, 8, 10, tzinfo=UTC)
HELD_OUT_START = datetime(2026, 8, 25, tzinfo=UTC)

# The registered measure and its context-only siblings.
PRIMARY_LOOKBACK_MINUTES = 10
CONTEXT_LOOKBACK_MINUTES: tuple[int, ...] = (5, 20)

QUINTILE_COUNT = 5
MIN_EPISODES_PER_QUINTILE = 150
MIN_ASSET_CLUSTERS = 30
CANDIDATE_SPREAD_PP = 1.5
REJECT_SPREAD_PP = 0.5

# The shared conservative cost model, applied to the raw forward short return
# to make it net. Funding scales with the fixed 60-minute hold; two-sided
# taker fees are flat. Slippage is NOT modeled here: the forward-outcome path
# carries no fills or order-book depth, so no per-decision slippage exists to
# subtract. Because the deduction is identical for every episode at this fixed
# horizon, it is a constant offset that cancels out of the top-minus-bottom
# spread; it changes only the absolute per-quintile median levels. This is a
# stated assumption -- see the result stub -- not a silent omission.
COST_PARAMETERS: CostParameters = DEFAULT_COSTS


def cost_pct_at_horizon(
    horizon_minutes: int = HORIZON_MINUTES, costs: CostParameters = COST_PARAMETERS
) -> float:
    """Deterministic fee+funding deduction in percentage points for one
    round-trip position held `horizon_minutes`, from the shared cost model."""
    fee_cost_bps = 2 * costs.taker_fee_bps_per_side
    funding_cost_bps = costs.funding_cost_bps_per_8h * horizon_minutes / 480
    return (fee_cost_bps + funding_cost_bps) / 100.0


def net_short_return_pct(gross_short_return_pct: float) -> float:
    """Apply the shared cost model to a raw forward short return."""
    return gross_short_return_pct - cost_pct_at_horizon()


VERDICT_INCONCLUSIVE = "inconclusive"
VERDICT_CANDIDATE = "candidate"
VERDICT_REJECTED = "rejected"


# --- Per-decision measured row (exactly what the repository yields) ------


@dataclass(frozen=True)
class MeasuredEpisode:
    """One resolved decision that cleared coverage: it has a complete 60m
    outcome AND a complete ten-bar pre-window on its own venue. `cluster_key`
    is the asset cluster (the exchange-independent `base`, which merges the
    same asset across bybit/binance into one cluster for the diversity floor).
    Returns are in percentage points; the feature values are the summed
    per-bar taker imbalances over the respective lookbacks."""

    decision_id: str
    cluster_key: str
    exchange: str
    ts: datetime
    taker_imbalance_10m: float
    taker_imbalance_5m: float | None
    taker_imbalance_20m: float | None
    gross_short_return_pct: float
    net_short_return_pct: float
    mfe_pct: float | None
    mae_pct: float | None


# --- Raw resolved-decision row (exactly what the repository fetches) -----


@dataclass(frozen=True)
class ResolvedDecisionRow:
    """One EPISODE -- the representative decision per `pump_event_id` -- after
    SQL-side outcome qualification, identity resolution and bar aggregation.
    `outcome_qualified` is True only when the representative decision has its
    OWN complete, same-venue 60m outcome (`short_return_pct` is then non-None);
    False means no qualifying outcome (a counted coverage step, never a
    negative). `match_count` is the number of distinct native market ids the
    decision's (exchange, base) resolved to in the point-in-time snapshot: 0 =
    identity unresolved, 1 = resolved, >1 = ambiguous (fail closed -- both
    non-1 cases are coverage loss, never a negative outcome). `bars_10m/5m/20m`
    count the complete (trades_complete) AND already-received bars found in
    each pre-decision window; a window is usable only when its full bar count
    is present. Imbalance sums are the summed per-bar `(sell-buy)/(sell+buy)`
    over those bars, or None when that window's bars were incomplete or the
    episode never reached bar aggregation."""

    decision_id: str
    pump_event_id: str
    base: str
    exchange: str
    ts: datetime
    outcome_qualified: bool
    short_return_pct: float | None
    mfe_pct: float | None
    mae_pct: float | None
    match_count: int
    native_market_id: str | None
    market_type: str | None
    bars_10m: int
    bars_5m: int
    bars_20m: int
    imbalance_10m: float | None
    imbalance_5m: float | None
    imbalance_20m: float | None


# --- Coverage funnel (per-exchange, decisions -> measured episodes) ------


@dataclass(frozen=True)
class CoverageFunnelStep:
    step: int
    label: str
    remaining: int
    excluded: int
    exclusion_reason: str | None


@dataclass(frozen=True)
class ExchangeCoverage:
    exchange: str
    episodes: int
    with_complete_outcome: int
    identity_resolved: int
    measured_episodes: int
    no_complete_outcome: int
    unresolved_identity: int
    ambiguous_identity: int
    missing_or_incomplete_bars: int


@dataclass(frozen=True)
class CoverageResult:
    measured: tuple[MeasuredEpisode, ...]
    funnel: tuple[CoverageFunnelStep, ...]
    by_exchange: tuple[ExchangeCoverage, ...]


_FULL_BAR_COUNT = {5: 5, 10: 10, 20: 20}


def _has_complete_outcome(r: ResolvedDecisionRow) -> bool:
    return r.outcome_qualified and r.short_return_pct is not None


def _identity_resolved(r: ResolvedDecisionRow) -> bool:
    return r.match_count == 1 and r.native_market_id is not None


def _bars_complete(r: ResolvedDecisionRow) -> bool:
    return r.bars_10m == _FULL_BAR_COUNT[10] and r.imbalance_10m is not None


def build_coverage(rows: tuple[ResolvedDecisionRow, ...]) -> CoverageResult:
    """Pure episodes -> measured-episode funnel, so the coverage/fail-closed
    rules are unit-testable without a database. Each row is already one EPISODE
    (the representative decision per pump_event_id). An episode becomes a
    measured episode only when it has its own complete, same-venue 60m outcome
    AND its identity resolved to exactly one native market AND its full ten-bar
    pre-window is complete and was available at decision time. Every drop is an
    explicit, counted coverage step (never a silent WHERE-clause disappearance)
    and is reported per exchange; a missing/incomplete outcome, an
    unresolved/ambiguous identity, or a missing/unavailable ten-bar window is
    coverage loss, NOT a negative outcome."""
    all_rows = list(rows)
    with_outcome = [r for r in all_rows if _has_complete_outcome(r)]
    identity_resolved = [r for r in with_outcome if _identity_resolved(r)]
    bars_complete = [r for r in identity_resolved if _bars_complete(r)]

    measured = tuple(
        MeasuredEpisode(
            decision_id=r.decision_id,
            cluster_key=r.base,
            exchange=r.exchange,
            ts=r.ts,
            taker_imbalance_10m=r.imbalance_10m,  # type: ignore[arg-type]
            taker_imbalance_5m=(r.imbalance_5m if r.bars_5m == _FULL_BAR_COUNT[5] else None),
            taker_imbalance_20m=(r.imbalance_20m if r.bars_20m == _FULL_BAR_COUNT[20] else None),
            gross_short_return_pct=r.short_return_pct,  # type: ignore[arg-type]
            net_short_return_pct=net_short_return_pct(r.short_return_pct),  # type: ignore[arg-type]
            mfe_pct=r.mfe_pct,
            mae_pct=r.mae_pct,
        )
        for r in bars_complete
    )

    funnel = (
        CoverageFunnelStep(1, "episodes", len(all_rows), 0, None),
        CoverageFunnelStep(
            2,
            "with_complete_same_venue_60m_outcome",
            len(with_outcome),
            len(all_rows) - len(with_outcome),
            "no_complete_same_venue_outcome",
        ),
        CoverageFunnelStep(
            3,
            "identity_resolved_to_single_native_market",
            len(identity_resolved),
            len(with_outcome) - len(identity_resolved),
            "unresolved_or_ambiguous_identity",
        ),
        CoverageFunnelStep(
            4,
            "complete_available_ten_bar_pre_window",
            len(bars_complete),
            len(identity_resolved) - len(bars_complete),
            "missing_incomplete_or_unavailable_bars",
        ),
        CoverageFunnelStep(5, "measured_episodes", len(measured), 0, None),
    )

    by_exchange: list[ExchangeCoverage] = []
    for exchange in sorted({r.exchange for r in all_rows}):
        scoped = [r for r in all_rows if r.exchange == exchange]
        outcome_here = [r for r in scoped if _has_complete_outcome(r)]
        resolved = [r for r in outcome_here if _identity_resolved(r)]
        measured_here = [r for r in resolved if _bars_complete(r)]
        by_exchange.append(
            ExchangeCoverage(
                exchange=exchange,
                episodes=len(scoped),
                with_complete_outcome=len(outcome_here),
                identity_resolved=len(resolved),
                measured_episodes=len(measured_here),
                no_complete_outcome=len(scoped) - len(outcome_here),
                unresolved_identity=sum(1 for r in outcome_here if r.match_count == 0),
                ambiguous_identity=sum(1 for r in outcome_here if r.match_count > 1),
                missing_or_incomplete_bars=len(resolved) - len(measured_here),
            )
        )

    return CoverageResult(measured=measured, funnel=funnel, by_exchange=tuple(by_exchange))


# --- Quintile statistics -------------------------------------------------


@dataclass(frozen=True)
class QuintileStat:
    index: int  # 1..5, 1 = lowest taker imbalance, 5 = highest
    episodes: int
    distinct_clusters: int
    feature_min: float
    feature_max: float
    median_net_return_pct: float | None
    median_gross_return_pct: float | None
    median_mfe_pct: float | None
    median_mae_pct: float | None


@dataclass(frozen=True)
class TieDiagnostics:
    """Rule 6 evidence. `distinct_value_count`/`largest_tied_group` describe
    the whole measured feature column; `adjacent_boundaries_distinct` is True
    only when every adjacent quintile pair is actually separated in the
    feature (no tied value straddles a boundary)."""

    total_episodes: int
    distinct_value_count: int
    largest_tied_group: int
    adjacent_boundaries_distinct: bool
    tied_boundary_pairs: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class QuintileAnalysis:
    lookback_minutes: int
    quintiles: tuple[QuintileStat, ...]
    top_minus_bottom_median_spread_pp: float | None
    monotone_increasing: bool
    monotone_decreasing: bool
    ties: TieDiagnostics


def _median_or_none(values: list[float]) -> float | None:
    return median(values) if values else None


def _quintile_slices(count: int, buckets: int = QUINTILE_COUNT) -> list[tuple[int, int]]:
    """Contiguous, as-equal-as-possible rank ranges over `count` sorted
    items. The first `count % buckets` buckets take one extra item, so the
    split is deterministic and total-covering (Rule 6 acknowledges that a tie
    straddling a boundary is then split purely by sort order -- that split is
    made visible by TieDiagnostics rather than silently smoothed)."""
    base, extra = divmod(count, buckets)
    slices: list[tuple[int, int]] = []
    start = 0
    for i in range(buckets):
        size = base + (1 if i < extra else 0)
        slices.append((start, start + size))
        start += size
    return slices


def _tie_diagnostics(
    ordered_features: list[float], slices: list[tuple[int, int]]
) -> TieDiagnostics:
    total = len(ordered_features)
    distinct = len(set(ordered_features))
    largest_tied = 0
    if ordered_features:
        run = 1
        for i in range(1, total):
            if ordered_features[i] == ordered_features[i - 1]:
                run += 1
            else:
                largest_tied = max(largest_tied, run)
                run = 1
        largest_tied = max(largest_tied, run)

    tied_pairs: list[tuple[int, int]] = []
    for lower_index in range(len(slices) - 1):
        lower = slices[lower_index]
        upper = slices[lower_index + 1]
        if lower[1] <= lower[0] or upper[1] <= upper[0]:
            # An empty bucket cannot be "distinct" from its neighbour.
            tied_pairs.append((lower_index + 1, lower_index + 2))
            continue
        last_of_lower = ordered_features[lower[1] - 1]
        first_of_upper = ordered_features[upper[0]]
        if last_of_lower >= first_of_upper:
            tied_pairs.append((lower_index + 1, lower_index + 2))
    return TieDiagnostics(
        total_episodes=total,
        distinct_value_count=distinct,
        largest_tied_group=largest_tied,
        adjacent_boundaries_distinct=not tied_pairs,
        tied_boundary_pairs=tuple(tied_pairs),
    )


def _feature_of(episode: MeasuredEpisode, lookback_minutes: int) -> float | None:
    if lookback_minutes == 10:
        return episode.taker_imbalance_10m
    if lookback_minutes == 5:
        return episode.taker_imbalance_5m
    if lookback_minutes == 20:
        return episode.taker_imbalance_20m
    raise ValueError(f"unsupported lookback: {lookback_minutes}")


def compute_quintiles(
    episodes: tuple[MeasuredEpisode, ...], *, lookback_minutes: int
) -> QuintileAnalysis:
    """Rank episodes by the given lookback's taker imbalance and split into
    five contiguous buckets. Episodes with no feature at this lookback (a
    context window that could not be measured) are dropped from THIS analysis
    only; the registered 10-minute measure never has a missing feature by
    construction (a measured episode requires the full ten-bar pre-window)."""
    usable = [
        (feature, e) for e in episodes if (feature := _feature_of(e, lookback_minutes)) is not None
    ]
    usable.sort(key=lambda pair: (pair[0], pair[1].decision_id))
    ordered_features = [feature for feature, _ in usable]
    ordered_episodes = [e for _, e in usable]
    count = len(ordered_episodes)
    slices = _quintile_slices(count) if count else []
    ties = _tie_diagnostics(ordered_features, slices)

    quintiles: list[QuintileStat] = []
    medians: list[float | None] = []
    for i, (start, end) in enumerate(slices, start=1):
        bucket = ordered_episodes[start:end]
        bucket_features = ordered_features[start:end]
        net_returns = [e.net_short_return_pct for e in bucket]
        median_net = _median_or_none(net_returns)
        medians.append(median_net)
        quintiles.append(
            QuintileStat(
                index=i,
                episodes=len(bucket),
                distinct_clusters=len({e.cluster_key for e in bucket}),
                feature_min=bucket_features[0] if bucket_features else float("nan"),
                feature_max=bucket_features[-1] if bucket_features else float("nan"),
                median_net_return_pct=median_net,
                median_gross_return_pct=_median_or_none([e.gross_short_return_pct for e in bucket]),
                median_mfe_pct=_median_or_none(
                    [e.mfe_pct for e in bucket if e.mfe_pct is not None]
                ),
                median_mae_pct=_median_or_none(
                    [e.mae_pct for e in bucket if e.mae_pct is not None]
                ),
            )
        )

    spread: float | None = None
    monotone_increasing = False
    monotone_decreasing = False
    if len(quintiles) == QUINTILE_COUNT and all(m is not None for m in medians):
        concrete = [m for m in medians if m is not None]
        spread = concrete[-1] - concrete[0]
        monotone_increasing = all(concrete[i] <= concrete[i + 1] for i in range(len(concrete) - 1))
        monotone_decreasing = all(concrete[i] >= concrete[i + 1] for i in range(len(concrete) - 1))

    return QuintileAnalysis(
        lookback_minutes=lookback_minutes,
        quintiles=tuple(quintiles),
        top_minus_bottom_median_spread_pp=spread,
        monotone_increasing=monotone_increasing,
        monotone_decreasing=monotone_decreasing,
        ties=ties,
    )


def compared_distinct_clusters(
    episodes: tuple[MeasuredEpisode, ...], primary: QuintileAnalysis
) -> int:
    """Exact union of asset clusters across the top and bottom 10-minute
    quintiles, recomputed from the measured episodes in the same rank order
    the quintiles used. Summing the two per-bucket distinct counts would
    double-count a base present in both, so the union is taken here."""
    if len(primary.quintiles) != QUINTILE_COUNT:
        return 0
    usable = sorted(episodes, key=lambda e: (e.taker_imbalance_10m, e.decision_id))
    slices = _quintile_slices(len(usable))
    bottom = usable[slices[0][0] : slices[0][1]]
    top = usable[slices[-1][0] : slices[-1][1]]
    return len({e.cluster_key for e in (*bottom, *top)})


# --- Verdict -------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    verdict: str
    reasons: tuple[str, ...]
    top_quintile_episodes: int
    bottom_quintile_episodes: int
    compared_distinct_clusters: int
    spread_pp: float | None
    meets_episode_floor: bool
    meets_cluster_floor: bool


def evaluate_verdict(primary: QuintileAnalysis, *, compared_clusters: int) -> Verdict:
    """The floor binds first and can only ever produce `inconclusive`. A
    rejection is reachable only above the floor. Rule 6's adjacent-boundary
    separation is a precondition of `candidate`, never of a rejection.

    `compared_clusters` is the exact union of asset clusters across the top
    and bottom quintiles (see `compared_distinct_clusters`)."""
    quintiles = primary.quintiles
    spread = primary.top_minus_bottom_median_spread_pp
    if len(quintiles) != QUINTILE_COUNT:
        return Verdict(
            verdict=VERDICT_INCONCLUSIVE,
            reasons=(f"fewer_than_{QUINTILE_COUNT}_quintiles_formed",),
            top_quintile_episodes=quintiles[-1].episodes if quintiles else 0,
            bottom_quintile_episodes=quintiles[0].episodes if quintiles else 0,
            compared_distinct_clusters=compared_clusters,
            spread_pp=spread,
            meets_episode_floor=False,
            meets_cluster_floor=False,
        )

    bottom, top = quintiles[0], quintiles[-1]
    meets_episode_floor = (
        top.episodes >= MIN_EPISODES_PER_QUINTILE and bottom.episodes >= MIN_EPISODES_PER_QUINTILE
    )
    meets_cluster_floor = compared_clusters >= MIN_ASSET_CLUSTERS

    if not (meets_episode_floor and meets_cluster_floor):
        reasons: list[str] = []
        if not meets_episode_floor:
            reasons.append(
                f"episodes_per_compared_quintile_below_{MIN_EPISODES_PER_QUINTILE} "
                f"(top={top.episodes}, bottom={bottom.episodes})"
            )
        if not meets_cluster_floor:
            reasons.append(
                f"compared_asset_clusters_{compared_clusters}_below_{MIN_ASSET_CLUSTERS}"
            )
        return Verdict(
            verdict=VERDICT_INCONCLUSIVE,
            reasons=tuple(reasons),
            top_quintile_episodes=top.episodes,
            bottom_quintile_episodes=bottom.episodes,
            compared_distinct_clusters=compared_clusters,
            spread_pp=spread,
            meets_episode_floor=meets_episode_floor,
            meets_cluster_floor=meets_cluster_floor,
        )

    if spread is not None and spread > CANDIDATE_SPREAD_PP:
        blockers: list[str] = []
        if not primary.monotone_increasing:
            blockers.append("quintile_medians_not_monotone_increasing")
        if not primary.ties.adjacent_boundaries_distinct:
            blockers.append(
                "rule6_tied_value_straddles_quintile_boundary "
                f"(pairs={list(primary.ties.tied_boundary_pairs)})"
            )
        if not blockers:
            return Verdict(
                verdict=VERDICT_CANDIDATE,
                reasons=(f"spread_{spread:.4f}pp_above_{CANDIDATE_SPREAD_PP}_and_monotone",),
                top_quintile_episodes=top.episodes,
                bottom_quintile_episodes=bottom.episodes,
                compared_distinct_clusters=compared_clusters,
                spread_pp=spread,
                meets_episode_floor=True,
                meets_cluster_floor=True,
            )
        return Verdict(
            verdict=VERDICT_INCONCLUSIVE,
            reasons=tuple(blockers),
            top_quintile_episodes=top.episodes,
            bottom_quintile_episodes=bottom.episodes,
            compared_distinct_clusters=compared_clusters,
            spread_pp=spread,
            meets_episode_floor=True,
            meets_cluster_floor=True,
        )

    if spread is not None and abs(spread) < REJECT_SPREAD_PP:
        return Verdict(
            verdict=VERDICT_REJECTED,
            reasons=(f"abs_spread_{abs(spread):.4f}pp_below_{REJECT_SPREAD_PP}_above_floor",),
            top_quintile_episodes=top.episodes,
            bottom_quintile_episodes=bottom.episodes,
            compared_distinct_clusters=compared_clusters,
            spread_pp=spread,
            meets_episode_floor=True,
            meets_cluster_floor=True,
        )

    return Verdict(
        verdict=VERDICT_INCONCLUSIVE,
        reasons=("spread_between_reject_and_candidate_thresholds",),
        top_quintile_episodes=top.episodes,
        bottom_quintile_episodes=bottom.episodes,
        compared_distinct_clusters=compared_clusters,
        spread_pp=spread,
        meets_episode_floor=True,
        meets_cluster_floor=True,
    )


# --- Dataset fingerprint -------------------------------------------------


def _fingerprint_ready(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def dataset_fingerprint(episodes: tuple[MeasuredEpisode, ...]) -> str:
    """Hash the full measured dataset, one canonical tuple per episode in a
    sorted order, so two runs over the same nominal window that differ in any
    measured value or coverage outcome cannot collide."""
    rows = sorted(
        tuple(_fingerprint_ready(getattr(e, f.name)) for f in fields(e)) for e in episodes
    )
    payload = {"cost_model_version": COST_MODEL_VERSION, "episodes": rows}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


__all__ = [
    "CANDIDATE_SPREAD_PP",
    "CONTEXT_LOOKBACK_MINUTES",
    "COST_MODEL_VERSION",
    "DISCOVERY_START",
    "HELD_OUT_START",
    "HORIZON_MINUTES",
    "HYPOTHESIS_ID",
    "MIN_ASSET_CLUSTERS",
    "MIN_EPISODES_PER_QUINTILE",
    "PRIMARY_LOOKBACK_MINUTES",
    "REJECT_SPREAD_PP",
    "REPORT_VERSION",
    "RESOLVER_VERSION",
    "STRATEGY_VERSION",
    "VERDICT_CANDIDATE",
    "VERDICT_INCONCLUSIVE",
    "VERDICT_REJECTED",
    "CoverageFunnelStep",
    "CoverageResult",
    "ExchangeCoverage",
    "MeasuredEpisode",
    "QuintileAnalysis",
    "QuintileStat",
    "ResolvedDecisionRow",
    "TieDiagnostics",
    "Verdict",
    "build_coverage",
    "compared_distinct_clusters",
    "compute_quintiles",
    "cost_pct_at_horizon",
    "dataset_fingerprint",
    "evaluate_verdict",
    "net_short_return_pct",
]
