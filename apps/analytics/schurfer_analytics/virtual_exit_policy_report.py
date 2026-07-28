"""Read-only paired report for the pre-registered exit-policy family."""

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
from statistics import fmean

from .challenger_inference import (
    DEFAULT_INFERENCE_SETTINGS,
    ChallengerEpisode,
    ChallengerInference,
    build_challenger_inference,
)
from .episode_replay import PROTOCOL_VERSION
from .outcomes import RESOLVER_VERSION
from .replay import (
    DEFAULT_REPLAY_HORIZONS,
    FOUNDATION_VERSION,
    QUERY_VERSION,
    ReplayDataset,
    ReplayEpisode,
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
)
from .virtual_strategy import (
    BASELINE_EXIT_POLICY,
    COST_MODEL_VERSION,
    DEFAULT_COSTS,
    ENTRY_MODEL_VERSION,
    EXIT_MODEL_VERSION,
    EXIT_POLICIES,
    EXIT_POLICY_FAMILY_VERSION,
    SELECTION_MODEL_VERSION,
    VIRTUAL_STRATEGY_VERSION,
    CostParameters,
    ExitPolicy,
    MarketPath,
    VirtualTrade,
    exit_parameters,
    exit_policy_family_path_is_complete,
    market_path_fingerprint,
    select_episode_decision,
    simulate_episode,
)

EXIT_POLICY_REPORT_VERSION = "virtual_exit_policy_report_v1"
EXIT_POLICY_MARKET_PATH_VERSION = "ccxt_5m_exact_exit_policy_family_v1"
EXIT_POLICY_INFERENCE_VERSION = "exit_policy_formal_inference_v1"
EXIT_POLICY_COHORT_START = datetime(2026, 7, 29, tzinfo=UTC)
EXIT_POLICY_STRATEGY_VERSIONS = ("pump_short_v1_market_quality",)


@dataclass(frozen=True)
class ExitPolicySpec:
    key: str
    version: str
    protect_breakeven_after_activation: bool
    no_progress_minutes: int | None
    max_extension_minutes: int
    minimum_progress_pct: float
    recent_progress_lookback_minutes: int | None
    extension_trail_pct: float | None
    maximum_hold_by_pump_band_minutes: tuple[int, int, int]


@dataclass(frozen=True)
class ExitPolicyManifest:
    protocol_version: str
    replay_engine_version: str
    replay_query_version: str
    report_version: str
    virtual_strategy_version: str
    selection_model_version: str
    entry_model_version: str
    exit_model_version: str
    exit_policy_family_version: str
    cost_model_version: str
    market_path_version: str
    inference_version: str
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
    baseline: ExitPolicySpec
    challengers: tuple[ExitPolicySpec, ...]
    taker_fee_bps_per_side: float
    funding_cost_bps_per_8h: float
    bootstrap_iterations: int
    bootstrap_seed: int
    bootstrap_confidence_level: float
    holm_family_alpha: float
    observation_unit: str = "pump_event_id"
    selection_policy: str = "same_point_in_time_decision_for_every_exit"
    entry_policy: str = "same_next_complete_5m_open_for_every_exit"
    path_policy: str = "longest_registered_window_required_for_paired_family"
    within_bar_policy: str = "conservative_stop_first"
    formal_sample_policy: str = "first_100_eligible_episodes_chronological"
    report_scope: str = "formal_inference_when_ready_shadow_only"


@dataclass(frozen=True)
class PolicyTrade:
    policy_key: str
    trade: VirtualTrade


@dataclass(frozen=True)
class ExitPolicyMetrics:
    policy_key: str
    eligible_episodes: int
    resolved_episodes: int
    unresolved_episodes: int
    completed_trades: int
    mean_net_return_pct: float | None
    total_net_pnl_usd: float | None
    profit_factor: float | None
    win_rate_pct: float | None
    initial_stop_rate_pct: float | None
    protected_stop_rate_pct: float | None
    mean_duration_minutes: float | None
    mean_mfe_pct: float | None
    mean_mae_pct: float | None
    mean_captured_move_pct: float | None
    max_sequential_drawdown_usd: float | None


