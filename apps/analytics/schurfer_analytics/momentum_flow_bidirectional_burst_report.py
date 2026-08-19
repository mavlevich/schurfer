"""CLI/report layer for analysis/momentum-flow-bidirectional-burst-study-v1.

Wires momentum_flow_bidirectional_burst_repository's real-Postgres queries
into momentum_flow_bidirectional_burst_study's pure computation, then
renders markdown/json. Discovery-level only: the thresholds below
(EXTREME_THRESHOLD_PCT, REFRACTORY_MINUTES, MIN_VOLUME_24H_USD) are this
report's own provisional scan parameters, not the frozen v1 WATCH contract
(momentum_flow_watch_contract.py) -- running this, or reading its output,
does not authorize any change to that contract or to any live paper/WATCH
threshold.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from .momentum_flow_bidirectional_burst_study import (
    DIRECTIONS,
    OUTCOME_HORIZONS_MINUTES,
    PRECURSOR_LOOKBACK_MINUTES,
    BurstEpisode,
    HorizonEconomics,
    build_horizon_economics,
    compute_episode_outcomes,
    decluster_episodes,
)
from .momentum_flow_capture_contract import (
    BYBIT_MOMENTUM_CAPTURE_VERSION,
    BYBIT_MOMENTUM_EXCHANGE,
    BYBIT_MOMENTUM_MARKET_TYPE,
)
from .reporting import horizon_label, parse_utc_datetime
from .reporting import json_ready as _json_ready
from .reporting import markdown_table as _table

parse_datetime = parse_utc_datetime

# Provisional discovery-scan parameters -- not a frozen contract, tune freely
# per run via CLI flags. 10% of a symbol's own trailing-24h notional volume
# concentrated into one real 5-minute window is a strong concentration
# signal without being so high it only fires on the handful of names that
# already pumped hardest (the selection-bias failure mode this rebuild
# specifically set out to fix -- see the module's own doc comment).
DEFAULT_EXTREME_THRESHOLD_PCT = 10.0
DEFAULT_REFRACTORY_MINUTES = 60
# A liquidity floor, not a signal parameter: excludes near-dead symbols
# where a tiny absolute trade can register as a huge relative "burst" for
# reasons that have nothing to do with genuine order-flow concentration.
DEFAULT_MIN_VOLUME_24H_USD = 50_000.0
DEFAULT_MAX_CANDIDATE_MINUTES = 200_000


@dataclass(frozen=True)
class BurstStudyWindow:
    since: datetime
    until: datetime
    extreme_threshold_pct: float
    refractory_minutes: int
    min_volume_24h_usd: float

    def __post_init__(self) -> None:
        if self.since >= self.until:
            raise ValueError("since must be earlier than until")
        if self.extreme_threshold_pct <= 0:
            raise ValueError("extreme_threshold_pct must be positive")
        if self.refractory_minutes <= 0:
            raise ValueError("refractory_minutes must be positive")
        if self.min_volume_24h_usd < 0:
            raise ValueError("min_volume_24h_usd must not be negative")


@dataclass(frozen=True)
class DirectionSummary:
    direction: str
    episodes: int
    clusters: int
    weeks: int
    resolved_outcomes: int


@dataclass(frozen=True)
class BidirectionalBurstReport:
    generated_at: datetime
    exchange: str
    window: BurstStudyWindow
    candidate_minutes: int
    directions: tuple[DirectionSummary, ...]
    economics: tuple[HorizonEconomics, ...]


def render_json(report: BidirectionalBurstReport) -> str:
    return json.dumps(_json_ready(asdict(report)), indent=2, sort_keys=True)


def render_markdown(report: BidirectionalBurstReport) -> str:
    lines = [
        "# Bidirectional Buy/Sell Volume-Burst Discovery Study",
        "",
        "Discovery-level only. Does not authorize any change to the live",
        "WATCH/paper contract regardless of verdict below. Net economics are",
        "fees + funding only (entry/exit slippage = 0.0 bps, `costs_partial`)",
        "-- no real bid/ask slippage/impact model backs these figures.",
        "",
        f"Generated: {report.generated_at.isoformat()}",
        f"Exchange: {report.exchange}",
        f"Window: {report.window.since.isoformat()} to {report.window.until.isoformat()}",
        f"Extreme threshold: {report.window.extreme_threshold_pct:.1f}% of trailing 24h volume "
        "in a real 5-minute window",
        f"Refractory: {report.window.refractory_minutes}min",
        f"Min 24h volume: ${report.window.min_volume_24h_usd:,.0f}",
        "",
        f"Candidate extreme minutes scanned: {report.candidate_minutes}",
        "",
        "## Episodes by direction",
        "",
    ]
    lines.extend(
        _table(
            ("Direction", "Episodes", "Clusters (symbols)", "UTC weeks", "Resolved outcomes"),
            [
                (d.direction, d.episodes, d.clusters, d.weeks, d.resolved_outcomes)
                for d in report.directions
            ],
        )
    )
    lines.extend(["", "## After-cost economics by horizon x side", ""])
    rows = []
    for e in report.economics:
        readiness = "n/a (n<2)"
        verdict = "n/a"
        if e.inference is not None:
            readiness = e.inference.readiness.status
            if e.inference.challengers:
                verdict = e.inference.challengers[0].verdict
        rows.append(
            (
                horizon_label(e.horizon_minutes),
                e.side,
                e.n,
                f"{e.mean_gross_pct:.2f}%",
                f"{e.mean_net_pct:.2f}%",
                f"{e.win_rate_pct:.1f}%",
                "n/a" if e.baseline_mean_gross_pct is None else f"{e.baseline_mean_gross_pct:.2f}%",
                readiness,
                verdict,
            )
        )
    lines.extend(
        _table(
            (
                "Horizon",
                "Side",
                "N",
                "Mean gross",
                "Mean net",
                "Win rate",
                "Baseline (same-symbol)",
                "Readiness",
                "Verdict",
            ),
            rows,
        )
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Bidirectional buy/sell volume-burst discovery study over already-captured "
            "bars. Discovery-level only -- does not authorize any WATCH/paper contract "
            "change."
        )
    )
    parser.add_argument(
        "--since", type=parse_datetime, required=True, help="inclusive UTC ISO-8601"
    )
    parser.add_argument(
        "--until", type=parse_datetime, required=True, help="exclusive UTC ISO-8601"
    )
    parser.add_argument(
        "--exchange", default=BYBIT_MOMENTUM_EXCHANGE, help=f"default {BYBIT_MOMENTUM_EXCHANGE}"
    )
    parser.add_argument("--market-type", default=BYBIT_MOMENTUM_MARKET_TYPE)
    parser.add_argument("--capture-version", default=BYBIT_MOMENTUM_CAPTURE_VERSION)
    parser.add_argument(
        "--extreme-threshold-pct",
        type=float,
        default=DEFAULT_EXTREME_THRESHOLD_PCT,
        help=f"default {DEFAULT_EXTREME_THRESHOLD_PCT}",
    )
    parser.add_argument(
        "--refractory-minutes",
        type=int,
        default=DEFAULT_REFRACTORY_MINUTES,
        help=f"default {DEFAULT_REFRACTORY_MINUTES}",
    )
    parser.add_argument(
        "--min-volume-24h-usd",
        type=float,
        default=DEFAULT_MIN_VOLUME_24H_USD,
        help=f"default {DEFAULT_MIN_VOLUME_24H_USD}",
    )
    parser.add_argument(
        "--max-candidate-minutes",
        type=int,
        default=DEFAULT_MAX_CANDIDATE_MINUTES,
        help=(
            f"default {DEFAULT_MAX_CANDIDATE_MINUTES}; fails loudly rather than silently truncating"
        ),
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser


def check_candidate_count(count: int, max_candidate_minutes: int) -> None:
    if count > max_candidate_minutes:
        raise ValueError(
            f"window produced {count} candidate extreme minutes, over "
            f"--max-candidate-minutes={max_candidate_minutes}; narrow --since/--until, "
            "raise --extreme-threshold-pct, or raise --max-candidate-minutes explicitly "
            "rather than silently evaluating a truncated scan"
        )


async def _run(args: argparse.Namespace) -> str:
    from .momentum_flow_bidirectional_burst_repository import (
        MomentumFlowBidirectionalBurstRepository,
    )

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for bidirectional-burst-study-report")

    window = BurstStudyWindow(
        since=args.since,
        until=args.until,
        extreme_threshold_pct=args.extreme_threshold_pct,
        refractory_minutes=args.refractory_minutes,
        min_volume_24h_usd=args.min_volume_24h_usd,
    )

    repository = MomentumFlowBidirectionalBurstRepository.from_url(db_url)
    try:
        candidate_minutes = await repository.fetch_candidate_extreme_minutes(
            exchange=args.exchange,
            market_type=args.market_type,
            capture_version=args.capture_version,
            since=window.since,
            until=window.until,
            min_volume_24h_usd=window.min_volume_24h_usd,
            extreme_threshold_pct=window.extreme_threshold_pct,
        )
        check_candidate_count(len(candidate_minutes), args.max_candidate_minutes)

        all_episodes: list[BurstEpisode] = []
        episodes_by_direction: dict[str, tuple[BurstEpisode, ...]] = {}
        # decluster_episodes' own episode_id restarts at 1 on every call --
        # buy and sell are always two separate calls here, so start_id must
        # be threaded across them or a buy episode and a sell episode can
        # share an id, which build_horizon_economics' downstream
        # ChallengerEpisode uniqueness check rejects outright (found running
        # this against real prod data, not a hypothetical).
        next_episode_id = 1
        for direction in DIRECTIONS:
            episodes = decluster_episodes(
                candidate_minutes,
                direction=direction,
                threshold_pct=window.extreme_threshold_pct,
                refractory_minutes=window.refractory_minutes,
                start_id=next_episode_id,
            )
            episodes_by_direction[direction] = episodes
            all_episodes.extend(episodes)
            next_episode_id += len(episodes)

        symbol_timestamps: set[tuple[str, datetime]] = set()
        for episode in all_episodes:
            symbol_timestamps.add((episode.symbol, episode.trigger_at))
            symbol_timestamps.add(
                (episode.symbol, episode.trigger_at - timedelta(minutes=PRECURSOR_LOOKBACK_MINUTES))
            )
            for horizon in OUTCOME_HORIZONS_MINUTES:
                symbol_timestamps.add(
                    (episode.symbol, episode.trigger_at + timedelta(minutes=horizon))
                )
        symbols = sorted({episode.symbol for episode in all_episodes})

        # Independent of each other (one derives from symbol_timestamps, the
        # other from symbols/window) -- fetched concurrently over the
        # repository's own pool_size=2 connection pool rather than back to
        # back, since a large window can make either one a real wait.
        price_at, baseline = await asyncio.gather(
            repository.fetch_prices_at(
                exchange=args.exchange,
                market_type=args.market_type,
                capture_version=args.capture_version,
                symbol_timestamps=tuple(symbol_timestamps),
            ),
            repository.fetch_symbol_baseline_forward_returns(
                exchange=args.exchange,
                market_type=args.market_type,
                capture_version=args.capture_version,
                since=window.since,
                until=window.until,
                symbols=symbols,
                horizons_minutes=list(OUTCOME_HORIZONS_MINUTES),
            ),
        )

        outcomes_by_direction = {
            direction: compute_episode_outcomes(episodes, price_at)
            for direction, episodes in episodes_by_direction.items()
        }
        all_outcomes = tuple(
            outcome for outcomes in outcomes_by_direction.values() for outcome in outcomes
        )

        economics = build_horizon_economics(all_outcomes, baseline)

        direction_summaries = tuple(
            DirectionSummary(
                direction=direction,
                episodes=len(episodes_by_direction[direction]),
                clusters=len({e.cluster_key for e in episodes_by_direction[direction]}),
                weeks=len({e.week_key for e in episodes_by_direction[direction]}),
                resolved_outcomes=len(outcomes_by_direction[direction]),
            )
            for direction in DIRECTIONS
        )

        report = BidirectionalBurstReport(
            generated_at=datetime.now(UTC),
            exchange=args.exchange,
            window=window,
            candidate_minutes=len(candidate_minutes),
            directions=direction_summaries,
            economics=economics,
        )
    finally:
        await repository.close()
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    args = build_parser().parse_args()
    sys.stdout.write(asyncio.run(_run(args)))
