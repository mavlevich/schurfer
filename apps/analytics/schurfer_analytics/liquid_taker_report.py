"""Prospective report for the pre-registered low-impact taker shelf."""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import fmean, median
from typing import Any, Literal

from .clustered_inference import (
    CLUSTER_BOOTSTRAP_VERSION,
    DEFAULT_BOOTSTRAP_ITERATIONS,
    DEFAULT_BOOTSTRAP_SEED,
    ClusterObservation,
    cluster_bootstrap_mean,
    derived_seed,
)
from .episode_replay import PROTOCOL_VERSION
from .outcomes import RESOLVER_VERSION
from .replay import (
    DEFAULT_REPLAY_HORIZONS,
    FOUNDATION_VERSION,
    MIN_FORMAL_CLUSTERS,
    QUERY_VERSION,
    ReplayDataset,
    ReplayDecision,
    ReplayEpisode,
    ReplayFilters,
)
from .reporting import (
    ReportWindowNotStartedError,
    format_number,
    format_percentage,
    markdown_table,
    normalize_code_revision,
    parse_utc_datetime,
    profit_factor,
    render_dataclass_json,
    resolve_report_until,
)
from .runtime_observability import log_report_phase
from .virtual_market import (
    DECISION_MARKET_PATH_VERSION,
    DecisionMarketPath,
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
    decision_impact_bps,
    max_sequential_drawdown_usd,
    simulate_decision,
)

LIQUID_TAKER_REPORT_VERSION = "liquid_taker_forward_report_v1"
LIQUID_TAKER_CANDIDATE_VERSION = "liquid_taker_candidate_v1"
LIQUID_TAKER_SELECTION_VERSION = "first_recorded_liquid_gate_eligible_decision_v1"
LIQUID_TAKER_COHORT_START = datetime(2026, 7, 30, tzinfo=UTC)
LIQUID_TAKER_STRATEGY_VERSIONS = ("pump_short_v1_market_quality",)
MAX_ROUND_TRIP_IMPACT_BPS = 20.0
FORMAL_EPISODES = 100
FORMAL_WEEKS = 4
TOP_ASSET_SENSITIVITY_COUNT = 5

SelectionStatus = Literal["selected", "not_triggered", "unresolved"]


@dataclass(frozen=True)
class LiquidTakerSelection:
    status: SelectionStatus
    decision: ReplayDecision | None
    bid_impact_bps: float | None
    ask_impact_bps: float | None
    measured_capacity_floor_usd: float | None
    error: str | None = None


@dataclass(frozen=True)
class LiquidTakerManifest:
    protocol_version: str
    replay_engine_version: str
    replay_query_version: str
    report_version: str
    candidate_version: str
    selection_version: str
    virtual_strategy_version: str
    entry_model_version: str
    exit_model_version: str
    cost_model_version: str
    market_path_version: str
    bootstrap_version: str
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
    maximum_round_trip_impact_bps: float
    taker_fee_bps_per_side: float
    funding_cost_bps_per_8h: float
    bootstrap_iterations: int
    bootstrap_seed: int
    formal_episodes: int = FORMAL_EPISODES
    formal_clusters: int = MIN_FORMAL_CLUSTERS
    formal_weeks: int = FORMAL_WEEKS
    exact_venue_only: bool = True
    no_trigger_policy: str = "zero_return_cash_when_never_triggered"
    report_scope: str = "prospective_shadow_only"


@dataclass(frozen=True)
class LiquidTakerResult:
    pump_event_id: int
    cluster_key: str
    base: str
    episode_at: datetime
    episode_week: str
    status: str
    selected_decision_id: str | None
    selected_at: datetime | None
    exchange: str | None
    recorded_score: int | None
    recorded_score_threshold: int | None
    bid_impact_bps: float | None
    ask_impact_bps: float | None
    round_trip_impact_bps: float | None
    measured_capacity_floor_usd: float | None
    episode_net_return_pct: float | None
    trade: VirtualTrade | None
    error: str | None = None


@dataclass(frozen=True)
class LiquidTakerMetrics:
    eligible_episodes: int
    resolved_episodes: int
    selected: int
    cash: int
    completed_trades: int
    unresolved: int
    clusters: int
    calendar_weeks: int
    calendar_days: int
    opportunities_per_calendar_day: float | None
    trade_rate_pct: float | None
    mean_episode_net_return_pct: float | None
    mean_trade_net_return_pct: float | None
    median_trade_net_return_pct: float | None
    win_rate_pct: float | None
    profit_factor: float | None
    total_net_pnl_usd: float | None
    max_sequential_drawdown_usd: float | None
    initial_stop_rate_pct: float | None
    mean_mfe_pct: float | None
    mean_mae_pct: float | None
    mean_gross_return_pct: float | None
    mean_fee_cost_bps: float | None
    mean_funding_cost_bps: float | None
    mean_slippage_cost_bps: float | None
    mean_position_usd: float | None
    median_measured_capacity_floor_usd: float | None
    capacity_coverage_pct: float | None
    expected_concurrent_positions: float | None
    expected_occupied_notional_usd: float | None
    descriptive_monthly_net_pnl_usd: float | None


