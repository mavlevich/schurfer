"""Discovery-only pump-magnitude surface over point-in-time threshold crossings."""

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

from .episode_replay import PROTOCOL_VERSION
from .outcomes import RESOLVER_VERSION
from .replay import (
    FOUNDATION_VERSION,
    QUERY_VERSION,
    ReplayDataset,
    ReplayDecision,
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
    max_sequential_drawdown_usd,
)
from .virtual_threshold_challenger_report import (
    ThresholdEpisodeResult,
    evaluate_threshold,
)
from .virtual_threshold_challengers import (
    ENTRY_THRESHOLD_COHORT_START,
    ENTRY_THRESHOLD_SELECTION_VERSION,
    selected_threshold_decisions_for,
)

PUMP_MAGNITUDE_REPORT_VERSION = "pump_magnitude_surface_v1"
PUMP_MAGNITUDE_COHORT_START = ENTRY_THRESHOLD_COHORT_START
PUMP_MAGNITUDE_STRATEGY_VERSIONS = (
    "pump_short_measurement_v1",
    "pump_short_v1_market_quality",
)
PUMP_MAGNITUDE_FLOORS_PCT = (20.0, 30.0, 50.0, 70.0, 100.0, 150.0, 200.0)
PUMP_MAGNITUDE_REQUIRED_HORIZONS = (240, 480)
FIXED_HORIZON_MINUTES = 240


def _progress(message: str) -> None:
    sys.stderr.write(f"[pump-magnitude] {message}\n")
    sys.stderr.flush()


@dataclass(frozen=True)
class PumpMagnitudeManifest:
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
    floors_pct: tuple[float, ...]
    fixed_horizon_minutes: int
    taker_fee_bps_per_side: float
    funding_cost_bps_per_8h: float
    interpretation: str = "discovery_only_no_strategy_change"
    no_trigger_policy: str = "zero_return_cash_episode"
    within_bar_policy: str = "conservative_stop_first"


@dataclass(frozen=True)
class CountRow:
    name: str
    count: int


@dataclass(frozen=True)
class PumpMagnitudeMetrics:
    threshold_key: str
    floor_pct: float
    eligible_episodes: int
    resolved_episodes: int
    triggered: int
    cash: int
    unresolved: int
    completed_trades: int
    clusters: int
    largest_cluster_share_pct: float | None
    exchanges: int
    largest_exchange_share_pct: float | None
    triggered_per_calendar_day: float
    trade_rate_pct: float | None
    mean_selected_pump_pct: float | None
    total_net_pnl_usd: float | None
    mean_episode_gross_return_pct: float | None
    mean_episode_net_return_pct: float | None
    mean_trade_net_return_pct: float | None
    median_trade_net_return_pct: float | None
    win_rate_pct: float | None
    profit_factor: float | None
    max_sequential_drawdown_usd: float | None
    initial_stop_rate_pct: float | None
    mean_mfe_pct: float | None
    mean_mae_pct: float | None
    mean_duration_minutes: float | None
    mean_fee_cost_bps: float | None
    mean_funding_cost_bps: float | None
    mean_slippage_cost_bps: float | None
    fixed_240_resolved_episodes: int
    fixed_240_episode_gross_return_pct: float | None


@dataclass(frozen=True)
class PumpMagnitudeReport:
    manifest: PumpMagnitudeManifest
    calendar_days: int
    dataset_episodes: int
    eligible_episodes: int
    excluded_episodes: int
    input_exclusion_reasons: tuple[CountRow, ...]
    path_statuses: tuple[CountRow, ...]
    metrics: tuple[PumpMagnitudeMetrics, ...]
    episode_results: tuple[ThresholdEpisodeResult, ...]
    market_paths: tuple[DecisionMarketPath, ...]


def _mean(values: list[float]) -> float | None:
    return fmean(values) if values else None


def _median(values: list[float]) -> float | None:
    return median(values) if values else None


def _count_rows(counter: Counter[str]) -> tuple[CountRow, ...]:
    return tuple(
        CountRow(name, count)
        for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    )


def _floor_key(floor_pct: float) -> str:
    return f"floor_{floor_pct:g}"


def _fixed_horizon_return(
    result: ThresholdEpisodeResult,
    decision_by_id: dict[str, ReplayDecision],
) -> float | None:
    if result.status == "not_triggered":
        return 0.0
    decision_id = result.selected_decision_id
    decision = decision_by_id.get(decision_id or "")
    if decision is None:
        return None
    outcome = next(
        (item for item in decision.outcomes if item.horizon_minutes == FIXED_HORIZON_MINUTES),
        None,
    )
    return outcome.short_return_pct if outcome is not None else None


