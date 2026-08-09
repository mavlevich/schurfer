"""Read-only formal report for the pre-registered confirmed-OI-growth baseline
filter — a forward-only challenger, never a production score change.

Why this exists: the live `oi_trend` score component (`apps/api-gateway/
internal/pumps/handler.go`, `oiChangeThresholdPct = 5.0`) scores a recorded
aggregate OI change from the strategy's anchor time to decision time above
+5% as BAD for a short (0 points, "new money entering") and below -5% as GOOD
(2 points, "distribution underway"). An informal read of the score_6
baseline's own triggered replay trades (2026-07-26 to 2026-08-09, via
`component_snapshot`) found the opposite direction on the subset where OI was
CONFIRMED growing (`data_available=True`, `points=0`): N=23, 17 assets, mean
return +0.94%, PF 1.38 — but concentrated in exactly 2 UTC weeks, one slightly
negative and one strongly positive. That is a lead, not evidence: too easy for
the aggregate sign to be one lucky week away from flipping.

This report tests the lead as a genuine forward-only filter, not a score
inversion. The baseline (`score_6`, live formula) is completely unchanged.
The challenger trades exactly the same decision the baseline would have taken,
but ONLY when `oi_trend.data_available is True and points == 0` AND the raw
recorded `value` independently agrees with this filter's own frozen +-5%
threshold (see `oi_growth_reason` — a `points`/`value` disagreement, e.g. from
a since-changed live threshold, is `unresolved`, never silently trusted via
`points` alone) — every other baseline-triggered decision (declining OI,
neutral OI, unconfirmed/missing OI data) becomes cash for the challenger. This
is deliberately NOT the same shape of change as
`virtual_banded_price_extent_report.py`: that challenger recomputes a score
component and can select a different decision than the baseline; this one
never recomputes anything and never selects a different decision — it only
ever gates the baseline's own decision to cash or not.

Cohort start is locked to the day AFTER this hypothesis was registered
(2026-08-10) — the window inspected to find the lead must never be reused to
"confirm" it, or this becomes exactly the p-hacking the project's inference
discipline exists to prevent. If this PR merges after that date, the cohort
start must move to the first full UTC day after actual registration instead
of silently running against a date already in the past.

Population the formal sample is built on (critical, see `build_
oi_growth_filter_report`): NOT the first 100 eligible episodes overall.
Baseline itself only triggers on a small share of all eligible episodes, and
confirmed growth is a further subset of that — freezing the formal sample on
"eligible episodes" would almost certainly stall forever at
`insufficient_triggers` regardless of how much time passes, since
`challenger_inference` freezes its formal sample as the first `FORMAL_
EPISODES` items of whatever it is given and never revisits that slice. The
formal sample here is instead the first 100 baseline-triggered opportunities
with a CONFIRMED oi_trend reading (missing/unknown-quality readings are
excluded from this population entirely, not folded into cash — see
`operational_challenger_metrics` for that view separately, kept out of the
primary population so a data-availability pattern can never masquerade as the
OI-growth effect itself). Overall eligible-episode counts and capacity are
still reported for context, just not used to freeze the formal sample.

"Triggered" (both baseline and challenger) means the selection criteria
fired, independent of whether the market path later resolved to a return —
matching `virtual_threshold_challenger_report.py`'s established convention
(`baseline_triggered = selected_decision_id is not None`). Resolution is a
separate, orthogonal concern tracked via `status`/return-is-None.

Promotion requires ALL of:
- The run is canonical (no CLI override of strategy cohort, resolver,
  fallback tolerance, or costs — see `_is_canonical_run`). A sensitivity run
  can never emit a promotion verdict regardless of its numbers.
- `challenger_inference.build_challenger_inference`'s own formal machinery on
  the population above: >=100 formal-sample opportunities, >=30 distinct
  asset clusters, `minimum_triggered_episodes=20` (a low-trigger-rate family
  reaching "ready" on an almost-entirely-cash sample must not look ready —
  see that module's own docstring on this exact failure mode), a
  paired-delta bootstrap CI whose lower bound is above zero after Holm
  correction, and a positive minimum leave-one-asset-out delta.
- At least 4 distinct UTC weeks in the frozen formal sample, AND an
  independent minimum leave-one-UTC-week-out delta above zero, computed
  separately over that same frozen sample (see `_week_sensitivity`) —
  because the historical lead's own sign flipped between its two observed
  weeks, and asset-level sensitivity alone cannot see that risk. A single-
  week sample cannot even compute leave-one-out (excluding the only week
  would remove every observation) and is reported as collecting, not a
  crash.
- The challenger's own absolute profit factor, computed on the SAME frozen
  formal sample the statistical verdict used (never the whole, still-growing
  history, which could silently drift the verdict later without a new
  cohort) — above 1. A positive paired delta against a losing baseline is
  not by itself a profitable strategy; all of the above must hold together.

No missing or ambiguous OI reading is ever treated as confirmed growth —
`data_available` must be exactly `True`; `False` (confirmed missing) and
`None` (unknown quality) both fail closed to cash, same as every other
component-availability check in this project's discovery tooling.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from statistics import fmean
from typing import TYPE_CHECKING

from .challenger_inference import (
    DEFAULT_INFERENCE_SETTINGS,
    ChallengerEpisode,
    ChallengerInference,
    build_challenger_inference,
)
from .clustered_inference import ClusterObservation, leave_one_cluster_out_means
from .decision_quality import (
    SCORE_THRESHOLD_BASELINE_POLICY,
    SCORE_THRESHOLD_FAMILY_VERSION,
    component_snapshot,
    select_score_policy,
)
from .episode_replay import PROTOCOL_VERSION
from .outcomes import RESOLVER_VERSION
from .replay import (
    DEFAULT_REPLAY_HORIZONS,
    FOUNDATION_VERSION,
    QUERY_VERSION,
    ReplayDataset,
    ReplayFilters,
    build_replay_dataset,
)
from .reporting import (
    ReportWindowNotStartedError,
    format_number,
    format_percentage,
    json_ready,
    markdown_table,
    normalize_code_revision,
    parse_utc_datetime,
    profit_factor,
    resolve_report_until,
)
from .virtual_market import (
    DECISION_MARKET_PATH_VERSION,
    decision_market_path_fingerprint,
)
from .virtual_strategy import (
    COST_MODEL_VERSION,
    DEFAULT_COSTS,
    ENTRY_MODEL_VERSION,
    EXIT_MODEL_VERSION,
    VIRTUAL_STRATEGY_VERSION,
    CostParameters,
    MarketPath,
    VirtualTrade,
    simulate_decision,
)

if TYPE_CHECKING:
    from .decision_quality import ComponentSnapshot
    from .replay import ReplayDecision, ReplayEpisode
    from .virtual_market import DecisionMarketPath

OI_GROWTH_FILTER_REPORT_VERSION = "oi_growth_filter_report_v1"
OI_GROWTH_FILTER_CANDIDATE_VERSION = "confirmed_oi_growth_baseline_filter_v1"
OI_GROWTH_FILTER_INFERENCE_VERSION = "oi_growth_filter_formal_inference_v1"
# Registered 2026-08-10 — the day after the historical lead (2026-07-26 to
# 2026-08-09) was found. That window must never be reused to "confirm" this.
OI_GROWTH_FILTER_COHORT_START = datetime(2026, 8, 10, tzinfo=UTC)
OI_GROWTH_FILTER_STRATEGY_VERSIONS = ("pump_short_v1_market_quality",)
# Must track apps/api-gateway/internal/pumps/handler.go's oiChangeThresholdPct.
# Analytics does not depend on the Go service, so this is a deliberate,
# commented duplication rather than a cross-language import.
OI_CHANGE_THRESHOLD_PCT = 5.0
OI_GROWTH_MIN_TRIGGERED_EPISODES = 20
OI_GROWTH_MIN_PROFIT_FACTOR = 1.0
# The historical lead's own sign flipped between its two observed weeks —
# promotion must never rest on a sample this temporally concentrated again.
OI_GROWTH_MIN_FORMAL_SAMPLE_WEEKS = 4
CHALLENGER_VARIANT_KEY = "confirmed_oi_growth"


def _week_key(value: datetime) -> str:
    year, week, _ = value.astimezone(UTC).isocalendar()
    return f"{year}-W{week:02d}"


@dataclass(frozen=True)
class OiGrowthEpisodeResult:
    pump_event_id: int
    cluster_key: str
    base: str
    week_key: str
    status: str
    oi_selection_reason: str
    oi_value_pct: float | None
    decision_id: str | None
    decision_ts: datetime | None
    baseline_net_return_pct: float | None
    baseline_triggered: bool
    challenger_net_return_pct: float | None
    challenger_triggered: bool
    trade: VirtualTrade | None
    error: str | None = None


def _cash_result(episode: ReplayEpisode, week: str) -> OiGrowthEpisodeResult:
    return OiGrowthEpisodeResult(
        episode.pump_event_id,
        episode.cluster_key,
        episode.base,
        week,
        "not_triggered",
        "baseline_not_triggered",
        None,
        None,
        None,
        0.0,
        False,
        0.0,
        False,
        None,
    )


def _unresolved_result(
    episode: ReplayEpisode, week: str, reason: str, error: str
) -> OiGrowthEpisodeResult:
    return OiGrowthEpisodeResult(
        episode.pump_event_id,
        episode.cluster_key,
        episode.base,
        week,
        "selection_unresolved",
        reason,
        None,
        None,
        None,
        None,
        False,
        None,
        False,
        None,
        error=error,
    )


def _missing_path(episode: ReplayEpisode, decision: ReplayDecision) -> MarketPath:
    return MarketPath(
        pump_event_id=episode.pump_event_id,
        exchange=decision.exchange,
        base=decision.base,
        status="missing_path",
        candles=(),
        error="market path was not loaded",
    )


def _classify_by_value(value: float) -> str:
    """The registered v1 threshold (+-5%) is frozen at registration time —
    independent of whatever `handler.go`'s own threshold happens to be when
    this runs. If the two ever disagree (a future change to the live
    threshold, or a decision recorded under an older threshold), that is an
    anomaly this filter must not silently trust `points` alone to resolve."""
    if value > OI_CHANGE_THRESHOLD_PCT:
        return "oi_confirmed_growth"
    if value < -OI_CHANGE_THRESHOLD_PCT:
        return "oi_confirmed_declining"
    return "oi_confirmed_neutral"


def _classify_by_points(points: int) -> str | None:
    return {0: "oi_confirmed_growth", 2: "oi_confirmed_declining", 1: "oi_confirmed_neutral"}.get(
        points
    )


def oi_growth_reason(oi: ComponentSnapshot) -> str:
    """Pure, fail-closed classification of the already-recorded oi_trend
    component. `data_available` must be exactly True to ever confirm growth —
    False (confirmed missing) and None (unknown quality) both fail closed.
    Independently re-derives the classification from the raw `value` against
    this filter's own frozen +-5% threshold and requires it to agree with the
    recorded `points` — see `_classify_by_value`."""
    if oi.data_available is False:
        return "oi_data_confirmed_missing"
    if oi.data_available is None:
        return "oi_data_quality_unknown"
    by_value = _classify_by_value(oi.value)
    by_points = _classify_by_points(oi.points)
    if by_points is None or by_points != by_value:
        return "oi_points_value_mismatch"
    return by_value


def evaluate_oi_growth_episode(
    episode: ReplayEpisode,
    path_by_decision: dict[str, MarketPath],
    costs: CostParameters,
) -> OiGrowthEpisodeResult:
    """Pure per-episode evaluation (no I/O beyond the already-fetched market
    path map) — testable directly against hand-built episodes/decisions."""
    baseline_selection = select_score_policy(episode, SCORE_THRESHOLD_BASELINE_POLICY)
    week = _week_key(episode.first_decision_at)

    if baseline_selection.status == "not_triggered":
        return _cash_result(episode, week)
    if baseline_selection.status == "unresolved" or baseline_selection.decision is None:
        return _unresolved_result(
            episode,
            week,
            "baseline_selection_unresolved",
            baseline_selection.error or "baseline selection failed",
        )

    decision = baseline_selection.decision
    # Week is keyed by the actually-selected decision's own timestamp, not
    # the episode's first-seen time — they can diverge when the score
    # crossing happens well after the episode was first observed.
    week = _week_key(decision.ts)
    components, err = component_snapshot(decision)
    if components is None:
        return _unresolved_result(
            episode, week, "oi_component_unresolved", err or "component snapshot failed"
        )
    oi = next((component for component in components if component.name == "oi_trend"), None)
    if oi is None:
        return _unresolved_result(
            episode, week, "oi_component_missing", "oi_trend component absent from snapshot"
        )

    reason = oi_growth_reason(oi)
    if reason == "oi_points_value_mismatch":
        return _unresolved_result(
            episode,
            week,
            reason,
            f"recorded points={oi.points} disagrees with value={oi.value} against "
            f"the frozen +-{OI_CHANGE_THRESHOLD_PCT}% threshold",
        )

    path = path_by_decision.get(decision.decision_id or "") or _missing_path(episode, decision)
    trade = simulate_decision(
        episode,
        path,
        decision,
        selection_reason=f"oi_growth_filter:{OI_GROWTH_FILTER_CANDIDATE_VERSION}",
        costs=costs,
    )
    # "Triggered" means the selection criteria fired — independent of
    # whether the market path later resolved to a return. Resolution is a
    # separate, orthogonal concern (see `status` below), matching the
    # established convention in virtual_threshold_challenger_report.py
    # (baseline_triggered = selected_decision_id is not None).
    baseline_triggered = True
    baseline_resolved = trade.status == "complete"
    baseline_return = trade.net_return_pct if baseline_resolved else None

    challenger_triggered = reason == "oi_confirmed_growth"
    if challenger_triggered:
        challenger_return: float | None = trade.net_return_pct if baseline_resolved else None
    else:
        # Entry condition (confirmed OI growth) was not met -> definitely
        # cash for the challenger, regardless of whether the underlying
        # market path happens to resolve.
        challenger_return = 0.0

    return OiGrowthEpisodeResult(
        episode.pump_event_id,
        episode.cluster_key,
        episode.base,
        week,
        "triggered" if baseline_resolved else "unresolved_path",
        reason,
        oi.value,
        decision.decision_id,
        decision.ts,
        baseline_return,
        baseline_triggered,
        challenger_return,
        challenger_triggered,
        trade,
        trade.error,
    )


@dataclass(frozen=True)
class OiGrowthMetrics:
    label: str
    eligible_episodes: int
    resolved_episodes: int
    triggered: int
    cash: int
    unresolved: int
    trade_rate_pct: float | None
    mean_episode_net_return_pct: float | None
    conditional_trade_net_return_pct: float | None
    total_net_pnl_usd: float | None
    profit_factor: float | None
    win_rate_pct: float | None


def _metrics(
    label: str,
    resolved_returns: list[float | None],
    triggered_flags: list[bool],
    trades: list[VirtualTrade],
) -> OiGrowthMetrics:
    """`triggered_flags` is selection-based (see evaluate_oi_growth_episode)
    and is independent of `resolved_returns` — a triggered episode can still
    be unresolved (missing market data), so `triggered` is not simply
    `len(resolved) - cash` here."""
    resolved = [value for value in resolved_returns if value is not None]
    resolved_cash = sum(
        1
        for value, triggered in zip(resolved_returns, triggered_flags, strict=True)
        if value is not None and not triggered
    )
    trade_returns = [trade.net_return_pct for trade in trades if trade.net_return_pct is not None]
    pnl = [trade.net_pnl_usd for trade in trades if trade.net_pnl_usd is not None]
    triggered_count = sum(triggered_flags)
    return OiGrowthMetrics(
        label=label,
        eligible_episodes=len(resolved_returns),
        resolved_episodes=len(resolved),
        triggered=triggered_count,
        cash=resolved_cash,
        unresolved=len(resolved_returns) - len(resolved),
        trade_rate_pct=(
            triggered_count / len(resolved_returns) * 100 if resolved_returns else None
        ),
        mean_episode_net_return_pct=fmean(resolved) if resolved else None,
        conditional_trade_net_return_pct=fmean(trade_returns) if trade_returns else None,
        total_net_pnl_usd=sum(pnl) if pnl else None,
        profit_factor=profit_factor(trade_returns),
        win_rate_pct=(
            sum(1 for value in trade_returns if value > 0) / len(trade_returns) * 100
            if trade_returns
            else None
        ),
    )


@dataclass(frozen=True)
class OiReasonRow:
    reason: str
    count: int


@dataclass(frozen=True)
class WeekSensitivity:
    distinct_weeks: int
    minimum_leave_one_week_out_pct: float | None
    leave_one_week_out: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class OiGrowthManifest:
    protocol_version: str
    replay_engine_version: str
    replay_query_version: str
    report_version: str
    candidate_version: str
    inference_version: str
    virtual_strategy_version: str
    selection_model_version: str
    entry_model_version: str
    exit_model_version: str
    cost_model_version: str
    market_path_version: str
    code_revision: str
    working_tree_dirty: bool
    generated_at: datetime
    dataset_since: datetime
    dataset_until_exclusive: datetime
    decision_input_fingerprint: str
    market_path_fingerprint: str
    strategy_versions: tuple[str, ...]
    resolver_version: str
    required_horizons: tuple[int, ...]
    fallback_allowed: bool
    oi_change_threshold_pct: float
    min_triggered_episodes: int
    min_profit_factor: float
    taker_fee_bps_per_side: float
    funding_cost_bps_per_8h: float
    bootstrap_iterations: int
    bootstrap_seed: int
    bootstrap_confidence_level: float
    holm_family_alpha: float
    canonical_run: bool
    min_formal_sample_weeks: int
    report_scope: str = "forward_only_baseline_filter_no_production_change"
    no_trigger_policy: str = "zero_return_cash_when_condition_not_met"


@dataclass(frozen=True)
class OiGrowthReport:
    manifest: OiGrowthManifest
    dataset_episodes: int
    eligible_episodes: int
    excluded_episodes: int
    input_exclusion_reasons: tuple[OiReasonRow, ...]
    oi_reason_distribution: tuple[OiReasonRow, ...]
    baseline_metrics: OiGrowthMetrics
    challenger_metrics_all_to_date: OiGrowthMetrics
    challenger_metrics_formal_sample: OiGrowthMetrics
    operational_challenger_metrics: OiGrowthMetrics
    mean_paired_delta_pct: float | None
    inference: ChallengerInference
    week_sensitivity: WeekSensitivity | None
    canonical_run: bool
    final_verdict: str
    episode_results: tuple[OiGrowthEpisodeResult, ...]
    market_paths: tuple[DecisionMarketPath, ...]


def _week_sensitivity(
    results: tuple[OiGrowthEpisodeResult, ...],
    formal_sample_event_ids: tuple[int, ...],
) -> WeekSensitivity | None:
    """Independent leave-one-UTC-week-out check on the paired delta, over the
    exact same formal sample `challenger_inference` locked — see module
    docstring on why asset-level sensitivity alone is not enough here.
    `results` must already be restricted to the population fed into that
    inference (baseline-triggered, OI-resolved) — an unrestricted `results`
    would let baseline-not-triggered cash episodes (delta always exactly
    zero) dilute the sensitivity read."""
    by_event = {result.pump_event_id: result for result in results}
    observations: list[ClusterObservation] = []
    for event_id in formal_sample_event_ids:
        result = by_event.get(event_id)
        if (
            result is None
            or result.baseline_net_return_pct is None
            or result.challenger_net_return_pct is None
        ):
            continue
        delta = result.challenger_net_return_pct - result.baseline_net_return_pct
        observations.append(ClusterObservation(result.week_key, delta))
    if not observations:
        return None
    weeks = tuple(sorted({observation.cluster_key for observation in observations}))
    if len(weeks) < 2:
        # leave_one_cluster_out_means raises if excluding the only cluster
        # would remove every observation — with a single UTC week that is
        # exactly what would happen. Report the count so the caller can see
        # collection is still in progress, without crashing.
        return WeekSensitivity(
            distinct_weeks=len(weeks), minimum_leave_one_week_out_pct=None, leave_one_week_out=()
        )
    sensitivity = leave_one_cluster_out_means(tuple(observations), weeks)
    return WeekSensitivity(
        distinct_weeks=len(weeks),
        minimum_leave_one_week_out_pct=min(value for _, value in sensitivity),
        leave_one_week_out=sensitivity,
    )


def _is_canonical_run(filters: ReplayFilters, costs: CostParameters) -> bool:
    """Only the registered, unmodified configuration may ever produce a
    `shadow_candidate` verdict — any override (a different strategy cohort,
    resolver, fallback tolerance, or cost assumption) makes this a
    sensitivity run, however the numbers come out. The CLI still allows
    these overrides for legitimate ad hoc comparison; this just stops one
    from quietly becoming a headline finding."""
    return (
        filters.strategy_versions == OI_GROWTH_FILTER_STRATEGY_VERSIONS
        and filters.resolver_version == RESOLVER_VERSION
        and filters.allow_fallback is False
        and costs.taker_fee_bps_per_side == DEFAULT_COSTS.taker_fee_bps_per_side
        and costs.funding_cost_bps_per_8h == DEFAULT_COSTS.funding_cost_bps_per_8h
    )


def _final_verdict(
    inference: ChallengerInference,
    week_sensitivity: WeekSensitivity | None,
    challenger_profit_factor: float | None,
    *,
    canonical_run: bool,
) -> str:
    if not inference.challengers:
        return "collecting"
    if not canonical_run:
        return "sensitivity_only_no_promotion"
    challenger = inference.challengers[0]
    if challenger.verdict == "no_go":
        return "no_go"
    if (
        challenger.verdict == "shadow_candidate"
        and week_sensitivity is not None
        and week_sensitivity.distinct_weeks >= OI_GROWTH_MIN_FORMAL_SAMPLE_WEEKS
        and week_sensitivity.minimum_leave_one_week_out_pct is not None
        and week_sensitivity.minimum_leave_one_week_out_pct > 0
        and challenger_profit_factor is not None
        and challenger_profit_factor > OI_GROWTH_MIN_PROFIT_FACTOR
    ):
        return "shadow_candidate"
    return "inconclusive"


def build_oi_growth_filter_report(
    dataset: ReplayDataset,
    filters: ReplayFilters,
    paths: tuple[DecisionMarketPath, ...],
    *,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
    costs: CostParameters = DEFAULT_COSTS,
) -> OiGrowthReport:
    revision = normalize_code_revision(code_revision)
    if filters.since != OI_GROWTH_FILTER_COHORT_START:
        raise ValueError(
            "formal oi-growth-filter report requires the registered forward cohort start"
        )
    if filters.strategy_versions != OI_GROWTH_FILTER_STRATEGY_VERSIONS:
        raise ValueError("formal oi-growth-filter report requires the registered strategy cohort")

    path_counts = Counter(path.decision_id for path in paths)
    duplicates = sorted(decision_id for decision_id, count in path_counts.items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate market paths for decisions: {duplicates}")
    path_by_decision = {item.decision_id: item.path for item in paths}

    results = tuple(
        evaluate_oi_growth_episode(episode, path_by_decision, costs)
        for episode in dataset.eligible_episodes
    )

    baseline_returns = [result.baseline_net_return_pct for result in results]
    baseline_triggered_flags = [result.baseline_triggered for result in results]
    baseline_trades = [
        result.trade for result in results if result.baseline_triggered and result.trade
    ]

    # "Opportunities": episodes where the baseline itself selected a trade —
    # only these are ever relevant to the challenger's own gating decision.
    opportunities = tuple(result for result in results if result.baseline_triggered)
    # The population the formal inference is built on: opportunities with a
    # CONFIRMED oi_trend reading. Missing/unknown-quality readings are
    # excluded here entirely (not folded into cash) so a data-availability
    # pattern can never masquerade as the OI-growth effect being tested —
    # see module docstring. They still appear in `operational_metrics` below.
    missing_or_unknown = {"oi_data_confirmed_missing", "oi_data_quality_unknown"}
    formal_population = tuple(
        result for result in opportunities if result.oi_selection_reason not in missing_or_unknown
    )

    inference = build_challenger_inference(
        tuple(
            ChallengerEpisode(
                pump_event_id=result.pump_event_id,
                cluster_key=result.cluster_key,
                baseline_return_pct=result.baseline_net_return_pct,
                challenger_returns_pct=(
                    (CHALLENGER_VARIANT_KEY, result.challenger_net_return_pct),
                ),
                baseline_triggered=result.baseline_triggered,
                challenger_triggered=((CHALLENGER_VARIANT_KEY, result.challenger_triggered),),
            )
            for result in formal_population
        ),
        (CHALLENGER_VARIANT_KEY,),
        inference_version=OI_GROWTH_FILTER_INFERENCE_VERSION,
        minimum_triggered_episodes=OI_GROWTH_MIN_TRIGGERED_EPISODES,
    )
    week_sensitivity = _week_sensitivity(formal_population, inference.formal_sample_event_ids)

    # Promotion-relevant PF uses exactly the frozen formal sample — the same
    # population challenger_inference locked — never the whole, still-
    # growing history (which could drift the verdict later without a new
    # cohort). All-to-date PF is shown separately, informational only.
    formal_sample_ids = set(inference.formal_sample_event_ids)
    formal_sample_results = [
        result for result in formal_population if result.pump_event_id in formal_sample_ids
    ]
    formal_sample_challenger_trades = [
        result.trade
        for result in formal_sample_results
        if result.challenger_triggered and result.trade
    ]
    canonical_run = _is_canonical_run(filters, costs)
    challenger_pf_formal = profit_factor(
        trade.net_return_pct
        for trade in formal_sample_challenger_trades
        if trade.net_return_pct is not None
    )
    final_verdict = _final_verdict(
        inference, week_sensitivity, challenger_pf_formal, canonical_run=canonical_run
    )

    paired_deltas = [
        challenger - baseline
        for baseline, challenger in zip(
            [result.baseline_net_return_pct for result in formal_sample_results],
            [result.challenger_net_return_pct for result in formal_sample_results],
            strict=True,
        )
        if baseline is not None and challenger is not None
    ]

    input_exclusions = Counter(
        reason for episode in dataset.excluded_episodes for reason in episode.exclusion_reasons
    )
    oi_reasons = Counter(result.oi_selection_reason for result in results)

    return OiGrowthReport(
        manifest=OiGrowthManifest(
            protocol_version=PROTOCOL_VERSION,
            replay_engine_version=FOUNDATION_VERSION,
            replay_query_version=QUERY_VERSION,
            report_version=OI_GROWTH_FILTER_REPORT_VERSION,
            candidate_version=OI_GROWTH_FILTER_CANDIDATE_VERSION,
            inference_version=OI_GROWTH_FILTER_INFERENCE_VERSION,
            virtual_strategy_version=VIRTUAL_STRATEGY_VERSION,
            selection_model_version=SCORE_THRESHOLD_FAMILY_VERSION,
            entry_model_version=ENTRY_MODEL_VERSION,
            exit_model_version=EXIT_MODEL_VERSION,
            cost_model_version=COST_MODEL_VERSION,
            market_path_version=DECISION_MARKET_PATH_VERSION,
            code_revision=revision,
            working_tree_dirty=working_tree_dirty,
            generated_at=generated_at,
            dataset_since=filters.since,
            dataset_until_exclusive=filters.until,
            decision_input_fingerprint=dataset.input_fingerprint,
            market_path_fingerprint=decision_market_path_fingerprint(paths),
            strategy_versions=filters.strategy_versions,
            resolver_version=filters.resolver_version,
            required_horizons=filters.required_horizons,
            fallback_allowed=filters.allow_fallback,
            oi_change_threshold_pct=OI_CHANGE_THRESHOLD_PCT,
            min_triggered_episodes=OI_GROWTH_MIN_TRIGGERED_EPISODES,
            min_profit_factor=OI_GROWTH_MIN_PROFIT_FACTOR,
            taker_fee_bps_per_side=costs.taker_fee_bps_per_side,
            funding_cost_bps_per_8h=costs.funding_cost_bps_per_8h,
            bootstrap_iterations=DEFAULT_INFERENCE_SETTINGS.iterations,
            bootstrap_seed=DEFAULT_INFERENCE_SETTINGS.seed,
            bootstrap_confidence_level=DEFAULT_INFERENCE_SETTINGS.confidence_level,
            holm_family_alpha=DEFAULT_INFERENCE_SETTINGS.family_alpha,
            canonical_run=canonical_run,
            min_formal_sample_weeks=OI_GROWTH_MIN_FORMAL_SAMPLE_WEEKS,
        ),
        dataset_episodes=len(dataset.episodes),
        eligible_episodes=len(dataset.eligible_episodes),
        excluded_episodes=len(dataset.excluded_episodes),
        input_exclusion_reasons=tuple(
            OiReasonRow(reason, count)
            for reason, count in sorted(
                input_exclusions.items(), key=lambda item: (-item[1], item[0])
            )
        ),
        oi_reason_distribution=tuple(
            OiReasonRow(reason, count)
            for reason, count in sorted(oi_reasons.items(), key=lambda item: (-item[1], item[0]))
        ),
        baseline_metrics=_metrics(
            "baseline", baseline_returns, baseline_triggered_flags, baseline_trades
        ),
        challenger_metrics_all_to_date=_metrics(
            f"{CHALLENGER_VARIANT_KEY} (all-to-date, growing)",
            [result.challenger_net_return_pct for result in formal_population],
            [result.challenger_triggered for result in formal_population],
            [
                result.trade
                for result in formal_population
                if result.challenger_triggered and result.trade
            ],
        ),
        challenger_metrics_formal_sample=_metrics(
            f"{CHALLENGER_VARIANT_KEY} (frozen formal sample, promotion-relevant)",
            [result.challenger_net_return_pct for result in formal_sample_results],
            [result.challenger_triggered for result in formal_sample_results],
            formal_sample_challenger_trades,
        ),
        operational_challenger_metrics=_metrics(
            f"{CHALLENGER_VARIANT_KEY} (operational, unresolved-OI as cash)",
            [result.challenger_net_return_pct for result in opportunities],
            [result.challenger_triggered for result in opportunities],
            [
                result.trade
                for result in opportunities
                if result.challenger_triggered and result.trade
            ],
        ),
        mean_paired_delta_pct=fmean(paired_deltas) if paired_deltas else None,
        inference=inference,
        week_sensitivity=week_sensitivity,
        canonical_run=canonical_run,
        final_verdict=final_verdict,
        episode_results=results,
        market_paths=paths,
    )


def render_json(report: OiGrowthReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True, allow_nan=False)


def render_markdown(report: OiGrowthReport) -> str:
    manifest = report.manifest
    lines = [
        "# Confirmed OI-Growth Baseline Filter (Forward Challenger)",
        "",
        f"Generated: {manifest.generated_at.isoformat()}",
        f"Code revision: `{manifest.code_revision}`",
        f"Working tree dirty: {'yes' if manifest.working_tree_dirty else 'no'}",
        f"Decision fingerprint: `{manifest.decision_input_fingerprint}`",
        f"Market-path fingerprint: `{manifest.market_path_fingerprint}`",
        (
            f"Scope: {manifest.dataset_since.isoformat()} <= decision "
            f"< {manifest.dataset_until_exclusive.isoformat()}"
        ),
        (
            f"OI change threshold (this filter's own frozen v1 value): "
            f"+-{manifest.oi_change_threshold_pct}%"
        ),
        f"Canonical run: {'yes' if report.canonical_run else 'no (sensitivity only)'}",
        "",
        (
            f"> Formal inference status: `{report.inference.readiness.status}`. "
            f"Final verdict: `{report.final_verdict}`. This report never changes "
            "production score settings or authorizes real trading."
        ),
        "",
        "## Funnel",
        "",
    ]
    lines.extend(
        markdown_table(
            ("Metric", "Value"),
            [
                ("Dataset episodes", report.dataset_episodes),
                ("Eligible episodes", report.eligible_episodes),
                ("Excluded episodes", report.excluded_episodes),
            ],
        )
    )
    lines.extend(["", "## Input exclusion reasons", ""])
    lines.extend(
        markdown_table(
            ("Reason", "Count"),
            [(row.reason, row.count) for row in report.input_exclusion_reasons],
        )
    )
    lines.extend(["", "## OI selection reason distribution", ""])
    lines.extend(
        markdown_table(
            ("Reason", "Count"),
            [(row.reason, row.count) for row in report.oi_reason_distribution],
        )
    )
    lines.extend(["", "## Baseline vs. challenger economics", ""])
    for metrics in (
        report.baseline_metrics,
        report.challenger_metrics_all_to_date,
        report.challenger_metrics_formal_sample,
        report.operational_challenger_metrics,
    ):
        lines.extend(
            markdown_table(
                (metrics.label, "Value"),
                [
                    ("Eligible episodes", metrics.eligible_episodes),
                    ("Resolved episodes", metrics.resolved_episodes),
                    ("Triggered (replay trades)", metrics.triggered),
                    ("Unresolved", metrics.unresolved),
                    ("Trade rate", format_percentage(metrics.trade_rate_pct, 1, missing="n/a")),
                    (
                        "Mean episode net return (%)",
                        format_percentage(metrics.mean_episode_net_return_pct, 4, missing="n/a"),
                    ),
                    (
                        "Conditional trade net return (%)",
                        format_percentage(
                            metrics.conditional_trade_net_return_pct, 4, missing="n/a"
                        ),
                    ),
                    (
                        "Total net PnL ($)",
                        format_number(metrics.total_net_pnl_usd, 2, missing="n/a"),
                    ),
                    ("Profit factor", format_number(metrics.profit_factor, 4, missing="n/a")),
                    ("Win rate (%)", format_percentage(metrics.win_rate_pct, 2, missing="n/a")),
                ],
            )
        )
        lines.append("")
    lines.extend(
        [
            f"Mean paired delta (challenger - baseline): "
            f"{format_percentage(report.mean_paired_delta_pct, 4, missing='n/a')}",
            "",
            "## Formal inference (challenger_inference.py)",
            "",
        ]
    )
    readiness = report.inference.readiness
    lines.extend(
        markdown_table(
            ("Metric", "Value"),
            [
                ("Readiness status", readiness.status),
                ("Eligible episodes", readiness.eligible_episodes),
                ("Formal sample episodes", readiness.formal_sample_episodes),
                ("Formal sample clusters (assets)", readiness.formal_sample_clusters),
                ("Completely paired episodes", readiness.completely_paired_episodes),
                ("Minimum triggered episodes required", readiness.minimum_triggered_episodes),
                ("Least-triggered variant", readiness.least_triggered_variant or "n/a"),
                ("Least-triggered count", readiness.least_triggered_count),
            ],
        )
    )
    if report.inference.challengers:
        challenger = report.inference.challengers[0]
        lines.extend(["", "### Challenger statistical result", ""])
        lines.extend(
            markdown_table(
                ("Metric", "Value"),
                [
                    ("Verdict (statistical only)", challenger.verdict),
                    (
                        "Own expectancy CI",
                        f"[{challenger.strategy.estimate.lower_bound:.4f}, "
                        f"{challenger.strategy.estimate.upper_bound:.4f}]",
                    ),
                    (
                        "Minimum leave-one-asset-out (%)",
                        f"{challenger.strategy.minimum_leave_one_cluster_out_pct:.4f}",
                    ),
                    ("Holm-adjusted p-value", f"{challenger.paired.holm_adjusted_p_value:.4f}"),
                    ("Holm rejected null", "yes" if challenger.paired.holm_rejected else "no"),
                    (
                        "Familywise CI",
                        f"[{challenger.paired.familywise_lower_bound:.4f}, "
                        f"{challenger.paired.familywise_upper_bound:.4f}]",
                    ),
                ],
            )
        )
    lines.extend(["", "## Independent leave-one-UTC-week-out sensitivity", ""])
    if report.week_sensitivity is None:
        lines.append("_No paired episodes available yet._")
    elif report.week_sensitivity.distinct_weeks < 2:
        lines.append(
            f"_Only {report.week_sensitivity.distinct_weeks} distinct UTC week(s) so far — "
            "leave-one-week-out needs at least 2 to be computable at all, and promotion "
            f"requires at least {manifest.min_formal_sample_weeks}._"
        )
    else:
        week_rows = [
            (week, f"{value:.4f}") for week, value in report.week_sensitivity.leave_one_week_out
        ]
        lines.extend(
            markdown_table(("Week held out", "Mean paired delta without it (%)"), week_rows)
        )
        lines.append(f"\nDistinct weeks: {report.week_sensitivity.distinct_weeks}")
        minimum = report.week_sensitivity.minimum_leave_one_week_out_pct
        lines.append(f"Minimum across weeks: {format_number(minimum, 4, missing='n/a')}%")
    lines.extend(
        [
            "",
            (
                "_Computed independently of `challenger_inference`'s own asset-level "
                "leave-one-out, because the historical lead's sign flipped between its "
                "two observed weeks — asset-level sensitivity alone cannot see that "
                "risk. See the module docstring._"
            ),
            "",
            f"## Final verdict: `{report.final_verdict}`",
            "",
            (
                "Requires: a canonical run (no CLI overrides) AND the statistical "
                "verdict `shadow_candidate` AND at least "
                f"{manifest.min_formal_sample_weeks} distinct UTC weeks in the frozen "
                "formal sample AND a positive minimum leave-one-week-out delta AND "
                "challenger profit factor (on the same frozen formal sample, never "
                f"all-to-date) > {manifest.min_profit_factor}. A positive paired delta "
                "against a losing baseline is not by itself a profitable strategy, and "
                "a non-canonical run (overridden resolver/costs/fallback) can never "
                "emit a promotion verdict regardless of its numbers."
            ),
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay the pre-registered confirmed-OI-growth baseline filter"
    )
    parser.add_argument(
        "--since",
        type=parse_utc_datetime,
        default=OI_GROWTH_FILTER_COHORT_START,
        help="inclusive UTC cutoff; fixed to the registered forward-only cohort start",
    )
    parser.add_argument(
        "--until",
        type=parse_utc_datetime,
        help="exclusive UTC cutoff; defaults to the run start",
    )
    parser.add_argument(
        "--strategy-version",
        action="append",
        help="recorded strategy cohort; fixed to the registered cohort",
    )
    parser.add_argument("--resolver-version", default=RESOLVER_VERSION)
    parser.add_argument(
        "--allow-fallback",
        action="store_true",
        help="allow fallback outcomes in a separately identified sensitivity run",
    )
    parser.add_argument(
        "--taker-fee-bps-per-side",
        type=float,
        default=DEFAULT_COSTS.taker_fee_bps_per_side,
    )
    parser.add_argument(
        "--funding-cost-bps-per-8h",
        type=float,
        default=DEFAULT_COSTS.funding_cost_bps_per_8h,
    )
    parser.add_argument("--code-revision", default=os.getenv("SCHURFER_GIT_SHA"))
    parser.add_argument(
        "--working-tree-dirty",
        action=argparse.BooleanOptionalAction,
        required=True,
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser


async def _run(args: argparse.Namespace) -> str:
    from .exchange_registry import EXCHANGE_FACTORIES
    from .replay_repository import ReplayRepository
    from .virtual_market import fetch_decision_market_paths

    generated_at = datetime.now(UTC)
    until = resolve_report_until(
        args.until,
        generated_at,
        cohort_start=OI_GROWTH_FILTER_COHORT_START,
        report_label="oi-growth-filter",
    )
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for oi-growth-filter-report")
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    filters = ReplayFilters(
        since=args.since,
        until=until,
        strategy_versions=tuple(args.strategy_version or OI_GROWTH_FILTER_STRATEGY_VERSIONS),
        resolver_version=args.resolver_version,
        required_horizons=DEFAULT_REPLAY_HORIZONS,
        allow_fallback=args.allow_fallback,
    )
    costs = CostParameters(
        taker_fee_bps_per_side=args.taker_fee_bps_per_side,
        funding_cost_bps_per_8h=args.funding_cost_bps_per_8h,
    )
    repository = ReplayRepository.from_url(db_url)
    try:
        decisions = await repository.load(filters)
    finally:
        await repository.close()
    dataset = build_replay_dataset(decisions, filters)
    selected = tuple(
        selection.decision
        for episode in dataset.eligible_episodes
        for selection in (select_score_policy(episode, SCORE_THRESHOLD_BASELINE_POLICY),)
        if selection.status == "selected" and selection.decision is not None
    )
    paths = await fetch_decision_market_paths(selected, EXCHANGE_FACTORIES)
    report = build_oi_growth_filter_report(
        dataset,
        filters,
        paths,
        generated_at=generated_at,
        code_revision=args.code_revision,
        working_tree_dirty=args.working_tree_dirty,
        costs=costs,
    )
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        output = asyncio.run(_run(args))
    except ReportWindowNotStartedError as exc:
        parser.error(str(exc))
        return
    sys.stdout.write(output)


if __name__ == "__main__":
    main()
