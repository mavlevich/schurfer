"""Read-only paired report for pre-registered entry-confirmation challengers."""

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

from .entry_challenger_inference import (
    DEFAULT_INFERENCE_SETTINGS,
    ENTRY_INFERENCE_VERSION,
    EntryChallengerInference,
    InferenceEpisode,
    build_entry_challenger_inference,
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
from .virtual_entry_challengers import (
    ENTRY_CHALLENGER_FAMILY_VERSION,
    ENTRY_CONFIRMATION_MODEL_VERSION,
    ENTRY_VARIANTS,
    EntryConfirmation,
    EntryVariant,
    evaluate_entry_confirmation,
)
from .virtual_strategy import (
    COST_MODEL_VERSION,
    DEFAULT_COSTS,
    EXIT_MODEL_VERSION,
    SELECTION_MODEL_VERSION,
    VIRTUAL_STRATEGY_VERSION,
    CostParameters,
    MarketPath,
    VirtualTrade,
    market_path_fingerprint,
    select_episode_decision,
    simulate_episode,
    simulate_episode_at_entry,
)

ENTRY_CHALLENGER_REPORT_VERSION = "virtual_entry_challenger_report_v2"
ENTRY_CHALLENGER_MARKET_PATH_VERSION = "ccxt_5m_exact_anchor_entry_context_v1"
ENTRY_CHALLENGER_COHORT_START = datetime(2026, 7, 29, tzinfo=UTC)
# Same latent gap as the 2026-08-04 entry-floor finding: a challenger whose entry
# condition rarely confirms can reach every other formal-sample gate almost
# entirely on not_confirmed (cash-equivalent) episodes while the actually-entered
# sample stays tiny. Require a floor of real confirmed entries before calling any
# variant's read formal. Baseline always has a recorded decision to replay (no
# threshold to fail), so only the challengers can fall short of this floor.
MINIMUM_TRIGGERED_EPISODES = 20


@dataclass(frozen=True)
class EntryVariantManifest:
    key: str
    version: str
    require_red_candle: bool
    min_retrace_pct: float
    lookback_bars: int
    max_wait_minutes: int
    execution_gap_bars: int


@dataclass(frozen=True)
class EntryChallengerManifest:
    protocol_version: str
    replay_engine_version: str
    replay_query_version: str
    report_version: str
    baseline_strategy_version: str
    selection_model_version: str
    entry_confirmation_model_version: str
    challenger_family_version: str
    exit_model_version: str
    cost_model_version: str
    market_path_version: str
    inference_version: str
    bootstrap_iterations: int
    bootstrap_seed: int
    bootstrap_confidence_level: float
    holm_family_alpha: float
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
    taker_fee_bps_per_side: float
    funding_cost_bps_per_8h: float
    variants: tuple[EntryVariantManifest, ...]
    observation_unit: str = "pump_event_id"
    side: str = "short"
    candle_availability_policy: str = "fully_closed_at_candidate_entry"
    eligibility_policy: str = "baseline_episode_eligibility_held_constant"
    liquidity_slippage_policy: str = "baseline_decision_snapshot_held_constant"
    within_bar_policy: str = "conservative_stop_first"
    formal_sample_policy: str = "first_100_eligible_episodes_chronological"
    report_scope: str = "formal_inference_when_ready_shadow_only"


@dataclass(frozen=True)
class ChallengerEpisodeResult:
    pump_event_id: int
    cluster_key: str
    base: str
    exchange: str
    variant_key: str
    status: str
    episode_net_return_pct: float | None
    confirmation: EntryConfirmation
    trade: VirtualTrade | None
    error: str | None = None


@dataclass(frozen=True)
class EntryVariantMetrics:
    variant_key: str
    eligible_episodes: int
    paired_resolved: int
    confirmed_entries: int
    not_confirmed: int
    avoided_losing_entries: int
    missed_winning_entries: int
    completed_trades: int
    unresolved: int
    trade_rate_pct: float | None
    mean_episode_net_return_pct: float | None
    conditional_trade_net_return_pct: float | None
    conditional_win_rate_pct: float | None
    initial_stop_rate_pct: float | None
    mean_confirmed_wait_minutes: float | None
    mean_effective_wait_minutes: float | None


@dataclass(frozen=True)
class PairedComparison:
    variant_key: str
    episodes: int
    mean_baseline_net_return_pct: float | None
    mean_challenger_net_return_pct: float | None
    mean_delta_pct: float | None
    improved_episodes: int
    worsened_episodes: int
    unchanged_episodes: int


@dataclass(frozen=True)
class CountRow:
    name: str
    count: int


@dataclass(frozen=True)
class EntryChallengerReport:
    manifest: EntryChallengerManifest
    dataset_episodes: int
    eligible_episodes: int
    excluded_episodes: int
    input_exclusion_reasons: tuple[CountRow, ...]
    baseline_trades: tuple[VirtualTrade, ...]
    variant_metrics: tuple[EntryVariantMetrics, ...]
    paired_comparisons: tuple[PairedComparison, ...]
    challenger_results: tuple[ChallengerEpisodeResult, ...]
    inference: EntryChallengerInference
    market_paths: tuple[MarketPath, ...]


def _mean(values: list[float]) -> float | None:
    return fmean(values) if values else None


def _count_rows(counter: Counter[str]) -> tuple[CountRow, ...]:
    return tuple(
        CountRow(name, count)
        for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    )


def _variant_manifest(variant: EntryVariant) -> EntryVariantManifest:
    return EntryVariantManifest(
        key=variant.key,
        version=variant.version,
        require_red_candle=variant.require_red_candle,
        min_retrace_pct=variant.min_retrace_pct,
        lookback_bars=variant.lookback_bars,
        max_wait_minutes=variant.max_wait_minutes,
        execution_gap_bars=variant.execution_gap_bars,
    )


def _unresolved_result(
    *,
    episode_id: int,
    cluster_key: str,
    base: str,
    exchange: str,
    variant: EntryVariant,
    status: str,
    error: str,
) -> ChallengerEpisodeResult:
    return ChallengerEpisodeResult(
        pump_event_id=episode_id,
        cluster_key=cluster_key,
        base=base,
        exchange=exchange,
        variant_key=variant.key,
        status=status,
        episode_net_return_pct=None,
        confirmation=EntryConfirmation(
            status="unresolved",
            entry_at_ms=None,
            signal_at=None,
            wait_minutes=None,
            closed_red=None,
            retrace_pct=None,
            error=error,
        ),
        trade=None,
        error=error,
    )


def _simulate_challenger(
    episode: ReplayEpisode,
    path: MarketPath,
    variant: EntryVariant,
    costs: CostParameters,
) -> ChallengerEpisodeResult:
    selection = select_episode_decision(episode)
    decision = selection.decision
    if path.status != "complete":
        return _unresolved_result(
            episode_id=episode.pump_event_id,
            cluster_key=episode.cluster_key,
            base=episode.base,
            exchange=decision.exchange,
            variant=variant,
            status="market_path_unavailable",
            error=path.error or path.status,
        )
    if (
        path.pump_event_id != episode.pump_event_id
        or path.exchange != decision.exchange
        or path.base.casefold() != decision.base.casefold()
    ):
        return _unresolved_result(
            episode_id=episode.pump_event_id,
            cluster_key=episode.cluster_key,
            base=episode.base,
            exchange=decision.exchange,
            variant=variant,
            status="market_path_mismatch",
            error="market path does not match selected episode decision",
        )
    confirmation = evaluate_entry_confirmation(decision, path.candles, variant)
    if confirmation.status == "unresolved":
        return ChallengerEpisodeResult(
            pump_event_id=episode.pump_event_id,
            cluster_key=episode.cluster_key,
            base=episode.base,
            exchange=decision.exchange,
            variant_key=variant.key,
            status="entry_confirmation_unresolved",
            episode_net_return_pct=None,
            confirmation=confirmation,
            trade=None,
            error=confirmation.error,
        )
    if confirmation.status == "not_confirmed":
        return ChallengerEpisodeResult(
            pump_event_id=episode.pump_event_id,
            cluster_key=episode.cluster_key,
            base=episode.base,
            exchange=decision.exchange,
            variant_key=variant.key,
            status="not_triggered",
            episode_net_return_pct=0.0,
            confirmation=confirmation,
            trade=None,
        )
    if confirmation.entry_at_ms is None:
        raise RuntimeError("confirmed entry is missing its timestamp")
    trade = simulate_episode_at_entry(
        episode,
        path,
        entry_at_ms=confirmation.entry_at_ms,
        selection_reason=f"challenger:{variant.version}",
        costs=costs,
    )
    return ChallengerEpisodeResult(
        pump_event_id=episode.pump_event_id,
        cluster_key=episode.cluster_key,
        base=episode.base,
        exchange=decision.exchange,
        variant_key=variant.key,
        status=trade.status,
        episode_net_return_pct=trade.net_return_pct,
        confirmation=confirmation,
        trade=trade,
        error=trade.error,
    )


def _metrics(
    variant: EntryVariant,
    results: tuple[ChallengerEpisodeResult, ...],
    baseline_by_event: dict[int, VirtualTrade],
) -> tuple[EntryVariantMetrics, PairedComparison]:
    selected = tuple(result for result in results if result.variant_key == variant.key)
    completed = tuple(
        result.trade
        for result in selected
        if result.trade is not None and result.trade.status == "complete"
    )
    not_confirmed = sum(result.status == "not_triggered" for result in selected)
    paired_values: list[tuple[float, float]] = []
    for result in selected:
        baseline = baseline_by_event.get(result.pump_event_id)
        challenger_return = result.episode_net_return_pct
        if (
            baseline is None
            or baseline.status != "complete"
            or baseline.net_return_pct is None
            or challenger_return is None
        ):
            continue
        paired_values.append((baseline.net_return_pct, challenger_return))
    baseline_returns = [baseline for baseline, _ in paired_values]
    episode_returns = [challenger for _, challenger in paired_values]
    no_entry_baseline_returns = [
        baseline.net_return_pct
        for result in selected
        if result.status == "not_triggered"
        and (baseline := baseline_by_event.get(result.pump_event_id)) is not None
        and baseline.status == "complete"
        and baseline.net_return_pct is not None
    ]
    trade_returns = [
        trade.net_return_pct for trade in completed if trade.net_return_pct is not None
    ]
    confirmed_waits = [
        result.confirmation.wait_minutes
        for result in selected
        if result.confirmation.status == "confirmed"
        and result.confirmation.wait_minutes is not None
    ]
    effective_waits = [
        result.confirmation.wait_minutes
        for result in selected
        if result.confirmation.status in {"confirmed", "not_confirmed"}
        and result.confirmation.wait_minutes is not None
    ]
    wins = sum(value > 0 for value in trade_returns)
    initial_stops = sum(trade.exit_reason == "initial_sl" for trade in completed)
    deltas = [
        challenger_return - baseline_return for baseline_return, challenger_return in paired_values
    ]
    return (
        EntryVariantMetrics(
            variant_key=variant.key,
            eligible_episodes=len(selected),
            paired_resolved=len(paired_values),
            confirmed_entries=sum(result.confirmation.status == "confirmed" for result in selected),
            not_confirmed=not_confirmed,
            avoided_losing_entries=sum(value <= 0 for value in no_entry_baseline_returns),
            missed_winning_entries=sum(value > 0 for value in no_entry_baseline_returns),
            completed_trades=len(completed),
            unresolved=len(selected) - len(completed) - not_confirmed,
            trade_rate_pct=(len(completed) / len(paired_values) * 100 if paired_values else None),
            mean_episode_net_return_pct=_mean(episode_returns),
            conditional_trade_net_return_pct=_mean(trade_returns),
            conditional_win_rate_pct=wins / len(trade_returns) * 100 if trade_returns else None,
            initial_stop_rate_pct=(initial_stops / len(completed) * 100 if completed else None),
            mean_confirmed_wait_minutes=_mean(
                [value for value in confirmed_waits if value is not None]
            ),
            mean_effective_wait_minutes=_mean(
                [value for value in effective_waits if value is not None]
            ),
        ),
        PairedComparison(
            variant_key=variant.key,
            episodes=len(paired_values),
            mean_baseline_net_return_pct=_mean(baseline_returns),
            mean_challenger_net_return_pct=_mean(episode_returns),
            mean_delta_pct=_mean(deltas),
            improved_episodes=sum(delta > 1e-12 for delta in deltas),
            worsened_episodes=sum(delta < -1e-12 for delta in deltas),
            unchanged_episodes=sum(abs(delta) <= 1e-12 for delta in deltas),
        ),
    )


def build_entry_challenger_report(
    dataset: ReplayDataset,
    filters: ReplayFilters,
    paths: tuple[MarketPath, ...],
    *,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
    costs: CostParameters = DEFAULT_COSTS,
) -> EntryChallengerReport:
    revision = normalize_code_revision(code_revision)
    if filters.since is None:
        raise ValueError("entry challenger report requires an explicit cohort start")
    event_counts = Counter(path.pump_event_id for path in paths)
    duplicates = sorted(event_id for event_id, count in event_counts.items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate market paths for episodes: {duplicates}")
    path_by_event = {path.pump_event_id: path for path in paths}
    baseline_trades = tuple(
        simulate_episode(
            episode,
            path_by_event.get(
                episode.pump_event_id,
                MarketPath(
                    pump_event_id=episode.pump_event_id,
                    exchange="",
                    base=episode.base,
                    status="missing_path",
                    candles=(),
                    error="market path was not loaded",
                ),
            ),
            costs=costs,
        )
        for episode in dataset.eligible_episodes
    )
    results = tuple(
        _simulate_challenger(
            episode,
            path_by_event.get(
                episode.pump_event_id,
                MarketPath(
                    pump_event_id=episode.pump_event_id,
                    exchange="",
                    base=episode.base,
                    status="missing_path",
                    candles=(),
                    error="market path was not loaded",
                ),
            ),
            variant,
            costs,
        )
        for episode in dataset.eligible_episodes
        for variant in ENTRY_VARIANTS
    )
    baseline_by_event = {trade.pump_event_id: trade for trade in baseline_trades}
    result_by_event_variant = {
        (result.pump_event_id, result.variant_key): result for result in results
    }
    inference = build_entry_challenger_inference(
        tuple(
            InferenceEpisode(
                pump_event_id=episode.pump_event_id,
                cluster_key=episode.cluster_key,
                baseline_return_pct=(
                    baseline.net_return_pct
                    if (baseline := baseline_by_event[episode.pump_event_id]).status == "complete"
                    else None
                ),
                challenger_returns_pct=tuple(
                    (
                        variant.key,
                        result_by_event_variant[
                            (episode.pump_event_id, variant.key)
                        ].episode_net_return_pct,
                    )
                    for variant in ENTRY_VARIANTS
                ),
                challenger_triggered=tuple(
                    (
                        variant.key,
                        result_by_event_variant[
                            (episode.pump_event_id, variant.key)
                        ].confirmation.status
                        == "confirmed",
                    )
                    for variant in ENTRY_VARIANTS
                ),
            )
            for episode in dataset.eligible_episodes
        ),
        tuple(variant.key for variant in ENTRY_VARIANTS),
        minimum_triggered_episodes=MINIMUM_TRIGGERED_EPISODES,
    )
    metric_pairs = tuple(
        _metrics(variant, results, baseline_by_event) for variant in ENTRY_VARIANTS
    )
    exclusions = Counter(
        reason for episode in dataset.excluded_episodes for reason in episode.exclusion_reasons
    )
    return EntryChallengerReport(
        manifest=EntryChallengerManifest(
            protocol_version=PROTOCOL_VERSION,
            replay_engine_version=FOUNDATION_VERSION,
            replay_query_version=QUERY_VERSION,
            report_version=ENTRY_CHALLENGER_REPORT_VERSION,
            baseline_strategy_version=VIRTUAL_STRATEGY_VERSION,
            selection_model_version=SELECTION_MODEL_VERSION,
            entry_confirmation_model_version=ENTRY_CONFIRMATION_MODEL_VERSION,
            challenger_family_version=ENTRY_CHALLENGER_FAMILY_VERSION,
            exit_model_version=EXIT_MODEL_VERSION,
            cost_model_version=COST_MODEL_VERSION,
            market_path_version=ENTRY_CHALLENGER_MARKET_PATH_VERSION,
            inference_version=ENTRY_INFERENCE_VERSION,
            bootstrap_iterations=DEFAULT_INFERENCE_SETTINGS.iterations,
            bootstrap_seed=DEFAULT_INFERENCE_SETTINGS.seed,
            bootstrap_confidence_level=DEFAULT_INFERENCE_SETTINGS.confidence_level,
            holm_family_alpha=DEFAULT_INFERENCE_SETTINGS.family_alpha,
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
            taker_fee_bps_per_side=costs.taker_fee_bps_per_side,
            funding_cost_bps_per_8h=costs.funding_cost_bps_per_8h,
            variants=tuple(_variant_manifest(variant) for variant in ENTRY_VARIANTS),
        ),
        dataset_episodes=len(dataset.episodes),
        eligible_episodes=len(dataset.eligible_episodes),
        excluded_episodes=len(dataset.excluded_episodes),
        input_exclusion_reasons=_count_rows(exclusions),
        baseline_trades=baseline_trades,
        variant_metrics=tuple(pair[0] for pair in metric_pairs),
        paired_comparisons=tuple(pair[1] for pair in metric_pairs),
        challenger_results=results,
        inference=inference,
        market_paths=paths,
    )


def render_json(report: EntryChallengerReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True)


def render_markdown(report: EntryChallengerReport) -> str:
    manifest = report.manifest
    lines = [
        "# Pump Short Entry Challenger Replay",
        "",
        f"Generated: {manifest.generated_at.isoformat()}",
        f"Code revision: `{manifest.code_revision}`",
        f"Working tree dirty: {'yes' if manifest.working_tree_dirty else 'no'}",
        f"Decision fingerprint: `{manifest.decision_input_fingerprint}`",
        f"Market-path fingerprint: `{manifest.market_path_fingerprint}`",
        (
            "Scope: "
            f"{manifest.dataset_since.isoformat()}"
            f" <= decision < {manifest.dataset_until_exclusive.isoformat()}"
        ),
        "",
        (
            "> Formal inference status: "
            f"`{report.inference.readiness.status}`. "
            "No result changes production configuration or authorizes real trading."
        ),
        "",
        "## Registered family",
        "",
    ]
    lines.extend(
        markdown_table(
            (
                "Variant",
                "Version",
                "Red required",
                "Min retrace",
                "Closed bars",
                "Execution gap",
                "Max wait",
            ),
            [
                (
                    variant.key,
                    variant.version,
                    "yes" if variant.require_red_candle else "no",
                    format_percentage(variant.min_retrace_pct),
                    variant.lookback_bars,
                    f"{variant.execution_gap_bars} bar",
                    f"{variant.max_wait_minutes}m",
                )
                for variant in manifest.variants
            ],
        )
    )
    lines.extend(["", "## Shared model", ""])
    lines.extend(
        markdown_table(
            ("Component", "Version / policy"),
            [
                ("Baseline", manifest.baseline_strategy_version),
                ("Selection", manifest.selection_model_version),
                ("Confirmation", manifest.entry_confirmation_model_version),
                ("Exit", manifest.exit_model_version),
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
                ("Holm family alpha", format_number(manifest.holm_family_alpha)),
                ("Eligibility", manifest.eligibility_policy),
                ("Liquidity slippage", manifest.liquidity_slippage_policy),
                ("Within-bar ambiguity", manifest.within_bar_policy),
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
                (
                    "Formal sample episodes",
                    report.inference.readiness.formal_sample_episodes,
                ),
                (
                    "Formal sample clusters",
                    report.inference.readiness.formal_sample_clusters,
                ),
                (
                    "Completely paired formal episodes",
                    report.inference.readiness.completely_paired_episodes,
                ),
                (
                    "Least-triggered variant (formal window)",
                    report.inference.readiness.least_triggered_variant or "n/a",
                ),
                (
                    "Least-triggered count (formal window)",
                    (
                        report.inference.readiness.least_triggered_count
                        if report.inference.readiness.least_triggered_count is not None
                        else "n/a"
                    ),
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
    lines.extend(["", "## Variant metrics", ""])
    lines.extend(
        markdown_table(
            (
                "Variant",
                "Paired N",
                "Triggered",
                "No entry",
                "Avoided loss",
                "Missed winner",
                "Unresolved",
                "Trade rate",
                "Episode net",
                "Traded net",
                "Win rate",
                "Initial SL",
                "Confirmed wait",
                "Effective wait",
            ),
            [
                (
                    row.variant_key,
                    row.paired_resolved,
                    row.confirmed_entries,
                    row.not_confirmed,
                    row.avoided_losing_entries,
                    row.missed_winning_entries,
                    row.unresolved,
                    format_percentage(row.trade_rate_pct, missing="n/a"),
                    format_percentage(row.mean_episode_net_return_pct, missing="n/a"),
                    format_percentage(
                        row.conditional_trade_net_return_pct,
                        missing="n/a",
                    ),
                    format_percentage(row.conditional_win_rate_pct, missing="n/a"),
                    format_percentage(row.initial_stop_rate_pct, missing="n/a"),
                    format_number(
                        row.mean_confirmed_wait_minutes,
                        suffix="m",
                        missing="n/a",
                    ),
                    format_number(
                        row.mean_effective_wait_minutes,
                        suffix="m",
                        missing="n/a",
                    ),
                )
                for row in report.variant_metrics
            ],
        )
    )
    lines.extend(["", "## Paired baseline comparison", ""])
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
                )
                for row in report.paired_comparisons
            ],
        )
    )
    lines.extend(["", "## Formal cluster inference", ""])
    if report.inference.baseline is None:
        lines.extend(
            [
                (
                    "_Formal intervals are withheld until the locked first 100 "
                    "eligible episodes are complete, fully resolved, and contain "
                    "at least 30 asset clusters._"
                )
            ]
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
                        format_number(
                            row.paired.holm_adjusted_p_value,
                            decimals=4,
                        ),
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
                (
                    row.cluster_key,
                    row.episodes,
                    format_percentage(row.share_pct),
                )
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
                "Variant",
                "Status",
                "Wait",
                "Red",
                "Retrace",
                "Exit",
                "Episode net",
            ),
            [
                (
                    row.pump_event_id,
                    row.base,
                    row.variant_key,
                    row.status,
                    format_number(
                        row.confirmation.wait_minutes,
                        suffix="m",
                        missing="n/a",
                    ),
                    (
                        "yes"
                        if row.confirmation.closed_red is True
                        else "no"
                        if row.confirmation.closed_red is False
                        else "n/a"
                    ),
                    format_percentage(row.confirmation.retrace_pct, missing="n/a"),
                    row.trade.exit_reason if row.trade and row.trade.exit_reason else "no entry",
                    format_percentage(row.episode_net_return_pct, missing="n/a"),
                )
                for row in report.challenger_results
            ],
        )
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay the pre-registered pump-short entry challenger family"
    )
    parser.add_argument(
        "--since",
        type=parse_utc_datetime,
        default=ENTRY_CHALLENGER_COHORT_START,
        help="inclusive UTC cutoff; defaults to the registered challenger cohort",
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
    from .virtual_market import fetch_entry_challenger_paths

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for virtual-entry-challenger-report")
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    generated_at = datetime.now(UTC)
    filters = ReplayFilters(
        since=args.since,
        until=args.until or generated_at,
        strategy_versions=tuple(args.strategy_version or ("pump_short_v1_market_quality",)),
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
    paths = await fetch_entry_challenger_paths(dataset.eligible_episodes, EXCHANGE_FACTORIES)
    report = build_entry_challenger_report(
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
