"""Discovery-only economics of an exchange as a signal source.

The execution venue and the venue that first observed a pump are different causal
roles. This report keeps them separate, applies the unchanged liquid-taker selector,
and replays the existing v1 exit without changing production.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from statistics import fmean, median

from .clustered_inference import (
    CLUSTER_BOOTSTRAP_VERSION,
    DEFAULT_BOOTSTRAP_ITERATIONS,
    DEFAULT_BOOTSTRAP_SEED,
    ClusterObservation,
    cluster_bootstrap_mean,
    cluster_bootstrap_mean_null_p_value,
    derived_seed,
    holm_step_down,
)
from .exchange_coverage_report import (
    CoverageFilters,
    ExchangeCoverageReport,
    SourceObservation,
)
from .exchange_coverage_report import (
    build_report as build_coverage_report,
)
from .liquid_taker_report import (
    LIQUID_TAKER_SELECTION_VERSION,
    LIQUID_TAKER_STRATEGY_VERSIONS,
    MAX_ROUND_TRIP_IMPACT_BPS,
    LiquidTakerResult,
    evaluate_liquid_taker_episode,
    liquid_taker_metrics,
    select_liquid_taker_decision,
    selected_liquid_taker_decisions,
)
from .outcomes import RESOLVER_VERSION
from .replay import (
    FOUNDATION_VERSION,
    QUERY_VERSION,
    ReplayDataset,
    ReplayDecision,
    ReplayEpisode,
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
    DecisionMarketPath,
    decision_market_path_fingerprint,
)
from .virtual_strategy import DEFAULT_COSTS, CostParameters, MarketPath

REPORT_VERSION = "exchange_source_economics_discovery_v1"
SOURCE_MODEL_VERSION = "scanner_observed_first_source_v1"
SOURCE_ECONOMICS_COHORT_START = datetime(2026, 7, 24, tzinfo=UTC)
SOURCE_ECONOMICS_HORIZONS = (240, 480)
UNATTRIBUTED_SOURCE = "<unattributed>"
SOURCE_AFTER_DECISION = "<source_after_decision>"
MIN_HOLM_EPISODES = 5
MIN_HOLM_CLUSTERS = 2


@dataclass(frozen=True)
class SourceAttribution:
    event_id: int
    first_source_key: str
    first_sources: tuple[str, ...]
    first_seen_at: datetime
    source_count: int
    sole_source_by_cutoff: bool
    next_confirmation_delay_seconds: float | None


@dataclass(frozen=True)
class FixedHorizonEconomics:
    horizon_minutes: int
    status: str
    gross_return_pct: float | None
    net_return_pct: float | None
    error: str | None = None


@dataclass(frozen=True)
class SourceEpisodeResult:
    pump_event_id: int
    cluster_key: str
    base: str
    episode_at: datetime
    episode_week: str
    source_status: str
    first_source_key: str
    first_sources: tuple[str, ...]
    source_count_by_cutoff: int
    sole_source_by_cutoff: bool
    next_confirmation_delay_seconds: float | None
    sources_at_decision: int | None
    source_to_decision_seconds: float | None
    fixed_horizons: tuple[FixedHorizonEconomics, ...]
    liquid_taker: LiquidTakerResult


@dataclass(frozen=True)
class SourceEconomicsRow:
    first_source: str
    episodes: int
    selected: int
    completed_trades: int
    cash: int
    unresolved: int
    asset_clusters: int
    largest_cluster_share_pct: float | None
    selected_per_calendar_day: float | None
    selection_rate_pct: float | None
    mean_episode_net_pct: float | None
    mean_trade_net_pct: float | None
    median_trade_net_pct: float | None
    win_rate_pct: float | None
    profit_factor: float | None
    initial_stop_rate_pct: float | None
    max_drawdown_usd: float | None
    median_capacity_floor_usd: float | None
    full_net_lower_95_pct: float | None
    full_net_upper_95_pct: float | None
    weakest_leave_one_cluster_out_pct: float | None
    excluding_busiest_week_pct: float | None
    raw_p_value: float | None
    holm_adjusted_p_value: float | None
    holm_rejected: bool | None
    fixed_240_n: int
    fixed_240_mean_net_pct: float | None
    fixed_240_median_net_pct: float | None
    fixed_480_n: int
    fixed_480_mean_net_pct: float | None
    fixed_480_median_net_pct: float | None
    median_source_to_decision_seconds: float | None
    median_next_confirmation_delay_seconds: float | None


@dataclass(frozen=True)
class SourceExecutionRoute:
    first_source: str
    execution_exchange: str
    selected: int
    completed_trades: int
    asset_clusters: int
    mean_trade_net_pct: float | None
    median_trade_net_pct: float | None


@dataclass(frozen=True)
class CountRow:
    name: str
    count: int


@dataclass(frozen=True)
class ExchangeSourceManifest:
    report_version: str
    source_model_version: str
    replay_engine_version: str
    replay_query_version: str
    selection_version: str
    market_path_version: str
    bootstrap_version: str
    code_revision: str
    working_tree_dirty: bool
    generated_at: datetime
    dataset_since: datetime
    dataset_until_exclusive: datetime
    decision_input_fingerprint: str
    source_input_fingerprint: str
    market_path_fingerprint: str
    strategy_versions: tuple[str, ...]
    resolver_version: str
    required_horizons: tuple[int, ...]
    maximum_round_trip_impact_bps: float
    taker_fee_bps_per_side: float
    funding_cost_bps_per_8h: float
    bootstrap_iterations: int
    bootstrap_seed: int
    report_scope: str = "post_hoc_discovery_only_no_strategy_change"
    sole_source_policy: str = "coverage_counterfactual_only_never_a_live_feature"


@dataclass(frozen=True)
class ExchangeSourceEconomicsReport:
    manifest: ExchangeSourceManifest
    coverage: ExchangeCoverageReport
    dataset_episodes: int
    eligible_episodes: int
    excluded_episodes: int
    input_exclusion_reasons: tuple[CountRow, ...]
    attribution_statuses: tuple[CountRow, ...]
    path_statuses: tuple[CountRow, ...]
    source_economics: tuple[SourceEconomicsRow, ...]
    source_execution_routes: tuple[SourceExecutionRoute, ...]
    episode_results: tuple[SourceEpisodeResult, ...]
    market_paths: tuple[DecisionMarketPath, ...]


def _week(value: datetime) -> str:
    year, week, _ = value.astimezone(UTC).isocalendar()
    return f"{year}-W{week:02d}"


def _mean(values: list[float]) -> float | None:
    return fmean(values) if values else None


def _median(values: list[float]) -> float | None:
    return median(values) if values else None


def _count_rows(counter: Counter[str]) -> tuple[CountRow, ...]:
    return tuple(
        CountRow(name, count)
        for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    )


def source_input_fingerprint(observations: list[SourceObservation]) -> str:
    if any(row.first_seen_at.utcoffset() is None for row in observations):
        raise ValueError("source attribution timestamps must be timezone-aware")
    payload: list[dict[str, int | str]] = []
    for row in sorted(
        observations,
        key=lambda row: (row.event_id, row.first_seen_at, row.exchange.strip().lower()),
    ):
        payload.append(
            {
                "event_id": row.event_id,
                "exchange": row.exchange.strip().lower(),
                "first_seen_at": row.first_seen_at.astimezone(UTC).isoformat(),
            }
        )
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def build_source_attributions(
    observations: list[SourceObservation],
) -> dict[int, SourceAttribution]:
    grouped: dict[int, list[SourceObservation]] = defaultdict(list)
    seen: set[tuple[int, str]] = set()
    for row in observations:
        if row.event_id <= 0:
            raise ValueError("source attribution requires a positive event id")
        exchange = row.exchange.strip().lower()
        if not exchange:
            raise ValueError("source attribution requires an exchange")
        if row.first_seen_at.utcoffset() is None:
            raise ValueError("source attribution timestamps must be timezone-aware")
        identity = (row.event_id, exchange)
        if identity in seen:
            raise ValueError(f"duplicate source attribution: {identity}")
        seen.add(identity)
        grouped[row.event_id].append(SourceObservation(row.event_id, exchange, row.first_seen_at))

    attributions: dict[int, SourceAttribution] = {}
    for event_id, rows in grouped.items():
        ordered = sorted(rows, key=lambda row: (row.first_seen_at, row.exchange))
        first_seen_at = ordered[0].first_seen_at
        first_sources = tuple(
            sorted(row.exchange for row in ordered if row.first_seen_at == first_seen_at)
        )
        later = next((row for row in ordered if row.first_seen_at > first_seen_at), None)
        attributions[event_id] = SourceAttribution(
            event_id=event_id,
            first_source_key="+".join(first_sources),
            first_sources=first_sources,
            first_seen_at=first_seen_at,
            source_count=len(ordered),
            sole_source_by_cutoff=len(ordered) == 1,
            next_confirmation_delay_seconds=(
                (later.first_seen_at - first_seen_at).total_seconds() if later else None
            ),
        )
    return attributions


def _fixed_horizon(
    decision: ReplayDecision,
    *,
    horizon: int,
    bid_impact_bps: float,
    ask_impact_bps: float,
    costs: CostParameters,
) -> FixedHorizonEconomics:
    matches = [row for row in decision.outcomes if row.horizon_minutes == horizon]
    if len(matches) != 1:
        return FixedHorizonEconomics(
            horizon,
            "unresolved",
            None,
            None,
            f"expected one outcome, found {len(matches)}",
        )
    outcome = matches[0]
    if (
        outcome.status != "complete"
        or outcome.coverage_ratio is None
        or outcome.coverage_ratio < 0.9
        or outcome.short_return_pct is None
        or not math.isfinite(outcome.short_return_pct)
    ):
        return FixedHorizonEconomics(
            horizon,
            "unresolved",
            outcome.short_return_pct,
            None,
            "outcome is not complete exact coverage",
        )
    if outcome.anchor_exchange != decision.exchange or outcome.source_exchange != decision.exchange:
        return FixedHorizonEconomics(
            horizon,
            "unresolved",
            outcome.short_return_pct,
            None,
            "outcome venue does not match selected execution venue",
        )
    cost_bps = (
        bid_impact_bps
        + ask_impact_bps
        + 2 * costs.taker_fee_bps_per_side
        + costs.funding_cost_bps_per_8h * horizon / 480
    )
    return FixedHorizonEconomics(
        horizon,
        "complete",
        outcome.short_return_pct,
        outcome.short_return_pct - cost_bps / 100,
    )


def _evaluate_episode(
    episode: ReplayEpisode,
    attribution: SourceAttribution | None,
    observations: tuple[SourceObservation, ...],
    path_by_decision: dict[str, MarketPath],
    costs: CostParameters,
) -> SourceEpisodeResult:
    liquid = evaluate_liquid_taker_episode(episode, path_by_decision, costs)
    selection = select_liquid_taker_decision(episode)
    source_status = "attributed" if attribution else "missing_source_attribution"
    source_key = attribution.first_source_key if attribution else UNATTRIBUTED_SOURCE
    first_sources = attribution.first_sources if attribution else ()
    source_count = attribution.source_count if attribution else 0
    sole = attribution.sole_source_by_cutoff if attribution else False
    confirmation_delay = attribution.next_confirmation_delay_seconds if attribution else None
    sources_at_decision: int | None = None
    source_to_decision: float | None = None
    fixed: tuple[FixedHorizonEconomics, ...] = ()

    decision = selection.decision
    if selection.status == "selected" and decision is not None:
        if attribution is not None and attribution.first_seen_at > decision.ts:
            source_status = "source_after_decision"
            source_key = SOURCE_AFTER_DECISION
        if attribution is not None:
            source_to_decision = (decision.ts - attribution.first_seen_at).total_seconds()
            sources_at_decision = sum(row.first_seen_at <= decision.ts for row in observations)
        if selection.bid_impact_bps is not None and selection.ask_impact_bps is not None:
            fixed = tuple(
                _fixed_horizon(
                    decision,
                    horizon=horizon,
                    bid_impact_bps=selection.bid_impact_bps,
                    ask_impact_bps=selection.ask_impact_bps,
                    costs=costs,
                )
                for horizon in SOURCE_ECONOMICS_HORIZONS
            )

    return SourceEpisodeResult(
        pump_event_id=episode.pump_event_id,
        cluster_key=episode.cluster_key,
        base=episode.base,
        episode_at=episode.first_decision_at,
        episode_week=_week(episode.first_decision_at),
        source_status=source_status,
        first_source_key=source_key,
        first_sources=first_sources,
        source_count_by_cutoff=source_count,
        sole_source_by_cutoff=sole,
        next_confirmation_delay_seconds=confirmation_delay,
        sources_at_decision=sources_at_decision,
        source_to_decision_seconds=source_to_decision,
        fixed_horizons=fixed,
        liquid_taker=liquid,
    )


def _fixed_values(rows: tuple[SourceEpisodeResult, ...], horizon: int) -> list[float]:
    return [
        fixed.net_return_pct
        for row in rows
        for fixed in row.fixed_horizons
        if fixed.horizon_minutes == horizon
        and fixed.status == "complete"
        and fixed.net_return_pct is not None
    ]


def _robustness(
    rows: tuple[SourceEpisodeResult, ...],
    *,
    bootstrap_iterations: int,
    bootstrap_seed: int,
    label: str,
) -> tuple[float | None, float | None, float | None, float | None, float | None]:
    resolved = tuple(row for row in rows if row.liquid_taker.episode_net_return_pct is not None)
    if not resolved or len(resolved) != len(rows):
        return None, None, None, None, None
    observations = tuple(
        ClusterObservation(row.cluster_key, row.liquid_taker.episode_net_return_pct or 0.0)
        for row in resolved
    )
    estimate = cluster_bootstrap_mean(
        observations,
        iterations=bootstrap_iterations,
        seed=derived_seed(bootstrap_seed, label),
    ).estimate
    clusters = sorted({row.cluster_key for row in resolved})
    exclusions = [
        fmean(
            row.liquid_taker.episode_net_return_pct or 0.0
            for row in resolved
            if row.cluster_key != excluded
        )
        for excluded in clusters
        if any(row.cluster_key != excluded for row in resolved)
    ]
    week_counts = Counter(row.episode_week for row in resolved)
    busiest_week = sorted(week_counts, key=lambda key: (-week_counts[key], key))[0]
    without_week = [
        row.liquid_taker.episode_net_return_pct or 0.0
        for row in resolved
        if row.episode_week != busiest_week
    ]
    raw_p = (
        cluster_bootstrap_mean_null_p_value(
            observations,
            iterations=bootstrap_iterations,
            seed=derived_seed(bootstrap_seed, f"{label}:null"),
        )
        if len(resolved) >= MIN_HOLM_EPISODES
        and len({row.cluster_key for row in resolved}) >= MIN_HOLM_CLUSTERS
        else None
    )
    return (
        estimate.lower_bound,
        estimate.upper_bound,
        min(exclusions) if exclusions else None,
        _mean(without_week),
        raw_p,
    )


def _source_rows(
    results: tuple[SourceEpisodeResult, ...],
    filters: ReplayFilters,
    *,
    bootstrap_iterations: int,
    bootstrap_seed: int,
) -> tuple[SourceEconomicsRow, ...]:
    grouped: dict[str, list[SourceEpisodeResult]] = defaultdict(list)
    for row in results:
        grouped[row.first_source_key].append(row)

    rows: list[SourceEconomicsRow] = []
    for source, raw_group in sorted(grouped.items()):
        group = tuple(raw_group)
        liquid_rows = tuple(row.liquid_taker for row in group)
        metrics = liquid_taker_metrics(liquid_rows, filters)
        completed = tuple(
            row.liquid_taker.trade
            for row in group
            if row.liquid_taker.trade is not None and row.liquid_taker.trade.status == "complete"
        )
        returns = [trade.net_return_pct for trade in completed if trade.net_return_pct is not None]
        cluster_counts = Counter(row.cluster_key for row in group)
        lower, upper, weakest_cluster, excluding_week, raw_p = _robustness(
            group,
            bootstrap_iterations=bootstrap_iterations,
            bootstrap_seed=bootstrap_seed,
            label=f"{REPORT_VERSION}:{source}",
        )
        fixed_240 = _fixed_values(group, 240)
        fixed_480 = _fixed_values(group, 480)
        decision_delays = [
            row.source_to_decision_seconds
            for row in group
            if row.source_to_decision_seconds is not None and row.source_to_decision_seconds >= 0
        ]
        confirmation_delays = [
            row.next_confirmation_delay_seconds
            for row in group
            if row.next_confirmation_delay_seconds is not None
        ]
        rows.append(
            SourceEconomicsRow(
                first_source=source,
                episodes=len(group),
                selected=metrics.selected,
                completed_trades=metrics.completed_trades,
                cash=metrics.cash,
                unresolved=metrics.unresolved,
                asset_clusters=len(cluster_counts),
                largest_cluster_share_pct=(
                    max(cluster_counts.values()) / len(group) * 100 if group else None
                ),
                selected_per_calendar_day=metrics.opportunities_per_calendar_day,
                selection_rate_pct=(metrics.selected / len(group) * 100 if group else None),
                mean_episode_net_pct=metrics.mean_episode_net_return_pct,
                mean_trade_net_pct=metrics.mean_trade_net_return_pct,
                median_trade_net_pct=metrics.median_trade_net_return_pct,
                win_rate_pct=metrics.win_rate_pct,
                profit_factor=profit_factor(returns),
                initial_stop_rate_pct=metrics.initial_stop_rate_pct,
                max_drawdown_usd=metrics.max_sequential_drawdown_usd,
                median_capacity_floor_usd=metrics.median_measured_capacity_floor_usd,
                full_net_lower_95_pct=lower,
                full_net_upper_95_pct=upper,
                weakest_leave_one_cluster_out_pct=weakest_cluster,
                excluding_busiest_week_pct=excluding_week,
                raw_p_value=raw_p,
                holm_adjusted_p_value=None,
                holm_rejected=None,
                fixed_240_n=len(fixed_240),
                fixed_240_mean_net_pct=_mean(fixed_240),
                fixed_240_median_net_pct=_median(fixed_240),
                fixed_480_n=len(fixed_480),
                fixed_480_mean_net_pct=_mean(fixed_480),
                fixed_480_median_net_pct=_median(fixed_480),
                median_source_to_decision_seconds=_median(decision_delays),
                median_next_confirmation_delay_seconds=_median(confirmation_delays),
            )
        )

    p_values = {
        row.first_source: row.raw_p_value
        for row in rows
        if row.raw_p_value is not None
        and row.first_source not in {UNATTRIBUTED_SOURCE, SOURCE_AFTER_DECISION}
    }
    if p_values:
        decisions = {row.key: row for row in holm_step_down(p_values)}
        rows = [
            replace(
                row,
                holm_adjusted_p_value=decisions[row.first_source].adjusted_p_value,
                holm_rejected=decisions[row.first_source].rejected,
            )
            if row.first_source in decisions
            else row
            for row in rows
        ]
    return tuple(sorted(rows, key=lambda row: (-row.episodes, row.first_source)))


def _routes(results: tuple[SourceEpisodeResult, ...]) -> tuple[SourceExecutionRoute, ...]:
    grouped: dict[tuple[str, str], list[SourceEpisodeResult]] = defaultdict(list)
    for row in results:
        execution = row.liquid_taker.exchange
        if row.liquid_taker.selected_decision_id is not None and execution:
            grouped[(row.first_source_key, execution)].append(row)

    routes: list[SourceExecutionRoute] = []
    for (source, execution), group in grouped.items():
        completed = tuple(
            row.liquid_taker.trade
            for row in group
            if row.liquid_taker.trade is not None and row.liquid_taker.trade.status == "complete"
        )
        returns = [trade.net_return_pct for trade in completed if trade.net_return_pct is not None]
        routes.append(
            SourceExecutionRoute(
                first_source=source,
                execution_exchange=execution,
                selected=len(group),
                completed_trades=len(completed),
                asset_clusters=len({row.cluster_key for row in group}),
                mean_trade_net_pct=_mean(returns),
                median_trade_net_pct=_median(returns),
            )
        )
    return tuple(
        sorted(routes, key=lambda row: (-row.selected, row.first_source, row.execution_exchange))
    )


def _validate_contract(filters: ReplayFilters, bootstrap_iterations: int) -> None:
    if filters.since is None or filters.since < SOURCE_ECONOMICS_COHORT_START:
        raise ValueError("source economics cannot include the left-censored attribution window")
    if filters.strategy_versions != LIQUID_TAKER_STRATEGY_VERSIONS:
        raise ValueError("source economics requires the market-quality strategy cohort")
    if filters.required_horizons != SOURCE_ECONOMICS_HORIZONS:
        raise ValueError("source economics requires the fixed 240m and 480m outcomes")
    if filters.allow_fallback:
        raise ValueError("source economics requires exact venue outcomes")
    if bootstrap_iterations < 100:
        raise ValueError("bootstrap iterations must be at least 100")


def _validate_costs(costs: CostParameters) -> None:
    if not (math.isfinite(costs.taker_fee_bps_per_side) and costs.taker_fee_bps_per_side >= 0):
        raise ValueError("taker fee must be finite and non-negative")
    if not math.isfinite(costs.funding_cost_bps_per_8h):
        raise ValueError("funding cost must be finite")


def build_exchange_source_economics_report(
    dataset: ReplayDataset,
    filters: ReplayFilters,
    source_observations: list[SourceObservation],
    total_source_scope_episodes: int,
    paths: tuple[DecisionMarketPath, ...],
    *,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
    costs: CostParameters = DEFAULT_COSTS,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    bootstrap_seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> ExchangeSourceEconomicsReport:
    revision = normalize_code_revision(code_revision)
    _validate_contract(filters, bootstrap_iterations)
    dataset_since = filters.since
    if dataset_since is None:  # Kept explicit for static type narrowing.
        raise ValueError("source economics requires an inclusive cohort start")
    if total_source_scope_episodes < 0:
        raise ValueError("source scope episode count cannot be negative")
    if generated_at.utcoffset() is None:
        raise ValueError("generated_at must be timezone-aware")
    _validate_costs(costs)

    source_event_ids = {row.event_id for row in source_observations}
    if len(source_event_ids) > total_source_scope_episodes:
        raise ValueError("source observations exceed the source-scope episode count")
    if any(row.first_seen_at >= filters.until for row in source_observations):
        raise ValueError("source observations must precede the exclusive report cutoff")

    path_counts = Counter(path.decision_id for path in paths)
    duplicates = sorted(key for key, count in path_counts.items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate market paths for decisions: {duplicates}")

    selected_ids = {
        decision.decision_id
        for decision in selected_liquid_taker_decisions(dataset.eligible_episodes)
        if decision.decision_id is not None
    }
    unexpected_paths = sorted(
        path.decision_id for path in paths if path.decision_id not in selected_ids
    )
    if unexpected_paths:
        raise ValueError(f"market paths do not belong to selected decisions: {unexpected_paths}")

    attributions = build_source_attributions(source_observations)
    grouped_observations: dict[int, list[SourceObservation]] = defaultdict(list)
    for observation in source_observations:
        grouped_observations[observation.event_id].append(observation)
    observations_by_event = {
        event_id: tuple(sorted(rows, key=lambda row: (row.first_seen_at, row.exchange)))
        for event_id, rows in grouped_observations.items()
    }
    path_by_decision = {path.decision_id: path.path for path in paths}
    results = tuple(
        _evaluate_episode(
            episode,
            attributions.get(episode.pump_event_id),
            observations_by_event.get(episode.pump_event_id, ()),
            path_by_decision,
            costs,
        )
        for episode in dataset.eligible_episodes
    )
    exclusions = Counter(
        reason for episode in dataset.excluded_episodes for reason in episode.exclusion_reasons
    )
    coverage_filters = CoverageFilters(since=dataset_since, until=filters.until)
    return ExchangeSourceEconomicsReport(
        manifest=ExchangeSourceManifest(
            report_version=REPORT_VERSION,
            source_model_version=SOURCE_MODEL_VERSION,
            replay_engine_version=FOUNDATION_VERSION,
            replay_query_version=QUERY_VERSION,
            selection_version=LIQUID_TAKER_SELECTION_VERSION,
            market_path_version=DECISION_MARKET_PATH_VERSION,
            bootstrap_version=CLUSTER_BOOTSTRAP_VERSION,
            code_revision=revision,
            working_tree_dirty=working_tree_dirty,
            generated_at=generated_at,
            dataset_since=dataset_since,
            dataset_until_exclusive=filters.until,
            decision_input_fingerprint=dataset.input_fingerprint,
            source_input_fingerprint=source_input_fingerprint(source_observations),
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
        coverage=build_coverage_report(
            coverage_filters,
            total_source_scope_episodes,
            source_observations,
        ),
        dataset_episodes=len(dataset.episodes),
        eligible_episodes=len(dataset.eligible_episodes),
        excluded_episodes=len(dataset.excluded_episodes),
        input_exclusion_reasons=_count_rows(exclusions),
        attribution_statuses=_count_rows(Counter(row.source_status for row in results)),
        path_statuses=_count_rows(Counter(path.path.status for path in paths)),
        source_economics=_source_rows(
            results,
            filters,
            bootstrap_iterations=bootstrap_iterations,
            bootstrap_seed=bootstrap_seed,
        ),
        source_execution_routes=_routes(results),
        episode_results=results,
        market_paths=paths,
    )


def render_json(report: ExchangeSourceEconomicsReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True, allow_nan=False)


def render_markdown(report: ExchangeSourceEconomicsReport) -> str:
    manifest = report.manifest
    lines = [
        "# Exchange Source Economics Discovery",
        "",
        f"Generated: {manifest.generated_at.isoformat()}",
        f"Code revision: `{manifest.code_revision}`",
        f"Working tree dirty: {'yes' if manifest.working_tree_dirty else 'no'}",
        (
            f"Scope: {manifest.dataset_since.isoformat()} <= decision "
            f"< {manifest.dataset_until_exclusive.isoformat()}"
        ),
        f"Decision fingerprint: `{manifest.decision_input_fingerprint}`",
        f"Source fingerprint: `{manifest.source_input_fingerprint}`",
        f"Market-path fingerprint: `{manifest.market_path_fingerprint}`",
        "",
        (
            "> Post-hoc discovery only. First source is point-in-time scanner observation; "
            "sole source is a removal counterfactual and is never a live entry feature."
        ),
        (
            "> Source and execution venue are separate. Fixed horizons are cost-screened, "
            "while full-v1 columns include the existing stop, trailing, clock, and costs."
        ),
        (
            "> First source uses Schurfer scanner-observation time, not an exchange event "
            "timestamp. Poll order and request latency may affect the label; inspect timing "
            "before treating venue differences as economic effects."
        ),
        "",
        "## Coverage",
        "",
    ]
    lines.extend(
        markdown_table(
            ("Metric", "Value"),
            [
                ("Source-scope episodes", report.coverage.total_episodes),
                ("Attributed source-scope episodes", report.coverage.attributed_episodes),
                ("Replay dataset episodes", report.dataset_episodes),
                ("Eligible replay episodes", report.eligible_episodes),
                ("Excluded replay episodes", report.excluded_episodes),
            ],
        )
    )
    lines.extend(["", "## Marginal source contribution", ""])
    lines.extend(
        markdown_table(
            ("Exchange", "Episodes", "Sole", "First", "Confirmed", "Lead p50", "Lead p95"),
            [
                (
                    row.exchange,
                    row.episodes,
                    row.sole_source_episodes,
                    row.first_source_episodes,
                    row.confirmed_episodes,
                    f"{row.lead_p50_seconds:.1f}s",
                    f"{row.lead_p95_seconds:.1f}s",
                )
                for row in report.coverage.sources
            ],
        )
    )
    lines.extend(["", "## Full-v1 economics by first source", ""])
    lines.extend(
        markdown_table(
            (
                "First source",
                "Episodes",
                "Selected",
                "Trades",
                "Cash",
                "Unresolved",
                "Clusters",
                "Select rate",
                "Episode net",
                "Trade net",
                "Median trade",
                "Win rate",
                "PF",
                "Initial SL",
            ),
            [
                (
                    row.first_source,
                    row.episodes,
                    row.selected,
                    row.completed_trades,
                    row.cash,
                    row.unresolved,
                    row.asset_clusters,
                    format_percentage(row.selection_rate_pct, missing="n/a"),
                    format_percentage(row.mean_episode_net_pct, missing="n/a"),
                    format_percentage(row.mean_trade_net_pct, missing="n/a"),
                    format_percentage(row.median_trade_net_pct, missing="n/a"),
                    format_percentage(row.win_rate_pct, missing="n/a"),
                    format_number(row.profit_factor, missing="n/a"),
                    format_percentage(row.initial_stop_rate_pct, missing="n/a"),
                )
                for row in report.source_economics
            ],
        )
    )
    lines.extend(["", "## Fixed-horizon cost screen", ""])
    lines.extend(
        markdown_table(
            ("First source", "4h N", "4h mean", "4h median", "8h N", "8h mean", "8h median"),
            [
                (
                    row.first_source,
                    row.fixed_240_n,
                    format_percentage(row.fixed_240_mean_net_pct, missing="n/a"),
                    format_percentage(row.fixed_240_median_net_pct, missing="n/a"),
                    row.fixed_480_n,
                    format_percentage(row.fixed_480_mean_net_pct, missing="n/a"),
                    format_percentage(row.fixed_480_median_net_pct, missing="n/a"),
                )
                for row in report.source_economics
            ],
        )
    )
    lines.extend(["", "## Robustness and capacity", ""])
    lines.extend(
        markdown_table(
            (
                "First source",
                "95% CI",
                "Weakest LOO",
                "Without busiest week",
                "Largest cluster",
                "Holm p",
                "Reject 0",
                "Capacity floor",
            ),
            [
                (
                    row.first_source,
                    (
                        f"[{format_percentage(row.full_net_lower_95_pct)}, "
                        f"{format_percentage(row.full_net_upper_95_pct)}]"
                        if row.full_net_lower_95_pct is not None
                        else "n/a"
                    ),
                    format_percentage(row.weakest_leave_one_cluster_out_pct, missing="n/a"),
                    format_percentage(row.excluding_busiest_week_pct, missing="n/a"),
                    format_percentage(row.largest_cluster_share_pct, missing="n/a"),
                    format_number(row.holm_adjusted_p_value, 4, missing="n/a"),
                    (
                        "yes"
                        if row.holm_rejected is True
                        else "no"
                        if row.holm_rejected is False
                        else "n/a"
                    ),
                    format_number(row.median_capacity_floor_usd, suffix=" USD", missing="n/a"),
                )
                for row in report.source_economics
            ],
        )
    )
    lines.extend(
        [
            "",
            "_Intervals and Holm-adjusted tests are descriptive on an inspected post-hoc family. "
            "They are withheld for a source bucket with any unresolved episode and cannot "
            "promote a source-aware strategy._",
            "",
            "## Source timing diagnostics",
            "",
        ]
    )
    lines.extend(
        markdown_table(
            (
                "First source",
                "Selected/day",
                "Source to decision p50",
                "Next venue p50",
            ),
            [
                (
                    row.first_source,
                    format_number(row.selected_per_calendar_day, missing="n/a"),
                    format_number(
                        row.median_source_to_decision_seconds,
                        suffix="s",
                        missing="n/a",
                    ),
                    format_number(
                        row.median_next_confirmation_delay_seconds,
                        suffix="s",
                        missing="n/a",
                    ),
                )
                for row in report.source_economics
            ],
        )
    )
    lines.extend(
        [
            "",
            "_These clocks describe the scanner pipeline. They do not prove that one venue "
            "caused the move or that its lead is tradeable._",
            "",
            "## Source to execution routes",
            "",
        ]
    )
    lines.extend(
        markdown_table(
            (
                "First source",
                "Execution",
                "Selected",
                "Trades",
                "Clusters",
                "Mean net",
                "Median net",
            ),
            [
                (
                    row.first_source,
                    row.execution_exchange,
                    row.selected,
                    row.completed_trades,
                    row.asset_clusters,
                    format_percentage(row.mean_trade_net_pct, missing="n/a"),
                    format_percentage(row.median_trade_net_pct, missing="n/a"),
                )
                for row in report.source_execution_routes
            ],
        )
    )
    lines.extend(["", "## Attribution diagnostics", ""])
    lines.extend(
        markdown_table(
            ("Status", "Episodes"),
            [(row.name, row.count) for row in report.attribution_statuses],
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
    lines.extend(["", "## Episode results", ""])
    lines.extend(
        markdown_table(
            (
                "Episode",
                "Base",
                "First source",
                "Sources",
                "Execution",
                "Status",
                "Exit",
                "Full net",
                "4h net",
                "8h net",
            ),
            [
                (
                    row.pump_event_id,
                    row.base,
                    row.first_source_key,
                    row.source_count_by_cutoff,
                    row.liquid_taker.exchange or "",
                    row.liquid_taker.status,
                    row.liquid_taker.trade.exit_reason if row.liquid_taker.trade else "",
                    format_percentage(row.liquid_taker.episode_net_return_pct, missing="n/a"),
                    format_percentage(
                        next(
                            (
                                item.net_return_pct
                                for item in row.fixed_horizons
                                if item.horizon_minutes == 240
                            ),
                            None,
                        ),
                        missing="n/a",
                    ),
                    format_percentage(
                        next(
                            (
                                item.net_return_pct
                                for item in row.fixed_horizons
                                if item.horizon_minutes == 480
                            ),
                            None,
                        ),
                        missing="n/a",
                    ),
                )
                for row in report.episode_results
            ],
        )
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover full-v1 economics by point-in-time first source venue"
    )
    parser.add_argument(
        "--since",
        type=parse_utc_datetime,
        default=SOURCE_ECONOMICS_COHORT_START,
        help="inclusive attribution-safe discovery start",
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
    return parser


async def _run(args: argparse.Namespace) -> str:
    from .exchange_coverage_repository import ExchangeCoverageRepository
    from .exchange_registry import EXCHANGE_FACTORIES
    from .replay_repository import ReplayRepository
    from .virtual_market import fetch_decision_market_paths

    generated_at = datetime.now(UTC)
    until = resolve_report_until(
        args.until,
        generated_at,
        cohort_start=SOURCE_ECONOMICS_COHORT_START,
        report_label="exchange-source economics discovery",
    )
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for exchange-source-economics-report")
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    filters = ReplayFilters(
        since=args.since,
        until=until,
        strategy_versions=tuple(args.strategy_version or LIQUID_TAKER_STRATEGY_VERSIONS),
        resolver_version=args.resolver_version,
        required_horizons=SOURCE_ECONOMICS_HORIZONS,
        allow_fallback=False,
    )
    costs = CostParameters(
        taker_fee_bps_per_side=args.taker_fee_bps_per_side,
        funding_cost_bps_per_8h=args.funding_cost_bps_per_8h,
    )
    _validate_contract(filters, args.bootstrap_iterations)
    _validate_costs(costs)
    normalize_code_revision(args.code_revision)
    sys.stderr.write("exchange-source-economics: loading replay inputs\n")
    replay_repository = ReplayRepository.from_url(db_url)
    try:
        decisions = await replay_repository.load(filters)
    finally:
        await replay_repository.close()
    dataset = build_replay_dataset(decisions, filters)

    sys.stderr.write("exchange-source-economics: loading source attribution\n")
    coverage_repository = ExchangeCoverageRepository.from_url(db_url)
    try:
        total_episodes, source_observations = await coverage_repository.load(
            CoverageFilters(since=filters.since, until=filters.until)
        )
    finally:
        await coverage_repository.close()

    selected = selected_liquid_taker_decisions(dataset.eligible_episodes)

    def progress(exchange: str, index: int, total: int) -> None:
        sys.stderr.write(f"exchange-source-economics: fetching {exchange} ({index}/{total})\n")

    paths = await fetch_decision_market_paths(
        selected,
        EXCHANGE_FACTORIES,
        on_exchange=progress,
    )
    sys.stderr.write("exchange-source-economics: building report\n")
    report = build_exchange_source_economics_report(
        dataset,
        filters,
        source_observations,
        total_episodes,
        paths,
        generated_at=generated_at,
        code_revision=args.code_revision,
        working_tree_dirty=args.working_tree_dirty,
        costs=costs,
        bootstrap_iterations=args.bootstrap_iterations,
        bootstrap_seed=args.bootstrap_seed,
    )
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        output = asyncio.run(_run(args))
    except ReportWindowNotStartedError as exc:
        parser.error(str(exc))
    sys.stdout.write(output)
