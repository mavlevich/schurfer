"""Discovery-only matched exit-policy and fixed-risk stop comparison."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from statistics import fmean, median

from .clustered_inference import (
    CLUSTER_BOOTSTRAP_VERSION,
    DEFAULT_BOOTSTRAP_ITERATIONS,
    DEFAULT_BOOTSTRAP_SEED,
    ClusterObservation,
    cluster_bootstrap_mean,
    derived_seed,
)
from .decision_quality import (
    MARKET_QUALITY_CONTROL_POLICY,
    selected_policy_decisions,
)
from .episode_replay import PROTOCOL_VERSION
from .exit_policy_discovery import (
    BASELINE_EXIT_DISCOVERY_VARIANT,
    EXIT_DISCOVERY_ATR_BARS,
    EXIT_DISCOVERY_ATR_MAX_BASELINE_MULTIPLIER,
    EXIT_DISCOVERY_ATR_MULTIPLIER,
    EXIT_DISCOVERY_ATR_VERSION,
    EXIT_DISCOVERY_CORE_VERSION,
    EXIT_DISCOVERY_RISK_MODEL_VERSION,
    EXIT_DISCOVERY_SELECTION_VERSION,
    EXIT_DISCOVERY_SIMPLE_LEVERAGE,
    EXIT_DISCOVERY_VARIANTS,
    EXIT_DISCOVERY_WIDER_STOP_MULTIPLIER,
    ExitDiscoveryResult,
    ExitDiscoveryVariant,
    build_exit_discovery_results,
    exit_discovery_path_bounds,
)
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
    format_number,
    format_percentage,
    json_ready,
    markdown_table,
    normalize_code_revision,
    parse_utc_datetime,
    profit_factor,
)
from .virtual_market import DecisionMarketPath, decision_market_path_fingerprint
from .virtual_strategy import (
    COST_MODEL_VERSION,
    DEFAULT_COSTS,
    ENTRY_MODEL_VERSION,
    EXIT_MODEL_VERSION,
    SELECTION_MODEL_VERSION,
    VIRTUAL_STRATEGY_VERSION,
    CostParameters,
    max_sequential_drawdown_usd,
)

EXIT_DISCOVERY_REPORT_VERSION = "virtual_exit_discovery_report_v1"
EXIT_DISCOVERY_MARKET_PATH_VERSION = "ccxt_5m_prior_atr_exit_family_v1"
EXIT_DISCOVERY_COHORT_START = datetime(2026, 7, 22, tzinfo=UTC)
EXIT_DISCOVERY_STRATEGY_VERSIONS = ("pump_short_v1_market_quality",)


@dataclass(frozen=True)
class ExitDiscoveryVariantSpec:
    key: str
    version: str
    exit_policy_key: str
    exit_policy_version: str
    stop_mode: str
    stop_multiplier: float
    atr_multiplier: float | None
    max_baseline_multiplier: float


@dataclass(frozen=True)
class ExitDiscoveryManifest:
    protocol_version: str
    replay_engine_version: str
    replay_query_version: str
    report_version: str
    core_version: str
    virtual_strategy_version: str
    selection_model_version: str
    discovery_selection_version: str
    entry_model_version: str
    exit_model_version: str
    risk_model_version: str
    atr_version: str
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
    fallback_allowed: bool
    variants: tuple[ExitDiscoveryVariantSpec, ...]
    atr_bars: int
    atr_multiplier: float
    atr_max_baseline_multiplier: float
    wider_stop_multiplier: float
    simple_leverage: float
    taker_fee_bps_per_side: float
    funding_cost_bps_per_8h: float
    bootstrap_iterations: int
    bootstrap_seed: int
    interpretation: str = "discovery_only_no_promotion"
    primary_metric: str = "risk_normalized_net_return_pct"
    observation_unit: str = "pump_event_id"
    selection_policy: str = "first_recorded_market_quality_allowed_decision"
    entry_policy: str = "same_next_complete_5m_open_for_every_variant"
    path_policy: str = "same_prior_atr_and_longest_exit_window_for_every_variant"
    sizing_policy: str = "recorded_notional_times_baseline_stop_over_effective_stop"
    slippage_policy: str = "recorded_original_notional_impact_reused_after_downsize"
    liquidation_policy: str = "simple_3x_price_distance_not_exchange_liquidation_model"


@dataclass(frozen=True)
class CountRow:
    name: str
    count: int


@dataclass(frozen=True)
class ClusterConcentrationRow:
    cluster_key: str
    episodes: int
    share_pct: float


@dataclass(frozen=True)
class ExitDiscoveryMetrics:
    variant_key: str
    episodes: int
    clusters: int
    mean_raw_net_return_pct: float | None
    mean_risk_normalized_net_return_pct: float | None
    median_risk_normalized_net_return_pct: float | None
    ci_95_lower_pct: float | None
    ci_95_upper_pct: float | None
    total_net_pnl_usd: float | None
    profit_factor: float | None
    win_rate_pct: float | None
    initial_stop_rate_pct: float | None
    mean_initial_stop_pct: float | None
    mean_position_scale_pct: float | None
    minimum_simple_3x_liquidation_buffer_pct: float | None
    mean_duration_minutes: float | None
    mean_mfe_pct: float | None
    mean_mae_pct: float | None
    max_sequential_drawdown_usd: float | None


@dataclass(frozen=True)
class PairedExitDiscoveryComparison:
    variant_key: str
    episodes: int
    mean_baseline_risk_normalized_pct: float | None
    mean_variant_risk_normalized_pct: float | None
    mean_delta_pct: float | None
    ci_95_lower_pct: float | None
    ci_95_upper_pct: float | None
    improved_episodes: int
    worsened_episodes: int
    unchanged_episodes: int
    rescued_initial_stops: int
    new_initial_stops: int
    different_exit_reason_episodes: int


@dataclass(frozen=True)
class ExitDiscoveryReport:
    manifest: ExitDiscoveryManifest
    dataset_episodes: int
    eligible_episodes: int
    excluded_episodes: int
    matched_episodes: int
    matched_clusters: int
    input_exclusion_reasons: tuple[CountRow, ...]
    result_statuses: tuple[CountRow, ...]
    unresolved_reasons: tuple[CountRow, ...]
    variant_metrics: tuple[ExitDiscoveryMetrics, ...]
    paired_comparisons: tuple[PairedExitDiscoveryComparison, ...]
    exit_reasons: tuple[tuple[str, str, int], ...]
    cluster_concentration: tuple[ClusterConcentrationRow, ...]
    results: tuple[ExitDiscoveryResult, ...]


def _variant_spec(variant: ExitDiscoveryVariant) -> ExitDiscoveryVariantSpec:
    return ExitDiscoveryVariantSpec(
        key=variant.key,
        version=variant.version,
        exit_policy_key=variant.exit_policy.key,
        exit_policy_version=variant.exit_policy.version,
        stop_mode=variant.stop_mode,
        stop_multiplier=variant.stop_multiplier,
        atr_multiplier=variant.atr_multiplier,
        max_baseline_multiplier=variant.max_baseline_multiplier,
    )


def _count_rows(counter: Counter[str]) -> tuple[CountRow, ...]:
    return tuple(
        CountRow(name, count)
        for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    )


def _paired_event_ids(results: tuple[ExitDiscoveryResult, ...]) -> tuple[int, ...]:
    by_event: dict[int, dict[str, ExitDiscoveryResult]] = {}
    for result in results:
        by_event.setdefault(result.pump_event_id, {})[result.variant_key] = result
    required = {variant.key for variant in EXIT_DISCOVERY_VARIANTS}
    return tuple(
        sorted(
            event_id
            for event_id, variants in by_event.items()
            if set(variants) == required
            and all(
                row.trade is not None
                and row.trade.status == "complete"
                and row.risk_normalized_net_return_pct is not None
                for row in variants.values()
            )
        )
    )


def _bootstrap_interval(
    observations: tuple[ClusterObservation, ...],
    *,
    label: str,
    iterations: int,
    seed: int,
) -> tuple[float | None, float | None]:
    if len({observation.cluster_key for observation in observations}) < 2:
        return None, None
    estimate = cluster_bootstrap_mean(
        observations,
        iterations=iterations,
        seed=derived_seed(seed, label),
    ).estimate
    return estimate.lower_bound, estimate.upper_bound


def _metrics(
    variant: ExitDiscoveryVariant,
    results: tuple[ExitDiscoveryResult, ...],
    paired_ids: tuple[int, ...],
    *,
    bootstrap_iterations: int,
    bootstrap_seed: int,
) -> ExitDiscoveryMetrics:
    selected = tuple(
        result
        for result in results
        if result.variant_key == variant.key and result.pump_event_id in paired_ids
    )
    risk_returns = [
        result.risk_normalized_net_return_pct
        for result in selected
        if result.risk_normalized_net_return_pct is not None
    ]
    raw_returns = [
        result.trade.net_return_pct
        for result in selected
        if result.trade is not None and result.trade.net_return_pct is not None
    ]
    observations = tuple(
        ClusterObservation(result.cluster_key, result.risk_normalized_net_return_pct)
        for result in selected
        if result.risk_normalized_net_return_pct is not None
    )
    ci_lower, ci_upper = _bootstrap_interval(
        observations,
        label=f"exit_discovery:{variant.key}",
        iterations=bootstrap_iterations,
        seed=bootstrap_seed,
    )
    trades = tuple(result.trade for result in selected if result.trade is not None)
    pnl = [trade.net_pnl_usd for trade in trades if trade.net_pnl_usd is not None]
    stops = [
        result.effective_initial_sl_pct
        for result in selected
        if result.effective_initial_sl_pct is not None
    ]
    scales = [result.position_scale for result in selected if result.position_scale is not None]
    buffers = [
        result.simple_3x_liquidation_buffer_pct
        for result in selected
        if result.simple_3x_liquidation_buffer_pct is not None
    ]
    durations = [trade.duration_minutes for trade in trades if trade.duration_minutes is not None]
    mfe = [trade.mfe_pct for trade in trades if trade.mfe_pct is not None]
    mae = [trade.mae_pct for trade in trades if trade.mae_pct is not None]
    return ExitDiscoveryMetrics(
        variant_key=variant.key,
        episodes=len(selected),
        clusters=len({result.cluster_key for result in selected}),
        mean_raw_net_return_pct=fmean(raw_returns) if raw_returns else None,
        mean_risk_normalized_net_return_pct=fmean(risk_returns) if risk_returns else None,
        median_risk_normalized_net_return_pct=median(risk_returns) if risk_returns else None,
        ci_95_lower_pct=ci_lower,
        ci_95_upper_pct=ci_upper,
        total_net_pnl_usd=sum(pnl) if pnl else None,
        profit_factor=profit_factor(risk_returns),
        win_rate_pct=(
            sum(value > 0 for value in risk_returns) / len(risk_returns) * 100
            if risk_returns
            else None
        ),
        initial_stop_rate_pct=(
            sum(trade.exit_reason == "initial_sl" for trade in trades) / len(trades) * 100
            if trades
            else None
        ),
        mean_initial_stop_pct=fmean(stops) if stops else None,
        mean_position_scale_pct=fmean(scales) * 100 if scales else None,
        minimum_simple_3x_liquidation_buffer_pct=min(buffers) if buffers else None,
        mean_duration_minutes=fmean(durations) if durations else None,
        mean_mfe_pct=fmean(mfe) if mfe else None,
        mean_mae_pct=fmean(mae) if mae else None,
        max_sequential_drawdown_usd=max_sequential_drawdown_usd(trades),
    )


def _paired_comparison(
    variant: ExitDiscoveryVariant,
    results: tuple[ExitDiscoveryResult, ...],
    paired_ids: tuple[int, ...],
    *,
    bootstrap_iterations: int,
    bootstrap_seed: int,
) -> PairedExitDiscoveryComparison:
    by_key = {
        (result.pump_event_id, result.variant_key): result
        for result in results
        if result.pump_event_id in paired_ids
    }
    pairs = tuple(
        (
            by_key[(event_id, BASELINE_EXIT_DISCOVERY_VARIANT.key)],
            by_key[(event_id, variant.key)],
        )
        for event_id in paired_ids
    )
    baseline_returns = [
        baseline.risk_normalized_net_return_pct
        for baseline, _ in pairs
        if baseline.risk_normalized_net_return_pct is not None
    ]
    variant_returns = [
        challenger.risk_normalized_net_return_pct
        for _, challenger in pairs
        if challenger.risk_normalized_net_return_pct is not None
    ]
    deltas = [
        (challenger.risk_normalized_net_return_pct or 0.0)
        - (baseline.risk_normalized_net_return_pct or 0.0)
        for baseline, challenger in pairs
    ]
    observations = tuple(
        ClusterObservation(baseline.cluster_key, delta)
        for (baseline, _), delta in zip(pairs, deltas, strict=True)
    )
    ci_lower, ci_upper = _bootstrap_interval(
        observations,
        label=f"exit_discovery_delta:{variant.key}",
        iterations=bootstrap_iterations,
        seed=bootstrap_seed,
    )
    return PairedExitDiscoveryComparison(
        variant_key=variant.key,
        episodes=len(pairs),
        mean_baseline_risk_normalized_pct=(fmean(baseline_returns) if baseline_returns else None),
        mean_variant_risk_normalized_pct=fmean(variant_returns) if variant_returns else None,
        mean_delta_pct=fmean(deltas) if deltas else None,
        ci_95_lower_pct=ci_lower,
        ci_95_upper_pct=ci_upper,
        improved_episodes=sum(delta > 1e-12 for delta in deltas),
        worsened_episodes=sum(delta < -1e-12 for delta in deltas),
        unchanged_episodes=sum(abs(delta) <= 1e-12 for delta in deltas),
        rescued_initial_stops=sum(
            baseline.trade is not None
            and challenger.trade is not None
            and baseline.trade.exit_reason == "initial_sl"
            and challenger.trade.exit_reason != "initial_sl"
            and delta > 0
            for (baseline, challenger), delta in zip(pairs, deltas, strict=True)
        ),
        new_initial_stops=sum(
            baseline.trade is not None
            and challenger.trade is not None
            and baseline.trade.exit_reason != "initial_sl"
            and challenger.trade.exit_reason == "initial_sl"
            for baseline, challenger in pairs
        ),
        different_exit_reason_episodes=sum(
            baseline.trade is not None
            and challenger.trade is not None
            and baseline.trade.exit_reason != challenger.trade.exit_reason
            for baseline, challenger in pairs
        ),
    )


def _cluster_concentration(
    results: tuple[ExitDiscoveryResult, ...],
    paired_ids: tuple[int, ...],
) -> tuple[ClusterConcentrationRow, ...]:
    baseline = tuple(
        result
        for result in results
        if result.variant_key == BASELINE_EXIT_DISCOVERY_VARIANT.key
        and result.pump_event_id in paired_ids
    )
    counts = Counter(result.cluster_key for result in baseline)
    return tuple(
        ClusterConcentrationRow(cluster_key, count, count / len(baseline) * 100)
        for cluster_key, count in sorted(
            counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
    )


def build_exit_discovery_report(
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
) -> ExitDiscoveryReport:
    revision = normalize_code_revision(code_revision)
    if filters.since != EXIT_DISCOVERY_COHORT_START:
        raise ValueError("exit discovery requires the locked discovery cohort start")
    if filters.strategy_versions != EXIT_DISCOVERY_STRATEGY_VERSIONS:
        raise ValueError("exit discovery requires the locked strategy cohort")
    if bootstrap_iterations < 100:
        raise ValueError("bootstrap iterations must be at least 100")
    path_counts = Counter(path.decision_id for path in paths)
    duplicates = sorted(key for key, count in path_counts.items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate market paths for decisions: {duplicates}")
    results = build_exit_discovery_results(dataset, paths, costs=costs)
    paired_ids = _paired_event_ids(results)
    exclusions = Counter(
        reason for episode in dataset.excluded_episodes for reason in episode.exclusion_reasons
    )
    unresolved = Counter(
        result.error or result.status
        for result in results
        if result.risk_normalized_net_return_pct is None
    )
    exit_reasons = Counter(
        (
            result.variant_key,
            result.trade.exit_reason if result.trade is not None else result.status,
        )
        for result in results
    )
    concentration = _cluster_concentration(results, paired_ids)
    challengers = tuple(
        variant
        for variant in EXIT_DISCOVERY_VARIANTS
        if variant is not BASELINE_EXIT_DISCOVERY_VARIANT
    )
    return ExitDiscoveryReport(
        manifest=ExitDiscoveryManifest(
            protocol_version=PROTOCOL_VERSION,
            replay_engine_version=FOUNDATION_VERSION,
            replay_query_version=QUERY_VERSION,
            report_version=EXIT_DISCOVERY_REPORT_VERSION,
            core_version=EXIT_DISCOVERY_CORE_VERSION,
            virtual_strategy_version=VIRTUAL_STRATEGY_VERSION,
            selection_model_version=SELECTION_MODEL_VERSION,
            discovery_selection_version=EXIT_DISCOVERY_SELECTION_VERSION,
            entry_model_version=ENTRY_MODEL_VERSION,
            exit_model_version=EXIT_MODEL_VERSION,
            risk_model_version=EXIT_DISCOVERY_RISK_MODEL_VERSION,
            atr_version=EXIT_DISCOVERY_ATR_VERSION,
            cost_model_version=COST_MODEL_VERSION,
            market_path_version=EXIT_DISCOVERY_MARKET_PATH_VERSION,
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
            fallback_allowed=filters.allow_fallback,
            variants=tuple(_variant_spec(variant) for variant in EXIT_DISCOVERY_VARIANTS),
            atr_bars=EXIT_DISCOVERY_ATR_BARS,
            atr_multiplier=EXIT_DISCOVERY_ATR_MULTIPLIER,
            atr_max_baseline_multiplier=EXIT_DISCOVERY_ATR_MAX_BASELINE_MULTIPLIER,
            wider_stop_multiplier=EXIT_DISCOVERY_WIDER_STOP_MULTIPLIER,
            simple_leverage=EXIT_DISCOVERY_SIMPLE_LEVERAGE,
            taker_fee_bps_per_side=costs.taker_fee_bps_per_side,
            funding_cost_bps_per_8h=costs.funding_cost_bps_per_8h,
            bootstrap_iterations=bootstrap_iterations,
            bootstrap_seed=bootstrap_seed,
        ),
        dataset_episodes=len(dataset.episodes),
        eligible_episodes=len(dataset.eligible_episodes),
        excluded_episodes=len(dataset.excluded_episodes),
        matched_episodes=len(paired_ids),
        matched_clusters=len(concentration),
        input_exclusion_reasons=_count_rows(exclusions),
        result_statuses=_count_rows(Counter(result.status for result in results)),
        unresolved_reasons=_count_rows(unresolved),
        variant_metrics=tuple(
            _metrics(
                variant,
                results,
                paired_ids,
                bootstrap_iterations=bootstrap_iterations,
                bootstrap_seed=bootstrap_seed,
            )
            for variant in EXIT_DISCOVERY_VARIANTS
        ),
        paired_comparisons=tuple(
            _paired_comparison(
                variant,
                results,
                paired_ids,
                bootstrap_iterations=bootstrap_iterations,
                bootstrap_seed=bootstrap_seed,
            )
            for variant in challengers
        ),
        exit_reasons=tuple(
            (variant_key, exit_reason or "unknown", count)
            for (variant_key, exit_reason), count in sorted(exit_reasons.items())
        ),
        cluster_concentration=concentration,
        results=results,
    )


def render_json(report: ExitDiscoveryReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True, allow_nan=False)


def render_markdown(report: ExitDiscoveryReport) -> str:
    manifest = report.manifest
    lines = [
        "# Pump Short Exit Discovery",
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
            "> Discovery only. Intervals are exploratory, no multiple-comparison verdict "
            "is issued, and this report cannot change production exits or position size."
        ),
        "",
        "## Model",
        "",
    ]
    lines.extend(
        markdown_table(
            ("Component", "Version / value"),
            [
                ("Core", manifest.core_version),
                ("Selection", manifest.discovery_selection_version),
                ("Entry", manifest.entry_model_version),
                ("Exit engine", manifest.exit_model_version),
                ("Risk model", manifest.risk_model_version),
                ("ATR", manifest.atr_version),
                ("Costs", manifest.cost_model_version),
                ("Market path", manifest.market_path_version),
                ("Primary metric", manifest.primary_metric),
                ("Sizing", manifest.sizing_policy),
                ("Slippage", manifest.slippage_policy),
                ("Liquidation diagnostic", manifest.liquidation_policy),
            ],
        )
    )
    lines.extend(["", "## Variants", ""])
    lines.extend(
        markdown_table(
            (
                "Variant",
                "Exit policy",
                "Stop mode",
                "Stop multiplier",
                "ATR multiplier",
                "Max baseline multiplier",
            ),
            [
                (
                    variant.key,
                    variant.exit_policy_key,
                    variant.stop_mode,
                    format_number(variant.stop_multiplier),
                    format_number(variant.atr_multiplier, missing="n/a"),
                    format_number(variant.max_baseline_multiplier),
                )
                for variant in manifest.variants
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
                ("Fully matched episodes", report.matched_episodes),
                ("Matched asset clusters", report.matched_clusters),
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
    lines.extend(["", "## Result statuses", ""])
    lines.extend(
        markdown_table(
            ("Status", "Rows"),
            [(row.name, row.count) for row in report.result_statuses],
        )
    )
    lines.extend(["", "## Unresolved reasons", ""])
    lines.extend(
        markdown_table(
            ("Reason", "Rows"),
            [(row.name, row.count) for row in report.unresolved_reasons],
        )
    )
    lines.extend(["", "## Matched variant metrics", ""])
    lines.extend(
        markdown_table(
            (
                "Variant",
                "N",
                "Clusters",
                "Raw net",
                "Risk-normalized net",
                "Median",
                "95% CI",
                "P&L",
                "PF",
                "Win rate",
                "Initial SL",
                "Mean stop",
                "Position scale",
                "Min 3x buffer",
                "Drawdown",
            ),
            [
                (
                    row.variant_key,
                    row.episodes,
                    row.clusters,
                    format_percentage(row.mean_raw_net_return_pct, missing="n/a"),
                    format_percentage(
                        row.mean_risk_normalized_net_return_pct,
                        missing="n/a",
                    ),
                    format_percentage(
                        row.median_risk_normalized_net_return_pct,
                        missing="n/a",
                    ),
                    (
                        f"[{format_percentage(row.ci_95_lower_pct)}, "
                        f"{format_percentage(row.ci_95_upper_pct)}]"
                        if row.ci_95_lower_pct is not None and row.ci_95_upper_pct is not None
                        else "n/a"
                    ),
                    format_number(row.total_net_pnl_usd, suffix=" USD", missing="n/a"),
                    format_number(row.profit_factor, missing="n/a"),
                    format_percentage(row.win_rate_pct, missing="n/a"),
                    format_percentage(row.initial_stop_rate_pct, missing="n/a"),
                    format_percentage(row.mean_initial_stop_pct, missing="n/a"),
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
    lines.extend(
        [
            "",
            (
                "_Risk-normalized return multiplies raw net return by the fixed-risk "
                "position scale. Wider stops therefore do not receive free extra risk. "
                "The 3x buffer is a simple price-distance diagnostic, not an exchange "
                "liquidation calculation._"
            ),
            "",
            "## Paired baseline comparison",
            "",
        ]
    )
    lines.extend(
        markdown_table(
            (
                "Variant",
                "N",
                "Baseline",
                "Variant",
                "Mean delta",
                "95% delta CI",
                "Improved",
                "Worsened",
                "Same",
                "Stops rescued",
                "New stops",
                "Different exit",
            ),
            [
                (
                    row.variant_key,
                    row.episodes,
                    format_percentage(
                        row.mean_baseline_risk_normalized_pct,
                        missing="n/a",
                    ),
                    format_percentage(
                        row.mean_variant_risk_normalized_pct,
                        missing="n/a",
                    ),
                    format_percentage(row.mean_delta_pct, missing="n/a"),
                    (
                        f"[{format_percentage(row.ci_95_lower_pct)}, "
                        f"{format_percentage(row.ci_95_upper_pct)}]"
                        if row.ci_95_lower_pct is not None and row.ci_95_upper_pct is not None
                        else "n/a"
                    ),
                    row.improved_episodes,
                    row.worsened_episodes,
                    row.unchanged_episodes,
                    row.rescued_initial_stops,
                    row.new_initial_stops,
                    row.different_exit_reason_episodes,
                )
                for row in report.paired_comparisons
            ],
        )
    )
    lines.extend(["", "## Exit reasons", ""])
    lines.extend(
        markdown_table(
            ("Variant", "Exit reason", "Episodes"),
            list(report.exit_reasons),
        )
    )
    lines.extend(["", "## Matched cluster concentration", ""])
    lines.extend(
        markdown_table(
            ("Cluster", "Episodes", "Share"),
            [
                (
                    row.cluster_key,
                    row.episodes,
                    format_percentage(row.share_pct),
                )
                for row in report.cluster_concentration[:10]
            ],
        )
    )
    lines.extend(["", "## Episode results", ""])
    lines.extend(
        markdown_table(
            (
                "Episode",
                "Base",
                "Exchange",
                "Variant",
                "Status",
                "Exit",
                "Stop",
                "ATR",
                "Scale",
                "Raw net",
                "Risk net",
                "Error",
            ),
            [
                (
                    row.pump_event_id,
                    row.base,
                    row.exchange or "",
                    row.variant_key,
                    row.status,
                    row.trade.exit_reason if row.trade is not None else "n/a",
                    format_percentage(row.effective_initial_sl_pct, missing="n/a"),
                    format_percentage(row.prior_atr_pct, missing="n/a"),
                    format_percentage(
                        row.position_scale * 100 if row.position_scale is not None else None,
                        missing="n/a",
                    ),
                    format_percentage(
                        row.trade.net_return_pct if row.trade is not None else None,
                        missing="n/a",
                    ),
                    format_percentage(
                        row.risk_normalized_net_return_pct,
                        missing="n/a",
                    ),
                    row.error or "",
                )
                for row in report.results
            ],
        )
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare discovery-only pump-short exit and fixed-risk stop variants"
    )
    parser.add_argument(
        "--since",
        type=parse_utc_datetime,
        default=EXIT_DISCOVERY_COHORT_START,
        help="inclusive UTC cutoff; fixed to the discovery cohort",
    )
    parser.add_argument(
        "--until",
        type=parse_utc_datetime,
        help="exclusive UTC cutoff; defaults to the run start",
    )
    parser.add_argument(
        "--strategy-version",
        action="append",
        help="recorded strategy cohort; fixed to pump_short_v1_market_quality",
    )
    parser.add_argument("--resolver-version", default=RESOLVER_VERSION)
    parser.add_argument(
        "--allow-fallback",
        action="store_true",
        help="allow fallback outcomes in a separately identified sensitivity run",
    )
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
    from .exchange_registry import EXCHANGE_FACTORIES
    from .replay_repository import ReplayRepository
    from .virtual_market import fetch_decision_market_paths

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for virtual-exit-discovery-report")
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    generated_at = datetime.now(UTC)
    filters = ReplayFilters(
        since=args.since,
        until=args.until or generated_at,
        strategy_versions=tuple(args.strategy_version or EXIT_DISCOVERY_STRATEGY_VERSIONS),
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
    selected = selected_policy_decisions(
        dataset.eligible_episodes,
        policies=(MARKET_QUALITY_CONTROL_POLICY,),
    )
    paths = await fetch_decision_market_paths(
        selected,
        EXCHANGE_FACTORIES,
        bounds=exit_discovery_path_bounds,
    )
    report = build_exit_discovery_report(
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
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    args = build_parser().parse_args()
    sys.stdout.write(asyncio.run(_run(args)))
