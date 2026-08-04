"""Read-only paired report for the pre-registered entry-floor family."""

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
    simulate_decision,
)
from .virtual_threshold_challengers import (
    BASELINE_ENTRY_FLOOR_PCT,
    ENTRY_THRESHOLD_COHORT_START,
    ENTRY_THRESHOLD_FAMILY_VERSION,
    ENTRY_THRESHOLD_SELECTION_VERSION,
    ENTRY_THRESHOLD_VARIANTS,
    EntryThresholdVariant,
    ThresholdSelection,
    select_threshold_decision,
    selected_threshold_decisions,
)

ENTRY_THRESHOLD_REPORT_VERSION = "entry_threshold_challenger_report_v1"
ENTRY_THRESHOLD_BASELINE_KEY = "floor_30"
ENTRY_THRESHOLD_STRATEGY_VERSIONS = (
    "pump_short_measurement_v1",
    "pump_short_v1_market_quality",
)
# A threshold that is rarely crossed can reach every other formal-sample gate
# (100 eligible episodes, 30 clusters, full resolution) almost entirely on
# zero_return_cash_episode rows while the actually-traded sample stays tiny —
# see the 2026-08-04 finding where +35/+40/+50% each had exactly one triggered
# trade in the locked 100-episode window. Require a floor of real trades before
# calling any threshold's read formal.
MINIMUM_TRIGGERED_EPISODES = 20


@dataclass(frozen=True)
class ThresholdVariantManifest:
    key: str
    version: str
    min_pump_pct: float


@dataclass(frozen=True)
class EntryThresholdManifest:
    protocol_version: str
    replay_engine_version: str
    replay_query_version: str
    report_version: str
    virtual_strategy_version: str
    selection_model_version: str
    entry_model_version: str
    exit_model_version: str
    cost_model_version: str
    market_path_version: str
    inference_version: str
    challenger_family_version: str
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
    baseline: ThresholdVariantManifest
    variants: tuple[ThresholdVariantManifest, ...]
    taker_fee_bps_per_side: float
    funding_cost_bps_per_8h: float
    bootstrap_iterations: int
    bootstrap_seed: int
    bootstrap_confidence_level: float
    holm_family_alpha: float
    observation_unit: str = "pump_event_id"
    no_trigger_policy: str = "zero_return_cash_episode"
    within_bar_policy: str = "conservative_stop_first"
    report_scope: str = "formal_inference_when_ready_shadow_only"


@dataclass(frozen=True)
class ThresholdEpisodeResult:
    pump_event_id: int
    cluster_key: str
    base: str
    threshold_key: str
    min_pump_pct: float
    status: str
    selected_decision_id: str | None
    selected_at: datetime | None
    selected_pump_pct: float | None
    exchange: str | None
    episode_net_return_pct: float | None
    trade: VirtualTrade | None
    error: str | None = None


@dataclass(frozen=True)
class ThresholdMetrics:
    threshold_key: str
    min_pump_pct: float
    eligible_episodes: int
    resolved_episodes: int
    triggered: int
    no_trigger: int
    completed_trades: int
    unresolved: int
    trade_rate_pct: float | None
    mean_episode_net_return_pct: float | None
    conditional_trade_net_return_pct: float | None
    conditional_win_rate_pct: float | None
    initial_stop_rate_pct: float | None


@dataclass(frozen=True)
class PairedThresholdComparison:
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
class EntryThresholdReport:
    manifest: EntryThresholdManifest
    dataset_episodes: int
    eligible_episodes: int
    excluded_episodes: int
    input_exclusion_reasons: tuple[CountRow, ...]
    threshold_metrics: tuple[ThresholdMetrics, ...]
    paired_comparisons: tuple[PairedThresholdComparison, ...]
    episode_results: tuple[ThresholdEpisodeResult, ...]
    inference: EntryChallengerInference
    market_paths: tuple[DecisionMarketPath, ...]


def _mean(values: list[float]) -> float | None:
    return fmean(values) if values else None


def _count_rows(counter: Counter[str]) -> tuple[CountRow, ...]:
    return tuple(
        CountRow(name, count)
        for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    )


def _manifest_variant(
    key: str,
    version: str,
    min_pump_pct: float,
) -> ThresholdVariantManifest:
    return ThresholdVariantManifest(key, version, min_pump_pct)


def _missing_path(episode: ReplayEpisode, selection: ThresholdSelection) -> MarketPath:
    decision = selection.decision
    return MarketPath(
        pump_event_id=episode.pump_event_id,
        exchange=decision.exchange if decision else "",
        base=decision.base if decision else episode.base,
        status="missing_path",
        candles=(),
        error="market path was not loaded",
    )


