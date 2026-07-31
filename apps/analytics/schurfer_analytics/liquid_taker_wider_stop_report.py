"""Prospective paired report for the liquid-taker fixed-risk wider-stop shadow."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from statistics import fmean, median
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from .clustered_inference import (
    CLUSTER_BOOTSTRAP_VERSION,
    DEFAULT_BOOTSTRAP_ITERATIONS,
    DEFAULT_BOOTSTRAP_SEED,
    BootstrapEstimate,
    ClusterObservation,
    cluster_bootstrap_mean,
    derived_seed,
)
from .episode_replay import PROTOCOL_VERSION
from .exit_policy_discovery import (
    EXIT_DISCOVERY_SIMPLE_LEVERAGE,
    EXIT_DISCOVERY_WIDER_STOP_MULTIPLIER,
)
from .liquid_taker_report import (
    LIQUID_TAKER_STRATEGY_VERSIONS,
    MAX_ROUND_TRIP_IMPACT_BPS,
    selected_liquid_taker_decisions,
)
from .liquid_taker_wider_stop import (
    LIQUID_TAKER_BASELINE_KEY,
    LIQUID_TAKER_WIDER_CORE_VERSION,
    LIQUID_TAKER_WIDER_KEY,
    LIQUID_TAKER_WIDER_POSITION_SCALE,
    LIQUID_TAKER_WIDER_RISK_VERSION,
    LIQUID_TAKER_WIDER_SELECTION_VERSION,
    LIQUID_TAKER_WIDER_VERSION,
    LiquidTakerWiderResult,
    build_liquid_taker_wider_results,
)
from .outcomes import RESOLVER_VERSION
from .replay import (
    DEFAULT_REPLAY_HORIZONS,
    FOUNDATION_VERSION,
    MIN_FORMAL_CLUSTERS,
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
    max_sequential_drawdown_usd,
)

LIQUID_TAKER_WIDER_REPORT_VERSION = "liquid_taker_wider_stop_forward_report_v1"
LIQUID_TAKER_WIDER_CONTRACT_VERSION = "liquid_taker_wider_stop_shadow_v1"
LIQUID_TAKER_WIDER_INFERENCE_VERSION = "paired_absolute_and_delta_cluster_inference_v1"
LIQUID_TAKER_WIDER_COHORT_START = datetime(2026, 8, 1, tzinfo=UTC)
LIQUID_TAKER_WIDER_STRATEGY_VERSIONS = LIQUID_TAKER_STRATEGY_VERSIONS
FORMAL_EPISODES = 100
FORMAL_WEEKS = 4
TOP_ASSET_SENSITIVITY_COUNT = 5


@dataclass(frozen=True)
class VariantSpec:
    key: str
    role: str
    version: str
    initial_stop_multiplier: float
    position_scale: float


@dataclass(frozen=True)
class Manifest:
    protocol_version: str
    replay_engine_version: str
    replay_query_version: str
    report_version: str
    contract_version: str
    core_version: str
    selection_version: str
    virtual_strategy_version: str
    entry_model_version: str
    exit_model_version: str
    risk_model_version: str
    cost_model_version: str
    market_path_version: str
    inference_version: str
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
    variants: tuple[VariantSpec, ...]
    maximum_round_trip_impact_bps: float
    taker_fee_bps_per_side: float
    funding_cost_bps_per_8h: float
    bootstrap_iterations: int
    bootstrap_seed: int
    simple_leverage: float
    formal_episodes: int = FORMAL_EPISODES
    formal_clusters: int = MIN_FORMAL_CLUSTERS
    formal_weeks: int = FORMAL_WEEKS
    exact_venue_only: bool = True
    observation_unit: str = "pump_event_id"
    no_trigger_policy: str = "zero_return_cash_for_both_variants"
    primary_metric: str = "challenger_risk_normalized_episode_net_return_pct"
    paired_metric: str = "challenger_minus_baseline_risk_normalized_episode_net_pct"
    report_scope: str = "prospective_shadow_only"
    liquidation_policy: str = "simple_3x_price_distance_not_exchange_liquidation_model"


@dataclass(frozen=True)
class EpisodeResult:
    pump_event_id: int
    cluster_key: str
    base: str
    episode_at: datetime
    episode_week: str
    status: str
    baseline: LiquidTakerWiderResult
    challenger: LiquidTakerWiderResult


@dataclass(frozen=True)
class VariantMetrics:
    variant_key: str
    resolved_episodes: int
    trades: int
    cash: int
    unresolved: int
    clusters: int
    mean_risk_normalized_net_return_pct: float | None
    median_risk_normalized_net_return_pct: float | None
    conditional_raw_trade_net_return_pct: float | None
    win_rate_pct: float | None
    profit_factor: float | None
    total_net_pnl_usd: float | None
    max_sequential_drawdown_usd: float | None
    initial_stop_rate_pct: float | None
    mean_initial_stop_pct: float | None
    mean_position_scale_pct: float | None
    minimum_simple_3x_liquidation_buffer_pct: float | None
    mean_mfe_pct: float | None
    mean_mae_pct: float | None


@dataclass(frozen=True)
class PairedComparison:
    episodes: int
    clusters: int
    mean_baseline_pct: float | None
    mean_challenger_pct: float | None
    mean_delta_pct: float | None
    median_delta_pct: float | None
    delta_lower_95_pct: float | None
    delta_upper_95_pct: float | None
    improved: int
    worsened: int
    unchanged: int
    rescued_initial_stops: int


@dataclass(frozen=True)
class FormalInference:
    status: str
    episodes: int
    clusters: int
    weeks: int
    baseline: BootstrapEstimate | None
    challenger: BootstrapEstimate | None
    paired_delta: BootstrapEstimate | None
    busiest_week: str | None
    challenger_excluding_busiest_week_pct: float | None
    delta_excluding_busiest_week_pct: float | None
    minimum_challenger_top_asset_exclusion_pct: float | None
    minimum_delta_top_asset_exclusion_pct: float | None
    verdict: str


@dataclass(frozen=True)
class CountRow:
    name: str
    count: int


@dataclass(frozen=True)
class Report:
    manifest: Manifest
    dataset_episodes: int
    eligible_episodes: int
    excluded_episodes: int
    selected_episodes: int
    cash_episodes: int
    unresolved_episodes: int
    calendar_days: int
    opportunities_per_calendar_day: float | None
    median_measured_capacity_floor_usd: float | None
    input_exclusion_reasons: tuple[CountRow, ...]
    path_statuses: tuple[CountRow, ...]
    variant_metrics: tuple[VariantMetrics, ...]
    paired_comparison: PairedComparison
    formal_inference: FormalInference
    episode_results: tuple[EpisodeResult, ...]
    market_paths: tuple[DecisionMarketPath, ...]


def _week(value: datetime) -> str:
    year, week, _ = value.astimezone(UTC).isocalendar()
    return f"{year}-W{week:02d}"


def _count_rows(counter: Counter[str]) -> tuple[CountRow, ...]:
    return tuple(
        CountRow(name, count)
        for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    )


def _mean(values: list[float]) -> float | None:
    return fmean(values) if values else None


def _calendar_days(since: datetime, until: datetime) -> int:
    elapsed_days = (until - since).total_seconds() / (24 * 60 * 60)
    return max(1, math.ceil(elapsed_days))


def _episode_results(
    dataset: ReplayDataset,
    results: tuple[LiquidTakerWiderResult, ...],
) -> tuple[EpisodeResult, ...]:
    by_event: dict[int, dict[str, LiquidTakerWiderResult]] = {}
    for result in results:
        variants = by_event.setdefault(result.pump_event_id, {})
        if result.variant_key in variants:
            raise ValueError(
                f"duplicate {result.variant_key} result for event {result.pump_event_id}"
            )
        variants[result.variant_key] = result
    episodes: list[EpisodeResult] = []
    required = {LIQUID_TAKER_BASELINE_KEY, LIQUID_TAKER_WIDER_KEY}
    for episode in dataset.eligible_episodes:
        variants = by_event.get(episode.pump_event_id, {})
        if set(variants) != required:
            raise ValueError(f"incomplete wider-stop family for event {episode.pump_event_id}")
        baseline = variants[LIQUID_TAKER_BASELINE_KEY]
        challenger = variants[LIQUID_TAKER_WIDER_KEY]
        if (
            baseline.risk_normalized_net_return_pct is not None
            and challenger.risk_normalized_net_return_pct is not None
        ):
            status = "cash" if baseline.status == challenger.status == "not_triggered" else "paired"
        else:
            status = "unresolved"
        episodes.append(
            EpisodeResult(
                pump_event_id=episode.pump_event_id,
                cluster_key=episode.cluster_key,
                base=episode.base,
                episode_at=episode.first_decision_at,
                episode_week=_week(episode.first_decision_at),
                status=status,
                baseline=baseline,
                challenger=challenger,
            )
        )
    return tuple(sorted(episodes, key=lambda row: (row.episode_at, row.pump_event_id)))


def _variant_metrics(
    variant_key: str,
    episodes: tuple[EpisodeResult, ...],
) -> VariantMetrics:
    selected = tuple(
        row.baseline if variant_key == LIQUID_TAKER_BASELINE_KEY else row.challenger
        for row in episodes
    )
    resolved = [
        row.risk_normalized_net_return_pct
        for row in selected
        if row.risk_normalized_net_return_pct is not None
    ]
    trades = tuple(
        row.trade for row in selected if row.trade is not None and row.trade.status == "complete"
    )
    raw_returns = [trade.net_return_pct for trade in trades if trade.net_return_pct is not None]
    risk_trade_returns = [
        row.risk_normalized_net_return_pct
        for row in selected
        if row.trade is not None and row.risk_normalized_net_return_pct is not None
    ]
    pnl = [trade.net_pnl_usd for trade in trades if trade.net_pnl_usd is not None]
    stops = [
        row.effective_initial_sl_pct for row in selected if row.effective_initial_sl_pct is not None
    ]
    scales = [row.position_scale for row in selected if row.position_scale is not None]
    buffers = [
        row.simple_3x_liquidation_buffer_pct
        for row in selected
        if row.simple_3x_liquidation_buffer_pct is not None
    ]
    mean_scale = _mean(scales)
    return VariantMetrics(
        variant_key=variant_key,
        resolved_episodes=len(resolved),
        trades=len(trades),
        cash=sum(row.status == "not_triggered" for row in selected),
        unresolved=len(selected) - len(resolved),
        clusters=len(
            {
                episode.cluster_key
                for episode, result in zip(episodes, selected, strict=True)
                if result.risk_normalized_net_return_pct is not None
            }
        ),
        mean_risk_normalized_net_return_pct=_mean(resolved),
        median_risk_normalized_net_return_pct=median(resolved) if resolved else None,
        conditional_raw_trade_net_return_pct=_mean(raw_returns),
        win_rate_pct=(
            sum(value > 0 for value in risk_trade_returns) / len(risk_trade_returns) * 100
            if risk_trade_returns
            else None
        ),
        profit_factor=profit_factor(risk_trade_returns),
        total_net_pnl_usd=sum(pnl) if pnl else None,
        max_sequential_drawdown_usd=max_sequential_drawdown_usd(trades),
        initial_stop_rate_pct=(
            sum(trade.exit_reason == "initial_sl" for trade in trades) / len(trades) * 100
            if trades
            else None
        ),
        mean_initial_stop_pct=_mean(stops),
        mean_position_scale_pct=mean_scale * 100 if mean_scale is not None else None,
        minimum_simple_3x_liquidation_buffer_pct=min(buffers) if buffers else None,
        mean_mfe_pct=_mean([trade.mfe_pct for trade in trades if trade.mfe_pct is not None]),
        mean_mae_pct=_mean([trade.mae_pct for trade in trades if trade.mae_pct is not None]),
    )


def _paired_rows(
    episodes: tuple[EpisodeResult, ...],
) -> tuple[tuple[EpisodeResult, float, float, float], ...]:
    rows: list[tuple[EpisodeResult, float, float, float]] = []
    for episode in episodes:
        baseline = episode.baseline.risk_normalized_net_return_pct
        challenger = episode.challenger.risk_normalized_net_return_pct
        if baseline is None or challenger is None:
            continue
        rows.append((episode, baseline, challenger, challenger - baseline))
    return tuple(rows)


def _bootstrap(
    rows: tuple[tuple[EpisodeResult, float, float, float], ...],
    *,
    value: Callable[[tuple[EpisodeResult, float, float, float]], float],
    label: str,
    iterations: int,
    seed: int,
) -> BootstrapEstimate | None:
    observations = tuple(ClusterObservation(row[0].cluster_key, value(row)) for row in rows)
    if len({row.cluster_key for row in observations}) < 2:
        return None
    return cluster_bootstrap_mean(
        observations,
        iterations=iterations,
        seed=derived_seed(seed, label),
    ).estimate


def _paired_comparison(
    episodes: tuple[EpisodeResult, ...],
    *,
    bootstrap_iterations: int,
    bootstrap_seed: int,
) -> PairedComparison:
    rows = _paired_rows(episodes)
    deltas = [row[3] for row in rows]
    interval = _bootstrap(
        rows,
        value=lambda row: row[3],
        label=f"{LIQUID_TAKER_WIDER_CONTRACT_VERSION}:all_delta",
        iterations=bootstrap_iterations,
        seed=bootstrap_seed,
    )
    return PairedComparison(
        episodes=len(rows),
        clusters=len({row[0].cluster_key for row in rows}),
        mean_baseline_pct=_mean([row[1] for row in rows]),
        mean_challenger_pct=_mean([row[2] for row in rows]),
        mean_delta_pct=_mean(deltas),
        median_delta_pct=median(deltas) if deltas else None,
        delta_lower_95_pct=interval.lower_bound if interval else None,
        delta_upper_95_pct=interval.upper_bound if interval else None,
        improved=sum(delta > 1e-12 for delta in deltas),
        worsened=sum(delta < -1e-12 for delta in deltas),
        unchanged=sum(abs(delta) <= 1e-12 for delta in deltas),
        rescued_initial_stops=sum(
            episode.baseline.trade is not None
            and episode.challenger.trade is not None
            and episode.baseline.trade.exit_reason == "initial_sl"
            and episode.challenger.trade.exit_reason != "initial_sl"
            and delta > 0
            for episode, _, _, delta in rows
        ),
    )


def _formal_prefix(
    episodes: tuple[EpisodeResult, ...],
) -> tuple[EpisodeResult, ...]:
    for end in range(FORMAL_EPISODES, len(episodes) + 1):
        candidate = episodes[:end]
        if (
            len({row.cluster_key for row in candidate}) >= MIN_FORMAL_CLUSTERS
            and len({row.episode_week for row in candidate}) >= FORMAL_WEEKS
        ):
            return candidate
    return ()


def _excluded_mean(
    rows: tuple[tuple[EpisodeResult, float, float, float], ...],
    *,
    excluded_week: str | None = None,
    excluded_cluster: str | None = None,
    value: Callable[[tuple[EpisodeResult, float, float, float]], float],
) -> float | None:
    values = [
        value(row)
        for row in rows
        if row[0].episode_week != excluded_week and row[0].cluster_key != excluded_cluster
    ]
    return _mean(values)


def _formal_inference(
    episodes: tuple[EpisodeResult, ...],
    *,
    bootstrap_iterations: int,
    bootstrap_seed: int,
) -> FormalInference:
    prefix = _formal_prefix(episodes)
    if not prefix:
        return FormalInference(
            status="collecting",
            episodes=len(episodes),
            clusters=len({row.cluster_key for row in episodes}),
            weeks=len({row.episode_week for row in episodes}),
            baseline=None,
            challenger=None,
            paired_delta=None,
            busiest_week=None,
            challenger_excluding_busiest_week_pct=None,
            delta_excluding_busiest_week_pct=None,
            minimum_challenger_top_asset_exclusion_pct=None,
            minimum_delta_top_asset_exclusion_pct=None,
            verdict="withheld",
        )
    rows = _paired_rows(prefix)
    if len(rows) != len(prefix):
        return FormalInference(
            status="awaiting_complete_resolution",
            episodes=len(prefix),
            clusters=len({row.cluster_key for row in prefix}),
            weeks=len({row.episode_week for row in prefix}),
            baseline=None,
            challenger=None,
            paired_delta=None,
            busiest_week=None,
            challenger_excluding_busiest_week_pct=None,
            delta_excluding_busiest_week_pct=None,
            minimum_challenger_top_asset_exclusion_pct=None,
            minimum_delta_top_asset_exclusion_pct=None,
            verdict="withheld",
        )
    baseline = _bootstrap(
        rows,
        value=lambda row: row[1],
        label=f"{LIQUID_TAKER_WIDER_CONTRACT_VERSION}:baseline",
        iterations=bootstrap_iterations,
        seed=bootstrap_seed,
    )
    challenger = _bootstrap(
        rows,
        value=lambda row: row[2],
        label=f"{LIQUID_TAKER_WIDER_CONTRACT_VERSION}:challenger",
        iterations=bootstrap_iterations,
        seed=bootstrap_seed,
    )
    delta = _bootstrap(
        rows,
        value=lambda row: row[3],
        label=f"{LIQUID_TAKER_WIDER_CONTRACT_VERSION}:delta",
        iterations=bootstrap_iterations,
        seed=bootstrap_seed,
    )
    if baseline is None or challenger is None or delta is None:
        raise RuntimeError("formal sample passed diversity but bootstrap was unavailable")
    week_counts = Counter(row[0].episode_week for row in rows)
    busiest_week = sorted(
        week_counts,
        key=lambda week: (-week_counts[week], week),
    )[0]
    cluster_counts = Counter(row[0].cluster_key for row in rows)
    top_assets = tuple(
        key
        for key, _ in sorted(
            cluster_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )[:TOP_ASSET_SENSITIVITY_COUNT]
    )
    challenger_without_week = _excluded_mean(
        rows,
        excluded_week=busiest_week,
        value=lambda row: row[2],
    )
    delta_without_week = _excluded_mean(
        rows,
        excluded_week=busiest_week,
        value=lambda row: row[3],
    )
    challenger_asset_sensitivity = [
        value
        for cluster in top_assets
        if (
            value := _excluded_mean(
                rows,
                excluded_cluster=cluster,
                value=lambda row: row[2],
            )
        )
        is not None
    ]
    delta_asset_sensitivity = [
        value
        for cluster in top_assets
        if (
            value := _excluded_mean(
                rows,
                excluded_cluster=cluster,
                value=lambda row: row[3],
            )
        )
        is not None
    ]
    minimum_challenger = min(challenger_asset_sensitivity) if challenger_asset_sensitivity else None
    minimum_delta = min(delta_asset_sensitivity) if delta_asset_sensitivity else None
    passed = (
        challenger.lower_bound > 0
        and delta.lower_bound > 0
        and challenger_without_week is not None
        and challenger_without_week > 0
        and delta_without_week is not None
        and delta_without_week > 0
        and minimum_challenger is not None
        and minimum_challenger > 0
        and minimum_delta is not None
        and minimum_delta > 0
    )
    return FormalInference(
        status="ready",
        episodes=len(prefix),
        clusters=len(cluster_counts),
        weeks=len(week_counts),
        baseline=baseline,
        challenger=challenger,
        paired_delta=delta,
        busiest_week=busiest_week,
        challenger_excluding_busiest_week_pct=challenger_without_week,
        delta_excluding_busiest_week_pct=delta_without_week,
        minimum_challenger_top_asset_exclusion_pct=minimum_challenger,
        minimum_delta_top_asset_exclusion_pct=minimum_delta,
        verdict="shadow_candidate" if passed else "do_not_promote",
    )


def build_report(
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
) -> Report:
    revision = normalize_code_revision(code_revision)
    if filters.since != LIQUID_TAKER_WIDER_COHORT_START:
        raise ValueError("wider-stop shadow requires the registered cohort start")
    if filters.strategy_versions != LIQUID_TAKER_WIDER_STRATEGY_VERSIONS:
        raise ValueError("wider-stop shadow requires the registered strategy cohort")
    if filters.allow_fallback:
        raise ValueError("wider-stop shadow requires exact venue outcomes")
    if bootstrap_iterations < 100:
        raise ValueError("bootstrap iterations must be at least 100")
    path_counts = Counter(path.decision_id for path in paths)
    duplicates = sorted(key for key, count in path_counts.items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate market paths for decisions: {duplicates}")
    core_results = build_liquid_taker_wider_results(dataset, paths, costs=costs)
    episodes = _episode_results(dataset, core_results)
    selected_count = sum(row.baseline.selected_decision_id is not None for row in episodes)
    calendar_days = _calendar_days(filters.since or filters.until, filters.until)
    capacities = [
        row.baseline.measured_capacity_floor_usd
        for row in episodes
        if row.baseline.selected_decision_id is not None
        and row.baseline.measured_capacity_floor_usd is not None
    ]
    exclusions = Counter(
        reason for episode in dataset.excluded_episodes for reason in episode.exclusion_reasons
    )
    return Report(
        manifest=Manifest(
            protocol_version=PROTOCOL_VERSION,
            replay_engine_version=FOUNDATION_VERSION,
            replay_query_version=QUERY_VERSION,
            report_version=LIQUID_TAKER_WIDER_REPORT_VERSION,
            contract_version=LIQUID_TAKER_WIDER_CONTRACT_VERSION,
            core_version=LIQUID_TAKER_WIDER_CORE_VERSION,
            selection_version=LIQUID_TAKER_WIDER_SELECTION_VERSION,
            virtual_strategy_version=VIRTUAL_STRATEGY_VERSION,
            entry_model_version=ENTRY_MODEL_VERSION,
            exit_model_version=EXIT_MODEL_VERSION,
            risk_model_version=LIQUID_TAKER_WIDER_RISK_VERSION,
            cost_model_version=COST_MODEL_VERSION,
            market_path_version=DECISION_MARKET_PATH_VERSION,
            inference_version=LIQUID_TAKER_WIDER_INFERENCE_VERSION,
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
            variants=(
                VariantSpec(
                    LIQUID_TAKER_BASELINE_KEY,
                    "baseline",
                    EXIT_MODEL_VERSION,
                    1.0,
                    1.0,
                ),
                VariantSpec(
                    LIQUID_TAKER_WIDER_KEY,
                    "challenger",
                    LIQUID_TAKER_WIDER_VERSION,
                    EXIT_DISCOVERY_WIDER_STOP_MULTIPLIER,
                    LIQUID_TAKER_WIDER_POSITION_SCALE,
                ),
            ),
            maximum_round_trip_impact_bps=MAX_ROUND_TRIP_IMPACT_BPS,
            taker_fee_bps_per_side=costs.taker_fee_bps_per_side,
            funding_cost_bps_per_8h=costs.funding_cost_bps_per_8h,
            bootstrap_iterations=bootstrap_iterations,
            bootstrap_seed=bootstrap_seed,
            simple_leverage=EXIT_DISCOVERY_SIMPLE_LEVERAGE,
        ),
        dataset_episodes=len(dataset.episodes),
        eligible_episodes=len(dataset.eligible_episodes),
        excluded_episodes=len(dataset.excluded_episodes),
        selected_episodes=selected_count,
        cash_episodes=sum(row.status == "cash" for row in episodes),
        unresolved_episodes=sum(row.status == "unresolved" for row in episodes),
        calendar_days=calendar_days,
        opportunities_per_calendar_day=selected_count / calendar_days,
        median_measured_capacity_floor_usd=(median(capacities) if capacities else None),
        input_exclusion_reasons=_count_rows(exclusions),
        path_statuses=_count_rows(Counter(path.path.status for path in paths)),
        variant_metrics=tuple(
            _variant_metrics(key, episodes)
            for key in (LIQUID_TAKER_BASELINE_KEY, LIQUID_TAKER_WIDER_KEY)
        ),
        paired_comparison=_paired_comparison(
            episodes,
            bootstrap_iterations=bootstrap_iterations,
            bootstrap_seed=bootstrap_seed,
        ),
        formal_inference=_formal_inference(
            episodes,
            bootstrap_iterations=bootstrap_iterations,
            bootstrap_seed=bootstrap_seed,
        ),
        episode_results=episodes,
        market_paths=paths,
    )


def render_json(report: Report) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True, allow_nan=False)


def render_markdown(report: Report) -> str:
    manifest = report.manifest
    inference = report.formal_inference
    lines = [
        "# Pump Short Liquid Taker Wider Stop Shadow",
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
            f"> Formal inference status: `{inference.status}`. This paired report is "
            "shadow-only and never changes production or authorizes real trading."
        ),
        "",
        "## Registered contract",
        "",
    ]
    lines.extend(
        markdown_table(
            ("Role", "Variant", "Version", "SL multiplier", "Position scale"),
            [
                (
                    variant.role,
                    variant.key,
                    variant.version,
                    format_number(variant.initial_stop_multiplier),
                    format_percentage(variant.position_scale * 100),
                )
                for variant in manifest.variants
            ],
        )
    )
    lines.extend(["", "## Shared guardrails", ""])
    lines.extend(
        markdown_table(
            ("Component", "Version / value"),
            [
                ("Contract", manifest.contract_version),
                ("Selection", manifest.selection_version),
                ("Entry", manifest.entry_model_version),
                ("Exit", manifest.exit_model_version),
                ("Risk", manifest.risk_model_version),
                ("Costs", manifest.cost_model_version),
                ("Market path", manifest.market_path_version),
                ("Maximum round-trip impact", f"{MAX_ROUND_TRIP_IMPACT_BPS:.2f} bps"),
                ("Exact venue only", "yes"),
                ("No trigger", manifest.no_trigger_policy),
                ("Primary", manifest.primary_metric),
                ("Paired metric", manifest.paired_metric),
                (
                    "Liquidation diagnostic",
                    manifest.liquidation_policy,
                ),
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
                ("Selected episodes", report.selected_episodes),
                ("Cash episodes", report.cash_episodes),
                ("Unresolved episodes", report.unresolved_episodes),
                ("UTC calendar days", report.calendar_days),
                (
                    "Opportunities / calendar day",
                    format_number(report.opportunities_per_calendar_day),
                ),
                (
                    "Median measured capacity floor",
                    format_number(
                        report.median_measured_capacity_floor_usd,
                        suffix=" USD",
                        missing="n/a",
                    ),
                ),
                ("Completely paired episodes", report.paired_comparison.episodes),
                ("Paired asset clusters", report.paired_comparison.clusters),
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
    lines.extend(["", "## Variant economics", ""])
    lines.extend(
        markdown_table(
            (
                "Variant",
                "Resolved",
                "Trades",
                "Risk net",
                "Median",
                "Raw trade net",
                "PF",
                "Win rate",
                "Initial SL",
                "Position scale",
                "Min 3x buffer",
                "Drawdown",
            ),
            [
                (
                    row.variant_key,
                    row.resolved_episodes,
                    row.trades,
                    format_percentage(
                        row.mean_risk_normalized_net_return_pct,
                        missing="n/a",
                    ),
                    format_percentage(
                        row.median_risk_normalized_net_return_pct,
                        missing="n/a",
                    ),
                    format_percentage(
                        row.conditional_raw_trade_net_return_pct,
                        missing="n/a",
                    ),
                    format_number(row.profit_factor, missing="n/a"),
                    format_percentage(row.win_rate_pct, missing="n/a"),
                    format_percentage(row.initial_stop_rate_pct, missing="n/a"),
                    format_percentage(row.mean_position_scale_pct, missing="n/a"),
                    format_percentage(
                        row.minimum_simple_3x_liquidation_buffer_pct,
                        missing="n/a",
                    ),
                    format_number(
                        row.max_sequential_drawdown_usd,
                        suffix=" USD",
                        missing="n/a",
                    ),
                )
                for row in report.variant_metrics
            ],
        )
    )
    comparison = report.paired_comparison
    lines.extend(["", "## Paired comparison", ""])
    lines.extend(
        markdown_table(
            ("Metric", "Value"),
            [
                ("Episodes", comparison.episodes),
                ("Clusters", comparison.clusters),
                ("Baseline risk net", format_percentage(comparison.mean_baseline_pct)),
                (
                    "Challenger risk net",
                    format_percentage(comparison.mean_challenger_pct),
                ),
                ("Mean delta", format_percentage(comparison.mean_delta_pct)),
                ("Median delta", format_percentage(comparison.median_delta_pct)),
                (
                    "95% delta CI",
                    (
                        f"[{format_percentage(comparison.delta_lower_95_pct)}, "
                        f"{format_percentage(comparison.delta_upper_95_pct)}]"
                        if comparison.delta_lower_95_pct is not None
                        else "n/a"
                    ),
                ),
                (
                    "Improved / worsened / same",
                    (f"{comparison.improved} / {comparison.worsened} / {comparison.unchanged}"),
                ),
                ("Rescued initial stops", comparison.rescued_initial_stops),
            ],
        )
    )
    lines.extend(["", "## Formal inference", ""])
    if inference.status != "ready":
        lines.append(
            "_Formal output is withheld until the earliest chronological prefix has "
            "at least 100 episodes, 30 asset clusters, four UTC calendar weeks, and "
            "complete pairing._"
        )
    else:
        lines.extend(
            markdown_table(
                ("Metric", "Value"),
                [
                    ("Episodes", inference.episodes),
                    ("Clusters", inference.clusters),
                    ("Weeks", inference.weeks),
                    (
                        "Baseline 95% CI",
                        (
                            f"[{format_percentage(inference.baseline.lower_bound)}, "
                            f"{format_percentage(inference.baseline.upper_bound)}]"
                            if inference.baseline
                            else "n/a"
                        ),
                    ),
                    (
                        "Challenger 95% CI",
                        (
                            f"[{format_percentage(inference.challenger.lower_bound)}, "
                            f"{format_percentage(inference.challenger.upper_bound)}]"
                            if inference.challenger
                            else "n/a"
                        ),
                    ),
                    (
                        "Paired delta 95% CI",
                        (
                            f"[{format_percentage(inference.paired_delta.lower_bound)}, "
                            f"{format_percentage(inference.paired_delta.upper_bound)}]"
                            if inference.paired_delta
                            else "n/a"
                        ),
                    ),
                    ("Busiest week", inference.busiest_week or "n/a"),
                    (
                        "Challenger excluding busiest week",
                        format_percentage(inference.challenger_excluding_busiest_week_pct),
                    ),
                    (
                        "Delta excluding busiest week",
                        format_percentage(inference.delta_excluding_busiest_week_pct),
                    ),
                    (
                        "Minimum challenger top-5 asset exclusion",
                        format_percentage(inference.minimum_challenger_top_asset_exclusion_pct),
                    ),
                    (
                        "Minimum delta top-5 asset exclusion",
                        format_percentage(inference.minimum_delta_top_asset_exclusion_pct),
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
                "Impact",
                "Capacity floor",
                "Status",
                "Baseline exit",
                "Baseline risk net",
                "Wider exit",
                "Wider risk net",
                "Delta",
            ),
            [
                (
                    row.pump_event_id,
                    row.base,
                    row.episode_week,
                    row.baseline.exchange or "",
                    format_number(
                        row.baseline.round_trip_impact_bps,
                        suffix=" bps",
                        missing="n/a",
                    ),
                    format_number(
                        row.baseline.measured_capacity_floor_usd,
                        suffix=" USD",
                        missing="n/a",
                    ),
                    row.status,
                    row.baseline.trade.exit_reason if row.baseline.trade else "",
                    format_percentage(
                        row.baseline.risk_normalized_net_return_pct,
                        missing="n/a",
                    ),
                    row.challenger.trade.exit_reason if row.challenger.trade else "",
                    format_percentage(
                        row.challenger.risk_normalized_net_return_pct,
                        missing="n/a",
                    ),
                    format_percentage(
                        (
                            row.challenger.risk_normalized_net_return_pct
                            - row.baseline.risk_normalized_net_return_pct
                            if row.challenger.risk_normalized_net_return_pct is not None
                            and row.baseline.risk_normalized_net_return_pct is not None
                            else None
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
        description="Replay the prospective liquid-taker wider-stop shadow"
    )
    parser.add_argument(
        "--since",
        type=parse_utc_datetime,
        default=LIQUID_TAKER_WIDER_COHORT_START,
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
    from .exchange_registry import EXCHANGE_FACTORIES
    from .replay_repository import ReplayRepository
    from .virtual_market import fetch_decision_market_paths

    generated_at = datetime.now(UTC)
    until = resolve_report_until(
        args.until,
        generated_at,
        cohort_start=LIQUID_TAKER_WIDER_COHORT_START,
        report_label="liquid-taker wider-stop",
    )
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for liquid-taker-wider-stop-report")
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    filters = ReplayFilters(
        since=args.since,
        until=until,
        strategy_versions=tuple(args.strategy_version or LIQUID_TAKER_WIDER_STRATEGY_VERSIONS),
        resolver_version=args.resolver_version,
        required_horizons=DEFAULT_REPLAY_HORIZONS,
        allow_fallback=False,
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
    selected = selected_liquid_taker_decisions(dataset.eligible_episodes)
    paths = await fetch_decision_market_paths(selected, EXCHANGE_FACTORIES)
    report = build_report(
        dataset,
        filters,
        paths,
        generated_at=generated_at,
        code_revision=args.code_revision,
        working_tree_dirty=args.working_tree_dirty,
        costs=costs,
        bootstrap_iterations=args.bootstrap_iterations,
        bootstrap_seed=args.bootstrap_seed,
    )
    if args.record_run:
        from sqlalchemy.exc import SQLAlchemyError

        from .report_registry import ReportRunRecord, record_report_run

        manifest = report.manifest
        inference = report.formal_inference
        paired = inference.paired_delta
        record = ReportRunRecord(
            contract=manifest.contract_version,
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
            eligible_episodes=report.eligible_episodes,
            asset_clusters=inference.clusters,
            calendar_weeks=inference.weeks,
            summary={
                "selected_episodes": report.selected_episodes,
                "cash_episodes": report.cash_episodes,
                "unresolved_episodes": report.unresolved_episodes,
                "paired_delta_pct": paired.point_estimate if paired else None,
                "paired_delta_lower_95_pct": paired.lower_bound if paired else None,
                "paired_delta_upper_95_pct": paired.upper_bound if paired else None,
            },
        )
        try:
            await record_report_run(db_url, record)
        except SQLAlchemyError as exc:
            sys.stderr.write(f"WARNING: research report registry write failed: {exc}\n")
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        output = asyncio.run(_run(args))
    except ReportWindowNotStartedError as exc:
        parser.error(str(exc))
    sys.stdout.write(output)