@dataclass(frozen=True)
class SliceMetrics:
    name: str
    episodes: int
    trades: int
    share_pct: float | None
    mean_episode_net_return_pct: float | None
    mean_trade_net_return_pct: float | None


@dataclass(frozen=True)
class FormalInference:
    status: str
    episodes: int
    clusters: int
    weeks: int
    point_estimate_pct: float | None
    lower_95_pct: float | None
    upper_95_pct: float | None
    busiest_week: str | None
    excluding_busiest_week_pct: float | None
    minimum_top_asset_exclusion_pct: float | None
    verdict: str


@dataclass(frozen=True)
class CountRow:
    name: str
    count: int


@dataclass(frozen=True)
class LiquidTakerReport:
    manifest: LiquidTakerManifest
    dataset_episodes: int
    eligible_episodes: int
    excluded_episodes: int
    input_exclusion_reasons: tuple[CountRow, ...]
    path_statuses: tuple[CountRow, ...]
    metrics: LiquidTakerMetrics
    venue_slices: tuple[SliceMetrics, ...]
    weekly_slices: tuple[SliceMetrics, ...]
    asset_slices: tuple[SliceMetrics, ...]
    formal_inference: FormalInference
    episode_results: tuple[LiquidTakerResult, ...]
    market_paths: tuple[DecisionMarketPath, ...]


def _finite_number(value: Any, *, positive: bool = False) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or (positive and parsed <= 0):
        return None
    return parsed


def _recorded_score_threshold(decision: ReplayDecision) -> int | None:
    if not isinstance(decision.features, dict):
        return None
    config = decision.features.get("config")
    if not isinstance(config, dict):
        return None
    value = config.get("score_threshold")
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10:
        return None
    return value


def _recorded_market_quality(decision: ReplayDecision) -> bool | None:
    if not isinstance(decision.features, dict):
        return None
    config = decision.features.get("config")
    if not isinstance(config, dict) or config.get("require_market_quality") is not True:
        return None
    if not isinstance(decision.liquidity, dict):
        return None
    quality = decision.liquidity.get("quality")
    if not isinstance(quality, dict):
        return None
    allowed = quality.get("allowed")
    return allowed if isinstance(allowed, bool) else None


def measured_capacity_floor_usd(
    decision: ReplayDecision,
    *,
    maximum_round_trip_impact_bps: float = MAX_ROUND_TRIP_IMPACT_BPS,
) -> float | None:
    """Return the largest measured notional that passes both recorded impact legs."""
    liquidity = decision.liquidity
    if not isinstance(liquidity, dict) or liquidity.get("status") != "sampled":
        return None
    bids = liquidity.get("bid_impact_bps")
    asks = liquidity.get("ask_impact_bps")
    if not isinstance(bids, dict) or not isinstance(asks, dict):
        return None
    passing: list[float] = []
    for raw_target, raw_bid in bids.items():
        target = _finite_number(raw_target, positive=True)
        bid = _finite_number(raw_bid)
        ask = _finite_number(asks.get(raw_target))
        if (
            target is not None
            and bid is not None
            and ask is not None
            and bid >= 0
            and ask >= 0
            and bid + ask <= maximum_round_trip_impact_bps
        ):
            passing.append(target)
    return max(passing) if passing else None


def select_liquid_taker_decision(
    episode: ReplayEpisode,
    *,
    maximum_round_trip_impact_bps: float = MAX_ROUND_TRIP_IMPACT_BPS,
) -> LiquidTakerSelection:
    """Select the first point-in-time decision that passes the registered shelf."""
    for decision in episode.decisions:
        threshold = _recorded_score_threshold(decision)
        if (
            threshold is None
            or decision.score is None
            or isinstance(decision.score, bool)
            or not 0 <= decision.score <= 10
        ):
            return LiquidTakerSelection(
                "unresolved", None, None, None, None, "invalid_recorded_score_gate"
            )
        if decision.score < threshold:
            continue
        quality_allowed = _recorded_market_quality(decision)
        if quality_allowed is None:
            return LiquidTakerSelection(
                "unresolved", None, None, None, None, "invalid_recorded_market_quality_gate"
            )
        if not quality_allowed:
            continue
        bid = decision_impact_bps(decision, "bid")
        ask = decision_impact_bps(decision, "ask")
        if bid is None or ask is None:
            return LiquidTakerSelection(
                "unresolved", None, None, None, None, "missing_configured_notional_impact"
            )
        if bid + ask > maximum_round_trip_impact_bps:
            continue
        return LiquidTakerSelection(
            "selected",
            decision,
            bid,
            ask,
            measured_capacity_floor_usd(
                decision,
                maximum_round_trip_impact_bps=maximum_round_trip_impact_bps,
            ),
        )
    return LiquidTakerSelection("not_triggered", None, None, None, None)


