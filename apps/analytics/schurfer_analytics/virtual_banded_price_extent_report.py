"""Read-only formal report for the pre-registered banded price-extent hypothesis.

Registered 2026-08-05 (see decision_quality.py's BANDED_PRICE_EXTENT_* constants):
informal reads across the entry-floor and decision-quality reports both showed a
worse short win rate at both smaller (20-25%) and much larger (35%+) pre-entry
pump magnitude than at the 30% baseline floor. The live price_extent score
component instead grants its maximum points to the LARGEST move.

This report tests that hypothesis as a genuine forward-only challenger: baseline
score_6 (live formula) vs score_6_with_banded_price_extent (same threshold, one
component recomputed from its own already-recorded raw value). The cohort start
is locked to the day AFTER the hypothesis was registered — the window already
inspected to invent the bands must never be reused to "confirm" them, or this
becomes exactly the p-hacking the project's own inference discipline exists to
prevent. Unlike decision_quality_report.py's descriptive score_6_without_*
ablations (which intentionally run over the full discovery history and never
claim to be evidence), this report locks a cohort and produces a formal bootstrap
verdict, the same treatment as every other registered challenger family.
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

from .challenger_inference import (
    DEFAULT_INFERENCE_SETTINGS,
    ChallengerEpisode,
    ChallengerInference,
    build_challenger_inference,
)
from .decision_quality import (
    BANDED_PRICE_EXTENT_CHALLENGER_POLICY,
    BANDED_PRICE_EXTENT_MODERATE_HIGH_PCT,
    BANDED_PRICE_EXTENT_MODERATE_LOW_PCT,
    BANDED_PRICE_EXTENT_MODERATE_POINTS,
    BANDED_PRICE_EXTENT_SWEET_SPOT_HIGH_PCT,
    BANDED_PRICE_EXTENT_SWEET_SPOT_LOW_PCT,
    BANDED_PRICE_EXTENT_SWEET_SPOT_POINTS,
    SCORE_THRESHOLD_BASELINE_POLICY,
    SCORE_THRESHOLD_FAMILY_VERSION,
    ScorePolicy,
    ScoreSelection,
    select_score_policy,
    selected_policy_decisions,
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
    MarketPath,
    VirtualTrade,
    max_sequential_drawdown_usd,
    simulate_decision,
)

BANDED_PRICE_EXTENT_REPORT_VERSION = "virtual_banded_price_extent_report_v1"
BANDED_PRICE_EXTENT_INFERENCE_VERSION = "banded_price_extent_formal_inference_v1"
# Locked to the day after the hypothesis was registered (2026-08-05) — never the
# window that was inspected to invent it. See the module docstring.
BANDED_PRICE_EXTENT_COHORT_START = datetime(2026, 8, 6, tzinfo=UTC)
BANDED_PRICE_EXTENT_STRATEGY_VERSIONS = ("pump_short_v1_market_quality",)
BANDED_PRICE_EXTENT_POLICIES = (
    SCORE_THRESHOLD_BASELINE_POLICY,
    BANDED_PRICE_EXTENT_CHALLENGER_POLICY,
)


@dataclass(frozen=True)
class ScorePolicySpec:
    key: str
    min_score: int


@dataclass(frozen=True)
class BandedPriceExtentManifest:
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
    baseline: ScorePolicySpec
    challenger: ScorePolicySpec
    sweet_spot_low_pct: float
    sweet_spot_high_pct: float
    sweet_spot_points: int
    moderate_low_pct: float
    moderate_high_pct: float
    moderate_points: int
    taker_fee_bps_per_side: float
    funding_cost_bps_per_8h: float
    bootstrap_iterations: int
    bootstrap_seed: int
    bootstrap_confidence_level: float
    holm_family_alpha: float
    observation_unit: str = "pump_event_id"
    selection_policy: str = "first_recorded_gate_eligible_score_crossing_v1"
    no_trigger_policy: str = "zero_return_cash_when_never_triggered"
    within_bar_policy: str = "conservative_stop_first"
    formal_sample_policy: str = "first_100_eligible_episodes_chronological"
    report_scope: str = "formal_inference_when_ready_shadow_only"


@dataclass(frozen=True)
class BandedPriceExtentResult:
    pump_event_id: int
    cluster_key: str
    base: str
    policy_key: str
    min_score: int
    status: str
    selected_decision_id: str | None
    selected_at: datetime | None
    selected_score: int | None
    exchange: str | None
    episode_net_return_pct: float | None
    trade: VirtualTrade | None
    error: str | None = None


@dataclass(frozen=True)
class BandedPriceExtentMetrics:
    policy_key: str
    min_score: int
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
    initial_stop_rate_pct: float | None
    mean_mfe_pct: float | None
    mean_mae_pct: float | None
    mean_captured_move_pct: float | None
    max_sequential_drawdown_usd: float | None


@dataclass(frozen=True)
class PairedBandedComparison:
    variant_key: str
    episodes: int
    mean_baseline_net_return_pct: float | None
    mean_challenger_net_return_pct: float | None
    mean_delta_pct: float | None
    improved_episodes: int
    worsened_episodes: int
    unchanged_episodes: int
    different_decision_episodes: int


@dataclass(frozen=True)
class CountRow:
    name: str
    count: int


@dataclass(frozen=True)
class BandedPriceExtentReport:
    manifest: BandedPriceExtentManifest
    dataset_episodes: int
    eligible_episodes: int
    excluded_episodes: int
    input_exclusion_reasons: tuple[CountRow, ...]
    path_statuses: tuple[CountRow, ...]
    policy_metrics: tuple[BandedPriceExtentMetrics, ...]
    paired_comparisons: tuple[PairedBandedComparison, ...]
    episode_results: tuple[BandedPriceExtentResult, ...]
    inference: ChallengerInference
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
) -> BandedPriceExtentResult:
    selection = select_score_policy(episode, policy)
    decision = selection.decision
    if selection.status == "not_triggered":
        return BandedPriceExtentResult(
            episode.pump_event_id,
            episode.cluster_key,
            episode.base,
            policy.key,
            policy.min_score,
            "not_triggered",
            None,
            None,
            None,
            None,
            0.0,
            None,
        )
    if selection.status == "unresolved" or decision is None:
        return BandedPriceExtentResult(
            episode.pump_event_id,
            episode.cluster_key,
            episode.base,
            policy.key,
            policy.min_score,
            "selection_unresolved",
            None,
            None,
            None,
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
        selection_reason=f"banded_price_extent:{policy.key}",
        costs=costs,
    )
    return BandedPriceExtentResult(
        episode.pump_event_id,
        episode.cluster_key,
        episode.base,
        policy.key,
        policy.min_score,
        trade.status,
        decision.decision_id,
        decision.ts,
        decision.score,
        decision.exchange,
        trade.net_return_pct,
        trade,
        trade.error,
    )


def _metrics(
    policy: ScorePolicy,
    results: tuple[BandedPriceExtentResult, ...],
) -> BandedPriceExtentMetrics:
    selected = tuple(result for result in results if result.policy_key == policy.key)
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
    pnl = [trade.net_pnl_usd for trade in trades if trade.net_pnl_usd is not None]
    return BandedPriceExtentMetrics(
        policy_key=policy.key,
        min_score=policy.min_score,
        eligible_episodes=len(selected),
        resolved_episodes=len(resolved),
        triggered=sum(result.selected_decision_id is not None for result in selected),
        cash=sum(result.status == "not_triggered" for result in selected),
        unresolved=len(selected) - len(resolved),
        trade_rate_pct=len(trades) / len(resolved) * 100 if resolved else None,
        mean_episode_net_return_pct=_mean(resolved),
        conditional_trade_net_return_pct=_mean(trade_returns),
        total_net_pnl_usd=sum(pnl) if pnl else None,
        profit_factor=profit_factor(trade_returns),
        win_rate_pct=(
            sum(value > 0 for value in trade_returns) / len(trade_returns) * 100
            if trade_returns
            else None
        ),
        initial_stop_rate_pct=(
            sum(trade.exit_reason == "initial_sl" for trade in trades) / len(trades) * 100
            if trades
            else None
        ),
        mean_mfe_pct=_mean([trade.mfe_pct for trade in trades if trade.mfe_pct is not None]),
        mean_mae_pct=_mean([trade.mae_pct for trade in trades if trade.mae_pct is not None]),
        mean_captured_move_pct=_mean(
            [trade.captured_move_pct for trade in trades if trade.captured_move_pct is not None]
        ),
        max_sequential_drawdown_usd=max_sequential_drawdown_usd(trades),
    )


def _paired_comparison(
    policy: ScorePolicy,
    baseline_by_event: dict[int, BandedPriceExtentResult],
    variant_by_event: dict[int, BandedPriceExtentResult],
) -> PairedBandedComparison:
    pairs: list[tuple[BandedPriceExtentResult, BandedPriceExtentResult, float, float]] = []
    for event_id, baseline in baseline_by_event.items():
        challenger = variant_by_event.get(event_id)
        if (
            challenger is None
            or baseline.episode_net_return_pct is None
            or challenger.episode_net_return_pct is None
        ):
            continue
        pairs.append(
            (
                baseline,
                challenger,
                baseline.episode_net_return_pct,
                challenger.episode_net_return_pct,
            )
        )
    deltas = [
        challenger_return - baseline_return for _, _, baseline_return, challenger_return in pairs
    ]
    return PairedBandedComparison(
        variant_key=policy.key,
        episodes=len(pairs),
        mean_baseline_net_return_pct=_mean([baseline_return for _, _, baseline_return, _ in pairs]),
        mean_challenger_net_return_pct=_mean(
            [challenger_return for _, _, _, challenger_return in pairs]
        ),
        mean_delta_pct=_mean(deltas),
        improved_episodes=sum(delta > 1e-12 for delta in deltas),
        worsened_episodes=sum(delta < -1e-12 for delta in deltas),
        unchanged_episodes=sum(abs(delta) <= 1e-12 for delta in deltas),
        different_decision_episodes=sum(
            baseline.selected_decision_id != challenger.selected_decision_id
            for baseline, challenger, _, _ in pairs
        ),
    )


def _resolved_return(result: BandedPriceExtentResult) -> float | None:
    return result.episode_net_return_pct


def build_banded_price_extent_report(
    dataset: ReplayDataset,
    filters: ReplayFilters,
    paths: tuple[DecisionMarketPath, ...],
    *,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
    costs: CostParameters = DEFAULT_COSTS,
) -> BandedPriceExtentReport:
    revision = normalize_code_revision(code_revision)
    if filters.since != BANDED_PRICE_EXTENT_COHORT_START:
        raise ValueError(
            "formal banded-price-extent report requires the registered forward cohort start"
        )
    if filters.strategy_versions != BANDED_PRICE_EXTENT_STRATEGY_VERSIONS:
        raise ValueError(
            "formal banded-price-extent report requires the registered strategy cohort"
        )
    path_counts = Counter(path.decision_id for path in paths)
    duplicates = sorted(decision_id for decision_id, count in path_counts.items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate market paths for decisions: {duplicates}")
    path_by_decision = {item.decision_id: item.path for item in paths}
    results = tuple(
        _evaluate_policy(episode, policy, path_by_decision, costs)
        for episode in dataset.eligible_episodes
        for policy in BANDED_PRICE_EXTENT_POLICIES
    )
    by_policy_event = {(result.policy_key, result.pump_event_id): result for result in results}
    baseline_by_event = {
        episode.pump_event_id: by_policy_event[
            (SCORE_THRESHOLD_BASELINE_POLICY.key, episode.pump_event_id)
        ]
        for episode in dataset.eligible_episodes
    }
    challenger = BANDED_PRICE_EXTENT_CHALLENGER_POLICY
    inference = build_challenger_inference(
        tuple(
            ChallengerEpisode(
                pump_event_id=episode.pump_event_id,
                cluster_key=episode.cluster_key,
                baseline_return_pct=_resolved_return(baseline_by_event[episode.pump_event_id]),
                challenger_returns_pct=(
                    (
                        challenger.key,
                        _resolved_return(by_policy_event[(challenger.key, episode.pump_event_id)]),
                    ),
                ),
            )
            for episode in dataset.eligible_episodes
        ),
        (challenger.key,),
        inference_version=BANDED_PRICE_EXTENT_INFERENCE_VERSION,
    )
    exclusions = Counter(
        reason for episode in dataset.excluded_episodes for reason in episode.exclusion_reasons
    )
    return BandedPriceExtentReport(
        manifest=BandedPriceExtentManifest(
            protocol_version=PROTOCOL_VERSION,
            replay_engine_version=FOUNDATION_VERSION,
            replay_query_version=QUERY_VERSION,
            report_version=BANDED_PRICE_EXTENT_REPORT_VERSION,
            virtual_strategy_version=VIRTUAL_STRATEGY_VERSION,
            selection_model_version=SCORE_THRESHOLD_FAMILY_VERSION,
            entry_model_version=ENTRY_MODEL_VERSION,
            exit_model_version=EXIT_MODEL_VERSION,
            cost_model_version=COST_MODEL_VERSION,
            market_path_version=DECISION_MARKET_PATH_VERSION,
            inference_version=BANDED_PRICE_EXTENT_INFERENCE_VERSION,
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
            baseline=ScorePolicySpec(
                SCORE_THRESHOLD_BASELINE_POLICY.key,
                SCORE_THRESHOLD_BASELINE_POLICY.min_score,
            ),
            challenger=ScorePolicySpec(challenger.key, challenger.min_score),
            sweet_spot_low_pct=BANDED_PRICE_EXTENT_SWEET_SPOT_LOW_PCT,
            sweet_spot_high_pct=BANDED_PRICE_EXTENT_SWEET_SPOT_HIGH_PCT,
            sweet_spot_points=BANDED_PRICE_EXTENT_SWEET_SPOT_POINTS,
            moderate_low_pct=BANDED_PRICE_EXTENT_MODERATE_LOW_PCT,
            moderate_high_pct=BANDED_PRICE_EXTENT_MODERATE_HIGH_PCT,
            moderate_points=BANDED_PRICE_EXTENT_MODERATE_POINTS,
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
        path_statuses=_count_rows(Counter(item.path.status for item in paths)),
        policy_metrics=tuple(_metrics(policy, results) for policy in BANDED_PRICE_EXTENT_POLICIES),
        paired_comparisons=(
            _paired_comparison(
                challenger,
                baseline_by_event,
                {
                    episode.pump_event_id: by_policy_event[(challenger.key, episode.pump_event_id)]
                    for episode in dataset.eligible_episodes
                },
            ),
        ),
        episode_results=results,
        inference=inference,
        market_paths=paths,
    )


def render_json(report: BandedPriceExtentReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True, allow_nan=False)


def render_markdown(report: BandedPriceExtentReport) -> str:
    manifest = report.manifest
    lines = [
        "# Pump Short Banded Price-Extent Challenger Replay",
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
            "This report never changes production score settings or authorizes real trading."
        ),
        "",
        "## Registered hypothesis",
        "",
    ]
    lines.extend(
        markdown_table(
            ("Role", "Policy", "Minimum score"),
            [
                ("baseline", manifest.baseline.key, manifest.baseline.min_score),
                ("challenger", manifest.challenger.key, manifest.challenger.min_score),
            ],
        )
    )
    lines.extend(["", "## Registered bands", ""])
    lines.extend(
        markdown_table(
            ("Band", "Range (peak_pct)", "Points"),
            [
                (
                    "sweet spot",
                    f"[{manifest.sweet_spot_low_pct:.0f}, {manifest.sweet_spot_high_pct:.0f})",
                    manifest.sweet_spot_points,
                ),
                (
                    "moderate",
                    (
                        f"[{manifest.moderate_low_pct:.0f}, {manifest.sweet_spot_low_pct:.0f}) or "
                        f"[{manifest.sweet_spot_high_pct:.0f}, {manifest.moderate_high_pct:.0f})"
                    ),
                    manifest.moderate_points,
                ),
                (
                    "floor",
                    f"<{manifest.moderate_low_pct:.0f} or >={manifest.moderate_high_pct:.0f}",
                    0,
                ),
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
                ("Holm family alpha", format_number(manifest.holm_family_alpha, decimals=4)),
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
    lines.extend(["", "## Market paths", ""])
    lines.extend(
        markdown_table(
            ("Status", "Paths"),
            [(row.name, row.count) for row in report.path_statuses],
        )
    )
    lines.extend(["", "## Score policy metrics", ""])
    lines.extend(
        markdown_table(
            (
                "Policy",
                "Score",
                "Resolved",
                "Triggered",
                "Cash",
                "Unresolved",
                "Trade rate",
                "Episode net",
                "Traded net",
                "Total P&L",
                "Profit factor",
                "Win rate",
                "Initial SL",
                "MFE",
                "MAE",
                "Captured MFE",
                "Max drawdown",
            ),
            [
                (
                    row.policy_key,
                    row.min_score,
                    row.resolved_episodes,
                    row.triggered,
                    row.cash,
                    row.unresolved,
                    format_percentage(row.trade_rate_pct, missing="n/a"),
                    format_percentage(row.mean_episode_net_return_pct, missing="n/a"),
                    format_percentage(
                        row.conditional_trade_net_return_pct,
                        missing="n/a",
                    ),
                    format_number(row.total_net_pnl_usd, suffix=" USD", missing="n/a"),
                    format_number(row.profit_factor, missing="n/a"),
                    format_percentage(row.win_rate_pct, missing="n/a"),
                    format_percentage(row.initial_stop_rate_pct, missing="n/a"),
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
                "Different decision",
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
                    row.different_decision_episodes,
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
        lines.extend(["", "### Registered challenger", ""])
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
                "Selected score",
                "Exchange",
                "Exit",
                "Episode net",
                "Error",
            ),
            [
                (
                    row.pump_event_id,
                    row.base,
                    row.policy_key,
                    row.status,
                    row.selected_score if row.selected_score is not None else "n/a",
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
        description="Replay the pre-registered pump-short banded price-extent challenger"
    )
    parser.add_argument(
        "--since",
        type=parse_utc_datetime,
        default=BANDED_PRICE_EXTENT_COHORT_START,
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
        cohort_start=BANDED_PRICE_EXTENT_COHORT_START,
        report_label="banded-price-extent",
    )
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for virtual-banded-price-extent-report")
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    filters = ReplayFilters(
        since=args.since,
        until=until,
        strategy_versions=tuple(args.strategy_version or BANDED_PRICE_EXTENT_STRATEGY_VERSIONS),
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
        BANDED_PRICE_EXTENT_POLICIES,
    )
    paths = await fetch_decision_market_paths(selected, EXCHANGE_FACTORIES)
    report = build_banded_price_extent_report(
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
    sys.stdout.write(output)