@dataclass(frozen=True)
class PairedExitComparison:
    variant_key: str
    episodes: int
    mean_baseline_net_return_pct: float | None
    mean_challenger_net_return_pct: float | None
    mean_delta_pct: float | None
    improved_episodes: int
    worsened_episodes: int
    unchanged_episodes: int
    different_exit_reason_episodes: int
    mean_duration_delta_minutes: float | None


@dataclass(frozen=True)
class CountRow:
    name: str
    count: int


@dataclass(frozen=True)
class PolicyExitReason:
    policy_key: str
    exit_reason: str
    count: int


@dataclass(frozen=True)
class ExitPolicyReport:
    manifest: ExitPolicyManifest
    dataset_episodes: int
    eligible_episodes: int
    excluded_episodes: int
    input_exclusion_reasons: tuple[CountRow, ...]
    policy_metrics: tuple[ExitPolicyMetrics, ...]
    paired_comparisons: tuple[PairedExitComparison, ...]
    exit_reasons: tuple[PolicyExitReason, ...]
    policy_trades: tuple[PolicyTrade, ...]
    inference: ChallengerInference
    market_paths: tuple[MarketPath, ...]


def _mean(values: list[float]) -> float | None:
    return fmean(values) if values else None


def _count_rows(counter: Counter[str]) -> tuple[CountRow, ...]:
    return tuple(
        CountRow(name, count)
        for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    )


def _policy_spec(policy: ExitPolicy) -> ExitPolicySpec:
    maximum_holds = tuple(
        policy.maximum_hold_minutes(exit_parameters(pump_pct)) for pump_pct in (40.0, 75.0, 125.0)
    )
    return ExitPolicySpec(
        key=policy.key,
        version=policy.version,
        protect_breakeven_after_activation=policy.protect_breakeven_after_activation,
        no_progress_minutes=policy.no_progress_minutes,
        max_extension_minutes=policy.max_extension_minutes,
        minimum_progress_pct=policy.minimum_progress_pct,
        recent_progress_lookback_minutes=policy.recent_progress_lookback_minutes,
        extension_trail_pct=policy.extension_trail_pct,
        maximum_hold_by_pump_band_minutes=(
            maximum_holds[0],
            maximum_holds[1],
            maximum_holds[2],
        ),
    )


def _missing_path(episode: ReplayEpisode) -> MarketPath:
    selection = select_episode_decision(episode)
    return MarketPath(
        pump_event_id=episode.pump_event_id,
        exchange=selection.decision.exchange,
        base=selection.decision.base,
        status="missing_path",
        candles=(),
        error="market path was not loaded",
    )


def _family_path(episode: ReplayEpisode, path: MarketPath) -> MarketPath:
    if path.status != "complete":
        return path
    decision = select_episode_decision(episode).decision
    if exit_policy_family_path_is_complete(decision, path.candles):
        return path
    return MarketPath(
        pump_event_id=path.pump_event_id,
        exchange=path.exchange,
        base=path.base,
        status="incomplete_family_path",
        candles=path.candles,
        error="missing one or more bars in the longest registered exit-policy window",
    )


def _resolved_return(trade: VirtualTrade) -> float | None:
    return trade.net_return_pct if trade.status == "complete" else None