def selected_liquid_taker_decisions(
    episodes: tuple[ReplayEpisode, ...],
) -> tuple[ReplayDecision, ...]:
    selected: dict[str, ReplayDecision] = {}
    for episode in episodes:
        result = select_liquid_taker_decision(episode)
        if result.status != "selected" or result.decision is None:
            continue
        decision_id = result.decision.decision_id
        if not decision_id:
            raise ValueError("selected liquid-taker decision requires a decision id")
        selected[decision_id] = result.decision
    return tuple(selected[key] for key in sorted(selected))


def _week(value: datetime) -> str:
    year, week, _ = value.astimezone(UTC).isocalendar()
    return f"{year}-W{week:02d}"


def _missing_path(episode: ReplayEpisode, decision: ReplayDecision) -> MarketPath:
    return MarketPath(
        pump_event_id=episode.pump_event_id,
        exchange=decision.exchange,
        base=decision.base,
        status="missing_path",
        candles=(),
        error="market path was not loaded",
    )


def evaluate_liquid_taker_episode(
    episode: ReplayEpisode,
    path_by_decision: dict[str, MarketPath],
    costs: CostParameters,
) -> LiquidTakerResult:
    selection = select_liquid_taker_decision(episode)
    week = _week(episode.first_decision_at)
    if selection.status == "not_triggered":
        return LiquidTakerResult(
            episode.pump_event_id,
            episode.cluster_key,
            episode.base,
            episode.first_decision_at,
            week,
            "not_triggered",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            0.0,
            None,
        )
    decision = selection.decision
    if selection.status == "unresolved" or decision is None:
        return LiquidTakerResult(
            episode.pump_event_id,
            episode.cluster_key,
            episode.base,
            episode.first_decision_at,
            week,
            "selection_unresolved",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            selection.error,
        )
    path = path_by_decision.get(decision.decision_id or "")
    if path is None:
        path = _missing_path(episode, decision)
    trade = simulate_decision(
        episode,
        path,
        decision,
        selection_reason=LIQUID_TAKER_CANDIDATE_VERSION,
        costs=costs,
    )
    threshold = _recorded_score_threshold(decision)
    round_trip = (
        selection.bid_impact_bps + selection.ask_impact_bps
        if selection.bid_impact_bps is not None and selection.ask_impact_bps is not None
        else None
    )
    return LiquidTakerResult(
        episode.pump_event_id,
        episode.cluster_key,
        episode.base,
        episode.first_decision_at,
        week,
        trade.status,
        decision.decision_id,
        decision.ts,
        decision.exchange,
        decision.score,
        threshold,
        selection.bid_impact_bps,
        selection.ask_impact_bps,
        round_trip,
        selection.measured_capacity_floor_usd,
        trade.net_return_pct,
        trade,
        trade.error,
    )


def _mean(values: list[float]) -> float | None:
    return fmean(values) if values else None


def _count_rows(counter: Counter[str]) -> tuple[CountRow, ...]:
    return tuple(
        CountRow(name, count)
        for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    )


def _calendar_days(since: datetime, until: datetime) -> int:
    elapsed_days = (until - since).total_seconds() / (24 * 60 * 60)
    return max(1, math.ceil(elapsed_days))


