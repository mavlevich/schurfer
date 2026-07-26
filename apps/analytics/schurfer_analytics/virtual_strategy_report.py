"""Read-only baseline virtual-strategy replay and reproducibility report."""

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

from .episode_replay import CONFIRMATION_COHORT_START, PROTOCOL_VERSION
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
)
from .virtual_strategy import (
    COST_MODEL_VERSION,
    DEFAULT_COSTS,
    ENTRY_MODEL_VERSION,
    EXIT_MODEL_VERSION,
    MARKET_PATH_VERSION,
    SELECTION_MODEL_VERSION,
    VIRTUAL_STRATEGY_VERSION,
    CostParameters,
    MarketPath,
    VirtualTrade,
    market_path_fingerprint,
    simulate_episode,
)

REPORT_VERSION = "virtual_strategy_report_v2"


@dataclass(frozen=True)
class VirtualReplayManifest:
    protocol_version: str
    replay_engine_version: str
    replay_query_version: str
    report_version: str
    strategy_model_version: str
    selection_model_version: str
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
    taker_fee_bps_per_side: float
    funding_cost_bps_per_8h: float
    observation_unit: str = "pump_event_id"
    side: str = "short"
    entry_price_source: str = "next_complete_5m_bar_open"
    within_bar_policy: str = "conservative_stop_first"
    report_scope: str = "descriptive_no_statistical_verdict"


@dataclass(frozen=True)
class VirtualReplayHealth:
    dataset_episodes: int
    eligible_episodes: int
    excluded_episodes: int
    completed_replays: int
    unresolved_replays: int
    taken: int
    skipped: int


@dataclass(frozen=True)
class VirtualReplayMetrics:
    mean_gross_return_pct: float | None
    mean_net_return_pct: float | None
    net_win_rate_pct: float | None
    total_gross_pnl_usd: float | None
    total_net_pnl_usd: float | None
    mean_fee_cost_bps: float | None
    mean_funding_cost_bps: float | None
    mean_slippage_cost_bps: float | None
    mean_mfe_pct: float | None
    mean_mae_pct: float | None
    mean_duration_minutes: float | None


@dataclass(frozen=True)
class CountRow:
    name: str
    count: int


@dataclass(frozen=True)
class VirtualReplayReport:
    manifest: VirtualReplayManifest
    health: VirtualReplayHealth
    metrics: VirtualReplayMetrics
    input_exclusion_reasons: tuple[CountRow, ...]
    classifications: tuple[CountRow, ...]
    exit_reasons: tuple[CountRow, ...]
    unresolved_reasons: tuple[CountRow, ...]
    trades: tuple[VirtualTrade, ...]
    market_paths: tuple[MarketPath, ...]


def _mean(trades: tuple[VirtualTrade, ...], field: str) -> float | None:
    values = [value for trade in trades if (value := getattr(trade, field)) is not None]
    return fmean(values) if values else None


def _sum(trades: tuple[VirtualTrade, ...], field: str) -> float | None:
    values = [value for trade in trades if (value := getattr(trade, field)) is not None]
    return sum(values) if values else None


def _count_rows(counter: Counter[str]) -> tuple[CountRow, ...]:
    return tuple(
        CountRow(name, count)
        for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    )