def evaluate_threshold(
    episode: ReplayEpisode,
    threshold_key: str,
    min_pump_pct: float,
    path_by_decision: dict[str, MarketPath],
    costs: CostParameters,
) -> ThresholdEpisodeResult:
    """Evaluate one point-in-time floor on one episode with the shared replay engine."""
    selection = select_threshold_decision(episode, min_pump_pct)
    decision = selection.decision
    if selection.status == "not_triggered":
        return ThresholdEpisodeResult(
            pump_event_id=episode.pump_event_id,
            cluster_key=episode.cluster_key,
            base=episode.base,
            threshold_key=threshold_key,
            min_pump_pct=min_pump_pct,
            status="not_triggered",
            selected_decision_id=None,
            selected_at=None,
            selected_pump_pct=None,
            exchange=None,
            episode_net_return_pct=0.0,
            trade=None,
        )
    if selection.status == "unresolved" or decision is None:
        return ThresholdEpisodeResult(
            pump_event_id=episode.pump_event_id,
            cluster_key=episode.cluster_key,
            base=episode.base,
            threshold_key=threshold_key,
            min_pump_pct=min_pump_pct,
            status="selection_unresolved",
            selected_decision_id=None,
            selected_at=None,
            selected_pump_pct=None,
            exchange=None,
            episode_net_return_pct=None,
            trade=None,
            error=selection.error or "threshold selection failed",
        )
    decision_id = decision.decision_id
    path = path_by_decision.get(decision_id or "")
    if path is None:
        path = _missing_path(episode, selection)
    trade = simulate_decision(
        episode,
        path,
        decision,
        selection_reason=f"threshold:{min_pump_pct:g}",
        costs=costs,
    )
    return ThresholdEpisodeResult(
        pump_event_id=episode.pump_event_id,
        cluster_key=episode.cluster_key,
        base=episode.base,
        threshold_key=threshold_key,
        min_pump_pct=min_pump_pct,
        status=trade.status,
        selected_decision_id=decision_id,
        selected_at=decision.ts,
        selected_pump_pct=decision.pump_pct,
        exchange=decision.exchange,
        episode_net_return_pct=trade.net_return_pct,
        trade=trade,
        error=trade.error,
    )


def threshold_metrics(
    threshold_key: str,
    min_pump_pct: float,
    results: tuple[ThresholdEpisodeResult, ...],
) -> ThresholdMetrics:
    """Summarize one threshold across episode-level replay results."""
    selected = tuple(result for result in results if result.threshold_key == threshold_key)
    resolved = [
        result.episode_net_return_pct
        for result in selected
        if result.episode_net_return_pct is not None
    ]
    trades = tuple(
        result.trade
        for result in selected
        if result.trade is not None and result.trade.status == "complete"
    )
    trade_returns = [trade.net_return_pct for trade in trades if trade.net_return_pct is not None]
    return ThresholdMetrics(
        threshold_key=threshold_key,
        min_pump_pct=min_pump_pct,
        eligible_episodes=len(selected),
        resolved_episodes=len(resolved),
        triggered=sum(result.selected_decision_id is not None for result in selected),
        no_trigger=sum(result.status == "not_triggered" for result in selected),
        completed_trades=len(trades),
        unresolved=len(selected) - len(resolved),
        trade_rate_pct=len(trades) / len(resolved) * 100 if resolved else None,
        mean_episode_net_return_pct=_mean(resolved),
        conditional_trade_net_return_pct=_mean(trade_returns),
        conditional_win_rate_pct=(
            sum(value > 0 for value in trade_returns) / len(trade_returns) * 100
            if trade_returns
            else None
        ),
        initial_stop_rate_pct=(
            sum(trade.exit_reason == "initial_sl" for trade in trades) / len(trades) * 100
            if trades
            else None
        ),
    )


def _paired_comparison(
    variant: EntryThresholdVariant,
    baseline_by_event: dict[int, ThresholdEpisodeResult],
    variant_by_event: dict[int, ThresholdEpisodeResult],
) -> PairedThresholdComparison:
    normalized: list[tuple[float, float]] = []
    for event_id, baseline in baseline_by_event.items():
        challenger = variant_by_event.get(event_id)
        baseline_return = baseline.episode_net_return_pct
        challenger_return = challenger.episode_net_return_pct if challenger is not None else None
        if baseline_return is None or challenger_return is None:
            continue
        normalized.append((baseline_return, challenger_return))
    deltas = [challenger - baseline for baseline, challenger in normalized]
    return PairedThresholdComparison(
        variant_key=variant.key,
        episodes=len(normalized),
        mean_baseline_net_return_pct=_mean([value[0] for value in normalized]),
        mean_challenger_net_return_pct=_mean([value[1] for value in normalized]),
        mean_delta_pct=_mean(deltas),
        improved_episodes=sum(delta > 1e-12 for delta in deltas),
        worsened_episodes=sum(delta < -1e-12 for delta in deltas),
        unchanged_episodes=sum(abs(delta) <= 1e-12 for delta in deltas),
    )