def liquid_taker_metrics(
    results: tuple[LiquidTakerResult, ...],
    filters: ReplayFilters,
) -> LiquidTakerMetrics:
    resolved = [
        result.episode_net_return_pct
        for result in results
        if result.episode_net_return_pct is not None
    ]
    trades = tuple(
        result.trade
        for result in results
        if result.trade is not None and result.trade.status == "complete"
    )
    returns = [trade.net_return_pct for trade in trades if trade.net_return_pct is not None]
    positions = [trade.position_usd for trade in trades if trade.position_usd is not None]
    capacities = [
        result.measured_capacity_floor_usd
        for result in results
        if result.selected_decision_id is not None
        and result.measured_capacity_floor_usd is not None
    ]
    durations = [trade.duration_minutes for trade in trades if trade.duration_minutes is not None]
    days = _calendar_days(filters.since or filters.until, filters.until)
    selected_count = sum(result.selected_decision_id is not None for result in results)
    opportunities_per_day = selected_count / days if days else None
    concurrent = (
        opportunities_per_day * fmean(durations) / (24 * 60)
        if opportunities_per_day is not None and durations
        else None
    )
    mean_position = _mean(positions)
    net_pnl_values = [trade.net_pnl_usd for trade in trades if trade.net_pnl_usd is not None]
    return LiquidTakerMetrics(
        eligible_episodes=len(results),
        resolved_episodes=len(resolved),
        selected=selected_count,
        cash=sum(result.status == "not_triggered" for result in results),
        completed_trades=len(trades),
        unresolved=len(results) - len(resolved),
        clusters=len({result.cluster_key for result in results}),
        calendar_weeks=len({result.episode_week for result in results}),
        calendar_days=days,
        opportunities_per_calendar_day=opportunities_per_day,
        trade_rate_pct=len(trades) / len(resolved) * 100 if resolved else None,
        mean_episode_net_return_pct=_mean(resolved),
        mean_trade_net_return_pct=_mean(returns),
        median_trade_net_return_pct=median(returns) if returns else None,
        win_rate_pct=sum(value > 0 for value in returns) / len(returns) * 100 if returns else None,
        profit_factor=profit_factor(returns),
        total_net_pnl_usd=sum(net_pnl_values) if net_pnl_values else None,
        max_sequential_drawdown_usd=max_sequential_drawdown_usd(trades),
        initial_stop_rate_pct=(
            sum(trade.exit_reason == "initial_sl" for trade in trades) / len(trades) * 100
            if trades
            else None
        ),
        mean_mfe_pct=_mean([trade.mfe_pct for trade in trades if trade.mfe_pct is not None]),
        mean_mae_pct=_mean([trade.mae_pct for trade in trades if trade.mae_pct is not None]),
        mean_gross_return_pct=_mean(
            [trade.gross_return_pct for trade in trades if trade.gross_return_pct is not None]
        ),
        mean_fee_cost_bps=_mean(
            [trade.fee_cost_bps for trade in trades if trade.fee_cost_bps is not None]
        ),
        mean_funding_cost_bps=_mean(
            [trade.funding_cost_bps for trade in trades if trade.funding_cost_bps is not None]
        ),
        mean_slippage_cost_bps=_mean(
            [trade.slippage_cost_bps for trade in trades if trade.slippage_cost_bps is not None]
        ),
        mean_position_usd=mean_position,
        median_measured_capacity_floor_usd=median(capacities) if capacities else None,
        capacity_coverage_pct=(len(capacities) / selected_count * 100 if selected_count else None),
        expected_concurrent_positions=concurrent,
        expected_occupied_notional_usd=(
            concurrent * mean_position
            if concurrent is not None and mean_position is not None
            else None
        ),
        descriptive_monthly_net_pnl_usd=(
            opportunities_per_day * 30 * fmean(net_pnl_values)
            if opportunities_per_day is not None and net_pnl_values
            else None
        ),
    )


def _slice_metrics(
    results: tuple[LiquidTakerResult, ...],
    *,
    key: Literal["exchange", "episode_week", "cluster_key"],
) -> tuple[SliceMetrics, ...]:
    resolved = tuple(
        result
        for result in results
        if result.episode_net_return_pct is not None
        and (key != "exchange" or result.selected_decision_id is not None)
    )
    names = sorted({str(getattr(result, key) or "<empty>") for result in resolved})
    rows: list[SliceMetrics] = []
    for name in names:
        group = tuple(
            result for result in resolved if str(getattr(result, key) or "<empty>") == name
        )
        trades = tuple(
            result.trade
            for result in group
            if result.trade is not None and result.trade.status == "complete"
        )
        rows.append(
            SliceMetrics(
                name=name,
                episodes=len(group),
                trades=len(trades),
                share_pct=len(group) / len(resolved) * 100 if resolved else None,
                mean_episode_net_return_pct=_mean(
                    [
                        result.episode_net_return_pct
                        for result in group
                        if result.episode_net_return_pct is not None
                    ]
                ),
                mean_trade_net_return_pct=_mean(
                    [trade.net_return_pct for trade in trades if trade.net_return_pct is not None]
                ),
            )
        )
    return tuple(sorted(rows, key=lambda row: (-row.episodes, row.name)))


