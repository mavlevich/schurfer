"""CLI and renderers for the extreme-mover endpoint discovery replay."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime

from schurfer_performance import DEFAULT_COSTS, CostParameters

from .clustered_inference import DEFAULT_BOOTSTRAP_ITERATIONS, DEFAULT_BOOTSTRAP_SEED
from .extreme_mover_replay import (
    DISCOVERY_END,
    DISCOVERY_START,
    HORIZONS_MINUTES,
    STRATEGY_VERSIONS,
    Report,
    build_report,
)
from .outcomes import RESOLVER_VERSION
from .replay import ReplayFilters
from .reporting import (
    format_number,
    format_percentage,
    markdown_table,
    parse_utc_datetime,
    render_dataclass_json,
)


def render_json(report: Report) -> str:
    return render_dataclass_json(report)


def render_markdown(report: Report) -> str:
    manifest = report.manifest
    lines = [
        "# Extreme-mover endpoint replay v1",
        "",
        "> Discovery on a viewed window. This report cannot promote a strategy "
        "or authorize orders.",
        "",
        "## Manifest",
        "",
    ]
    lines.extend(
        markdown_table(
            ("Field", "Value"),
            [
                ("Report version", manifest.report_version),
                ("Selection version", manifest.selection_version),
                (
                    "Window",
                    f"[{manifest.dataset_since.isoformat()}, "
                    f"{manifest.dataset_until_exclusive.isoformat()})",
                ),
                ("Database snapshot", manifest.database_snapshot_at.isoformat()),
                ("Generated", manifest.generated_at.isoformat()),
                ("Code revision", manifest.code_revision),
                ("Working tree dirty", str(manifest.working_tree_dirty).lower()),
                ("Input fingerprint", manifest.input_fingerprint),
                ("Resolver", manifest.resolver_version),
                ("Cost model", manifest.cost_model_version),
                ("Entry liquidity", manifest.entry_liquidity_version),
                ("Exit slippage", manifest.exit_slippage_version),
                ("Episodes / decisions", f"{report.episodes} / {report.decisions}"),
            ],
        )
    )
    lines.extend(["", "## Routing verdict", ""])
    lines.extend(
        markdown_table(
            ("Direction", "Verdict", "Selected cell", "Reason"),
            [
                (
                    row.direction,
                    row.verdict,
                    row.selected_cell or "—",
                    row.reason,
                )
                for row in report.verdicts
            ],
        )
    )
    lines.extend(["", "## Exact-path coverage by venue", ""])
    lines.extend(
        markdown_table(
            (
                "Venue",
                "Selected episodes",
                "Exact 60m outcomes",
                "Unresolved 60m",
                "Fillable long",
                "Fillable short",
            ),
            [
                (
                    row.exchange,
                    row.selected_episodes,
                    row.exact_outcomes_60m,
                    row.unresolved_outcomes_60m,
                    row.fillable_long_entries_60m,
                    row.fillable_short_entries_60m,
                )
                for row in report.exchange_coverage
            ],
        )
    )
    lines.extend(["", "## Cell economics", ""])
    lines.extend(
        markdown_table(
            (
                "Anchor",
                "Side",
                "Horizon",
                "Signals",
                "Trades",
                "Cash",
                "Unresolved",
                "Assets",
                "Weeks",
                "Mean/signal",
                "Mean/trade",
                "Mean 0bps exit",
                "Mean 30bps exit",
                "Median/trade",
                "PF",
                "95% CI mean/signal",
                "Candidate",
            ),
            [
                (
                    row.anchor,
                    row.direction,
                    f"{row.horizon_minutes}m",
                    row.total_signals,
                    row.completed_trades,
                    row.cash,
                    row.unresolved,
                    row.clusters,
                    row.utc_weeks,
                    format_percentage(row.mean_signal_net_return_pct),
                    format_percentage(row.mean_trade_net_return_pct),
                    format_percentage(row.mean_trade_zero_exit_slippage_pct),
                    format_percentage(row.mean_trade_double_exit_slippage_pct),
                    format_percentage(row.median_trade_net_return_pct),
                    format_number(row.profit_factor),
                    (
                        f"[{format_percentage(row.bootstrap_lower_pct)}, "
                        f"{format_percentage(row.bootstrap_upper_pct)}]"
                    ),
                    "yes" if row.candidate_ready else "no",
                )
                for row in report.metrics
            ],
        )
    )
    lines.extend(["", "## Risk and concentration", ""])
    lines.extend(
        markdown_table(
            (
                "Cell",
                "Net PnL",
                "Drawdown",
                "Worst",
                "Losing streak",
                "Trades/day",
                "Peak concurrent",
                "Occupancy",
                "Largest asset",
                "Largest venue",
                "Largest week",
                "Worst LOO asset",
                "Worst LOO venue",
                "Worst LOO week",
            ),
            [
                (
                    f"{row.anchor}:{row.direction}:{row.horizon_minutes}m",
                    format_number(row.total_net_pnl_usd, suffix=" USD"),
                    format_number(row.max_sequential_drawdown_usd, suffix=" USD"),
                    format_percentage(row.worst_trade_pct),
                    row.longest_losing_streak,
                    format_number(row.trades_per_calendar_day),
                    row.peak_concurrent_positions,
                    format_number(row.capital_occupancy_usd_hours, suffix=" USD-h"),
                    format_percentage(row.largest_asset_share_pct),
                    format_percentage(row.largest_venue_share_pct),
                    format_percentage(row.largest_week_share_pct),
                    format_percentage(row.worst_leave_one_asset_out_mean_pct),
                    format_percentage(row.worst_leave_one_venue_out_mean_pct),
                    format_percentage(row.worst_leave_one_week_out_mean_pct),
                )
                for row in report.metrics
            ],
        )
    )
    lines.extend(["", "## Coverage and cash reasons", ""])
    lines.extend(
        markdown_table(("Reason", "Count"), [(row.name, row.count) for row in report.coverage])
    )
    lines.extend(
        [
            "",
            "## Interpretation boundary",
            "",
            "- `complete` means exact same-venue `forward_v1` endpoint evidence, "
            "not an executable exit book.",
            "- Missing outcomes remain unresolved. Missing entry liquidity is cash, "
            "never a ticker-price fill.",
            "- LBank state-only rows cannot support return or expectancy claims.",
            "- A candidate verdict only authorizes a separately frozen prospective contract.",
            "",
        ]
    )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay extreme-mover endpoints")
    parser.add_argument("--since", type=parse_utc_datetime, default=DISCOVERY_START)
    parser.add_argument("--until", type=parse_utc_datetime, default=DISCOVERY_END)
    parser.add_argument("--code-revision", default=os.getenv("SCHURFER_GIT_SHA"))
    parser.add_argument(
        "--working-tree-dirty",
        action=argparse.BooleanOptionalAction,
        required=True,
    )
    parser.add_argument("--bootstrap-iterations", type=int, default=DEFAULT_BOOTSTRAP_ITERATIONS)
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
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser


async def _run(args: argparse.Namespace) -> str:
    from .extreme_mover_replay_repository import ExtremeMoverReplayRepository

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for extreme-mover-replay-report")
    if not args.code_revision or not args.code_revision.strip():
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    generated_at = datetime.now(UTC)
    until = args.until
    filters = ReplayFilters(
        since=args.since,
        until=until,
        strategy_versions=STRATEGY_VERSIONS,
        resolver_version=RESOLVER_VERSION,
        required_horizons=HORIZONS_MINUTES,
        allow_fallback=False,
    )
    costs = CostParameters(
        taker_fee_bps_per_side=args.taker_fee_bps_per_side,
        funding_cost_bps_per_8h=args.funding_cost_bps_per_8h,
    )
    repository = ExtremeMoverReplayRepository.from_url(db_url)
    try:
        database_snapshot_at, loaded = await repository.load(filters)
    finally:
        await repository.close()
    decisions = tuple(row for row in loaded if row.strategy_version in STRATEGY_VERSIONS)
    report = build_report(
        decisions,
        dataset_since=args.since,
        dataset_until_exclusive=until,
        database_snapshot_at=database_snapshot_at,
        generated_at=generated_at,
        code_revision=args.code_revision,
        working_tree_dirty=args.working_tree_dirty,
        costs=costs,
        bootstrap_iterations=args.bootstrap_iterations,
        bootstrap_seed=args.bootstrap_seed,
    )
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    sys.stdout.write(asyncio.run(_run(_parser().parse_args())))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