def build_entry_threshold_report(
    dataset: ReplayDataset,
    filters: ReplayFilters,
    paths: tuple[DecisionMarketPath, ...],
    *,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
    costs: CostParameters = DEFAULT_COSTS,
) -> EntryThresholdReport:
    revision = normalize_code_revision(code_revision)
    if filters.since != ENTRY_THRESHOLD_COHORT_START:
        raise ValueError("formal threshold report requires the registered cohort start")
    if filters.strategy_versions != ENTRY_THRESHOLD_STRATEGY_VERSIONS:
        raise ValueError("formal threshold report requires the registered strategy cohorts")
    path_counts = Counter(path.decision_id for path in paths)
    duplicates = sorted(key for key, count in path_counts.items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate market paths for decisions: {duplicates}")
    path_by_decision = {item.decision_id: item.path for item in paths}
    threshold_specs = (
        (
            ENTRY_THRESHOLD_BASELINE_KEY,
            "entry_floor_30_baseline_v1",
            BASELINE_ENTRY_FLOOR_PCT,
        ),
        *(
            (variant.key, variant.version, variant.min_pump_pct)
            for variant in ENTRY_THRESHOLD_VARIANTS
        ),
    )
    results = tuple(
        evaluate_threshold(episode, key, floor, path_by_decision, costs)
        for episode in dataset.eligible_episodes
        for key, _, floor in threshold_specs
    )
    by_key_event = {(result.threshold_key, result.pump_event_id): result for result in results}
    baseline_by_event = {
        episode.pump_event_id: by_key_event[(ENTRY_THRESHOLD_BASELINE_KEY, episode.pump_event_id)]
        for episode in dataset.eligible_episodes
    }
    inference = build_entry_challenger_inference(
        tuple(
            InferenceEpisode(
                pump_event_id=episode.pump_event_id,
                cluster_key=episode.cluster_key,
                baseline_return_pct=baseline_by_event[episode.pump_event_id].episode_net_return_pct,
                baseline_triggered=(
                    baseline_by_event[episode.pump_event_id].selected_decision_id is not None
                ),
                challenger_returns_pct=tuple(
                    (
                        variant.key,
                        by_key_event[(variant.key, episode.pump_event_id)].episode_net_return_pct,
                    )
                    for variant in ENTRY_THRESHOLD_VARIANTS
                ),
                challenger_triggered=tuple(
                    (
                        variant.key,
                        by_key_event[(variant.key, episode.pump_event_id)].selected_decision_id
                        is not None,
                    )
                    for variant in ENTRY_THRESHOLD_VARIANTS
                ),
            )
            for episode in dataset.eligible_episodes
        ),
        tuple(variant.key for variant in ENTRY_THRESHOLD_VARIANTS),
        minimum_triggered_episodes=MINIMUM_TRIGGERED_EPISODES,
    )
    exclusions = Counter(
        reason for episode in dataset.excluded_episodes for reason in episode.exclusion_reasons
    )
    return EntryThresholdReport(
        manifest=EntryThresholdManifest(
            protocol_version=PROTOCOL_VERSION,
            replay_engine_version=FOUNDATION_VERSION,
            replay_query_version=QUERY_VERSION,
            report_version=ENTRY_THRESHOLD_REPORT_VERSION,
            virtual_strategy_version=VIRTUAL_STRATEGY_VERSION,
            selection_model_version=ENTRY_THRESHOLD_SELECTION_VERSION,
            entry_model_version=ENTRY_MODEL_VERSION,
            exit_model_version=EXIT_MODEL_VERSION,
            cost_model_version=COST_MODEL_VERSION,
            market_path_version=DECISION_MARKET_PATH_VERSION,
            inference_version=ENTRY_INFERENCE_VERSION,
            challenger_family_version=ENTRY_THRESHOLD_FAMILY_VERSION,
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
            baseline=_manifest_variant(
                ENTRY_THRESHOLD_BASELINE_KEY,
                "entry_floor_30_baseline_v1",
                BASELINE_ENTRY_FLOOR_PCT,
            ),
            variants=tuple(
                _manifest_variant(variant.key, variant.version, variant.min_pump_pct)
                for variant in ENTRY_THRESHOLD_VARIANTS
            ),
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
        threshold_metrics=tuple(
            threshold_metrics(key, floor, results) for key, _, floor in threshold_specs
        ),
        paired_comparisons=tuple(
            _paired_comparison(
                variant,
                baseline_by_event,
                {
                    episode.pump_event_id: by_key_event[(variant.key, episode.pump_event_id)]
                    for episode in dataset.eligible_episodes
                },
            )
            for variant in ENTRY_THRESHOLD_VARIANTS
        ),
        episode_results=results,
        inference=inference,
        market_paths=paths,
    )


def render_json(report: EntryThresholdReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True)


def render_markdown(report: EntryThresholdReport) -> str:
    manifest = report.manifest
    lines = [
        "# Pump Short Entry Threshold Challenger Replay",
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
            "This report never changes production entry settings or authorizes real trading."
        ),
        "",
        "## Registered family",
        "",
    ]
    lines.extend(
        markdown_table(
            ("Role", "Variant", "Version", "Entry floor"),
            [
                (
                    "baseline",
                    manifest.baseline.key,
                    manifest.baseline.version,
                    format_percentage(manifest.baseline.min_pump_pct),
                ),
                *[
                    (
                        "challenger",
                        variant.key,
                        variant.version,
                        format_percentage(variant.min_pump_pct),
                    )
                    for variant in manifest.variants
                ],
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
    lines.extend(["", "## Threshold metrics", ""])
    lines.extend(
        markdown_table(
            (
                "Threshold",
                "Floor",
                "Resolved",
                "Triggered",
                "Cash",
                "Unresolved",
                "Trade rate",
                "Episode net",
                "Traded net",
                "Win rate",
                "Initial SL",
            ),
            [
                (
                    row.threshold_key,
                    format_percentage(row.min_pump_pct),
                    row.resolved_episodes,
                    row.triggered,
                    row.no_trigger,
                    row.unresolved,
                    format_percentage(row.trade_rate_pct, missing="n/a"),
                    format_percentage(row.mean_episode_net_return_pct, missing="n/a"),
                    format_percentage(
                        row.conditional_trade_net_return_pct,
                        missing="n/a",
                    ),
                    format_percentage(row.conditional_win_rate_pct, missing="n/a"),
                    format_percentage(row.initial_stop_rate_pct, missing="n/a"),
                )
                for row in report.threshold_metrics
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
        lines.append(
            "_Formal intervals are withheld until the locked first 100 eligible episodes "
            "are fully paired and contain at least 30 asset clusters._"
        )
    else:
        lines.extend(
            markdown_table(
                (
                    "Variant",
                    "Own mean",
                    "Own 95% lower",
                    "Own 95% upper",
                    "Paired delta",
                    "Familywise lower",
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
                        format_number(row.paired.holm_adjusted_p_value, decimals=4),
                        format_percentage(row.strategy.minimum_leave_one_cluster_out_pct),
                        row.verdict,
                    )
                    for row in report.inference.challengers
                ],
            )
        )
    lines.extend(["", "## Episode results", ""])
    lines.extend(
        markdown_table(
            (
                "Episode",
                "Base",
                "Threshold",
                "Status",
                "Selected pump",
                "Exchange",
                "Exit",
                "Episode net",
                "Error",
            ),
            [
                (
                    row.pump_event_id,
                    row.base,
                    row.threshold_key,
                    row.status,
                    format_percentage(row.selected_pump_pct, missing="n/a"),
                    row.exchange or "cash",
                    row.trade.exit_reason if row.trade and row.trade.exit_reason else "no entry",
                    format_percentage(row.episode_net_return_pct, missing="n/a"),
                    row.error or "",
                )
                for row in report.episode_results
            ],
        )
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay the pre-registered pump-short entry-floor family"
    )
    parser.add_argument(
        "--since",
        type=parse_utc_datetime,
        default=ENTRY_THRESHOLD_COHORT_START,
        help="inclusive UTC cutoff; fixed to the registered threshold cohort",
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
    from .virtual_market import fetch_decision_market_paths

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for virtual-threshold-challenger-report")
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    generated_at = datetime.now(UTC)
    filters = ReplayFilters(
        since=args.since,
        until=args.until or generated_at,
        strategy_versions=tuple(args.strategy_version or ENTRY_THRESHOLD_STRATEGY_VERSIONS),
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
    selected = selected_threshold_decisions(dataset.eligible_episodes)
    paths = await fetch_decision_market_paths(selected, EXCHANGE_FACTORIES)
    report = build_entry_threshold_report(
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