def _formal_inference(
    results: tuple[LiquidTakerResult, ...],
    *,
    bootstrap_iterations: int,
    bootstrap_seed: int,
) -> FormalInference:
    ordered = tuple(
        sorted(
            results,
            key=lambda result: (
                result.episode_at,
                result.pump_event_id,
            ),
        )
    )
    prefix: tuple[LiquidTakerResult, ...] = ()
    for end in range(1, len(ordered) + 1):
        candidate = ordered[:end]
        if (
            len(candidate) >= FORMAL_EPISODES
            and len({row.cluster_key for row in candidate}) >= MIN_FORMAL_CLUSTERS
            and len({row.episode_week for row in candidate}) >= FORMAL_WEEKS
        ):
            prefix = candidate
            break
    if not prefix:
        return FormalInference(
            "collecting",
            len(ordered),
            len({row.cluster_key for row in ordered}),
            len({row.episode_week for row in ordered}),
            None,
            None,
            None,
            None,
            None,
            None,
            "withheld",
        )
    if any(result.episode_net_return_pct is None for result in prefix):
        return FormalInference(
            "awaiting_complete_resolution",
            len(prefix),
            len({row.cluster_key for row in prefix}),
            len({row.episode_week for row in prefix}),
            None,
            None,
            None,
            None,
            None,
            None,
            "withheld",
        )
    observations = tuple(
        ClusterObservation(result.cluster_key, result.episode_net_return_pct)
        for result in prefix
        if result.episode_net_return_pct is not None
    )
    estimate = cluster_bootstrap_mean(
        observations,
        iterations=bootstrap_iterations,
        seed=derived_seed(bootstrap_seed, LIQUID_TAKER_CANDIDATE_VERSION),
    ).estimate
    week_counts = Counter(result.episode_week for result in prefix)
    busiest_week = sorted(week_counts, key=lambda week: (-week_counts[week], week))[0]
    without_week = [
        result.episode_net_return_pct
        for result in prefix
        if result.episode_week != busiest_week and result.episode_net_return_pct is not None
    ]
    asset_counts = Counter(result.cluster_key for result in prefix)
    top_assets = tuple(
        key
        for key, _ in sorted(asset_counts.items(), key=lambda item: (-item[1], item[0]))[
            :TOP_ASSET_SENSITIVITY_COUNT
        ]
    )
    asset_exclusions = [
        fmean(
            result.episode_net_return_pct
            for result in prefix
            if result.cluster_key != cluster and result.episode_net_return_pct is not None
        )
        for cluster in top_assets
        if any(result.cluster_key != cluster for result in prefix)
    ]
    excluding_week = _mean(without_week)
    minimum_asset = min(asset_exclusions) if asset_exclusions else None
    passed = (
        estimate.lower_bound > 0
        and excluding_week is not None
        and excluding_week > 0
        and minimum_asset is not None
        and minimum_asset > 0
    )
    return FormalInference(
        "ready",
        len(prefix),
        estimate.clusters,
        len(week_counts),
        estimate.point_estimate,
        estimate.lower_bound,
        estimate.upper_bound,
        busiest_week,
        excluding_week,
        minimum_asset,
        "shadow_candidate" if passed else "do_not_promote",
    )


