"""Prospective 14d/21d/28d no-time-exit margin-buffer research."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime

from .derivatives_context_resolver import OPEN_ENDED_MARGIN_FUNDING_RESOLVER_VERSION
from .long_horizon_funding_repository import LongHorizonFundingRepository
from .long_horizon_report import (
    LONG_HORIZON_STRATEGY_VERSIONS,
    build_long_horizon_dataset,
    build_long_horizon_report,
    render_json,
    render_markdown,
    select_long_horizon_decision,
)
from .outcomes import EXTENDED_HORIZONS_MINUTES, RESOLVER_VERSION
from .replay import ReplayFilters
from .replay_repository import ReplayRepository
from .reporting import normalize_code_revision, parse_utc_datetime
from .virtual_strategy import DEFAULT_COSTS

OPEN_ENDED_MARGIN_REPORT_VERSION = "open_ended_margin_report_v1"
OPEN_ENDED_MARGIN_ELIGIBILITY_VERSION = "prospective_no_time_exit_margin_buffer_v1"
OPEN_ENDED_MARGIN_COHORT_START = datetime(2026, 8, 3, tzinfo=UTC)
OPEN_ENDED_MARGIN_HORIZONS = EXTENDED_HORIZONS_MINUTES


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Describe prospective 14d, 21d, and 28d no-time-exit paths, signed "
            "funding, and collateral-buffer survival"
        )
    )
    parser.add_argument(
        "--since",
        type=parse_utc_datetime,
        default=OPEN_ENDED_MARGIN_COHORT_START,
    )
    parser.add_argument("--until", type=parse_utc_datetime)
    parser.add_argument(
        "--taker-fee-bps-per-side",
        type=float,
        default=DEFAULT_COSTS.taker_fee_bps_per_side,
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
    generated_at = datetime.now(UTC)
    until = args.until or generated_at
    if args.since != OPEN_ENDED_MARGIN_COHORT_START:
        raise ValueError(
            f"open-ended margin report is locked to {OPEN_ENDED_MARGIN_COHORT_START.isoformat()}"
        )
    if args.since >= until:
        raise ValueError("since must be earlier than until")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for open-ended-margin-report")
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    filters = ReplayFilters(
        since=args.since,
        until=until,
        strategy_versions=LONG_HORIZON_STRATEGY_VERSIONS,
        resolver_version=RESOLVER_VERSION,
        required_horizons=OPEN_ENDED_MARGIN_HORIZONS,
        allow_fallback=False,
    )
    replay_repository = ReplayRepository.from_url(db_url)
    funding_repository = LongHorizonFundingRepository.from_url(
        db_url,
        resolver_version=OPEN_ENDED_MARGIN_FUNDING_RESOLVER_VERSION,
    )
    try:
        decisions = await replay_repository.load(filters)
        dataset = build_long_horizon_dataset(decisions, filters)
        keys = tuple(
            (
                episode.pump_event_id,
                select_long_horizon_decision(episode).exchange,
            )
            for episode in dataset.eligible_episodes
        )
        funding_series = await funding_repository.load(keys)
    finally:
        await asyncio.gather(
            replay_repository.close(),
            funding_repository.close(),
        )
    report = build_long_horizon_report(
        dataset,
        filters,
        funding_series,
        generated_at=generated_at,
        code_revision=normalize_code_revision(args.code_revision),
        working_tree_dirty=args.working_tree_dirty,
        taker_fee_bps_per_side=args.taker_fee_bps_per_side,
        required_horizons=OPEN_ENDED_MARGIN_HORIZONS,
        report_version=OPEN_ENDED_MARGIN_REPORT_VERSION,
        eligibility_version=OPEN_ENDED_MARGIN_ELIGIBILITY_VERSION,
        funding_resolver_version=OPEN_ENDED_MARGIN_FUNDING_RESOLVER_VERSION,
    )
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    parser = build_parser()
    try:
        sys.stdout.write(asyncio.run(_run(parser.parse_args())))
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