def build_virtual_report(
    dataset: ReplayDataset,
    filters: ReplayFilters,
    paths: tuple[MarketPath, ...],
    *,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
    costs: CostParameters = DEFAULT_COSTS,
) -> VirtualReplayReport:
    revision = normalize_code_revision(code_revision)
    event_counts = Counter(path.pump_event_id for path in paths)
    duplicate_events = sorted(event_id for event_id, count in event_counts.items() if count > 1)
    if duplicate_events:
        raise ValueError(f"duplicate market paths for episodes: {duplicate_events}")
    path_by_event = {path.pump_event_id: path for path in paths}
    trades = tuple(
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
    complete = tuple(trade for trade in trades if trade.status == "complete")
    wins = sum(1 for trade in complete if (trade.net_return_pct or 0) > 0)
    classifications = Counter(trade.classification for trade in trades)
    exits = Counter(trade.exit_reason for trade in complete if trade.exit_reason)
    input_exclusions = Counter(
        reason for episode in dataset.excluded_episodes for reason in episode.exclusion_reasons
    )
    unresolved = Counter(
        f"{trade.status}: {trade.error or 'unknown'}"
        for trade in trades
        if trade.status != "complete"
    )
    return VirtualReplayReport(
        manifest=VirtualReplayManifest(
            protocol_version=PROTOCOL_VERSION,
            replay_engine_version=FOUNDATION_VERSION,
            replay_query_version=QUERY_VERSION,
            report_version=REPORT_VERSION,
            strategy_model_version=VIRTUAL_STRATEGY_VERSION,
            selection_model_version=SELECTION_MODEL_VERSION,
            entry_model_version=ENTRY_MODEL_VERSION,
            exit_model_version=EXIT_MODEL_VERSION,
            cost_model_version=COST_MODEL_VERSION,
            market_path_version=MARKET_PATH_VERSION,
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
        ),
        health=VirtualReplayHealth(
            dataset_episodes=len(dataset.episodes),
            eligible_episodes=len(dataset.eligible_episodes),
            excluded_episodes=len(dataset.excluded_episodes),
            completed_replays=len(complete),
            unresolved_replays=len(trades) - len(complete),
            taken=sum(1 for trade in trades if trade.taken),
            skipped=sum(1 for trade in trades if not trade.taken),
        ),
        metrics=VirtualReplayMetrics(
            mean_gross_return_pct=_mean(complete, "gross_return_pct"),
            mean_net_return_pct=_mean(complete, "net_return_pct"),
            net_win_rate_pct=wins / len(complete) * 100 if complete else None,
            total_gross_pnl_usd=_sum(complete, "gross_pnl_usd"),
            total_net_pnl_usd=_sum(complete, "net_pnl_usd"),
            mean_fee_cost_bps=_mean(complete, "fee_cost_bps"),
            mean_funding_cost_bps=_mean(complete, "funding_cost_bps"),
            mean_slippage_cost_bps=_mean(complete, "slippage_cost_bps"),
            mean_mfe_pct=_mean(complete, "mfe_pct"),
            mean_mae_pct=_mean(complete, "mae_pct"),
            mean_duration_minutes=_mean(complete, "duration_minutes"),
        ),
        input_exclusion_reasons=_count_rows(input_exclusions),
        classifications=_count_rows(classifications),
        exit_reasons=_count_rows(exits),
        unresolved_reasons=_count_rows(unresolved),
        trades=trades,
        market_paths=paths,
    )


def render_json(report: VirtualReplayReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True)


def render_markdown(report: VirtualReplayReport) -> str:
    manifest = report.manifest
    health = report.health
    metrics = report.metrics
    lines = [
        "# Pump Short v1 Virtual Replay",
        "",
        f"Generated: {manifest.generated_at.isoformat()}",
        f"Code revision: `{manifest.code_revision}`",
        f"Working tree dirty: {'yes' if manifest.working_tree_dirty else 'no'}",
        f"Decision fingerprint: `{manifest.decision_input_fingerprint}`",
        f"Market-path fingerprint: `{manifest.market_path_fingerprint}`",
        (
            "Scope: "
            f"{manifest.dataset_since.isoformat() if manifest.dataset_since else 'all time'}"
            f" <= decision < {manifest.dataset_until_exclusive.isoformat()}"
        ),
        "",
        "> Descriptive baseline replay only. It does not run confidence intervals,",
        "> select a challenger, or issue a go/no-go verdict.",
        "",
        "## Model",
        "",
    ]
    lines.extend(
        markdown_table(
            ("Component", "Version / value"),
            [
                ("Strategy", manifest.strategy_model_version),
                ("Replay engine", manifest.replay_engine_version),
                ("Replay query", manifest.replay_query_version),
                ("Selection", manifest.selection_model_version),
                ("Entry", manifest.entry_model_version),
                ("Exit", manifest.exit_model_version),
                ("Market path", manifest.market_path_version),
                ("Within-bar ambiguity", manifest.within_bar_policy),
                ("Taker fee per side", f"{manifest.taker_fee_bps_per_side:.2f} bps"),
                ("Conservative funding cost per 8h", f"{manifest.funding_cost_bps_per_8h:.2f} bps"),
            ],
        )
    )
    lines.extend(["", "## Coverage", ""])
    lines.extend(
        markdown_table(
            ("Metric", "Value"),
            [
                ("Dataset episodes", health.dataset_episodes),
                ("Eligible episodes", health.eligible_episodes),
                ("Excluded episodes", health.excluded_episodes),
                ("Completed replays", health.completed_replays),
                ("Unresolved replays", health.unresolved_replays),
                ("Recorded taken", health.taken),
                ("Counterfactual skipped", health.skipped),
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
    lines.extend(["", "## Descriptive metrics", ""])
    lines.extend(
        markdown_table(
            ("Metric", "Value"),
            [
                (
                    "Mean gross return",
                    format_percentage(metrics.mean_gross_return_pct, missing="n/a"),
                ),
                (
                    "Mean net return",
                    format_percentage(metrics.mean_net_return_pct, missing="n/a"),
                ),
                ("Net win rate", format_percentage(metrics.net_win_rate_pct, missing="n/a")),
                (
                    "Total gross P&L",
                    format_number(metrics.total_gross_pnl_usd, suffix=" USD", missing="n/a"),
                ),
                (
                    "Total net P&L",
                    format_number(metrics.total_net_pnl_usd, suffix=" USD", missing="n/a"),
                ),
                (
                    "Mean fee cost",
                    format_number(metrics.mean_fee_cost_bps, suffix=" bps", missing="n/a"),
                ),
                (
                    "Mean funding cost",
                    format_number(metrics.mean_funding_cost_bps, suffix=" bps", missing="n/a"),
                ),
                (
                    "Mean liquidity slippage",
                    format_number(
                        metrics.mean_slippage_cost_bps,
                        suffix=" bps",
                        missing="n/a",
                    ),
                ),
                ("Mean MFE", format_percentage(metrics.mean_mfe_pct, missing="n/a")),
                ("Mean MAE", format_percentage(metrics.mean_mae_pct, missing="n/a")),
                (
                    "Mean duration",
                    format_number(metrics.mean_duration_minutes, suffix=" min", missing="n/a"),
                ),
            ],
        )
    )
    for title, rows in (
        ("Classifications", report.classifications),
        ("Exit reasons", report.exit_reasons),
        ("Unresolved reasons", report.unresolved_reasons),
    ):
        lines.extend(["", f"## {title}", ""])
        lines.extend(markdown_table(("Name", "Count"), [(row.name, row.count) for row in rows]))
    lines.extend(["", "## Episode results", ""])
    lines.extend(
        markdown_table(
            (
                "Episode",
                "Base",
                "Exchange",
                "Taken",
                "Classification",
                "Exit",
                "Gross",
                "Net",
                "MFE",
                "MAE",
                "Duration",
            ),
            [
                (
                    trade.pump_event_id,
                    trade.base,
                    trade.exchange,
                    "yes" if trade.taken else "no",
                    trade.classification,
                    trade.exit_reason or trade.status,
                    format_percentage(trade.gross_return_pct, missing="n/a"),
                    format_percentage(trade.net_return_pct, missing="n/a"),
                    format_percentage(trade.mfe_pct, missing="n/a"),
                    format_percentage(trade.mae_pct, missing="n/a"),
                    format_number(trade.duration_minutes, suffix="m", missing="n/a"),
                )
                for trade in report.trades
            ],
        )
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay the pump-short v1 baseline by episode")
    parser.add_argument(
        "--since",
        type=parse_utc_datetime,
        default=CONFIRMATION_COHORT_START,
        help="inclusive UTC cutoff; defaults to the protocol confirmation start",
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
        help="allow fallback outcomes in the foundation filter; sensitivity only",
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
    from .virtual_market import fetch_market_paths

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for virtual-strategy-report")
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
    paths = await fetch_market_paths(dataset.eligible_episodes, EXCHANGE_FACTORIES)
    report = build_virtual_report(
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