def _resolved_gross_return(result: ThresholdEpisodeResult) -> float | None:
    if result.status == "not_triggered":
        return 0.0
    trade = result.trade
    return trade.gross_return_pct if trade is not None else None


def _largest_share(values: list[str]) -> tuple[int, float | None]:
    if not values:
        return 0, None
    counts = Counter(values)
    return len(counts), max(counts.values()) / len(values) * 100


def _magnitude_metrics(
    floor_pct: float,
    results: tuple[ThresholdEpisodeResult, ...],
    decision_by_id: dict[str, ReplayDecision],
    *,
    calendar_days: int,
) -> PumpMagnitudeMetrics:
    key = _floor_key(floor_pct)
    selected = tuple(result for result in results if result.threshold_key == key)
    resolved = tuple(result for result in selected if result.episode_net_return_pct is not None)
    trades = tuple(
        result.trade
        for result in selected
        if result.trade is not None and result.trade.status == "complete"
    )
    net_trade_returns = [
        trade.net_return_pct for trade in trades if trade.net_return_pct is not None
    ]
    episode_net_returns = [
        result.episode_net_return_pct
        for result in selected
        if result.episode_net_return_pct is not None
    ]
    episode_gross_returns = [
        value for result in selected if (value := _resolved_gross_return(result)) is not None
    ]
    fixed_returns = [
        value
        for result in selected
        if (value := _fixed_horizon_return(result, decision_by_id)) is not None
    ]
    cluster_count, largest_cluster_share = _largest_share(
        [result.cluster_key for result in resolved]
    )
    exchange_count, largest_exchange_share = _largest_share(
        [result.exchange for result in resolved if result.exchange]
    )
    selected_pumps = [
        result.selected_pump_pct for result in selected if result.selected_pump_pct is not None
    ]
    return PumpMagnitudeMetrics(
        threshold_key=key,
        floor_pct=floor_pct,
        eligible_episodes=len(selected),
        resolved_episodes=len(resolved),
        triggered=sum(result.selected_decision_id is not None for result in selected),
        cash=sum(result.status == "not_triggered" for result in selected),
        unresolved=len(selected) - len(resolved),
        completed_trades=len(trades),
        clusters=cluster_count,
        largest_cluster_share_pct=largest_cluster_share,
        exchanges=exchange_count,
        largest_exchange_share_pct=largest_exchange_share,
        triggered_per_calendar_day=(
            sum(result.selected_decision_id is not None for result in selected) / calendar_days
        ),
        trade_rate_pct=len(trades) / len(resolved) * 100 if resolved else None,
        mean_selected_pump_pct=_mean(selected_pumps),
        total_net_pnl_usd=(
            sum(trade.net_pnl_usd for trade in trades if trade.net_pnl_usd is not None)
            if trades
            else None
        ),
        mean_episode_gross_return_pct=_mean(episode_gross_returns),
        mean_episode_net_return_pct=_mean(episode_net_returns),
        mean_trade_net_return_pct=_mean(net_trade_returns),
        median_trade_net_return_pct=_median(net_trade_returns),
        win_rate_pct=(
            sum(value > 0 for value in net_trade_returns) / len(net_trade_returns) * 100
            if net_trade_returns
            else None
        ),
        profit_factor=profit_factor(net_trade_returns),
        max_sequential_drawdown_usd=max_sequential_drawdown_usd(trades),
        initial_stop_rate_pct=(
            sum(trade.exit_reason == "initial_sl" for trade in trades) / len(trades) * 100
            if trades
            else None
        ),
        mean_mfe_pct=_mean([trade.mfe_pct for trade in trades if trade.mfe_pct is not None]),
        mean_mae_pct=_mean([trade.mae_pct for trade in trades if trade.mae_pct is not None]),
        mean_duration_minutes=_mean(
            [trade.duration_minutes for trade in trades if trade.duration_minutes is not None]
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
        fixed_240_resolved_episodes=len(fixed_returns),
        fixed_240_episode_gross_return_pct=_mean(fixed_returns),
    )


def build_pump_magnitude_report(
    dataset: ReplayDataset,
    filters: ReplayFilters,
    paths: tuple[DecisionMarketPath, ...],
    *,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
    costs: CostParameters = DEFAULT_COSTS,
) -> PumpMagnitudeReport:
    revision = normalize_code_revision(code_revision)
    if filters.since is None:
        raise ValueError("pump-magnitude discovery requires an explicit cohort start")
    if filters.since < PUMP_MAGNITUDE_COHORT_START:
        raise ValueError("pump-magnitude discovery cannot include pre-measurement-split episodes")
    if filters.strategy_versions != PUMP_MAGNITUDE_STRATEGY_VERSIONS:
        raise ValueError("pump-magnitude discovery requires the measurement strategy cohorts")
    if filters.required_horizons != PUMP_MAGNITUDE_REQUIRED_HORIZONS:
        raise ValueError("pump-magnitude discovery requires locked 240m and 480m outcomes")
    path_counts = Counter(path.decision_id for path in paths)
    duplicates = sorted(key for key, count in path_counts.items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate market paths for decisions: {duplicates}")
    path_by_decision = {item.decision_id: item.path for item in paths}
    decision_by_id = {
        decision.decision_id: decision
        for decision in dataset.decisions
        if decision.decision_id is not None
    }
    results = tuple(
        evaluate_threshold(
            episode,
            _floor_key(floor_pct),
            floor_pct,
            path_by_decision,
            costs,
        )
        for episode in dataset.eligible_episodes
        for floor_pct in PUMP_MAGNITUDE_FLOORS_PCT
    )
    exclusions = Counter(
        reason for episode in dataset.excluded_episodes for reason in episode.exclusion_reasons
    )
    calendar_days = max(1, (filters.until.date() - filters.since.date()).days + 1)
    return PumpMagnitudeReport(
        manifest=PumpMagnitudeManifest(
            protocol_version=PROTOCOL_VERSION,
            replay_engine_version=FOUNDATION_VERSION,
            replay_query_version=QUERY_VERSION,
            report_version=PUMP_MAGNITUDE_REPORT_VERSION,
            virtual_strategy_version=VIRTUAL_STRATEGY_VERSION,
            selection_model_version=ENTRY_THRESHOLD_SELECTION_VERSION,
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
            floors_pct=PUMP_MAGNITUDE_FLOORS_PCT,
            fixed_horizon_minutes=FIXED_HORIZON_MINUTES,
            taker_fee_bps_per_side=costs.taker_fee_bps_per_side,
            funding_cost_bps_per_8h=costs.funding_cost_bps_per_8h,
        ),
        calendar_days=calendar_days,
        dataset_episodes=len(dataset.episodes),
        eligible_episodes=len(dataset.eligible_episodes),
        excluded_episodes=len(dataset.excluded_episodes),
        input_exclusion_reasons=_count_rows(exclusions),
        path_statuses=_count_rows(Counter(path.path.status for path in paths)),
        metrics=tuple(
            _magnitude_metrics(
                floor_pct,
                results,
                decision_by_id,
                calendar_days=calendar_days,
            )
            for floor_pct in PUMP_MAGNITUDE_FLOORS_PCT
        ),
        episode_results=results,
        market_paths=paths,
    )


def render_json(report: PumpMagnitudeReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True)


def render_markdown(report: PumpMagnitudeReport) -> str:
    manifest = report.manifest
    lines = [
        "# Pump Magnitude Discovery Surface",
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
            "> Discovery-only surface. It does not select a production threshold, "
            "authorize trading, or modify HYP-003."
        ),
        "",
        "## Coverage",
        "",
    ]
    lines.extend(
        markdown_table(
            ("Metric", "Value"),
            [
                ("Calendar days", report.calendar_days),
                ("Dataset episodes", report.dataset_episodes),
                ("Eligible episodes", report.eligible_episodes),
                ("Excluded episodes", report.excluded_episodes),
                ("Floors", ", ".join(f"{floor:g}%" for floor in manifest.floors_pct)),
                ("Fixed comparison horizon", f"{manifest.fixed_horizon_minutes}m"),
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
    lines.extend(["", "## Opportunity and concentration", ""])
    lines.extend(
        markdown_table(
            (
                "Floor",
                "Resolved",
                "Triggered",
                "Cash",
                "Unresolved",
                "Trades/day",
                "Trade rate",
                "Clusters",
                "Top cluster",
                "Venues",
                "Top venue",
                "Mean selected pump",
            ),
            [
                (
                    format_percentage(row.floor_pct),
                    row.resolved_episodes,
                    row.triggered,
                    row.cash,
                    row.unresolved,
                    format_number(row.triggered_per_calendar_day),
                    format_percentage(row.trade_rate_pct, missing="n/a"),
                    row.clusters,
                    format_percentage(row.largest_cluster_share_pct, missing="n/a"),
                    row.exchanges,
                    format_percentage(row.largest_exchange_share_pct, missing="n/a"),
                    format_percentage(row.mean_selected_pump_pct, missing="n/a"),
                )
                for row in report.metrics
            ],
        )
    )
    lines.extend(["", "## Strategy economics", ""])
    lines.extend(
        markdown_table(
            (
                "Floor",
                "Trades",
                "Episode gross",
                "Episode net",
                "Trade net mean",
                "Trade net median",
                "Win rate",
                "PF",
                "Net P&L",
                "Max DD",
                "Initial SL",
            ),
            [
                (
                    format_percentage(row.floor_pct),
                    row.completed_trades,
                    format_percentage(row.mean_episode_gross_return_pct, missing="n/a"),
                    format_percentage(row.mean_episode_net_return_pct, missing="n/a"),
                    format_percentage(row.mean_trade_net_return_pct, missing="n/a"),
                    format_percentage(row.median_trade_net_return_pct, missing="n/a"),
                    format_percentage(row.win_rate_pct, missing="n/a"),
                    format_number(row.profit_factor, missing="n/a"),
                    format_number(row.total_net_pnl_usd, suffix=" USD", missing="n/a"),
                    format_number(
                        row.max_sequential_drawdown_usd,
                        suffix=" USD",
                        missing="n/a",
                    ),
                    format_percentage(row.initial_stop_rate_pct, missing="n/a"),
                )
                for row in report.metrics
            ],
        )
    )
    lines.extend(
        [
            "",
            "## Exit-normalized and cost diagnostics",
            "",
            (
                "_Fixed 240m is gross short return with no stop or trailing policy. "
                "It isolates magnitude from the production exit mechanics._"
            ),
            "",
        ]
    )
    lines.extend(
        markdown_table(
            (
                "Floor",
                "Fixed N",
                "Fixed 240m episode gross",
                "MFE",
                "MAE",
                "Duration",
                "Fees",
                "Funding",
                "Slippage",
            ),
            [
                (
                    format_percentage(row.floor_pct),
                    row.fixed_240_resolved_episodes,
                    format_percentage(
                        row.fixed_240_episode_gross_return_pct,
                        missing="n/a",
                    ),
                    format_percentage(row.mean_mfe_pct, missing="n/a"),
                    format_percentage(row.mean_mae_pct, missing="n/a"),
                    format_number(row.mean_duration_minutes, suffix="m", missing="n/a"),
                    format_number(row.mean_fee_cost_bps, suffix=" bps", missing="n/a"),
                    format_number(row.mean_funding_cost_bps, suffix=" bps", missing="n/a"),
                    format_number(row.mean_slippage_cost_bps, suffix=" bps", missing="n/a"),
                )
                for row in report.metrics
            ],
        )
    )
    lines.extend(["", "## Episode results", ""])
    lines.extend(
        markdown_table(
            (
                "Episode",
                "Base",
                "Floor",
                "Status",
                "Selected pump",
                "Exchange",
                "Exit",
                "Net",
                "Error",
            ),
            [
                (
                    row.pump_event_id,
                    row.base,
                    format_percentage(row.min_pump_pct),
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
        description="Explore point-in-time pump-magnitude floors without changing strategy"
    )
    parser.add_argument(
        "--since",
        type=parse_utc_datetime,
        default=PUMP_MAGNITUDE_COHORT_START,
        help="inclusive UTC cutoff",
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
        raise ValueError("DATABASE_URL is required for pump-magnitude-report")
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    generated_at = datetime.now(UTC)
    if args.since < PUMP_MAGNITUDE_COHORT_START:
        raise ValueError("pump-magnitude discovery cannot include pre-measurement-split episodes")
    filters = ReplayFilters(
        since=args.since,
        until=args.until or generated_at,
        strategy_versions=tuple(args.strategy_version or PUMP_MAGNITUDE_STRATEGY_VERSIONS),
        resolver_version=args.resolver_version,
        required_horizons=PUMP_MAGNITUDE_REQUIRED_HORIZONS,
        allow_fallback=args.allow_fallback,
    )
    costs = CostParameters(
        taker_fee_bps_per_side=args.taker_fee_bps_per_side,
        funding_cost_bps_per_8h=args.funding_cost_bps_per_8h,
    )
    repository = ReplayRepository.from_url(db_url)
    _progress("loading replay inputs")
    try:
        decisions = await repository.load(filters)
    finally:
        await repository.close()
    dataset = build_replay_dataset(decisions, filters)
    selected = selected_threshold_decisions_for(
        dataset.eligible_episodes,
        PUMP_MAGNITUDE_FLOORS_PCT,
    )
    _progress(
        f"loaded {len(decisions)} decisions, "
        f"{len(dataset.eligible_episodes)} eligible episodes, "
        f"{len(selected)} selected paths"
    )

    def report_exchange(exchange: str, index: int, total: int) -> None:
        _progress(f"fetching {exchange} ({index}/{total})")

    paths = await fetch_decision_market_paths(
        selected,
        EXCHANGE_FACTORIES,
        on_exchange=report_exchange,
    )
    _progress("building report")
    report = build_pump_magnitude_report(
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