def _max_drawdown_usd(trades: tuple[VirtualTrade, ...]) -> float | None:
    ordered = sorted(
        (
            trade
            for trade in trades
            if trade.net_pnl_usd is not None and math.isfinite(trade.net_pnl_usd)
        ),
        key=lambda trade: (trade.decision_at, trade.pump_event_id),
    )
    if not ordered:
        return None
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for trade in ordered:
        equity += trade.net_pnl_usd or 0.0
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def _metrics(
    policy: ExitPolicy,
    rows: tuple[PolicyTrade, ...],
) -> ExitPolicyMetrics:
    selected = tuple(row.trade for row in rows if row.policy_key == policy.key)
    complete = tuple(trade for trade in selected if trade.status == "complete")
    returns = [trade.net_return_pct for trade in complete if trade.net_return_pct is not None]
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value <= 0]
    loss_magnitude = abs(sum(losses))
    pnl = [trade.net_pnl_usd for trade in complete if trade.net_pnl_usd is not None]
    durations = [trade.duration_minutes for trade in complete if trade.duration_minutes is not None]
    mfe = [trade.mfe_pct for trade in complete if trade.mfe_pct is not None]
    mae = [trade.mae_pct for trade in complete if trade.mae_pct is not None]
    captured = [
        trade.captured_move_pct for trade in complete if trade.captured_move_pct is not None
    ]
    return ExitPolicyMetrics(
        policy_key=policy.key,
        eligible_episodes=len(selected),
        resolved_episodes=len(complete),
        unresolved_episodes=len(selected) - len(complete),
        completed_trades=len(complete),
        mean_net_return_pct=_mean(returns),
        total_net_pnl_usd=sum(pnl) if pnl else None,
        profit_factor=sum(wins) / loss_magnitude if loss_magnitude > 0 else None,
        win_rate_pct=(
            sum(value > 0 for value in returns) / len(returns) * 100 if returns else None
        ),
        initial_stop_rate_pct=(
            sum(trade.exit_reason == "initial_sl" for trade in complete) / len(complete) * 100
            if complete
            else None
        ),
        protected_stop_rate_pct=(
            sum(trade.exit_reason == "protected_stop" for trade in complete) / len(complete) * 100
            if complete
            else None
        ),
        mean_duration_minutes=_mean(durations),
        mean_mfe_pct=_mean(mfe),
        mean_mae_pct=_mean(mae),
        mean_captured_move_pct=_mean(captured),
        max_sequential_drawdown_usd=_max_drawdown_usd(complete),
    )


def _paired_comparison(
    variant: ExitPolicy,
    baseline_by_event: dict[int, VirtualTrade],
    variant_by_event: dict[int, VirtualTrade],
) -> PairedExitComparison:
    pairs: list[tuple[VirtualTrade, VirtualTrade, float, float]] = []
    for event_id, baseline in baseline_by_event.items():
        challenger = variant_by_event.get(event_id)
        if (
            challenger is None
            or baseline.status != "complete"
            or challenger.status != "complete"
            or baseline.net_return_pct is None
            or challenger.net_return_pct is None
        ):
            continue
        pairs.append(
            (
                baseline,
                challenger,
                baseline.net_return_pct,
                challenger.net_return_pct,
            )
        )
    baseline_returns = [baseline_return for _, _, baseline_return, _ in pairs]
    challenger_returns = [challenger_return for _, _, _, challenger_return in pairs]
    deltas = [
        challenger_return - baseline_return for _, _, baseline_return, challenger_return in pairs
    ]
    duration_deltas = [
        challenger.duration_minutes - baseline.duration_minutes
        for baseline, challenger, _, _ in pairs
        if baseline.duration_minutes is not None and challenger.duration_minutes is not None
    ]
    return PairedExitComparison(
        variant_key=variant.key,
        episodes=len(pairs),
        mean_baseline_net_return_pct=_mean(baseline_returns),
        mean_challenger_net_return_pct=_mean(challenger_returns),
        mean_delta_pct=_mean(deltas),
        improved_episodes=sum(delta > 1e-12 for delta in deltas),
        worsened_episodes=sum(delta < -1e-12 for delta in deltas),
        unchanged_episodes=sum(abs(delta) <= 1e-12 for delta in deltas),
        different_exit_reason_episodes=sum(
            baseline.exit_reason != challenger.exit_reason for baseline, challenger, _, _ in pairs
        ),
        mean_duration_delta_minutes=_mean(duration_deltas),
    )


