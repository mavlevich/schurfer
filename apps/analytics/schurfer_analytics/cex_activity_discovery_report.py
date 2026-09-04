"""CEXTrack-like activity -> exact +25% move discovery report.

The primary family is frozen in code before this report's first real run:

* buy burst -> long-side +25% favorable excursion within 24h;
* sell burst -> short-side +25% favorable excursion within 24h.

Both use a 10% five-minute directional-notional share of the instrument's
own strictly complete trailing 24h volume, a $50k 24h liquidity floor, a
60-minute refractory period, entry at the next exact native one-minute bar
open, and a same-instrument/same-UTC-time quiet-day control.  Holm correction
covers the two registered directions.  The viewed discovery window can
nominate at most one direction for a later untouched forward shadow cohort.

Bar-open/high/low paths are market-outcome proxies, not executable quotes.
This report cannot authorize paper or live trading.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from .cex_activity_discovery import (
    CONTROL_BOUNDARY_POLICY_VERSION,
    CONTROL_QUIET_HOURS,
    CONTROL_SEARCH_DAYS,
    DISCOVERY_SINCE,
    DISCOVERY_UNTIL,
    HYPOTHESIS_ID,
    MATCHING_POLICY_VERSION,
    OUTCOME_HORIZON_MINUTES,
    PRIMARY_MOVE_PCT,
    DirectionMoveResult,
    OutcomeSignalEpisode,
    build_control_requests,
    build_direction_results,
    input_fingerprint,
    select_forward_candidate,
    select_matched_pairs,
    signal_request,
)
from .cex_activity_discovery_repository import (
    PATH_QUERY_VERSION,
    CexActivityDiscoveryRepository,
    report_maturity_at,
)
from .momentum_flow_bidirectional_burst_report import (
    DEFAULT_EXTREME_THRESHOLD_PCT,
    DEFAULT_MAX_CANDIDATE_MINUTES,
    DEFAULT_MIN_VOLUME_24H_USD,
    DEFAULT_REFRACTORY_MINUTES,
    check_candidate_count,
)
from .momentum_flow_bidirectional_burst_repository import (
    MomentumFlowBidirectionalBurstRepository,
)
from .momentum_flow_bidirectional_burst_study import DIRECTIONS, BurstEpisode, decluster_episodes
from .momentum_flow_capture_contract import (
    BYBIT_MOMENTUM_CAPTURE_VERSION,
    BYBIT_MOMENTUM_EXCHANGE,
    BYBIT_MOMENTUM_MARKET_TYPE,
)
from .reporting import json_ready, markdown_table, normalize_code_revision, parse_utc_datetime

if TYPE_CHECKING:
    from datetime import datetime

REPORT_VERSION = "cex_activity_discovery_report_v1"
CANDIDATE_QUERY_VERSION = "strict_5_of_5_and_1440_of_1440_range_windows_v1"
INTERPRETATION = "discovery_only_max_one_forward_candidate_no_trading_authorization"


@dataclass(frozen=True)
class CexActivityManifest:
    hypothesis_id: str
    report_version: str
    candidate_query_version: str
    path_query_version: str
    interpretation: str
    code_revision: str
    working_tree_dirty: bool
    generated_at: datetime
    database_snapshot_at: datetime
    since: datetime
    until_exclusive: datetime
    exchange: str
    market_type: str
    capture_version: str
    extreme_threshold_pct: float
    refractory_minutes: int
    min_volume_24h_usd: float
    primary_move_pct: float
    outcome_horizon_minutes: int
    control_search_days: int
    control_quiet_hours: int
    control_boundary_policy_version: str
    matching_policy_version: str
    input_fingerprint: str


@dataclass(frozen=True)
class CexActivityFunnel:
    """`unmatched_resolved_signal_episodes` (colleague review, 2026-09-03,
    research/cex-activity-discovery-completion-v1 planning): an episode
    whose own signal path resolved but never made it into a matched pair
    (no resolved control candidate existed, or it lost the maximum-
    cardinality assignment to another episode) previously only showed up
    as an implicit gap between resolved_signal_paths and matched_pairs --
    easy to miss, and impossible to distinguish from an episode whose
    signal itself never resolved. Counted explicitly here instead."""

    candidate_extreme_minutes: int
    independent_episodes: int
    resolved_signal_paths: int
    generated_control_candidates: int
    resolved_control_paths: int
    matched_pairs: int
    unmatched_resolved_signal_episodes: int


@dataclass(frozen=True)
class CexActivityDiscoveryReport:
    manifest: CexActivityManifest
    funnel: CexActivityFunnel
    directions: tuple[DirectionMoveResult, ...]
    selected_forward_candidate: str | None
    caveats: tuple[str, ...]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CEX activity -> exact +25% discovery with quiet-day matched controls"
    )
    # Defaulted to (and, in generate_report, validated against) the frozen
    # DISCOVERY_SINCE/DISCOVERY_UNTIL -- colleague review, 2026-09-03
    # (research/cex-activity-discovery-completion-v1 planning): this window
    # is already viewed and registered (docs/research/discovery-ledger.md's
    # own HYP-016 row), so accepting an arbitrary --since/--until here
    # would let a caller silently widen or shift the estimand after the
    # window was already chosen -- kept as real CLI arguments (not removed
    # outright) only so an invocation's own command line stays self-
    # documenting, mirroring source_lead_forward_cohort_report.py's own
    # established --since pattern.
    parser.add_argument("--since", type=parse_utc_datetime, default=DISCOVERY_SINCE)
    parser.add_argument("--until", type=parse_utc_datetime, default=DISCOVERY_UNTIL)
    parser.add_argument("--max-candidate-minutes", type=int, default=DEFAULT_MAX_CANDIDATE_MINUTES)
    parser.add_argument("--code-revision", required=True)
    dirty = parser.add_mutually_exclusive_group(required=True)
    dirty.add_argument("--working-tree-dirty", action="store_true")
    dirty.add_argument("--no-working-tree-dirty", action="store_true")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.set_defaults(
        exchange=BYBIT_MOMENTUM_EXCHANGE,
        market_type=BYBIT_MOMENTUM_MARKET_TYPE,
        capture_version=BYBIT_MOMENTUM_CAPTURE_VERSION,
        extreme_threshold_pct=DEFAULT_EXTREME_THRESHOLD_PCT,
        refractory_minutes=DEFAULT_REFRACTORY_MINUTES,
        min_volume_24h_usd=DEFAULT_MIN_VOLUME_24H_USD,
    )
    return parser


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}%"


def render_markdown(report: CexActivityDiscoveryReport) -> str:
    manifest = report.manifest
    lines = [
        "# CEX Activity → Exact 25% Move Discovery",
        "",
        "Discovery only. This report may nominate at most one direction for a new",
        "untouched forward shadow cohort; it cannot authorize paper/live trading.",
        "",
        f"Window: `{manifest.since.isoformat()}` → `{manifest.until_exclusive.isoformat()}`",
        f"Exact route: `{manifest.exchange}` / `{manifest.market_type}` / "
        f"`{manifest.capture_version}`",
        f"Signal: ≥{manifest.extreme_threshold_pct:.1f}% of strict trailing 24h volume "
        "inside five complete one-minute bars",
        f"Primary outcome: {manifest.primary_move_pct:.1f}% favorable excursion within "
        f"{manifest.outcome_horizon_minutes // 60}h from next-bar open",
        f"Input fingerprint: `{manifest.input_fingerprint}`",
        "",
        "## Funnel",
        "",
    ]
    lines.extend(
        markdown_table(
            ("Stage", "Count"),
            [
                ("Candidate extreme minutes", report.funnel.candidate_extreme_minutes),
                ("Independent episodes", report.funnel.independent_episodes),
                ("Resolved signal paths", report.funnel.resolved_signal_paths),
                ("Generated control candidates", report.funnel.generated_control_candidates),
                ("Resolved control paths", report.funnel.resolved_control_paths),
                ("Matched pairs", report.funnel.matched_pairs),
                (
                    "Resolved signal, unmatched",
                    report.funnel.unmatched_resolved_signal_episodes,
                ),
            ],
        )
    )
    lines.extend(["", "## Registered directional family", ""])
    rows = []
    for row in report.directions:
        estimate = row.estimate
        interval = (
            "n/a"
            if estimate is None
            else f"[{estimate.lower_bound * 100:.2f}%, {estimate.upper_bound * 100:.2f}%]"
        )
        rows.append(
            (
                row.direction,
                row.eligible_episodes,
                row.paired_episodes,
                row.clusters,
                row.utc_weeks,
                _fmt_pct(row.signal_hit_rate_pct),
                _fmt_pct(row.control_hit_rate_pct),
                _fmt_pct(row.paired_hit_rate_delta_pct),
                interval,
                "n/a" if row.holm_adjusted_p_value is None else f"{row.holm_adjusted_p_value:.4f}",
                row.readiness,
                row.verdict,
            )
        )
    lines.extend(
        markdown_table(
            (
                "Direction",
                "Episodes",
                "Pairs",
                "Assets",
                "Weeks",
                "Signal hit",
                "Control hit",
                "Paired delta",
                "95% cluster CI",
                "Holm p",
                "Readiness",
                "Verdict",
            ),
            rows,
        )
    )
    lines.extend(
        [
            "",
            "Selected forward candidate: "
            + (
                f"`{report.selected_forward_candidate}`"
                if report.selected_forward_candidate
                else "none"
            ),
            "",
            "## Caveats",
            "",
            *(f"- {item}" for item in report.caveats),
        ]
    )
    return "\n".join(lines) + "\n"


def render_json(report: CexActivityDiscoveryReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True)


async def generate_report(args: argparse.Namespace) -> CexActivityDiscoveryReport:
    if args.since != DISCOVERY_SINCE or args.until != DISCOVERY_UNTIL:
        raise ValueError(
            f"--since/--until must equal the frozen {HYPOTHESIS_ID} window "
            f"[{DISCOVERY_SINCE.isoformat()}, {DISCOVERY_UNTIL.isoformat()}); this contract "
            "registers exactly one already-viewed discovery window, not an arbitrary one"
        )
    code_revision = normalize_code_revision(args.code_revision)
    path_repository = CexActivityDiscoveryRepository.from_url(os.environ["DATABASE_URL"])
    burst_repository = MomentumFlowBidirectionalBurstRepository.from_url(os.environ["DATABASE_URL"])
    try:
        database_now = await path_repository.database_now()
        maturity_at = report_maturity_at(args.until)
        if database_now < maturity_at:
            raise ValueError(
                f"report window is immature: database now {database_now.isoformat()}, "
                f"requires at least {maturity_at.isoformat()}"
            )
        candidate_minutes = await burst_repository.fetch_candidate_extreme_minutes(
            exchange=args.exchange,
            market_type=args.market_type,
            capture_version=args.capture_version,
            since=args.since,
            until=args.until,
            min_volume_24h_usd=args.min_volume_24h_usd,
            extreme_threshold_pct=args.extreme_threshold_pct,
        )
        check_candidate_count(len(candidate_minutes), args.max_candidate_minutes)

        all_episodes: list[BurstEpisode] = []
        next_id = 1
        for direction in DIRECTIONS:
            direction_episodes = decluster_episodes(
                candidate_minutes,
                direction=direction,
                threshold_pct=args.extreme_threshold_pct,
                refractory_minutes=args.refractory_minutes,
                start_id=next_id,
            )
            all_episodes.extend(direction_episodes)
            next_id += len(direction_episodes)
        burst_episodes = tuple(
            sorted(all_episodes, key=lambda item: (item.trigger_at, item.episode_id))
        )
        outcome_episodes = tuple(
            OutcomeSignalEpisode(
                episode_id=episode.episode_id,
                signal_id=f"{episode.exchange}:{episode.symbol}:{episode.direction}:"
                f"{episode.trigger_at.isoformat()}",
                source=f"cex_{episode.direction}_burst_v1",
                exchange=episode.exchange,
                symbol=episode.symbol,
                direction=episode.direction,
                trigger_at=episode.trigger_at,
                entry_at=episode.trigger_at + timedelta(minutes=1),
                signal_value=episode.peak_burst_pct,
            )
            for episode in burst_episodes
        )

        signal_requests = tuple(signal_request(episode) for episode in outcome_episodes)
        signal_paths = await path_repository.fetch_exact_paths(
            exchange=args.exchange,
            market_type=args.market_type,
            capture_version=args.capture_version,
            requests=signal_requests,
        )
        controls_by_episode = build_control_requests(
            outcome_episodes,
            since=args.since,
            until=args.until,
        )
        control_request_rows = tuple(
            request for rows in controls_by_episode.values() for request in rows
        )
        control_paths = await path_repository.fetch_exact_paths(
            exchange=args.exchange,
            market_type=args.market_type,
            capture_version=args.capture_version,
            requests=control_request_rows,
        )
        pairs = select_matched_pairs(
            outcome_episodes,
            signal_paths=signal_paths,
            control_requests=controls_by_episode,
            control_paths=control_paths,
        )
        direction_results = build_direction_results(outcome_episodes, pairs, signal_paths)
        fingerprint = input_fingerprint(outcome_episodes, signal_paths, control_paths)
        matched_episode_ids = {pair.episode.episode_id for pair in pairs}
        unmatched_resolved_signal_episodes = sum(
            1
            for episode in outcome_episodes
            if episode.episode_id not in matched_episode_ids
            and (path := signal_paths.get(signal_request(episode).request_id)) is not None
            and path.resolved
        )
        return CexActivityDiscoveryReport(
            manifest=CexActivityManifest(
                hypothesis_id=HYPOTHESIS_ID,
                report_version=REPORT_VERSION,
                candidate_query_version=CANDIDATE_QUERY_VERSION,
                path_query_version=PATH_QUERY_VERSION,
                interpretation=INTERPRETATION,
                code_revision=code_revision,
                working_tree_dirty=args.working_tree_dirty,
                generated_at=database_now,
                database_snapshot_at=database_now,
                since=args.since,
                until_exclusive=args.until,
                exchange=args.exchange,
                market_type=args.market_type,
                capture_version=args.capture_version,
                extreme_threshold_pct=args.extreme_threshold_pct,
                refractory_minutes=args.refractory_minutes,
                min_volume_24h_usd=args.min_volume_24h_usd,
                primary_move_pct=PRIMARY_MOVE_PCT,
                outcome_horizon_minutes=OUTCOME_HORIZON_MINUTES,
                control_search_days=CONTROL_SEARCH_DAYS,
                control_quiet_hours=CONTROL_QUIET_HOURS,
                control_boundary_policy_version=CONTROL_BOUNDARY_POLICY_VERSION,
                matching_policy_version=MATCHING_POLICY_VERSION,
                input_fingerprint=fingerprint,
            ),
            funnel=CexActivityFunnel(
                candidate_extreme_minutes=len(candidate_minutes),
                independent_episodes=len(outcome_episodes),
                resolved_signal_paths=sum(path.resolved for path in signal_paths.values()),
                generated_control_candidates=len(control_request_rows),
                resolved_control_paths=sum(path.resolved for path in control_paths.values()),
                matched_pairs=len(pairs),
                unmatched_resolved_signal_episodes=unmatched_resolved_signal_episodes,
            ),
            directions=direction_results,
            selected_forward_candidate=select_forward_candidate(direction_results),
            caveats=(
                "Next-bar OHLCV open/high/low is exact-native market data "
                "but not an executable bid/ask fill.",
                "Quiet controls match exact instrument and UTC time, but not "
                "order-book depth or BTC regime.",
                "The viewed discovery window cannot be reused as forward confirmation evidence.",
                "A selected direction requires a new prospective quote-capture "
                "shadow before paper trading.",
            ),
        )
    finally:
        await asyncio.gather(path_repository.close(), burst_repository.close())


def main() -> None:
    args = build_parser().parse_args()
    if not os.getenv("DATABASE_URL"):
        raise ValueError("DATABASE_URL is required")
    report = asyncio.run(generate_report(args))
    sys.stdout.write(render_json(report) if args.format == "json" else render_markdown(report))
