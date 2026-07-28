"""Descriptive score and component diagnostics over virtual pump-short replays."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from statistics import fmean, median

from .clustered_inference import (
    DEFAULT_BOOTSTRAP_ITERATIONS,
    DEFAULT_BOOTSTRAP_SEED,
    ClusterObservation,
    cluster_bootstrap_mean,
    derived_seed,
)
from .decision_quality import (
    BASELINE_POLICY_KEY,
    DECISION_QUALITY_POLICY_VERSION,
    SCORE_COMPONENT_SCHEMA_VERSION,
    SCORE_POLICIES,
    ScorePolicy,
    ScoreSelection,
    select_score_policy,
    selected_policy_decisions,
)
from .episode_replay import CONFIRMATION_COHORT_START, PROTOCOL_VERSION
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
    profit_factor,
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
    MarketPath,
    VirtualTrade,
    max_sequential_drawdown_usd,
    simulate_decision,
)

DECISION_QUALITY_REPORT_VERSION = "decision_quality_report_v1"
DECISION_QUALITY_STRATEGY_VERSIONS = ("pump_short_v1_market_quality",)


@dataclass(frozen=True)
class PolicyManifest:
    key: str
    min_score: int
    omitted_component: str | None


@dataclass(frozen=True)
class DecisionQualityManifest:
    protocol_version: str
    replay_engine_version: str
    replay_query_version: str
    report_version: str
    policy_version: str
    score_component_schema_version: str
    virtual_strategy_version: str
    entry_model_version: str
    exit_model_version: str
    cost_model_version: str
    market_path_version: str
    code_revision: str
    working_tree_dirty: bool
    generated_at: datetime
    dataset_since: datetime | None
    dataset_until_exclusive: datetime
    decision_input_fingerprint: str
    market_path_fingerprint: str
    strategy_versions: tuple[str, ...]
    resolver_version: str
    required_horizons: tuple[int, ...]
    fallback_allowed: bool
    baseline_policy: str
    policies: tuple[PolicyManifest, ...]
    bootstrap_iterations: int
    bootstrap_seed: int
    taker_fee_bps_per_side: float
    funding_cost_bps_per_8h: float
    interpretation: str = "discovery_only"


@dataclass(frozen=True)
class CountRow:
    name: str
    count: int


@dataclass(frozen=True)
class PolicyEpisodeResult:
    pump_event_id: int
    cluster_key: str
    base: str
    policy_key: str
    status: str
    selected_decision_id: str | None
    selected_at: datetime | None
    exchange: str | None
    action: str | None
    recorded_score: int | None
    effective_score: int | None
    pump_pct: float | None
    liquidity_quality_reason: str | None
    component_points: tuple[tuple[str, int, bool | None], ...]
    episode_net_return_pct: float | None
    episode_net_pnl_usd: float | None
    trade: VirtualTrade | None
    error: str | None = None


@dataclass(frozen=True)
class PolicyMetrics:
    policy_key: str
    eligible_episodes: int
    resolved_episodes: int
    selected: int
    cash: int
    completed_trades: int
    unresolved: int
    clusters: int
    largest_cluster_share_pct: float | None
    sample_status: str
    trade_rate_pct: float | None
    total_net_pnl_usd: float | None
    mean_episode_net_return_pct: float | None
    ci_95_lower_pct: float | None
    ci_95_upper_pct: float | None
    mean_trade_net_return_pct: float | None
    median_trade_net_return_pct: float | None
    win_rate_pct: float | None
    profit_factor: float | None
    avg_win_pct: float | None
    avg_loss_pct: float | None
    max_sequential_drawdown_usd: float | None
    initial_stop_rate_pct: float | None
    mean_mfe_pct: float | None
    mean_mae_pct: float | None
    mean_captured_move_pct: float | None


@dataclass(frozen=True)
class BucketMetrics:
    group: str
    bucket: str
    trades: int
    clusters: int
    mean_net_return_pct: float | None
    median_net_return_pct: float | None
    win_rate_pct: float | None
    mean_mfe_pct: float | None
    mean_mae_pct: float | None
    initial_stop_rate_pct: float | None


@dataclass(frozen=True)
class DecisionQualityReport:
    manifest: DecisionQualityManifest
    dataset_episodes: int
    eligible_episodes: int
    excluded_episodes: int
    input_exclusion_reasons: tuple[CountRow, ...]
    path_statuses: tuple[CountRow, ...]
    policy_failures: tuple[CountRow, ...]
    policy_metrics: tuple[PolicyMetrics, ...]
    score_buckets: tuple[BucketMetrics, ...]
    component_buckets: tuple[BucketMetrics, ...]
    diagnostic_buckets: tuple[BucketMetrics, ...]
    episode_results: tuple[PolicyEpisodeResult, ...]
    market_paths: tuple[DecisionMarketPath, ...]


def _mean(values: list[float]) -> float | None:
    return fmean(values) if values else None


def _count_rows(counter: Counter[str]) -> tuple[CountRow, ...]:
    return tuple(
        CountRow(name, count)
        for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    )


def _missing_path(episode: ReplayEpisode, selection: ScoreSelection) -> MarketPath:
    decision = selection.decision
    return MarketPath(
        pump_event_id=episode.pump_event_id,
        exchange=decision.exchange if decision else "",
        base=decision.base if decision else episode.base,
        status="missing_path",
        candles=(),
        error="market path was not loaded",
    )


def _evaluate_policy(
    episode: ReplayEpisode,
    policy: ScorePolicy,
    path_by_decision: dict[str, MarketPath],
    costs: CostParameters,
) -> PolicyEpisodeResult:
    selection = select_score_policy(episode, policy)
    decision = selection.decision
    if selection.status == "not_triggered":
        return PolicyEpisodeResult(
            episode.pump_event_id,
            episode.cluster_key,
            episode.base,
            policy.key,
            "not_triggered",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            (),
            0.0,
            0.0,
            None,
        )
    if selection.status == "unresolved" or decision is None:
        return PolicyEpisodeResult(
            episode.pump_event_id,
            episode.cluster_key,
            episode.base,
            policy.key,
            "selection_unresolved",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            (),
            None,
            None,
            None,
            selection.error or "score-policy selection failed",
        )

    path = path_by_decision.get(decision.decision_id or "")
    if path is None:
        path = _missing_path(episode, selection)
    trade = simulate_decision(
        episode,
        path,
        decision,
        selection_reason=f"score_policy:{policy.key}",
        costs=costs,
    )
    quality_reason: str | None = None
    if isinstance(decision.liquidity, dict):
        quality = decision.liquidity.get("quality")
        if isinstance(quality, dict) and isinstance(quality.get("reason"), str):
            quality_reason = quality["reason"]
    return PolicyEpisodeResult(
        episode.pump_event_id,
        episode.cluster_key,
        episode.base,
        policy.key,
        trade.status,
        decision.decision_id,
        decision.ts,
        decision.exchange,
        decision.action,
        decision.score,
        selection.effective_score,
        decision.pump_pct,
        quality_reason,
        tuple((item.name, item.points, item.data_available) for item in selection.components),
        trade.net_return_pct,
        trade.net_pnl_usd,
        trade,
        trade.error,
    )


def _policy_metrics(
    policy: ScorePolicy,
    results: tuple[PolicyEpisodeResult, ...],
    *,
    bootstrap_iterations: int,
    bootstrap_seed: int,
) -> PolicyMetrics:
    selected = tuple(result for result in results if result.policy_key == policy.key)
    resolved = [
        result.episode_net_return_pct
        for result in selected
        if result.episode_net_return_pct is not None
    ]
    observations = tuple(
        ClusterObservation(result.cluster_key, result.episode_net_return_pct)
        for result in selected
        if result.episode_net_return_pct is not None
    )
    clusters = len({observation.cluster_key for observation in observations})
    cluster_counts = Counter(observation.cluster_key for observation in observations)
    largest_cluster_share = (
        max(cluster_counts.values()) / len(observations) * 100 if observations else None
    )
    sample_status = (
        "formal_size"
        if len(observations) >= 100 and clusters >= 30
        else ("directional" if len(observations) >= 50 else "collecting")
    )
    ci_lower: float | None = None
    ci_upper: float | None = None
    if observations and clusters >= 2:
        estimate = cluster_bootstrap_mean(
            observations,
            iterations=bootstrap_iterations,
            seed=derived_seed(bootstrap_seed, policy.key),
        ).estimate
        ci_lower = estimate.lower_bound
        ci_upper = estimate.upper_bound

    trades = tuple(
        result.trade
        for result in selected
        if result.trade is not None and result.trade.status == "complete"
    )
    returns = [trade.net_return_pct for trade in trades if trade.net_return_pct is not None]
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value <= 0]
    pnl_values = [
        result.episode_net_pnl_usd for result in selected if result.episode_net_pnl_usd is not None
    ]
    captured = [trade.captured_move_pct for trade in trades if trade.captured_move_pct is not None]
    return PolicyMetrics(
        policy_key=policy.key,
        eligible_episodes=len(selected),
        resolved_episodes=len(resolved),
        selected=sum(result.selected_decision_id is not None for result in selected),
        cash=sum(result.status == "not_triggered" for result in selected),
        completed_trades=len(trades),
        unresolved=len(selected) - len(resolved),
        clusters=clusters,
        largest_cluster_share_pct=largest_cluster_share,
        sample_status=sample_status,
        trade_rate_pct=len(trades) / len(resolved) * 100 if resolved else None,
        total_net_pnl_usd=sum(pnl_values) if pnl_values else None,
        mean_episode_net_return_pct=_mean(resolved),
        ci_95_lower_pct=ci_lower,
        ci_95_upper_pct=ci_upper,
        mean_trade_net_return_pct=_mean(returns),
        median_trade_net_return_pct=median(returns) if returns else None,
        win_rate_pct=sum(value > 0 for value in returns) / len(returns) * 100 if returns else None,
        profit_factor=profit_factor(returns),
        avg_win_pct=_mean(wins),
        avg_loss_pct=_mean(losses),
        max_sequential_drawdown_usd=max_sequential_drawdown_usd(trades),
        initial_stop_rate_pct=(
            sum(trade.exit_reason == "initial_sl" for trade in trades) / len(trades) * 100
            if trades
            else None
        ),
        mean_mfe_pct=_mean([trade.mfe_pct for trade in trades if trade.mfe_pct is not None]),
        mean_mae_pct=_mean([trade.mae_pct for trade in trades if trade.mae_pct is not None]),
        mean_captured_move_pct=_mean(captured),
    )


def _bucket_metrics(
    group: str,
    bucket: str,
    results: list[PolicyEpisodeResult],
) -> BucketMetrics:
    trades = tuple(
        result.trade
        for result in results
        if result.trade is not None and result.trade.status == "complete"
    )
    returns = [trade.net_return_pct for trade in trades if trade.net_return_pct is not None]
    return BucketMetrics(
        group=group,
        bucket=bucket,
        trades=len(trades),
        clusters=len({result.cluster_key for result in results if result.trade in trades}),
        mean_net_return_pct=_mean(returns),
        median_net_return_pct=median(returns) if returns else None,
        win_rate_pct=sum(value > 0 for value in returns) / len(returns) * 100 if returns else None,
        mean_mfe_pct=_mean([trade.mfe_pct for trade in trades if trade.mfe_pct is not None]),
        mean_mae_pct=_mean([trade.mae_pct for trade in trades if trade.mae_pct is not None]),
        initial_stop_rate_pct=(
            sum(trade.exit_reason == "initial_sl" for trade in trades) / len(trades) * 100
            if trades
            else None
        ),
    )


def _calibration_buckets(
    results: tuple[PolicyEpisodeResult, ...],
) -> tuple[
    tuple[BucketMetrics, ...],
    tuple[BucketMetrics, ...],
    tuple[BucketMetrics, ...],
]:
    controls = [
        result
        for result in results
        if result.policy_key == "score_any"
        and result.trade is not None
        and result.trade.status == "complete"
    ]
    by_score: dict[int, list[PolicyEpisodeResult]] = defaultdict(list)
    by_component: dict[tuple[str, str], list[PolicyEpisodeResult]] = defaultdict(list)
    diagnostics: dict[tuple[str, str], list[PolicyEpisodeResult]] = defaultdict(list)
    for result in controls:
        if result.recorded_score is not None:
            by_score[result.recorded_score].append(result)
        for name, points, data_available in result.component_points:
            bucket = (
                "missing"
                if data_available is False
                else ("unknown" if data_available is None else str(points))
            )
            by_component[(name, bucket)].append(result)
        pump_pct = result.pump_pct
        pump_band = (
            "unknown"
            if pump_pct is None
            else ("30-49" if pump_pct < 50 else ("50-99" if pump_pct < 100 else "100+"))
        )
        diagnostics[("pump_pct", pump_band)].append(result)
        diagnostics[("exchange", result.exchange or "missing")].append(result)
        diagnostics[("action", result.action or "missing")].append(result)
        diagnostics[("liquidity_quality", result.liquidity_quality_reason or "missing")].append(
            result
        )
    score_rows = tuple(
        _bucket_metrics("recorded_score", str(score), rows)
        for score, rows in sorted(by_score.items())
    )
    component_rows = tuple(
        _bucket_metrics(name, bucket, rows) for (name, bucket), rows in sorted(by_component.items())
    )
    diagnostic_rows = tuple(
        _bucket_metrics(name, bucket, rows) for (name, bucket), rows in sorted(diagnostics.items())
    )
    return score_rows, component_rows, diagnostic_rows


def build_decision_quality_report(
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
) -> DecisionQualityReport:
    revision = normalize_code_revision(code_revision)
    if bootstrap_iterations < 100:
        raise ValueError("bootstrap iterations must be at least 100")
    path_counts = Counter(path.decision_id for path in paths)
    duplicates = sorted(key for key, count in path_counts.items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate market paths for decisions: {duplicates}")
    path_by_decision = {item.decision_id: item.path for item in paths}
    results = tuple(
        _evaluate_policy(episode, policy, path_by_decision, costs)
        for episode in dataset.eligible_episodes
        for policy in SCORE_POLICIES
    )
    score_buckets, component_buckets, diagnostic_buckets = _calibration_buckets(results)
    exclusions = Counter(
        reason for episode in dataset.excluded_episodes for reason in episode.exclusion_reasons
    )
    return DecisionQualityReport(
        manifest=DecisionQualityManifest(
            protocol_version=PROTOCOL_VERSION,
            replay_engine_version=FOUNDATION_VERSION,
            replay_query_version=QUERY_VERSION,
            report_version=DECISION_QUALITY_REPORT_VERSION,
            policy_version=DECISION_QUALITY_POLICY_VERSION,
            score_component_schema_version=SCORE_COMPONENT_SCHEMA_VERSION,
            virtual_strategy_version=VIRTUAL_STRATEGY_VERSION,
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
            baseline_policy=BASELINE_POLICY_KEY,
            policies=tuple(
                PolicyManifest(policy.key, policy.min_score, policy.omitted_component)
                for policy in SCORE_POLICIES
            ),
            bootstrap_iterations=bootstrap_iterations,
            bootstrap_seed=bootstrap_seed,
            taker_fee_bps_per_side=costs.taker_fee_bps_per_side,
            funding_cost_bps_per_8h=costs.funding_cost_bps_per_8h,
        ),
        dataset_episodes=len(dataset.episodes),
        eligible_episodes=len(dataset.eligible_episodes),
        excluded_episodes=len(dataset.excluded_episodes),
        input_exclusion_reasons=_count_rows(exclusions),
        path_statuses=_count_rows(Counter(item.path.status for item in paths)),
        policy_failures=_count_rows(
            Counter(
                result.error or result.status
                for result in results
                if result.episode_net_return_pct is None
            )
        ),
        policy_metrics=tuple(
            _policy_metrics(
                policy,
                results,
                bootstrap_iterations=bootstrap_iterations,
                bootstrap_seed=bootstrap_seed,
            )
            for policy in SCORE_POLICIES
        ),
        score_buckets=score_buckets,
        component_buckets=component_buckets,
        diagnostic_buckets=diagnostic_buckets,
        episode_results=results,
        market_paths=paths,
    )


def render_json(report: DecisionQualityReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True, allow_nan=False)


def _factor(value: float | None) -> str:
    if value is None:
        return "n/a"
    if math.isinf(value):
        return "inf"
    return format_number(value)


def _usd(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.2f}"


def render_markdown(report: DecisionQualityReport) -> str:
    manifest = report.manifest
    since = manifest.dataset_since.isoformat() if manifest.dataset_since else "unbounded"
    lines = [
        "# Pump Short Decision Quality Report",
        "",
        f"Generated: {manifest.generated_at.isoformat()}",
        f"Code revision: `{manifest.code_revision}`",
        f"Working tree dirty: {'yes' if manifest.working_tree_dirty else 'no'}",
        f"Decision fingerprint: `{manifest.decision_input_fingerprint}`",
        f"Market-path fingerprint: `{manifest.market_path_fingerprint}`",
        f"Scope: {since} <= decision < {manifest.dataset_until_exclusive.isoformat()}",
        "",
        (
            "> Discovery-only diagnostics. This report compares independent episode "
            "replays, does not model account-level capital constraints, and cannot "
            "authorize a production score change."
        ),
        "",
        "## Coverage",
        "",
    ]
    lines.extend(
        markdown_table(
            ("Metric", "Value"),
            [
                ("Dataset episodes", report.dataset_episodes),
                ("Eligible episodes", report.eligible_episodes),
                ("Excluded episodes", report.excluded_episodes),
                ("Selected exact paths", len(report.market_paths)),
                ("Fallback outcomes allowed", "yes" if manifest.fallback_allowed else "no"),
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
    lines.extend(["", "## Market path coverage", ""])
    lines.extend(
        markdown_table(
            ("Status", "Paths"),
            [(row.name, row.count) for row in report.path_statuses],
        )
    )
    lines.extend(["", "## Unresolved policy evaluations", ""])
    lines.extend(
        markdown_table(
            ("Reason", "Evaluations"),
            [(row.name, row.count) for row in report.policy_failures],
        )
    )
    lines.extend(["", "## Policy scoreboard", ""])
    lines.extend(
        markdown_table(
            (
                "Policy",
                "Resolved",
                "Trades",
                "Cash",
                "Unresolved",
                "Clusters",
                "Largest cluster",
                "Sample",
                "Trade rate",
                "Total P&L",
                "Episode net",
                "95% CI",
                "Trade net",
                "Win rate",
                "PF",
                "Max DD",
                "Initial SL",
            ),
            [
                (
                    row.policy_key,
                    row.resolved_episodes,
                    row.completed_trades,
                    row.cash,
                    row.unresolved,
                    row.clusters,
                    format_percentage(row.largest_cluster_share_pct, missing="n/a"),
                    row.sample_status,
                    format_percentage(row.trade_rate_pct, missing="n/a"),
                    _usd(row.total_net_pnl_usd),
                    format_percentage(row.mean_episode_net_return_pct, missing="n/a"),
                    (
                        f"{format_percentage(row.ci_95_lower_pct)} .. "
                        f"{format_percentage(row.ci_95_upper_pct)}"
                        if row.ci_95_lower_pct is not None
                        else "n/a"
                    ),
                    format_percentage(row.mean_trade_net_return_pct, missing="n/a"),
                    format_percentage(row.win_rate_pct, missing="n/a"),
                    _factor(row.profit_factor),
                    _usd(row.max_sequential_drawdown_usd),
                    format_percentage(row.initial_stop_rate_pct, missing="n/a"),
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
                "independent episode results. They are not an account equity simulation._"
            ),
            "",
            "## Policy trade diagnostics",
            "",
        ]
    )
    lines.extend(
        markdown_table(
            (
                "Policy",
                "Avg win",
                "Avg loss",
                "Median trade",
                "Mean MFE",
                "Mean MAE",
                "Captured move",
            ),
            [
                (
                    row.policy_key,
                    format_percentage(row.avg_win_pct, missing="n/a"),
                    format_percentage(row.avg_loss_pct, missing="n/a"),
                    format_percentage(row.median_trade_net_return_pct, missing="n/a"),
                    format_percentage(row.mean_mfe_pct, missing="n/a"),
                    format_percentage(row.mean_mae_pct, missing="n/a"),
                    format_percentage(row.mean_captured_move_pct, missing="n/a"),
                )
                for row in report.policy_metrics
            ],
        )
    )
    lines.extend(
        [
            "",
            "## Recorded score calibration",
            "",
        ]
    )
    lines.extend(
        markdown_table(
            ("Score", "Trades", "Clusters", "Mean net", "Median", "Win rate", "MFE", "MAE"),
            [
                (
                    row.bucket,
                    row.trades,
                    row.clusters,
                    format_percentage(row.mean_net_return_pct, missing="n/a"),
                    format_percentage(row.median_net_return_pct, missing="n/a"),
                    format_percentage(row.win_rate_pct, missing="n/a"),
                    format_percentage(row.mean_mfe_pct, missing="n/a"),
                    format_percentage(row.mean_mae_pct, missing="n/a"),
                )
                for row in report.score_buckets
            ],
        )
    )
    lines.extend(["", "## Component calibration", ""])
    lines.extend(
        markdown_table(
            (
                "Component",
                "Points",
                "Trades",
                "Clusters",
                "Mean net",
                "Win rate",
                "MFE",
                "MAE",
                "Initial SL",
            ),
            [
                (
                    row.group,
                    row.bucket,
                    row.trades,
                    row.clusters,
                    format_percentage(row.mean_net_return_pct, missing="n/a"),
                    format_percentage(row.win_rate_pct, missing="n/a"),
                    format_percentage(row.mean_mfe_pct, missing="n/a"),
                    format_percentage(row.mean_mae_pct, missing="n/a"),
                    format_percentage(row.initial_stop_rate_pct, missing="n/a"),
                )
                for row in report.component_buckets
            ],
        )
    )
    lines.extend(["", "## Operational and market segments", ""])
    lines.extend(
        markdown_table(
            (
                "Dimension",
                "Bucket",
                "Trades",
                "Clusters",
                "Mean net",
                "Win rate",
                "MFE",
                "MAE",
                "Initial SL",
            ),
            [
                (
                    row.group,
                    row.bucket,
                    row.trades,
                    row.clusters,
                    format_percentage(row.mean_net_return_pct, missing="n/a"),
                    format_percentage(row.win_rate_pct, missing="n/a"),
                    format_percentage(row.mean_mfe_pct, missing="n/a"),
                    format_percentage(row.mean_mae_pct, missing="n/a"),
                    format_percentage(row.initial_stop_rate_pct, missing="n/a"),
                )
                for row in report.diagnostic_buckets
            ],
        )
    )
    lines.extend(["", "## Interpretation guardrails", ""])
    lines.extend(
        [
            "- `score_any` is the market-quality-only control.",
            f"- `{BASELINE_POLICY_KEY}` mirrors the current score floor.",
            (
                "- `score_6_without_*` removes recorded component points while keeping "
                "the cutoff fixed. It is a discovery diagnostic, not a causal estimate."
            ),
            (
                "- Score and component buckets use one first market-quality-eligible "
                "decision per episode, so repeated ticks do not inflate the sample."
            ),
            (
                "- Any promising threshold or component rule must be registered and "
                "confirmed on a new untouched cohort before live shadow or production."
            ),
        ]
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Describe pump-short score and component quality with virtual replays"
    )
    parser.add_argument(
        "--since",
        type=parse_utc_datetime,
        default=CONFIRMATION_COHORT_START,
        help="inclusive UTC cutoff; defaults to the replay confirmation cohort",
    )
    parser.add_argument(
        "--until",
        type=parse_utc_datetime,
        help="exclusive UTC cutoff; defaults to the run start",
    )
    parser.add_argument(
        "--strategy-version",
        action="append",
        help="recorded strategy cohort; repeat for multiple cohorts",
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
        raise ValueError("DATABASE_URL is required for decision-quality-report")
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    generated_at = datetime.now(UTC)
    filters = ReplayFilters(
        since=args.since,
        until=args.until or generated_at,
        strategy_versions=tuple(args.strategy_version or DECISION_QUALITY_STRATEGY_VERSIONS),
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
    selected = selected_policy_decisions(dataset.eligible_episodes)
    paths = await fetch_decision_market_paths(selected, EXCHANGE_FACTORIES)
    report = build_decision_quality_report(
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
