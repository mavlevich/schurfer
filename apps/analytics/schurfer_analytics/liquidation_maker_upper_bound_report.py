"""DB-fetch, CLI, and Markdown/JSON rendering for liquidation_maker_upper_
bound_v1 (research/liquidation-maker-upper-bound-v1).

Orchestrates `liquidation_maker_upper_bound.py`'s pure declustering/
resolution/verdict functions against real data: fetches per-minute
rolling trailing notional from Postgres (`LiquidationMakerUpperBoundRepository
.fetch_trigger_minutes`), resolves each cascade episode's own exact-venue
OHLCV (never a reconstructed symbol -- `native_market_id` is looked up in
each exchange's own loaded `markets_by_id` index, mirroring
`source_lead_capture.py`'s own established pattern, and only a candidate
whose `linear`/`swap`/`quote == USDT`/`settle == USDT` fields all match is
accepted), and reports two independent directions (long-liquidation ->
buy reversion, short-liquidation -> sell reversion) with their own
evidence floors and verdicts -- never pooled into one sample, per the
pure module's own frozen design.

## Sensitivity family scope

`SENSITIVITY_CASCADE_NOTIONAL_USD_FAMILY` (100k/250k/500k) is
pre-registered context, not three parallel formal reads: this report
computes full economics (net return, MFE/MAE, verdict) for the PRIMARY
threshold (250k) only, and reports EPISODE COUNTS ONLY for 100k/500k --
enough to see whether the primary threshold's episode population is a
reasonable, non-arbitrary point in the family, without tripling the
OHLCV fetch load for two thresholds whose own full economics are never
allowed to gate anything.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from .clustered_inference import ClusterObservation
from .exchange_registry import EXCHANGE_FACTORIES
from .liquidation_maker_upper_bound import (
    CONTRACT_VERSION,
    DIRECTIONS,
    EVIDENCE_FLOOR,
    INTERPRETATION,
    MAX_POSITION_HOLD_MINUTES,
    MAX_SINGLE_ASSET_EPISODE_SHARE,
    MAX_SINGLE_WEEK_EPISODE_SHARE,
    PRIMARY_CASCADE_NOTIONAL_USD,
    SENSITIVITY_CASCADE_NOTIONAL_USD_FAMILY,
    CascadeEpisode,
    EpisodeInputs,
    EpisodeResult,
    LiquidationTriggerMinute,
    decluster_cascade_episodes,
    formal_verdict,
    primary_sensitivity_ci,
    resolve_episode,
)
from .liquidation_maker_upper_bound_repository import (
    LiquidationMakerUpperBoundRepository,
    RawTriggerMinute,
    check_trigger_minute_count,
)
from .momentum_flow_bidirectional_burst_study import utc_week_key
from .ohlcv import fetch_symbol_candles
from .reporting import (
    canonical_json_array_fingerprint,
    json_ready,
    markdown_table,
    normalize_code_revision,
    parse_utc_datetime,
    profit_factor,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from .ohlcv import Candle

REPORT_VERSION = "liquidation_maker_upper_bound_report_v1"

# Generous headroom guard, this codebase's usual fail-loud-not-silently-
# large convention.
DEFAULT_MAX_TRIGGER_MINUTES = 500_000
DEFAULT_CANDLE_FETCH_CONCURRENCY = 8

# Padding around an episode's own [first_trigger_at, last_trigger_at +
# MAX_POSITION_HOLD_MINUTES] span so the exit-bar gap tolerance always has
# real candles available to check against.
_FETCH_PAD_MINUTES = 5


def check_native_market_id_ambiguous(candidates: int, native_market_id: str) -> None:
    if candidates > 1:
        raise ValueError(
            f"native_market_id {native_market_id!r} matched {candidates} candidate USDT "
            "linear swap markets on this exchange -- refusing to guess which one"
        )


def resolve_unified_symbol(exchange_client: Any, native_market_id: str) -> str | None:
    """Never reconstructs a symbol from a bare ticker (AI_RULES.md) --
    looks the market up by its native id via ccxt's own `markets_by_id`
    index (mirrors `source_lead_capture.py`'s own established pattern),
    and requires the match to be an unambiguous USDT-margined linear swap.
    Returns None (never guesses) when the id is absent or ambiguous."""
    markets_by_id = (
        exchange_client.markets_by_id if isinstance(exchange_client.markets_by_id, dict) else {}
    )
    candidates = markets_by_id.get(native_market_id)
    if isinstance(candidates, dict):
        candidates = [candidates]
    if not isinstance(candidates, list):
        return None
    matches = [
        market
        for market in candidates
        if isinstance(market, dict)
        and market.get("swap") is True
        and market.get("linear") is True
        and market.get("quote") == "USDT"
        and market.get("settle") == "USDT"
        and isinstance(market.get("symbol"), str)
    ]
    if not matches:
        return None
    check_native_market_id_ambiguous(len(matches), native_market_id)
    return str(matches[0]["symbol"])


@dataclass(frozen=True)
class SensitivityCount:
    cascade_notional_usd: float
    episodes: int


@dataclass(frozen=True)
class DirectionFunnel:
    trigger_minutes: int
    episodes_primary_threshold: int
    matured_episodes: int
    resolved_episodes: int
    unresolved_by_reason: dict[str, int]
    sensitivity_family: tuple[SensitivityCount, ...]


@dataclass(frozen=True)
class DirectionResult:
    resolved_episodes: int
    distinct_asset_clusters: int
    distinct_utc_weeks: int
    max_single_asset_share: float
    max_single_week_share: float
    median_net_return_pct: float | None
    mean_net_return_pct: float | None
    profit_factor: float | None
    win_rate: float | None
    median_mfe_pct: float | None
    median_mae_pct: float | None
    ci_lower_bound_pct: float | None
    ci_upper_bound_pct: float | None
    verdict: str


@dataclass(frozen=True)
class DirectionReport:
    position_side: str
    funnel: DirectionFunnel
    result: DirectionResult


@dataclass(frozen=True)
class ReportManifest:
    report_version: str
    contract_version: str
    interpretation: str
    trigger_minute_query_version: str
    code_revision: str
    working_tree_dirty: bool
    generated_at: datetime
    since: datetime
    until: datetime
    primary_cascade_notional_usd: float
    input_fingerprint: str


@dataclass(frozen=True)
class LiquidationMakerUpperBoundReport:
    manifest: ReportManifest
    directions: tuple[DirectionReport, ...]
    caveats: tuple[str, ...]


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


async def _fetch_episode_candles(
    clients: dict[str, Any],
    symbol_cache: dict[tuple[str, str], str | None],
    episode: CascadeEpisode,
    semaphore: asyncio.Semaphore,
) -> tuple[CascadeEpisode, tuple[Candle, ...]]:
    client = clients.get(episode.exchange)
    if client is None:
        return episode, ()
    cache_key = (episode.exchange, episode.native_market_id)
    if cache_key not in symbol_cache:
        symbol_cache[cache_key] = resolve_unified_symbol(client, episode.native_market_id)
    symbol = symbol_cache[cache_key]
    if symbol is None:
        return episode, ()

    start_ms = int(episode.first_trigger_at.timestamp() * 1000) - _FETCH_PAD_MINUTES * 60_000
    end_ms = (
        int(episode.last_trigger_at.timestamp() * 1000)
        + (MAX_POSITION_HOLD_MINUTES + _FETCH_PAD_MINUTES) * 60_000
    )
    async with semaphore:
        candles = await fetch_symbol_candles(client, symbol, start_ms, end_ms, use_cache=True)
    return episode, tuple(candles)


async def _resolve_direction(
    *,
    position_side: str,
    raw_minutes: Sequence[RawTriggerMinute],
    clients: dict[str, Any],
    symbol_cache: dict[tuple[str, str], str | None],
    concurrency: int,
    database_now: datetime,
) -> tuple[DirectionReport, list[dict[str, Any]]]:
    side_minutes = [row for row in raw_minutes if row.position_side == position_side]

    sensitivity_counts: list[SensitivityCount] = []
    for threshold in SENSITIVITY_CASCADE_NOTIONAL_USD_FAMILY:
        triggers = tuple(
            LiquidationTriggerMinute(
                row.exchange, row.native_market_id, row.position_side, row.bucket_start
            )
            for row in side_minutes
            if row.trailing_notional_usd >= threshold
        )
        episode_count = len(decluster_cascade_episodes(triggers)) if triggers else 0
        sensitivity_counts.append(SensitivityCount(threshold, episode_count))

    primary_triggers = tuple(
        LiquidationTriggerMinute(
            row.exchange, row.native_market_id, row.position_side, row.bucket_start
        )
        for row in side_minutes
        if row.trailing_notional_usd >= PRIMARY_CASCADE_NOTIONAL_USD
    )
    episodes = decluster_cascade_episodes(primary_triggers)

    maturity_cutoff = database_now - timedelta(minutes=MAX_POSITION_HOLD_MINUTES)
    matured = [episode for episode in episodes if episode.last_trigger_at <= maturity_cutoff]

    semaphore = asyncio.Semaphore(concurrency)
    fetched = await asyncio.gather(
        *(_fetch_episode_candles(clients, symbol_cache, episode, semaphore) for episode in matured)
    )
    results: list[EpisodeResult] = [
        resolve_episode(EpisodeInputs(episode, candles)) for episode, candles in fetched
    ]

    resolved = [result for result in results if result.resolved]
    unresolved_reasons: list[str] = []
    for result in results:
        if result.resolved:
            continue
        assert result.unresolved_reason is not None
        unresolved_reasons.append(result.unresolved_reason)
    unresolved_by_reason = dict(Counter(unresolved_reasons))

    net_returns: list[float] = []
    mfe_values: list[float] = []
    mae_values: list[float] = []
    for result in resolved:
        assert result.net_return_pct is not None
        assert result.mfe_pct is not None
        assert result.mae_pct is not None
        net_returns.append(result.net_return_pct)
        mfe_values.append(result.mfe_pct)
        mae_values.append(result.mae_pct)

    weeks_by_episode = [utc_week_key(result.entry_at) for result in resolved if result.entry_at]
    distinct_asset_clusters = len({result.native_market_id for result in resolved})
    distinct_utc_weeks = len(set(weeks_by_episode))
    asset_counts = Counter(result.native_market_id for result in resolved)
    week_counts = Counter(weeks_by_episode)
    max_single_asset_share = max(asset_counts.values()) / len(resolved) if resolved else 0.0
    max_single_week_share = max(week_counts.values()) / len(resolved) if resolved else 0.0

    ci_lower: float | None = None
    ci_upper: float | None = None
    if resolved:
        observations = tuple(
            ClusterObservation(cluster_key=result.native_market_id, value=value)
            for result, value in zip(resolved, net_returns, strict=True)
        )
        ci_lower, ci_upper = primary_sensitivity_ci(observations)

    verdict = formal_verdict(
        resolved_episodes=len(resolved),
        distinct_asset_clusters=distinct_asset_clusters,
        distinct_utc_weeks=distinct_utc_weeks,
        max_single_asset_share=max_single_asset_share,
        max_single_week_share=max_single_week_share,
        ci_upper_bound_pct=ci_upper,
    )

    fingerprint_rows = [
        {
            "episode_id": episode.episode_id,
            "native_market_id": episode.native_market_id,
            "position_side": episode.position_side,
            "first_trigger_at": episode.first_trigger_at.isoformat(),
            "last_trigger_at": episode.last_trigger_at.isoformat(),
            "resolved": result.resolved,
            "unresolved_reason": result.unresolved_reason,
            "net_return_pct": result.net_return_pct,
        }
        for episode, result in zip(matured, results, strict=True)
    ]

    direction = DirectionReport(
        position_side=position_side,
        funnel=DirectionFunnel(
            trigger_minutes=len(side_minutes),
            episodes_primary_threshold=len(episodes),
            matured_episodes=len(matured),
            resolved_episodes=len(resolved),
            unresolved_by_reason=unresolved_by_reason,
            sensitivity_family=tuple(sensitivity_counts),
        ),
        result=DirectionResult(
            resolved_episodes=len(resolved),
            distinct_asset_clusters=distinct_asset_clusters,
            distinct_utc_weeks=distinct_utc_weeks,
            max_single_asset_share=round(max_single_asset_share, 4),
            max_single_week_share=round(max_single_week_share, 4),
            median_net_return_pct=_median(net_returns),
            mean_net_return_pct=(sum(net_returns) / len(net_returns)) if net_returns else None,
            profit_factor=profit_factor(net_returns),
            win_rate=(
                sum(1 for value in net_returns if value > 0) / len(net_returns)
                if net_returns
                else None
            ),
            median_mfe_pct=_median(mfe_values),
            median_mae_pct=_median(mae_values),
            ci_lower_bound_pct=(round(ci_lower, 6) if ci_lower is not None else None),
            ci_upper_bound_pct=(round(ci_upper, 6) if ci_upper is not None else None),
            verdict=verdict,
        ),
    )
    return direction, fingerprint_rows


async def generate_report(args: argparse.Namespace) -> LiquidationMakerUpperBoundReport:
    if args.since >= args.until:
        raise ValueError("--since must be earlier than --until")
    code_revision = normalize_code_revision(args.code_revision)
    repository = LiquidationMakerUpperBoundRepository.from_url(os.environ["DATABASE_URL"])
    database_now = await repository.database_now()
    raw_minutes = await repository.fetch_trigger_minutes(
        since=args.since, until=args.until, limit=args.max_trigger_minutes + 1
    )
    check_trigger_minute_count(len(raw_minutes), args.max_trigger_minutes)

    exchanges_needed = {row.exchange for row in raw_minutes if row.exchange in EXCHANGE_FACTORIES}
    clients = {exchange: EXCHANGE_FACTORIES[exchange]() for exchange in exchanges_needed}
    symbol_cache: dict[tuple[str, str], str | None] = {}
    try:
        for client in clients.values():
            await client.load_markets()

        direction_reports: list[DirectionReport] = []
        all_fingerprint_rows: list[dict[str, Any]] = []
        for position_side in DIRECTIONS:
            direction, rows = await _resolve_direction(
                position_side=position_side,
                raw_minutes=raw_minutes,
                clients=clients,
                symbol_cache=symbol_cache,
                concurrency=args.candle_fetch_concurrency,
                database_now=database_now,
            )
            direction_reports.append(direction)
            all_fingerprint_rows.extend(rows)
    finally:
        await asyncio.gather(*(client.close() for client in clients.values()))

    fingerprint = canonical_json_array_fingerprint(all_fingerprint_rows)

    return LiquidationMakerUpperBoundReport(
        manifest=ReportManifest(
            report_version=REPORT_VERSION,
            contract_version=CONTRACT_VERSION,
            interpretation=INTERPRETATION,
            trigger_minute_query_version="liquidation_maker_upper_bound_trigger_minutes_v1",
            code_revision=code_revision,
            working_tree_dirty=args.working_tree_dirty,
            generated_at=database_now,
            since=args.since,
            until=args.until,
            primary_cascade_notional_usd=PRIMARY_CASCADE_NOTIONAL_USD,
            input_fingerprint=fingerprint,
        ),
        directions=tuple(direction_reports),
        caveats=(
            "Entry is a post-hoc optimistic upper bound (the exact extremum the episode "
            "itself touched), not an executable order -- see the pure module's own "
            "docstring. A negative result rejects the direction; a positive result only "
            "warrants building a causal BBO/L2 shadow test, never paper or live trading "
            "on its own.",
            "Touching a price is not proof of a maker fill: queue position, available "
            "depth, and this strategy's own place in the book are all unknown here.",
            "order_expiry (how long an unfilled resting order could wait) is not modeled "
            "-- only MAX_POSITION_HOLD_MINUTES (60), the hold after the assumed fill.",
            f"Sensitivity family {SENSITIVITY_CASCADE_NOTIONAL_USD_FAMILY} is pre-"
            f"registered context (episode counts only, no full economics) -- the primary "
            f"verdict gates on {PRIMARY_CASCADE_NOTIONAL_USD:.0f} alone, never the "
            "best-looking threshold after seeing results.",
            f"Evidence floor: {EVIDENCE_FLOOR['min_resolved_episodes']} resolved episodes, "
            f"{EVIDENCE_FLOOR['min_distinct_asset_clusters']} distinct asset clusters, "
            f"{EVIDENCE_FLOOR['min_distinct_utc_weeks']} distinct UTC weeks, concentration "
            f"caps {MAX_SINGLE_ASSET_EPISODE_SHARE:.0%} asset / "
            f"{MAX_SINGLE_WEEK_EPISODE_SHARE:.0%} week, applied per direction.",
        ),
    )


def render_json(report: LiquidationMakerUpperBoundReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True)


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}%"


def _fmt_ratio(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def render_markdown(report: LiquidationMakerUpperBoundReport) -> str:
    manifest = report.manifest
    lines = [
        "# Liquidation-Cascade Maker Reversion — Post-Hoc Upper Bound",
        "",
        "Discovery only. Entry is a post-hoc optimistic upper bound, not an executable "
        "order -- see caveats. A `reject` verdict closes the direction; "
        "`positive_warrants_shadow_test` only justifies the next causal BBO/L2 test, "
        "never paper or live trading.",
        "",
        f"Window: `{manifest.since.isoformat()}` -> `{manifest.until.isoformat()}`",
        f"Contract: `{manifest.contract_version}`",
        f"Primary cascade threshold: ${manifest.primary_cascade_notional_usd:,.0f}",
        f"Generated at: `{manifest.generated_at.isoformat()}`",
        f"Input fingerprint: `{manifest.input_fingerprint}`",
        "",
    ]
    for direction in report.directions:
        funnel = direction.funnel
        result = direction.result
        interval = (
            "n/a"
            if result.ci_lower_bound_pct is None
            else f"[{result.ci_lower_bound_pct:.4f}%, {result.ci_upper_bound_pct:.4f}%]"
        )
        lines += [
            f"## {direction.position_side}-liquidation "
            f"({'buy' if direction.position_side == 'long' else 'sell'} reversion)",
            "",
            "### Funnel",
            "",
            *markdown_table(
                ("Stage", "Count"),
                [
                    ("Trigger minutes (any threshold)", funnel.trigger_minutes),
                    ("Episodes @ primary threshold", funnel.episodes_primary_threshold),
                    ("Matured", funnel.matured_episodes),
                    ("Resolved", funnel.resolved_episodes),
                    *[
                        (f"Unresolved: {reason}", count)
                        for reason, count in sorted(funnel.unresolved_by_reason.items())
                    ],
                ],
            ),
            "",
            "### Sensitivity family (episode counts only, context, never gates)",
            "",
            *markdown_table(
                ("Cascade threshold", "Episodes"),
                [
                    (f"${entry.cascade_notional_usd:,.0f}", entry.episodes)
                    for entry in funnel.sensitivity_family
                ],
            ),
            "",
            "### Primary result",
            "",
            *markdown_table(
                (
                    "Resolved",
                    "Clusters",
                    "Weeks",
                    "Median net",
                    "Mean net",
                    "Profit factor",
                    "Win rate",
                    "Median MFE",
                    "Median MAE",
                    "95% cluster CI",
                    "Verdict",
                ),
                [
                    (
                        result.resolved_episodes,
                        result.distinct_asset_clusters,
                        result.distinct_utc_weeks,
                        _fmt_pct(result.median_net_return_pct),
                        _fmt_pct(result.mean_net_return_pct),
                        _fmt_ratio(result.profit_factor),
                        "n/a" if result.win_rate is None else f"{result.win_rate:.2%}",
                        _fmt_pct(result.median_mfe_pct),
                        _fmt_pct(result.median_mae_pct),
                        interval,
                        result.verdict,
                    )
                ],
            ),
            "",
        ]
    lines += ["## Caveats", "", *(f"- {item}" for item in report.caveats)]
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="liquidation_maker_upper_bound_v1 -- post-hoc oracle upper-bound discovery"
    )
    parser.add_argument("--since", type=parse_utc_datetime, required=True)
    parser.add_argument("--until", type=parse_utc_datetime, required=True)
    parser.add_argument(
        "--max-trigger-minutes",
        type=int,
        default=DEFAULT_MAX_TRIGGER_MINUTES,
        help=(
            f"default {DEFAULT_MAX_TRIGGER_MINUTES}; fails loudly rather than silently "
            "evaluating an unexpectedly large result"
        ),
    )
    parser.add_argument(
        "--candle-fetch-concurrency", type=int, default=DEFAULT_CANDLE_FETCH_CONCURRENCY
    )
    parser.add_argument("--code-revision", default=os.getenv("SCHURFER_GIT_SHA"))
    parser.add_argument(
        "--working-tree-dirty",
        action=argparse.BooleanOptionalAction,
        required=True,
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not os.getenv("DATABASE_URL"):
        raise ValueError("DATABASE_URL is required")
    if not args.code_revision:
        raise ValueError("--code-revision (or SCHURFER_GIT_SHA) is required")
    report = asyncio.run(generate_report(args))
    sys.stdout.write(render_json(report) if args.format == "json" else render_markdown(report))