def build_liquid_taker_report(
    dataset: ReplayDataset,
    filters: ReplayFilters,
    paths: tuple[DecisionMarketPath, ...],
    *,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
    costs: CostParameters = DEFAULT_COSTS,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    bootstrap_seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> LiquidTakerReport:
    revision = normalize_code_revision(code_revision)
    if filters.since != LIQUID_TAKER_COHORT_START:
        raise ValueError("liquid-taker report requires the registered cohort start")
    if filters.strategy_versions != LIQUID_TAKER_STRATEGY_VERSIONS:
        raise ValueError("liquid-taker report requires the registered strategy cohort")
    if filters.allow_fallback:
        raise ValueError("liquid-taker report requires exact venue outcomes")
    path_counts = Counter(path.decision_id for path in paths)
    duplicates = sorted(decision_id for decision_id, count in path_counts.items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate market paths for decisions: {duplicates}")
    path_by_decision = {item.decision_id: item.path for item in paths}
    results = tuple(
        evaluate_liquid_taker_episode(episode, path_by_decision, costs)
        for episode in dataset.eligible_episodes
    )
    exclusions = Counter(
        reason for episode in dataset.excluded_episodes for reason in episode.exclusion_reasons
    )
    return LiquidTakerReport(
        manifest=LiquidTakerManifest(
            protocol_version=PROTOCOL_VERSION,
            replay_engine_version=FOUNDATION_VERSION,
            replay_query_version=QUERY_VERSION,
            report_version=LIQUID_TAKER_REPORT_VERSION,
            candidate_version=LIQUID_TAKER_CANDIDATE_VERSION,
            selection_version=LIQUID_TAKER_SELECTION_VERSION,
            virtual_strategy_version=VIRTUAL_STRATEGY_VERSION,
            entry_model_version=ENTRY_MODEL_VERSION,
            exit_model_version=EXIT_MODEL_VERSION,
            cost_model_version=COST_MODEL_VERSION,
            market_path_version=DECISION_MARKET_PATH_VERSION,
            bootstrap_version=CLUSTER_BOOTSTRAP_VERSION,
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
            maximum_round_trip_impact_bps=MAX_ROUND_TRIP_IMPACT_BPS,
            taker_fee_bps_per_side=costs.taker_fee_bps_per_side,
            funding_cost_bps_per_8h=costs.funding_cost_bps_per_8h,
            bootstrap_iterations=bootstrap_iterations,
            bootstrap_seed=bootstrap_seed,
        ),
        dataset_episodes=len(dataset.episodes),
        eligible_episodes=len(dataset.eligible_episodes),
        excluded_episodes=len(dataset.excluded_episodes),
        input_exclusion_reasons=_count_rows(exclusions),
        path_statuses=_count_rows(Counter(item.path.status for item in paths)),
        metrics=liquid_taker_metrics(results, filters),
        venue_slices=_slice_metrics(results, key="exchange"),
        weekly_slices=_slice_metrics(results, key="episode_week"),
        asset_slices=_slice_metrics(results, key="cluster_key"),
        formal_inference=_formal_inference(
            results,
            bootstrap_iterations=bootstrap_iterations,
            bootstrap_seed=bootstrap_seed,
        ),
        episode_results=results,
        market_paths=paths,
    )


def render_json(report: LiquidTakerReport) -> str:
    return render_dataclass_json(report)


def _render_slices(rows: tuple[SliceMetrics, ...]) -> list[str]:
    return markdown_table(
        ("Slice", "Episodes", "Trades", "Share", "Episode net", "Trade net"),
        [
            (
                row.name,
                row.episodes,
                row.trades,
                format_percentage(row.share_pct, missing="n/a"),
                format_percentage(row.mean_episode_net_return_pct, missing="n/a"),
                format_percentage(row.mean_trade_net_return_pct, missing="n/a"),
            )
            for row in rows
        ],
    )


def render_markdown(report: LiquidTakerReport) -> str:
    manifest = report.manifest
    metrics = report.metrics
    inference = report.formal_inference
    lines = [
        "# Pump Short Liquid Taker Forward Replay",
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
        "",
        (
            f"> Formal inference status: `{inference.status}`. "
            "This report is shadow-only and never changes production or authorizes real trading."
        ),
        "",
        "## Registered candidate",
        "",
    ]
    lines.extend(
        markdown_table(
            ("Component", "Version / value"),
            [
                ("Candidate", manifest.candidate_version),
                ("Selection", manifest.selection_version),
                ("Entry", manifest.entry_model_version),
                ("Exit", manifest.exit_model_version),
                ("Costs", manifest.cost_model_version),
                ("Market path", manifest.market_path_version),
                ("Maximum round-trip impact", f"{manifest.maximum_round_trip_impact_bps:.2f} bps"),
                ("Exact venue only", "yes"),
                ("No trigger", manifest.no_trigger_policy),
            ],
        )
    )
    lines.extend(["", "## Coverage", ""])
    lines.extend(
        markdown_table(
            ("Metric", "Value"),
            [
                ("Dataset episodes", report.dataset_episodes),
                ("Eligible episodes", report.eligible_episodes),
                ("Excluded episodes", report.excluded_episodes),
                ("Resolved candidate episodes", metrics.resolved_episodes),
                ("Selected decisions", metrics.selected),
                ("Completed trades", metrics.completed_trades),
                ("Cash episodes", metrics.cash),
                ("Unresolved", metrics.unresolved),
                ("Asset clusters", metrics.clusters),
                ("UTC calendar weeks", metrics.calendar_weeks),
                ("UTC calendar days", metrics.calendar_days),
            ],
        )
    )
    lines.extend(["", "## Input exclusions", ""])
    lines.extend(
        markdown_table(
            ("Reason", "Episodes"),
            [(row.name, row.count) for row in report.input_exclusion_reasons],
        )
    )
    lines.extend(["", "## Market paths", ""])
    lines.extend(
        markdown_table(
            ("Status", "Paths"),
            [(row.name, row.count) for row in report.path_statuses],
        )
    )
    lines.extend(["", "## Economics", ""])
    lines.extend(
        markdown_table(
            ("Metric", "Value"),
            [
                (
                    "Opportunities / calendar day",
                    format_number(metrics.opportunities_per_calendar_day),
                ),
                ("Trade rate", format_percentage(metrics.trade_rate_pct, missing="n/a")),
                (
                    "Mean episode net",
                    format_percentage(metrics.mean_episode_net_return_pct, missing="n/a"),
                ),
                (
                    "Mean trade net",
                    format_percentage(metrics.mean_trade_net_return_pct, missing="n/a"),
                ),
                (
                    "Median trade net",
                    format_percentage(metrics.median_trade_net_return_pct, missing="n/a"),
                ),
                ("Win rate", format_percentage(metrics.win_rate_pct, missing="n/a")),
                ("Profit factor", format_number(metrics.profit_factor, missing="n/a")),
                (
                    "Total net P&L",
                    format_number(metrics.total_net_pnl_usd, suffix=" USD", missing="n/a"),
                ),
                (
                    "Max sequential drawdown",
                    format_number(
                        metrics.max_sequential_drawdown_usd, suffix=" USD", missing="n/a"
                    ),
                ),
                (
                    "Initial stop rate",
                    format_percentage(metrics.initial_stop_rate_pct, missing="n/a"),
                ),
                ("Mean MFE", format_percentage(metrics.mean_mfe_pct, missing="n/a")),
                ("Mean MAE", format_percentage(metrics.mean_mae_pct, missing="n/a")),
            ],
        )
    )
    lines.extend(["", "## Cost decomposition", ""])
    lines.extend(
        markdown_table(
            ("Metric", "Value"),
            [
                (
                    "Mean gross return",
                    format_percentage(metrics.mean_gross_return_pct, missing="n/a"),
                ),
                (
                    "Mean fee cost",
                    format_number(metrics.mean_fee_cost_bps, suffix=" bps", missing="n/a"),
                ),
                (
                    "Mean funding cost",
                    format_number(metrics.mean_funding_cost_bps, suffix=" bps", missing="n/a"),
                ),
                (
                    "Mean slippage cost",
                    format_number(metrics.mean_slippage_cost_bps, suffix=" bps", missing="n/a"),
                ),
            ],
        )
    )
    lines.extend(["", "## Capacity and scale", ""])
    lines.extend(
        markdown_table(
            ("Metric", "Value"),
            [
                (
                    "Mean recorded position",
                    format_number(metrics.mean_position_usd, suffix=" USD", missing="n/a"),
                ),
                (
                    "Median measured capacity floor",
                    format_number(
                        metrics.median_measured_capacity_floor_usd,
                        suffix=" USD",
                        missing="n/a",
                    ),
                ),
                (
                    "Capacity coverage",
                    format_percentage(metrics.capacity_coverage_pct, missing="n/a"),
                ),
                (
                    "Expected concurrent positions",
                    format_number(metrics.expected_concurrent_positions, missing="n/a"),
                ),
                (
                    "Expected occupied notional",
                    format_number(
                        metrics.expected_occupied_notional_usd,
                        suffix=" USD",
                        missing="n/a",
                    ),
                ),
                (
                    "Descriptive monthly net P&L",
                    format_number(
                        metrics.descriptive_monthly_net_pnl_usd,
                        suffix=" USD",
                        missing="n/a",
                    ),
                ),
            ],
        )
    )
    lines.extend(
        [
            "",
            (
                "_Capacity is the largest recorded depth target that passed the 20 bps "
                "round-trip limit. It is a measured floor, not unlimited executable size. "
                "Monthly P&L is a descriptive run-rate, not a forecast._"
            ),
            "",
            "## Venue sensitivity",
            "",
        ]
    )
    lines.extend(_render_slices(report.venue_slices))
    lines.extend(["", "## Weekly concentration", ""])
    lines.extend(_render_slices(report.weekly_slices))
    lines.extend(["", "## Asset concentration", ""])
    lines.extend(_render_slices(report.asset_slices[:10]))
    lines.extend(["", "## Formal cluster inference", ""])
    if inference.status != "ready":
        lines.append(
            "_Formal inference is withheld until the earliest chronological prefix has "
            "100 episodes, 30 asset clusters, four UTC weeks, and complete resolution._"
        )
    else:
        lines.extend(
            markdown_table(
                ("Metric", "Value"),
                [
                    ("Episodes", inference.episodes),
                    ("Clusters", inference.clusters),
                    ("Weeks", inference.weeks),
                    ("Mean net", format_percentage(inference.point_estimate_pct)),
                    ("95% lower", format_percentage(inference.lower_95_pct)),
                    ("95% upper", format_percentage(inference.upper_95_pct)),
                    ("Busiest week", inference.busiest_week or "n/a"),
                    (
                        "Net excluding busiest week",
                        format_percentage(inference.excluding_busiest_week_pct),
                    ),
                    (
                        "Minimum top-5 asset exclusion",
                        format_percentage(inference.minimum_top_asset_exclusion_pct),
                    ),
                    ("Verdict", inference.verdict),
                ],
            )
        )
    lines.extend(["", "## Episode results", ""])
    lines.extend(
        markdown_table(
            (
                "Episode",
                "Base",
                "Week",
                "Exchange",
                "Score",
                "Impact",
                "Capacity floor",
                "Status",
                "Exit",
                "Net",
            ),
            [
                (
                    result.pump_event_id,
                    result.base,
                    result.episode_week,
                    result.exchange or "",
                    (
                        f"{result.recorded_score}/{result.recorded_score_threshold}"
                        if result.recorded_score is not None
                        and result.recorded_score_threshold is not None
                        else "n/a"
                    ),
                    format_number(result.round_trip_impact_bps, suffix=" bps", missing="n/a"),
                    format_number(
                        result.measured_capacity_floor_usd,
                        suffix=" USD",
                        missing="n/a",
                    ),
                    result.status,
                    result.trade.exit_reason if result.trade else "",
                    format_percentage(result.episode_net_return_pct, missing="n/a"),
                )
                for result in report.episode_results
            ],
        )
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay the prospective low-impact taker shelf without changing production"
    )
    parser.add_argument(
        "--since",
        type=parse_utc_datetime,
        default=LIQUID_TAKER_COHORT_START,
        help="inclusive registered cohort start",
    )
    parser.add_argument("--until", type=parse_utc_datetime)
    parser.add_argument("--strategy-version", action="append")
    parser.add_argument("--resolver-version", default=RESOLVER_VERSION)
    parser.add_argument(
        "--bootstrap-iterations",
        type=int,
        default=DEFAULT_BOOTSTRAP_ITERATIONS,
    )
    parser.add_argument("--bootstrap-seed", type=int, default=DEFAULT_BOOTSTRAP_SEED)
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
    parser.add_argument(
        "--record-run",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="append sanitized run metadata to the research report registry",
    )
    return parser


async def _run(args: argparse.Namespace) -> str:
    from .liquid_taker_runtime import load_liquid_taker_runtime_inputs

    generated_at = datetime.now(UTC)
    until = resolve_report_until(
        args.until,
        generated_at,
        cohort_start=LIQUID_TAKER_COHORT_START,
        report_label="liquid-taker",
    )
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for liquid-taker-report")
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    filters = ReplayFilters(
        since=args.since,
        until=until,
        strategy_versions=tuple(args.strategy_version or LIQUID_TAKER_STRATEGY_VERSIONS),
        resolver_version=args.resolver_version,
        required_horizons=DEFAULT_REPLAY_HORIZONS,
        allow_fallback=False,
    )
    costs = CostParameters(
        taker_fee_bps_per_side=args.taker_fee_bps_per_side,
        funding_cost_bps_per_8h=args.funding_cost_bps_per_8h,
    )
    runtime_inputs = await load_liquid_taker_runtime_inputs(
        db_url,
        filters,
        report_name="liquid_taker",
        select_decisions=selected_liquid_taker_decisions,
    )
    report = build_liquid_taker_report(
        runtime_inputs.dataset,
        filters,
        runtime_inputs.market_paths,
        generated_at=generated_at,
        code_revision=args.code_revision,
        working_tree_dirty=args.working_tree_dirty,
        costs=costs,
        bootstrap_iterations=args.bootstrap_iterations,
        bootstrap_seed=args.bootstrap_seed,
    )
    log_report_phase(
        "liquid_taker",
        "report_built",
        episode_results=len(report.episode_results),
    )
    # The report retains formal market-path provenance; release the replay graph.
    del runtime_inputs
    if args.record_run:
        from sqlalchemy.exc import SQLAlchemyError

        from .report_registry import ReportRunRecord, record_report_run

        manifest = report.manifest
        inference = report.formal_inference
        metrics = report.metrics
        record = ReportRunRecord(
            contract=manifest.candidate_version,
            report_version=manifest.report_version,
            generated_at=manifest.generated_at,
            dataset_since=manifest.dataset_since,
            dataset_until_exclusive=manifest.dataset_until_exclusive,
            code_revision=manifest.code_revision,
            working_tree_dirty=manifest.working_tree_dirty,
            decision_input_fingerprint=manifest.decision_input_fingerprint,
            market_path_fingerprint=manifest.market_path_fingerprint,
            status=inference.status,
            verdict=inference.verdict,
            eligible_episodes=metrics.eligible_episodes,
            asset_clusters=metrics.clusters,
            calendar_weeks=metrics.calendar_weeks,
            summary={
                "resolved_episodes": metrics.resolved_episodes,
                "selected": metrics.selected,
                "cash": metrics.cash,
                "point_estimate_pct": inference.point_estimate_pct,
                "lower_95_pct": inference.lower_95_pct,
                "upper_95_pct": inference.upper_95_pct,
            },
        )
        try:
            await record_report_run(db_url, record)
        except SQLAlchemyError as exc:
            sys.stderr.write(f"WARNING: research report registry write failed: {exc}\n")
    output = render_json(report) if args.format == "json" else render_markdown(report)
    log_report_phase("liquid_taker", "report_rendered", output_characters=len(output))
    return output


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        output = asyncio.run(_run(args))
    except ReportWindowNotStartedError as exc:
        parser.error(str(exc))
    sys.stdout.write(output)
