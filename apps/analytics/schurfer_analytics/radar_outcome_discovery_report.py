"""First WATCH decision -> exact +25% move discovery foundation.

The historical window is already viewed and therefore discovery-only. This
report replaces the invalid pump-event denominator with every point-in-time
WATCH decision, enters no earlier than the first full native minute after the
recorded decision timestamp, and compares it with the same instrument and UTC
time on a quiet day. It may freeze one long-side forward candidate; it cannot
authorize paper or live trading.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from .cex_activity_discovery import (
    CONTROL_QUIET_HOURS,
    CONTROL_SEARCH_DAYS,
    MATCHING_POLICY_VERSION,
    OUTCOME_HORIZON_MINUTES,
    PRIMARY_MOVE_PCT,
    DirectionMoveResult,
    OutcomeSignalEpisode,
    build_control_requests,
    build_direction_results,
    input_fingerprint,
    select_matched_pairs,
    signal_request,
)
from .cex_activity_discovery_repository import (
    PATH_QUERY_VERSION,
    CexActivityDiscoveryRepository,
    report_maturity_at,
)
from .momentum_flow_watch_contract import FROZEN_WATCH_CONTRACT, WATCH_CONTRACT_SHA256
from .radar_outcome_discovery_repository import (
    WATCH_QUERY_VERSION,
    RadarOutcomeDiscoveryRepository,
)
from .reporting import json_ready, markdown_table, normalize_code_revision, parse_utc_datetime

if TYPE_CHECKING:
    from datetime import datetime

REPORT_VERSION = "radar_outcome_discovery_report_v1"
INTERPRETATION = "viewed_discovery_only_max_one_forward_candidate_no_trading_authorization"
REGISTERED_DIRECTIONS = ("buy",)
# Colleague review, 2026-09-01: fetch_watch_signals had no post-fetch bound,
# unlike cex_activity_discovery_report.py's own check_candidate_count for
# the analogous burst-minute scan. momentum_flow_watch_evaluations_1m is
# already filtered to quality_ready/raw_qualified WATCH rows (much sparser
# than the raw bars table -- HYP-017's own real run saw 1,273 decisions
# over a 9-day window), so this default has generous headroom, but the
# guard exists for the same reason check_candidate_count does: fail loudly
# on a genuinely unexpected result size rather than silently evaluating
# whatever came back.
DEFAULT_MAX_WATCH_DECISIONS = 200_000


@dataclass(frozen=True)
class RadarOutcomeManifest:
    report_version: str
    interpretation: str
    watch_query_version: str
    path_query_version: str
    watch_version: str
    watch_contract_sha256: str
    code_revision: str
    working_tree_dirty: bool
    generated_at: datetime
    since: datetime
    until_exclusive: datetime
    primary_move_pct: float
    outcome_horizon_minutes: int
    control_search_days: int
    control_quiet_hours: int
    matching_policy_version: str
    input_fingerprint: str


@dataclass(frozen=True)
class RadarOutcomeFunnel:
    watch_decisions: int
    resolved_signal_paths: int
    generated_control_candidates: int
    resolved_control_paths: int
    matched_pairs: int


@dataclass(frozen=True)
class RadarOutcomeReport:
    manifest: RadarOutcomeManifest
    funnel: RadarOutcomeFunnel
    result: DirectionMoveResult
    selected_forward_candidate: str | None
    caveats: tuple[str, ...]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="First prospective WATCH -> exact +25% discovery foundation"
    )
    parser.add_argument("--since", type=parse_utc_datetime, required=True)
    parser.add_argument("--until", type=parse_utc_datetime, required=True)
    parser.add_argument(
        "--max-watch-decisions",
        type=int,
        default=DEFAULT_MAX_WATCH_DECISIONS,
        help=(
            f"default {DEFAULT_MAX_WATCH_DECISIONS}; fails loudly rather than silently "
            "evaluating an unexpectedly large result"
        ),
    )
    parser.add_argument("--code-revision", required=True)
    dirty = parser.add_mutually_exclusive_group(required=True)
    dirty.add_argument("--working-tree-dirty", action="store_true")
    dirty.add_argument("--no-working-tree-dirty", action="store_true")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser


def check_watch_decision_count(count: int, max_watch_decisions: int) -> None:
    if count > max_watch_decisions:
        raise ValueError(
            f"window produced {count} WATCH decisions, over "
            f"--max-watch-decisions={max_watch_decisions}; narrow --since/--until "
            "or raise --max-watch-decisions explicitly rather than silently evaluating "
            "an unexpectedly large result"
        )


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}%"


def render_markdown(report: RadarOutcomeReport) -> str:
    row = report.result
    estimate = row.estimate
    interval = (
        "n/a"
        if estimate is None
        else f"[{estimate.lower_bound * 100:.2f}%, {estimate.upper_bound * 100:.2f}%]"
    )
    lines = [
        "# WATCH Radar → Exact 25% Move Discovery",
        "",
        "Discovery only; the viewed historical window cannot confirm a strategy.",
        "",
        f"Window: `{report.manifest.since.isoformat()}` → "
        f"`{report.manifest.until_exclusive.isoformat()}`",
        f"WATCH contract: `{report.manifest.watch_version}` / "
        f"`{report.manifest.watch_contract_sha256}`",
        f"Input fingerprint: `{report.manifest.input_fingerprint}`",
        "",
        "## Funnel",
        "",
        *markdown_table(
            ("Stage", "Count"),
            [
                ("Point-in-time WATCH decisions", report.funnel.watch_decisions),
                ("Resolved exact 24h signal paths", report.funnel.resolved_signal_paths),
                ("Generated quiet control candidates", report.funnel.generated_control_candidates),
                ("Resolved quiet control paths", report.funnel.resolved_control_paths),
                ("Matched signal/control pairs", report.funnel.matched_pairs),
            ],
        ),
        "",
        "## Frozen primary comparison",
        "",
        *markdown_table(
            (
                "Pairs",
                "Assets",
                "Weeks",
                "WATCH +25% hit",
                "Control +25% hit",
                "Paired delta",
                "95% cluster CI",
                "p",
                "Verdict",
            ),
            [
                (
                    row.paired_episodes,
                    row.clusters,
                    row.utc_weeks,
                    _fmt_pct(row.signal_hit_rate_pct),
                    _fmt_pct(row.control_hit_rate_pct),
                    _fmt_pct(row.paired_hit_rate_delta_pct),
                    interval,
                    "n/a"
                    if row.holm_adjusted_p_value is None
                    else f"{row.holm_adjusted_p_value:.4f}",
                    row.verdict,
                )
            ],
        ),
        "",
        "Selected forward candidate: "
        + ("`watch_long_v1`" if report.selected_forward_candidate else "none"),
        "",
        "## Caveats",
        "",
        *(f"- {item}" for item in report.caveats),
    ]
    return "\n".join(lines) + "\n"


def render_json(report: RadarOutcomeReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True)


async def generate_report(args: argparse.Namespace) -> RadarOutcomeReport:
    if args.since >= args.until:
        raise ValueError("--since must be earlier than --until")
    code_revision = normalize_code_revision(args.code_revision)
    watch_repository = RadarOutcomeDiscoveryRepository.from_url(os.environ["DATABASE_URL"])
    path_repository = CexActivityDiscoveryRepository.from_url(os.environ["DATABASE_URL"])
    try:
        database_now = await path_repository.database_now()
        maturity_at = report_maturity_at(args.until)
        if database_now < maturity_at:
            raise ValueError(
                f"report window is immature: database now {database_now.isoformat()}, "
                f"requires at least {maturity_at.isoformat()}"
            )
        contract = FROZEN_WATCH_CONTRACT
        watch_signals = await watch_repository.fetch_watch_signals(
            exchange=contract.source_exchange,
            market_type=contract.market_type,
            capture_version=contract.capture_version,
            watch_version=contract.watch_version,
            since=args.since,
            until=args.until,
            # +1 so the returned count alone can distinguish "exactly at
            # the cap" from "genuinely over it" -- the SQL LIMIT itself
            # already bounds the DB scan/network/memory cost, this local
            # count check only decides whether to raise.
            limit=args.max_watch_decisions + 1,
        )
        check_watch_decision_count(len(watch_signals), args.max_watch_decisions)
        episodes = tuple(
            OutcomeSignalEpisode(
                episode_id=index,
                signal_id=signal.watch_id,
                source=contract.watch_version,
                exchange=signal.exchange,
                symbol=signal.symbol,
                direction="buy",
                trigger_at=signal.decision_at,
                entry_at=signal.entry_at,
                signal_value=signal.oi_growth_60m_pct,
            )
            for index, signal in enumerate(watch_signals, start=1)
        )
        signal_paths = await path_repository.fetch_exact_paths(
            exchange=contract.source_exchange,
            market_type=contract.market_type,
            capture_version=contract.capture_version,
            requests=tuple(signal_request(episode) for episode in episodes),
        )
        controls_by_episode = build_control_requests(
            episodes,
            since=args.since,
            until=args.until,
        )
        control_requests = tuple(
            request for requests in controls_by_episode.values() for request in requests
        )
        control_paths = await path_repository.fetch_exact_paths(
            exchange=contract.source_exchange,
            market_type=contract.market_type,
            capture_version=contract.capture_version,
            requests=control_requests,
        )
        pairs = select_matched_pairs(
            episodes,
            signal_paths=signal_paths,
            control_requests=controls_by_episode,
            control_paths=control_paths,
        )
        (result,) = build_direction_results(
            episodes,
            pairs,
            signal_paths,
            registered_directions=REGISTERED_DIRECTIONS,
        )
        fingerprint = input_fingerprint(episodes, signal_paths, control_paths)
        selected = "watch_long_v1" if result.verdict == "forward_candidate" else None
        return RadarOutcomeReport(
            manifest=RadarOutcomeManifest(
                report_version=REPORT_VERSION,
                interpretation=INTERPRETATION,
                watch_query_version=WATCH_QUERY_VERSION,
                path_query_version=PATH_QUERY_VERSION,
                watch_version=contract.watch_version,
                watch_contract_sha256=WATCH_CONTRACT_SHA256,
                code_revision=code_revision,
                working_tree_dirty=args.working_tree_dirty,
                generated_at=database_now,
                since=args.since,
                until_exclusive=args.until,
                primary_move_pct=PRIMARY_MOVE_PCT,
                outcome_horizon_minutes=OUTCOME_HORIZON_MINUTES,
                control_search_days=CONTROL_SEARCH_DAYS,
                control_quiet_hours=CONTROL_QUIET_HOURS,
                matching_policy_version=MATCHING_POLICY_VERSION,
                input_fingerprint=fingerprint,
            ),
            funnel=RadarOutcomeFunnel(
                watch_decisions=len(episodes),
                resolved_signal_paths=sum(path.resolved for path in signal_paths.values()),
                generated_control_candidates=len(control_requests),
                resolved_control_paths=sum(path.resolved for path in control_paths.values()),
                matched_pairs=len(pairs),
            ),
            result=result,
            selected_forward_candidate=selected,
            caveats=(
                "WATCH decisions and thresholds were prospective, but this outcome window "
                "has already been viewed and is discovery-only.",
                "The next full minute avoids entering before decision_at, but OHLCV high/low "
                "is still not executable bid/ask evidence.",
                "Quiet controls match exact instrument and UTC time, not BTC regime, funding, "
                "spread or order-book depth.",
                "A selected candidate must be frozen before an untouched forward quote-capture "
                "cohort begins.",
            ),
        )
    finally:
        await asyncio.gather(watch_repository.close(), path_repository.close())


def main() -> None:
    args = build_parser().parse_args()
    if not os.getenv("DATABASE_URL"):
        raise ValueError("DATABASE_URL is required")
    report = asyncio.run(generate_report(args))
    sys.stdout.write(render_json(report) if args.format == "json" else render_markdown(report))