def build_exit_policy_report(
    dataset: ReplayDataset,
    filters: ReplayFilters,
    paths: tuple[MarketPath, ...],
    *,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
    costs: CostParameters = DEFAULT_COSTS,
) -> ExitPolicyReport:
    revision = normalize_code_revision(code_revision)
    if filters.since != EXIT_POLICY_COHORT_START:
        raise ValueError("formal exit-policy report requires the registered cohort start")
    if filters.strategy_versions != EXIT_POLICY_STRATEGY_VERSIONS:
        raise ValueError("formal exit-policy report requires the registered strategy cohort")
    event_counts = Counter(path.pump_event_id for path in paths)
    duplicates = sorted(event_id for event_id, count in event_counts.items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate market paths for episodes: {duplicates}")
    path_by_event = {path.pump_event_id: path for path in paths}
    family_paths = {
        episode.pump_event_id: _family_path(
            episode,
            path_by_event.get(episode.pump_event_id, _missing_path(episode)),
        )
        for episode in dataset.eligible_episodes
    }
    policy_trades = tuple(
        PolicyTrade(
            policy.key,
            simulate_episode(
                episode,
                family_paths[episode.pump_event_id],
                costs=costs,
                exit_policy=policy,
            ),
        )
        for episode in dataset.eligible_episodes
        for policy in EXIT_POLICIES
    )
    trades_by_policy_event = {
        (row.policy_key, row.trade.pump_event_id): row.trade for row in policy_trades
    }
    challengers = tuple(policy for policy in EXIT_POLICIES if policy is not BASELINE_EXIT_POLICY)
    baseline_by_event = {
        episode.pump_event_id: trades_by_policy_event[
            (BASELINE_EXIT_POLICY.key, episode.pump_event_id)
        ]
        for episode in dataset.eligible_episodes
    }
    inference = build_challenger_inference(
        tuple(
            ChallengerEpisode(
                pump_event_id=episode.pump_event_id,
                cluster_key=episode.cluster_key,
                baseline_return_pct=_resolved_return(baseline_by_event[episode.pump_event_id]),
                challenger_returns_pct=tuple(
                    (
                        policy.key,
                        _resolved_return(
                            trades_by_policy_event[(policy.key, episode.pump_event_id)]
                        ),
                    )
                    for policy in challengers
                ),
            )
            for episode in dataset.eligible_episodes
        ),
        tuple(policy.key for policy in challengers),
        inference_version=EXIT_POLICY_INFERENCE_VERSION,
    )
    exclusions = Counter(
        reason for episode in dataset.excluded_episodes for reason in episode.exclusion_reasons
    )
    exit_reason_counts = Counter(
        (row.policy_key, row.trade.exit_reason or row.trade.status) for row in policy_trades
    )
    return ExitPolicyReport(
        manifest=ExitPolicyManifest(
            protocol_version=PROTOCOL_VERSION,
            replay_engine_version=FOUNDATION_VERSION,
            replay_query_version=QUERY_VERSION,
            report_version=EXIT_POLICY_REPORT_VERSION,
            virtual_strategy_version=VIRTUAL_STRATEGY_VERSION,
            selection_model_version=SELECTION_MODEL_VERSION,
            entry_model_version=ENTRY_MODEL_VERSION,
            exit_model_version=EXIT_MODEL_VERSION,
            exit_policy_family_version=EXIT_POLICY_FAMILY_VERSION,
            cost_model_version=COST_MODEL_VERSION,
            market_path_version=EXIT_POLICY_MARKET_PATH_VERSION,
            inference_version=EXIT_POLICY_INFERENCE_VERSION,
            code_revision=revision,
            working_tree_dirty=working_tree_dirty,
            generated_at=generated_at,
            dataset_since=filters.since,
            dataset_until_exclusive=filters.until,
            decision_input_fingerprint=dataset.input_fingerprint,
            market_path_fingerprint=market_path_fingerprint(paths),
            strategy_versions=filters.strategy_versions,
            resolver_version=filters.resolver_version,
            required_horizons=filters.required_horizons,
            fallback_allowed=filters.allow_fallback,
            baseline=_policy_spec(BASELINE_EXIT_POLICY),
            challengers=tuple(_policy_spec(policy) for policy in challengers),
            taker_fee_bps_per_side=costs.taker_fee_bps_per_side,
            funding_cost_bps_per_8h=costs.funding_cost_bps_per_8h,
            bootstrap_iterations=DEFAULT_INFERENCE_SETTINGS.iterations,
            bootstrap_seed=DEFAULT_INFERENCE_SETTINGS.seed,
            bootstrap_confidence_level=DEFAULT_INFERENCE_SETTINGS.confidence_level,
            holm_family_alpha=DEFAULT_INFERENCE_SETTINGS.family_alpha,
        ),
        dataset_episodes=len(dataset.episodes),
        eligible_episodes=len(dataset.eligible_episodes),
        excluded_episodes=len(dataset.excluded_episodes),
        input_exclusion_reasons=_count_rows(exclusions),
        policy_metrics=tuple(_metrics(policy, policy_trades) for policy in EXIT_POLICIES),
        paired_comparisons=tuple(
            _paired_comparison(
                policy,
                baseline_by_event,
                {
                    episode.pump_event_id: trades_by_policy_event[
                        (policy.key, episode.pump_event_id)
                    ]
                    for episode in dataset.eligible_episodes
                },
            )
            for policy in challengers
        ),
        exit_reasons=tuple(
            PolicyExitReason(policy_key, exit_reason, count)
            for (policy_key, exit_reason), count in sorted(exit_reason_counts.items())
        ),
        policy_trades=policy_trades,
        inference=inference,
        market_paths=paths,
    )


def render_json(report: ExitPolicyReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True)


def render_markdown(report: ExitPolicyReport) -> str:
    manifest = report.manifest
    policies = (manifest.baseline, *manifest.challengers)
    lines = [
        "# Pump Short Exit Policy Challenger Replay",
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
            f"> Formal inference status: `{report.inference.readiness.status}`. "
            "This report never changes production exits or authorizes real trading."
        ),
        "",
        "## Registered family",
        "",
    ]
    lines.extend(
        markdown_table(
            (
                "Role",
                "Policy",
                "Version",
                "Breakeven",
                "No progress",
                "Extension",
                "Recent progress",
                "Extension trail",
                "Max hold by pump band",
            ),
            [
                (
                    "baseline" if policy.key == manifest.baseline.key else "challenger",
                    policy.key,
                    policy.version,
                    "yes" if policy.protect_breakeven_after_activation else "no",
                    (
                        f"{policy.no_progress_minutes}m"
                        if policy.no_progress_minutes is not None
                        else "off"
                    ),
                    f"{policy.max_extension_minutes}m",
                    (
                        f"{policy.recent_progress_lookback_minutes}m"
                        if policy.recent_progress_lookback_minutes is not None
                        else "off"
                    ),
                    (
                        format_percentage(policy.extension_trail_pct, missing="off")
                        if policy.extension_trail_pct is not None
                        else "off"
                    ),
                    "/".join(f"{value}m" for value in policy.maximum_hold_by_pump_band_minutes),
                )
                for policy in policies
            ],
        )
    )
    lines.extend(["", "## Shared model", ""])
    lines.extend(
        markdown_table(
            ("Component", "Version / policy"),
            [
                ("Replay", manifest.virtual_strategy_version),
                ("Selection", manifest.selection_model_version),
                ("Entry", manifest.entry_model_version),
                ("Exit engine", manifest.exit_model_version),
                ("Exit family", manifest.exit_policy_family_version),
                ("Costs", manifest.cost_model_version),
                ("Market path", manifest.market_path_version),
                ("Inference", manifest.inference_version),
                ("Cluster bootstrap", report.inference.bootstrap_version),
                ("Multiple comparisons", report.inference.holm_version),
                ("Seed derivation", report.inference.seed_derivation),
                ("Bootstrap iterations", manifest.bootstrap_iterations),
                ("Bootstrap seed", manifest.bootstrap_seed),
                (
                    "Bootstrap confidence",
                    format_percentage(manifest.bootstrap_confidence_level * 100),
                ),
                ("Holm family alpha", format_number(manifest.holm_family_alpha, decimals=4)),
                ("Path completeness", manifest.path_policy),
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
                ("Formal sample episodes", report.inference.readiness.formal_sample_episodes),
                ("Formal sample clusters", report.inference.readiness.formal_sample_clusters),
                (
                    "Completely paired formal episodes",
                    report.inference.readiness.completely_paired_episodes,
                ),
                ("Inference readiness", report.inference.readiness.status),
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
    lines.extend(["", "## Policy metrics", ""])
    lines.extend(
        markdown_table(
            (
                "Policy",
                "Resolved",
                "Unresolved",
                "Mean net",
                "Total P&L",
                "Profit factor",
                "Win rate",
                "Initial SL",
                "Protected stop",
                "Duration",
                "MFE",
                "MAE",
                "Captured MFE",
                "Max drawdown",
            ),
            [
                (
                    row.policy_key,
                    row.resolved_episodes,
                    row.unresolved_episodes,
                    format_percentage(row.mean_net_return_pct, missing="n/a"),
                    format_number(row.total_net_pnl_usd, suffix=" USD", missing="n/a"),
                    format_number(row.profit_factor, missing="n/a"),
                    format_percentage(row.win_rate_pct, missing="n/a"),
                    format_percentage(row.initial_stop_rate_pct, missing="n/a"),
                    format_percentage(row.protected_stop_rate_pct, missing="n/a"),
                    format_number(row.mean_duration_minutes, suffix="m", missing="n/a"),
                    format_percentage(row.mean_mfe_pct, missing="n/a"),
                    format_percentage(row.mean_mae_pct, missing="n/a"),
                    format_percentage(row.mean_captured_move_pct, missing="n/a"),
                    format_number(
                        row.max_sequential_drawdown_usd,
                        suffix=" USD",
                        missing="n/a",
                    ),
                )
                for row in report.policy_metrics
            ],
        )
    )
    lines.extend(
        [
            "",
            (
                "_Total P&L and max drawdown use recorded position size and chronological "
                "independent episodes. They are not a portfolio simulation._"
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
                "Baseline net",
                "Challenger net",
                "Mean delta",
                "Improved",
                "Worsened",
                "Same",
                "Different exit",
                "Duration delta",
            ),
            [
                (
                    row.variant_key,
                    row.episodes,
                    format_percentage(row.mean_baseline_net_return_pct, missing="n/a"),
                    format_percentage(row.mean_challenger_net_return_pct, missing="n/a"),
                    format_percentage(row.mean_delta_pct, missing="n/a"),
                    row.improved_episodes,
                    row.worsened_episodes,
                    row.unchanged_episodes,
                    row.different_exit_reason_episodes,
                    format_number(
                        row.mean_duration_delta_minutes,
                        suffix="m",
                        missing="n/a",
                    ),
                )
                for row in report.paired_comparisons
            ],
        )
    )
    lines.extend(["", "## Exit reasons", ""])
    lines.extend(
        markdown_table(
            ("Policy", "Exit reason", "Episodes"),
            [(row.policy_key, row.exit_reason, row.count) for row in report.exit_reasons],
        )
    )
    lines.extend(["", "## Formal cluster inference", ""])
    if report.inference.baseline is None:
        lines.append(
            "_Formal intervals are withheld until the locked first 100 eligible episodes "
            "are fully paired and contain at least 30 asset clusters._"
        )
    else:
        baseline = report.inference.baseline
        lines.extend(
            markdown_table(
                (
                    "Strategy",
                    "N",
                    "Clusters",
                    "Mean net",
                    "95% lower",
                    "95% upper",
                    "Min leave-one-out",
                    "Verdict",
                ),
                [
                    (
                        baseline.strategy_key,
                        baseline.estimate.episodes,
                        baseline.estimate.clusters,
                        format_percentage(baseline.estimate.point_estimate),
                        format_percentage(baseline.estimate.lower_bound),
                        format_percentage(baseline.estimate.upper_bound),
                        format_percentage(baseline.minimum_leave_one_cluster_out_pct),
                        baseline.verdict,
                    )
                ],
            )
        )
        lines.extend(["", "### Registered challengers", ""])
        lines.extend(
            markdown_table(
                (
                    "Variant",
                    "Own mean",
                    "Own 95% lower",
                    "Own 95% upper",
                    "Paired delta",
                    "Familywise lower",
                    "Familywise upper",
                    "Raw p",
                    "Holm adjusted p",
                    "Min leave-one-out",
                    "Verdict",
                ),
                [
                    (
                        row.variant_key,
                        format_percentage(row.strategy.estimate.point_estimate),
                        format_percentage(row.strategy.estimate.lower_bound),
                        format_percentage(row.strategy.estimate.upper_bound),
                        format_percentage(row.paired.estimate.point_estimate),
                        format_percentage(row.paired.familywise_lower_bound),
                        format_percentage(row.paired.familywise_upper_bound),
                        format_number(row.paired.raw_p_value, decimals=4),
                        format_number(row.paired.holm_adjusted_p_value, decimals=4),
                        format_percentage(row.strategy.minimum_leave_one_cluster_out_pct),
                        row.verdict,
                    )
                    for row in report.inference.challengers
                ],
            )
        )
    lines.extend(["", "## Formal sample cluster concentration", ""])
    lines.extend(
        markdown_table(
            ("Cluster", "Episodes", "Share"),
            [
                (row.cluster_key, row.episodes, format_percentage(row.share_pct))
                for row in report.inference.cluster_concentration[:10]
            ],
        )
    )
    lines.extend(["", "## Episode results", ""])
    lines.extend(
        markdown_table(
            (
                "Episode",
                "Base",
                "Policy",
                "Status",
                "Exit",
                "Net",
                "MFE",
                "Captured",
                "Duration",
                "Error",
            ),
            [
                (
                    row.trade.pump_event_id,
                    row.trade.base,
                    row.policy_key,
                    row.trade.status,
                    row.trade.exit_reason or "n/a",
                    format_percentage(row.trade.net_return_pct, missing="n/a"),
                    format_percentage(row.trade.mfe_pct, missing="n/a"),
                    format_percentage(row.trade.captured_move_pct, missing="n/a"),
                    format_number(
                        row.trade.duration_minutes,
                        suffix="m",
                        missing="n/a",
                    ),
                    row.trade.error or "",
                )
                for row in report.policy_trades
            ],
        )
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay the pre-registered pump-short exit-policy family"
    )
    parser.add_argument(
        "--since",
        type=parse_utc_datetime,
        default=EXIT_POLICY_COHORT_START,
        help="inclusive UTC cutoff; fixed to the registered exit-policy cohort",
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
    from .virtual_market import fetch_exit_policy_paths

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for virtual-exit-policy-report")
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    generated_at = datetime.now(UTC)
    until = args.until or generated_at
    if args.since >= until:
        raise ValueError(
            "the registered exit-policy cohort has not started; rerun after "
            f"{args.since.isoformat()}"
        )
    filters = ReplayFilters(
        since=args.since,
        until=until,
        strategy_versions=tuple(args.strategy_version or EXIT_POLICY_STRATEGY_VERSIONS),
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
    paths = await fetch_exit_policy_paths(dataset.eligible_episodes, EXCHANGE_FACTORIES)
    report = build_exit_policy_report(
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
    args = build_parser().parse_args()
    sys.stdout.write(asyncio.run(_run(args)))
