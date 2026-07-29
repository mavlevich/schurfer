"""Discovery report for the strict maker-entry OHLCV upper bound."""

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

from .decision_quality import (
    MARKET_QUALITY_CONTROL_POLICY,
    select_score_policy,
    selected_policy_decisions,
)
from .episode_replay import PROTOCOL_VERSION
from .maker_entry import (
    MAKER_COST_MODEL_VERSION,
    MAKER_ENTRY_FEE_BPS,
    MAKER_ENTRY_MODEL_VERSION,
    MAKER_FILL_EVIDENCE_VERSION,
    MAKER_SELECTION_VERSION,
    MakerEntryResult,
    evaluate_maker_entry,
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
from .virtual_market import (
    MAKER_FILL_TIMEOUT_MINUTES,
    MAKER_MARKET_PATH_VERSION,
    MakerDecisionPaths,
    maker_market_path_fingerprint,
)
from .virtual_strategy import (
    COST_MODEL_VERSION,
    DEFAULT_COSTS,
    EXIT_MODEL_VERSION,
    VIRTUAL_STRATEGY_VERSION,
    CostParameters,
    max_sequential_drawdown_usd,
)

MAKER_ENTRY_REPORT_VERSION = "maker_entry_upper_bound_report_v1"
MAKER_ENTRY_COHORT_START = datetime(2026, 7, 22, tzinfo=UTC)
MAKER_ENTRY_STRATEGY_VERSIONS = ("pump_short_v1_market_quality",)


@dataclass(frozen=True)
class CountRow:
    name: str
    count: int


@dataclass(frozen=True)
class MakerEntryManifest:
    protocol_version: str
    replay_engine_version: str
    replay_query_version: str
    report_version: str
    selection_version: str
    maker_entry_model_version: str
    fill_evidence_version: str
    virtual_strategy_version: str
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
    fill_timeout_minutes: int
    maker_entry_fee_bps: float
    taker_exit_fee_bps: float
    funding_cost_bps_per_8h: float
    order_activation: str = "first_bar_strictly_after_decision"
    fill_semantics: str = "potential_fill_not_queue_or_partial_fill_proof"
    post_only_acceptance: str = "unobservable_from_ohlcv"
    fill_bar_exit_semantics: str = "exposure_starts_on_bar_after_potential_fill"
    comparison_granularity: str = "maker_1m_primary_or_5m_fallback_vs_5m_taker_baseline"
    comparison_interpretation: str = "not_an_entry_only_causal_delta"
    unfilled_policy: str = "zero_return_cash"
    scope: str = "discovery_upper_bound_only"


@dataclass(frozen=True)
class MakerEntryMetrics:
    selected: int
    known_fill_opportunities: int
    potential_fills: int
    cash_unfilled: int
    unresolved: int
    primary_1m: int
    fallback_5m: int
    touched_only: int
    post_only_reject_risk: int
    fill_rate_pct: float | None
    mean_episode_net_return_pct: float | None
    mean_filled_trade_net_return_pct: float | None
    baseline_matched_mean_net_return_pct: float | None
    matched_mean_delta_pct: float | None
    win_rate_pct: float | None
    profit_factor: float | None
    initial_stop_rate_pct: float | None
    adverse_selection_stop_30m_rate_pct: float | None
    missed_baseline_winners: int
    missed_baseline_winner_rate_pct: float | None
    mean_fee_cost_bps: float | None
    mean_slippage_cost_bps: float | None
    total_net_pnl_usd: float | None
    max_sequential_drawdown_usd: float | None


@dataclass(frozen=True)
class TimeframeMetrics:
    timeframe: str
    episodes: int
    potential_fills: int
    fill_rate_pct: float | None
    mean_episode_net_return_pct: float | None
    mean_filled_trade_net_return_pct: float | None
    baseline_matched_mean_net_return_pct: float | None
    matched_mean_delta_pct: float | None
    adverse_selection_stop_30m_rate_pct: float | None
    missed_baseline_winners: int


@dataclass(frozen=True)
class MakerEntryReport:
    manifest: MakerEntryManifest
    dataset_episodes: int
    eligible_episodes: int
    excluded_episodes: int
    input_exclusions: tuple[CountRow, ...]
    path_statuses: tuple[CountRow, ...]
    result_statuses: tuple[CountRow, ...]
    metrics: MakerEntryMetrics
    timeframe_metrics: tuple[TimeframeMetrics, ...]
    results: tuple[MakerEntryResult, ...]
    market_paths: tuple[MakerDecisionPaths, ...]


def _count_rows(counter: Counter[str]) -> tuple[CountRow, ...]:
    return tuple(
        CountRow(name, count)
        for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    )


def _mean(values: list[float]) -> float | None:
    return fmean(values) if values else None


def _metrics(results: tuple[MakerEntryResult, ...]) -> MakerEntryMetrics:
    selected = [result for result in results if result.selected_decision_id is not None]
    known_economics = [
        result for result in selected if result.status in {"complete", "cash_unfilled"}
    ]
    known_fills = [
        result
        for result in selected
        if result.path_timeframe is not None
        and (result.fill_bar_at_ms is not None or result.status == "cash_unfilled")
    ]
    filled = [result for result in known_economics if result.status == "complete"]
    trades = [
        result.maker_trade
        for result in filled
        if result.maker_trade is not None
        and result.maker_trade.status == "complete"
        and result.maker_trade.net_return_pct is not None
    ]
    episode_returns = [
        result.episode_net_return_pct
        for result in known_economics
        if result.episode_net_return_pct is not None
    ]
    trade_returns = [trade.net_return_pct for trade in trades if trade.net_return_pct is not None]
    matched = [
        result
        for result in known_economics
        if result.baseline_trade is not None
        and result.baseline_trade.status == "complete"
        and result.baseline_trade.net_return_pct is not None
        and result.episode_net_return_pct is not None
    ]
    baseline_returns = [
        result.baseline_trade.net_return_pct
        for result in matched
        if result.baseline_trade is not None and result.baseline_trade.net_return_pct is not None
    ]
    deltas = [
        (result.episode_net_return_pct or 0.0)
        - (
            result.baseline_trade.net_return_pct
            if result.baseline_trade is not None
            and result.baseline_trade.net_return_pct is not None
            else 0.0
        )
        for result in matched
    ]
    missed_winners = sum(result.missed_baseline_winner for result in known_economics)
    unfilled = sum(result.status == "cash_unfilled" for result in known_economics)
    return MakerEntryMetrics(
        selected=len(selected),
        known_fill_opportunities=len(known_fills),
        potential_fills=sum(result.fill_bar_at_ms is not None for result in known_fills),
        cash_unfilled=unfilled,
        unresolved=len(selected) - len(known_economics),
        primary_1m=sum(result.path_timeframe == "1m_primary" for result in known_fills),
        fallback_5m=sum(result.path_timeframe == "5m_fallback" for result in known_fills),
        touched_only=sum(result.fill_evidence == "touched_only" for result in known_fills),
        post_only_reject_risk=sum(
            result.fill_evidence == "marketable_at_bar_open" for result in known_fills
        ),
        fill_rate_pct=(
            sum(result.fill_bar_at_ms is not None for result in known_fills)
            / len(known_fills)
            * 100
            if known_fills
            else None
        ),
        mean_episode_net_return_pct=_mean(episode_returns),
        mean_filled_trade_net_return_pct=_mean(trade_returns),
        baseline_matched_mean_net_return_pct=_mean(baseline_returns),
        matched_mean_delta_pct=_mean(deltas),
        win_rate_pct=(
            sum(value > 0 for value in trade_returns) / len(trade_returns) * 100
            if trade_returns
            else None
        ),
        profit_factor=profit_factor(trade_returns),
        initial_stop_rate_pct=(
            sum(trade.exit_reason == "initial_sl" for trade in trades) / len(trades) * 100
            if trades
            else None
        ),
        adverse_selection_stop_30m_rate_pct=(
            sum(result.adverse_selection_stop_30m for result in filled) / len(filled) * 100
            if filled
            else None
        ),
        missed_baseline_winners=missed_winners,
        missed_baseline_winner_rate_pct=(missed_winners / unfilled * 100 if unfilled else None),
        mean_fee_cost_bps=_mean(
            [trade.fee_cost_bps for trade in trades if trade.fee_cost_bps is not None]
        ),
        mean_slippage_cost_bps=_mean(
            [trade.slippage_cost_bps for trade in trades if trade.slippage_cost_bps is not None]
        ),
        total_net_pnl_usd=sum(
            trade.net_pnl_usd for trade in trades if trade.net_pnl_usd is not None
        )
        if trades
        else None,
        max_sequential_drawdown_usd=max_sequential_drawdown_usd(trades),
    )


def _timeframe_metrics(
    results: tuple[MakerEntryResult, ...],
) -> tuple[TimeframeMetrics, ...]:
    rows: list[TimeframeMetrics] = []
    for timeframe in ("1m_primary", "5m_fallback"):
        selected = tuple(result for result in results if result.path_timeframe == timeframe)
        metrics = _metrics(selected)
        rows.append(
            TimeframeMetrics(
                timeframe=timeframe,
                episodes=metrics.known_fill_opportunities,
                potential_fills=metrics.potential_fills,
                fill_rate_pct=metrics.fill_rate_pct,
                mean_episode_net_return_pct=metrics.mean_episode_net_return_pct,
                mean_filled_trade_net_return_pct=metrics.mean_filled_trade_net_return_pct,
                baseline_matched_mean_net_return_pct=(metrics.baseline_matched_mean_net_return_pct),
                matched_mean_delta_pct=metrics.matched_mean_delta_pct,
                adverse_selection_stop_30m_rate_pct=(metrics.adverse_selection_stop_30m_rate_pct),
                missed_baseline_winners=metrics.missed_baseline_winners,
            )
        )
    return tuple(rows)


def build_maker_entry_report(
    dataset: ReplayDataset,
    filters: ReplayFilters,
    paths: tuple[MakerDecisionPaths, ...],
    *,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
    costs: CostParameters = DEFAULT_COSTS,
    maker_entry_fee_bps: float = MAKER_ENTRY_FEE_BPS,
) -> MakerEntryReport:
    revision = normalize_code_revision(code_revision)
    if filters.since != MAKER_ENTRY_COHORT_START:
        raise ValueError("maker-entry report requires the locked discovery cohort start")
    if filters.strategy_versions != MAKER_ENTRY_STRATEGY_VERSIONS:
        raise ValueError("maker-entry report requires the locked strategy cohort")
    if filters.allow_fallback:
        raise ValueError("maker-entry report requires exact venue outcomes")
    if not math.isfinite(maker_entry_fee_bps) or maker_entry_fee_bps < 0:
        raise ValueError("maker entry fee must be finite and non-negative")
    counts = Counter(path.decision_id for path in paths)
    duplicates = sorted(key for key, count in counts.items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate maker paths for decisions: {duplicates}")
    path_by_decision = {path.decision_id: path for path in paths}
    results: list[MakerEntryResult] = []
    for episode in dataset.eligible_episodes:
        selection = select_score_policy(episode, MARKET_QUALITY_CONTROL_POLICY)
        decision_id = selection.decision.decision_id if selection.decision is not None else ""
        results.append(
            evaluate_maker_entry(
                episode,
                path_by_decision.get(decision_id or ""),
                costs=costs,
                maker_entry_fee_bps=maker_entry_fee_bps,
            )
        )
    frozen_results = tuple(results)
    exclusions = Counter(
        reason for episode in dataset.excluded_episodes for reason in episode.exclusion_reasons
    )
    path_statuses = Counter(
        (f"1m:{path.one_minute.status}" if timeframe == "1m" else f"5m:{path.five_minute.status}")
        for path in paths
        for timeframe in ("1m", "5m")
    )
    return MakerEntryReport(
        manifest=MakerEntryManifest(
            protocol_version=PROTOCOL_VERSION,
            replay_engine_version=FOUNDATION_VERSION,
            replay_query_version=QUERY_VERSION,
            report_version=MAKER_ENTRY_REPORT_VERSION,
            selection_version=MAKER_SELECTION_VERSION,
            maker_entry_model_version=MAKER_ENTRY_MODEL_VERSION,
            fill_evidence_version=MAKER_FILL_EVIDENCE_VERSION,
            virtual_strategy_version=VIRTUAL_STRATEGY_VERSION,
            exit_model_version=EXIT_MODEL_VERSION,
            cost_model_version=f"{COST_MODEL_VERSION}+{MAKER_COST_MODEL_VERSION}",
            market_path_version=MAKER_MARKET_PATH_VERSION,
            code_revision=revision,
            working_tree_dirty=working_tree_dirty,
            generated_at=generated_at,
            dataset_since=filters.since,
            dataset_until_exclusive=filters.until,
            decision_input_fingerprint=dataset.input_fingerprint,
            market_path_fingerprint=maker_market_path_fingerprint(paths),
            strategy_versions=filters.strategy_versions,
            resolver_version=filters.resolver_version,
            required_horizons=filters.required_horizons,
            fill_timeout_minutes=MAKER_FILL_TIMEOUT_MINUTES,
            maker_entry_fee_bps=maker_entry_fee_bps,
            taker_exit_fee_bps=costs.taker_fee_bps_per_side,
            funding_cost_bps_per_8h=costs.funding_cost_bps_per_8h,
        ),
        dataset_episodes=len(dataset.episodes),
        eligible_episodes=len(dataset.eligible_episodes),
        excluded_episodes=len(dataset.excluded_episodes),
        input_exclusions=_count_rows(exclusions),
        path_statuses=_count_rows(path_statuses),
        result_statuses=_count_rows(Counter(result.status for result in frozen_results)),
        metrics=_metrics(frozen_results),
        timeframe_metrics=_timeframe_metrics(frozen_results),
        results=frozen_results,
        market_paths=paths,
    )


def render_json(report: MakerEntryReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True, allow_nan=False)


def render_markdown(report: MakerEntryReport) -> str:
    manifest = report.manifest
    metrics = report.metrics
    lines = [
        "# Pump Short Maker Entry Upper Bound",
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
            "> Discovery-only optimistic upper bound. A candle crossing the limit does "
            "not prove post-only acceptance, queue position, partial fill, or executable "
            "size. This report cannot promote the strategy or authorize live trading."
        ),
        "",
        "## Locked contract",
        "",
    ]
    lines.extend(
        markdown_table(
            ("Component", "Version / value"),
            [
                ("Selection", manifest.selection_version),
                ("Entry", manifest.maker_entry_model_version),
                ("Potential-fill evidence", manifest.fill_evidence_version),
                ("Exit", manifest.exit_model_version),
                ("Market path", manifest.market_path_version),
                ("Order activation", manifest.order_activation),
                ("Post-only acceptance", manifest.post_only_acceptance),
                ("Comparison granularity", manifest.comparison_granularity),
                ("Comparison interpretation", manifest.comparison_interpretation),
                ("Fill timeout", f"{manifest.fill_timeout_minutes} minutes"),
                ("Unfilled", manifest.unfilled_policy),
                ("Maker entry fee", f"{manifest.maker_entry_fee_bps:.2f} bps"),
                ("Taker exit fee", f"{manifest.taker_exit_fee_bps:.2f} bps"),
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
                ("Selected decisions", metrics.selected),
                ("Known fill opportunities", metrics.known_fill_opportunities),
                ("Potential fills", metrics.potential_fills),
                ("Cash unfilled", metrics.cash_unfilled),
                ("Unresolved", metrics.unresolved),
                ("1m primary", metrics.primary_1m),
                ("5m fallback", metrics.fallback_5m),
                ("Exact touches only", metrics.touched_only),
                ("Post-only reject risk", metrics.post_only_reject_risk),
            ],
        )
    )
    lines.extend(["", "## Timeframe-separated economics", ""])
    lines.extend(
        markdown_table(
            (
                "Path",
                "Episodes",
                "Potential fills",
                "Fill rate",
                "Episode net",
                "Filled net",
                "Taker baseline",
                "Maker delta",
                "Stop <=30m",
                "Missed winners",
            ),
            [
                (
                    row.timeframe,
                    row.episodes,
                    row.potential_fills,
                    format_percentage(row.fill_rate_pct, missing="n/a"),
                    format_percentage(row.mean_episode_net_return_pct, missing="n/a"),
                    format_percentage(
                        row.mean_filled_trade_net_return_pct,
                        missing="n/a",
                    ),
                    format_percentage(
                        row.baseline_matched_mean_net_return_pct,
                        missing="n/a",
                    ),
                    format_percentage(row.matched_mean_delta_pct, missing="n/a"),
                    format_percentage(
                        row.adverse_selection_stop_30m_rate_pct,
                        missing="n/a",
                    ),
                    row.missed_baseline_winners,
                )
                for row in report.timeframe_metrics
            ],
        )
    )
    lines.extend(
        [
            "",
            "_The 1m row is primary. The 5m row is fallback sensitivity and must not "
            "be used to repair an unfavorable 1m result. The 1m maker path resolves "
            "exits at 1m while the taker baseline remains 5m, so their delta is not "
            "a pure causal estimate of entry execution alone._",
            "",
            "## All resolved economics",
            "",
        ]
    )
    lines.extend(
        markdown_table(
            ("Metric", "Value"),
            [
                ("Potential fill rate", format_percentage(metrics.fill_rate_pct, missing="n/a")),
                (
                    "Mean episode net incl. cash",
                    format_percentage(metrics.mean_episode_net_return_pct, missing="n/a"),
                ),
                (
                    "Mean filled-trade net",
                    format_percentage(metrics.mean_filled_trade_net_return_pct, missing="n/a"),
                ),
                (
                    "Matched taker baseline net",
                    format_percentage(
                        metrics.baseline_matched_mean_net_return_pct,
                        missing="n/a",
                    ),
                ),
                (
                    "Matched maker delta",
                    format_percentage(metrics.matched_mean_delta_pct, missing="n/a"),
                ),
                ("Filled win rate", format_percentage(metrics.win_rate_pct, missing="n/a")),
                ("Filled profit factor", format_number(metrics.profit_factor, missing="n/a")),
                (
                    "Initial stop rate",
                    format_percentage(metrics.initial_stop_rate_pct, missing="n/a"),
                ),
                (
                    "Stop within 30m after fill",
                    format_percentage(
                        metrics.adverse_selection_stop_30m_rate_pct,
                        missing="n/a",
                    ),
                ),
                ("Missed baseline winners", metrics.missed_baseline_winners),
                (
                    "Missed-winner share of unfilled",
                    format_percentage(
                        metrics.missed_baseline_winner_rate_pct,
                        missing="n/a",
                    ),
                ),
                (
                    "Mean fee cost",
                    format_number(metrics.mean_fee_cost_bps, suffix=" bps", missing="n/a"),
                ),
                (
                    "Mean slippage cost",
                    format_number(
                        metrics.mean_slippage_cost_bps,
                        suffix=" bps",
                        missing="n/a",
                    ),
                ),
                (
                    "Total net P&L",
                    format_number(metrics.total_net_pnl_usd, suffix=" USD", missing="n/a"),
                ),
                (
                    "Max sequential drawdown",
                    format_number(
                        metrics.max_sequential_drawdown_usd,
                        suffix=" USD",
                        missing="n/a",
                    ),
                ),
            ],
        )
    )
    lines.extend(["", "## Result statuses", ""])
    lines.extend(
        markdown_table(
            ("Status", "Episodes"),
            [(row.name, row.count) for row in report.result_statuses],
        )
    )
    lines.extend(["", "## Path fetch statuses", ""])
    lines.extend(
        markdown_table(
            ("Status", "Paths"),
            [(row.name, row.count) for row in report.path_statuses],
        )
    )
    lines.extend(["", "## Input exclusions", ""])
    lines.extend(
        markdown_table(
            ("Reason", "Episodes"),
            [(row.name, row.count) for row in report.input_exclusions],
        )
    )
    lines.extend(["", "## Episode results", ""])
    lines.extend(
        markdown_table(
            (
                "Episode",
                "Base",
                "Exchange",
                "TF",
                "Status",
                "Evidence",
                "Baseline",
                "Maker / cash",
                "Exit",
            ),
            [
                (
                    result.pump_event_id,
                    result.base,
                    result.exchange or "",
                    result.path_timeframe or "",
                    result.status,
                    result.fill_evidence or "",
                    format_percentage(
                        result.baseline_trade.net_return_pct
                        if result.baseline_trade is not None
                        else None,
                        missing="n/a",
                    ),
                    format_percentage(result.episode_net_return_pct, missing="n/a"),
                    result.maker_trade.exit_reason if result.maker_trade is not None else "",
                )
                for result in report.results
            ],
        )
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Estimate an optimistic post-only entry upper bound from exact-venue candles"
    )
    parser.add_argument("--since", type=parse_utc_datetime, default=MAKER_ENTRY_COHORT_START)
    parser.add_argument("--until", type=parse_utc_datetime)
    parser.add_argument("--strategy-version", action="append")
    parser.add_argument("--resolver-version", default=RESOLVER_VERSION)
    parser.add_argument(
        "--maker-entry-fee-bps",
        type=float,
        default=MAKER_ENTRY_FEE_BPS,
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
    from .virtual_market import fetch_maker_decision_paths

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for maker-entry-report")
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    generated_at = datetime.now(UTC)
    filters = ReplayFilters(
        since=args.since,
        until=args.until or generated_at,
        strategy_versions=tuple(args.strategy_version or MAKER_ENTRY_STRATEGY_VERSIONS),
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
    selected = selected_policy_decisions(
        dataset.eligible_episodes,
        policies=(MARKET_QUALITY_CONTROL_POLICY,),
    )
    paths = await fetch_maker_decision_paths(selected, EXCHANGE_FACTORIES)
    report = build_maker_entry_report(
        dataset,
        filters,
        paths,
        generated_at=generated_at,
        code_revision=args.code_revision,
        working_tree_dirty=args.working_tree_dirty,
        costs=costs,
        maker_entry_fee_bps=args.maker_entry_fee_bps,
    )
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.stdout.write(asyncio.run(_run(args)))
