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
accepted), and reports independent SCOPES with their own evidence floors
and verdicts -- never pooled into one sample, per the pure module's own
frozen design.

## Colleague review (2026-09-03): five real gaps, fixed

1. **Wrong candle timeframe.** `fetch_symbol_candles` was called without a
   `timeframe`/`timeframe_ms` override, silently using its own 5m default
   while the pure module's exit-boundary math and hold-window gap check
   are 1m-aligned (`EXIT_BAR_TIMEFRAME_MS`). This was more severe here than
   in a plain boundary check: `resolve_episode`'s own `covers_window_
   without_gaps` check over 5m candles fails almost every real episode as
   `hold_window_has_gaps`. `_fetch_episode_candles` now passes
   `timeframe="1m", timeframe_ms=EXIT_BAR_TIMEFRAME_MS` explicitly.
2. **Maturity allowed reading a not-yet-closed exit bar.** A flat
   `last_trigger_at + MAX_POSITION_HOLD_MINUTES` cutoff ignores that the
   ACTUAL entry (the episode's own extremum) can land anywhere inside the
   trigger window, and that the exit bar itself needs to fully CLOSE, not
   merely open, before it is safe to read. Maturity is now computed from
   the LATEST possible entry instant in the episode (`last_trigger_at`,
   the same upper bound `resolve_episode` itself uses when scanning for
   the extremum) plus the hold plus one full exit-bar close.
3. **Incompatible coverage semantics blended into one verdict.**
   `timeseries.liquidation_events` captures Bybit under `coverage_kind
   = 'complete_stream'` (a genuine full event stream) and Binance under
   `'latest_per_symbol_1000ms'` (a lossy periodic sample) -- verified
   against real production data (migration 0022's own CHECK constraint;
   `SELECT exchange, coverage_kind, count(*) ... GROUP BY` showed a clean
   1:1 split). The repository's own rolling-window SQL now groups AND
   partitions by `coverage_kind`, not just `(exchange, native_market_id,
   position_side)` -- proven against real Postgres by
   `test_fetch_trigger_minutes_never_blends_two_coverage_kinds_into_one_
   rolling_sum`, which forces two coverage_kinds onto the SAME (exchange,
   native_market_id, position_side) and checks their rolling sums stay
   independent. The report now produces one independent verdict per
   (position_side, exchange, coverage_kind) SCOPE -- `ScopeReport`, not
   `DirectionReport` -- rather than one per direction alone.
4. **`native_market_id` labeled an "asset cluster".** It is an instrument
   identifier on ONE exchange, not a cross-venue canonical asset identity
   resolved against an identity registry -- see the pure module's own
   docstring, "Cluster identity is instrument-level, not verified canonical
   asset identity". Scoping per exchange (point 3) already removes the
   most severe risk (a ticker colliding ACROSS venues); this report's own
   naming/labels are updated to say "instrument" rather than "asset"
   wherever this cluster key is surfaced.
5. **`REQUIRE_EXIT_SLIPPAGE_SENSITIVITY` and drawdown/breakdowns were
   frozen but never computed.** This report now resolves each scope's
   matured episodes at all of `EXIT_SLIPPAGE_SENSITIVITY_BPS` (0bps /
   primary / 2x primary) -- cheap, since the candles are already fetched
   once and reused across all three resolutions -- and reports
   `max_sequential_drawdown_pct` plus per-instrument and per-week
   breakdowns, all named in the pure module's own "Primary metrics" list.

Also narrowed (same review): the caveat text previously said a negative
result means "no real order could plausibly beat" this upper bound --
overclaiming, since this study only bounds ONE specific candidate
(extremum entry, fixed `MAX_POSITION_HOLD_MINUTES` taker exit). A negative
result here rejects THAT candidate, not the entire maker-reversion idea
space; see the pure module's own docstring and `formal_verdict`'s
docstring for the corrected framing. And the fingerprint now covers more
of what could silently change a result: exchange, coverage_kind, entry
price/timestamp, MFE/MAE, and a hash of the exact candle path each
episode resolved against, plus the frozen contract's own cost/slippage/
bootstrap versions -- not just episode identity and net_return_pct.

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
import hashlib
import json
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from .clustered_inference import ClusterObservation
from .exchange_registry import EXCHANGE_FACTORIES
from .liquidation_maker_upper_bound import (
    BOOTSTRAP_ITERATIONS,
    BOOTSTRAP_SEED,
    BOOTSTRAP_VERSION,
    CASCADE_COOLDOWN_MINUTES,
    CONFIDENCE_LEVEL,
    CONTRACT_VERSION,
    EVIDENCE_FLOOR,
    EXIT_BAR_TIMEFRAME_MS,
    EXIT_SLIPPAGE_BPS_ASSUMED,
    EXIT_SLIPPAGE_SENSITIVITY_BPS,
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
    max_sequential_drawdown_pct,
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

REPORT_VERSION = "liquidation_maker_upper_bound_report_v2"

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


def episode_is_matured(episode: CascadeEpisode, database_now: datetime) -> bool:
    """True once the exit bar for the LATEST possible entry instant in this
    episode has fully CLOSED. Colleague review, 2026-09-03: the previous
    version used `last_trigger_at + MAX_POSITION_HOLD_MINUTES` as the
    cutoff directly -- close, but it ignores that the exit bar itself must
    fully close (not merely open) before it is safe to read, the same
    class of maturity bug found in source_lead_forward_cohort_report.py's
    own episode_is_matured. `resolve_episode` itself scans for the
    extremum only within `[first_trigger_at, last_trigger_at]`, so
    `last_trigger_at` is also the correct upper bound on entry_at to
    compute maturity from -- the exit boundary this function checks
    against is >= any real exit boundary the episode could actually
    resolve to."""
    latest_possible_entry_ms = int(episode.last_trigger_at.timestamp() * 1000)
    boundary_ms = latest_possible_entry_ms + MAX_POSITION_HOLD_MINUTES * 60_000
    # Ceil-aligned the same way _expected_exit_boundary_ms is (that
    # function is private to the pure module and not exported -- this
    # mirrors its own ceil-to-1m-boundary math rather than importing a
    # private symbol).
    boundary_ms = -(-boundary_ms // EXIT_BAR_TIMEFRAME_MS) * EXIT_BAR_TIMEFRAME_MS
    database_now_ms = int(database_now.timestamp() * 1000)
    return database_now_ms >= boundary_ms + EXIT_BAR_TIMEFRAME_MS


@dataclass(frozen=True)
class SensitivityCount:
    cascade_notional_usd: float
    episodes: int


@dataclass(frozen=True)
class SlippageSensitivityPoint:
    exit_slippage_bps: float
    is_primary: bool
    resolved_episodes: int
    mean_net_return_pct: float | None
    ci_lower_bound_pct: float | None
    ci_upper_bound_pct: float | None


@dataclass(frozen=True)
class InstrumentBreakdownRow:
    native_market_id: str
    resolved_episodes: int
    mean_net_return_pct: float | None


@dataclass(frozen=True)
class WeekBreakdownRow:
    utc_week: str
    resolved_episodes: int
    mean_net_return_pct: float | None


@dataclass(frozen=True)
class ScopeFunnel:
    trigger_minutes: int
    episodes_primary_threshold: int
    matured_episodes: int
    resolved_episodes: int
    unresolved_by_reason: dict[str, int]
    sensitivity_family: tuple[SensitivityCount, ...]


@dataclass(frozen=True)
class ScopeResult:
    resolved_episodes: int
    distinct_instrument_clusters: int
    distinct_utc_weeks: int
    max_single_instrument_share: float
    max_single_week_share: float
    median_net_return_pct: float | None
    mean_net_return_pct: float | None
    profit_factor: float | None
    win_rate: float | None
    median_mfe_pct: float | None
    median_mae_pct: float | None
    max_sequential_drawdown_pct: float | None
    ci_lower_bound_pct: float | None
    ci_upper_bound_pct: float | None
    verdict: str
    slippage_sensitivity: tuple[SlippageSensitivityPoint, ...]
    instrument_breakdown: tuple[InstrumentBreakdownRow, ...]
    week_breakdown: tuple[WeekBreakdownRow, ...]


@dataclass(frozen=True)
class ScopeReport:
    position_side: str
    exchange: str
    coverage_kind: str
    funnel: ScopeFunnel
    result: ScopeResult


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
    scopes: tuple[ScopeReport, ...]
    caveats: tuple[str, ...]


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _candle_path_sha256(candles: Sequence[Candle]) -> str:
    """A hash of the exact candle path an episode resolved against --
    colleague review, 2026-09-03: two runs that silently resolved the same
    episode against DIFFERENT underlying candle data (a cache
    invalidation, a late-arriving exchange correction, a bug fetching the
    wrong window) previously produced no visible difference in the
    fingerprint beyond whatever net_return_pct happened to come out --
    this makes the actual INPUT path itself part of what the fingerprint
    covers, not just its derived output."""
    payload = json.dumps(
        [[candle.ts_ms, candle.close] for candle in sorted(candles, key=lambda c: c.ts_ms)],
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


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
        candles = await fetch_symbol_candles(
            client,
            symbol,
            start_ms,
            end_ms,
            timeframe="1m",
            timeframe_ms=EXIT_BAR_TIMEFRAME_MS,
            use_cache=True,
        )
    return episode, tuple(candles)


def _slippage_sensitivity_point(
    *,
    exit_slippage_bps: float,
    matured: Sequence[CascadeEpisode],
    candles_by_episode: Sequence[tuple[Candle, ...]],
) -> tuple[SlippageSensitivityPoint, tuple[EpisodeResult, ...]]:
    results = tuple(
        resolve_episode(EpisodeInputs(episode, candles), exit_slippage_bps=exit_slippage_bps)
        for episode, candles in zip(matured, candles_by_episode, strict=True)
    )
    resolved_returns = [result.net_return_pct for result in results if result.resolved]
    resolved_values = [value for value in resolved_returns if value is not None]
    mean_net_return_pct = (
        round(sum(resolved_values) / len(resolved_values), 6) if resolved_values else None
    )
    ci_lower: float | None = None
    ci_upper: float | None = None
    if resolved_values:
        observations = tuple(
            ClusterObservation(cluster_key=result.native_market_id, value=value)
            for result, value in zip(results, resolved_returns, strict=True)
            if result.resolved and value is not None
        )
        ci_lower, ci_upper = primary_sensitivity_ci(observations)
        ci_lower = round(ci_lower, 6)
        ci_upper = round(ci_upper, 6)
    return (
        SlippageSensitivityPoint(
            exit_slippage_bps=exit_slippage_bps,
            is_primary=(exit_slippage_bps == EXIT_SLIPPAGE_BPS_ASSUMED),
            resolved_episodes=len(resolved_values),
            mean_net_return_pct=mean_net_return_pct,
            ci_lower_bound_pct=ci_lower,
            ci_upper_bound_pct=ci_upper,
        ),
        results,
    )


async def _resolve_scope(
    *,
    position_side: str,
    exchange: str,
    coverage_kind: str,
    raw_minutes: Sequence[RawTriggerMinute],
    clients: dict[str, Any],
    symbol_cache: dict[tuple[str, str], str | None],
    concurrency: int,
    database_now: datetime,
) -> tuple[ScopeReport, list[dict[str, Any]]]:
    scope_minutes = [
        row
        for row in raw_minutes
        if row.position_side == position_side
        and row.exchange == exchange
        and row.coverage_kind == coverage_kind
    ]

    sensitivity_counts: list[SensitivityCount] = []
    for threshold in SENSITIVITY_CASCADE_NOTIONAL_USD_FAMILY:
        triggers = tuple(
            LiquidationTriggerMinute(
                row.exchange, row.native_market_id, row.position_side, row.bucket_start
            )
            for row in scope_minutes
            if row.trailing_notional_usd >= threshold
        )
        episode_count = len(decluster_cascade_episodes(triggers)) if triggers else 0
        sensitivity_counts.append(SensitivityCount(threshold, episode_count))

    primary_triggers = tuple(
        LiquidationTriggerMinute(
            row.exchange, row.native_market_id, row.position_side, row.bucket_start
        )
        for row in scope_minutes
        if row.trailing_notional_usd >= PRIMARY_CASCADE_NOTIONAL_USD
    )
    episodes = decluster_cascade_episodes(primary_triggers)

    matured = [episode for episode in episodes if episode_is_matured(episode, database_now)]

    semaphore = asyncio.Semaphore(concurrency)
    fetched = await asyncio.gather(
        *(_fetch_episode_candles(clients, symbol_cache, episode, semaphore) for episode in matured)
    )
    candles_by_episode = tuple(candles for _episode, candles in fetched)

    sensitivity_points: list[SlippageSensitivityPoint] = []
    primary_point: SlippageSensitivityPoint | None = None
    primary_results: tuple[EpisodeResult, ...] = ()
    for exit_slippage_bps in EXIT_SLIPPAGE_SENSITIVITY_BPS:
        point, results_at_bps = _slippage_sensitivity_point(
            exit_slippage_bps=exit_slippage_bps,
            matured=matured,
            candles_by_episode=candles_by_episode,
        )
        sensitivity_points.append(point)
        if point.is_primary:
            primary_point = point
            primary_results = results_at_bps
    assert primary_point is not None  # EXIT_SLIPPAGE_SENSITIVITY_BPS always includes the primary

    resolved_with_candles = [
        (episode, result, candles)
        for episode, result, candles in zip(
            matured, primary_results, candles_by_episode, strict=True
        )
        if result.resolved
    ]
    unresolved_reasons: list[str] = []
    for result in primary_results:
        if result.resolved:
            continue
        assert result.unresolved_reason is not None
        unresolved_reasons.append(result.unresolved_reason)
    unresolved_by_reason = dict(Counter(unresolved_reasons))

    net_returns: list[float] = []
    mfe_values: list[float] = []
    mae_values: list[float] = []
    for _episode, result, _candles in resolved_with_candles:
        assert result.net_return_pct is not None
        assert result.mfe_pct is not None
        assert result.mae_pct is not None
        net_returns.append(result.net_return_pct)
        mfe_values.append(result.mfe_pct)
        mae_values.append(result.mae_pct)

    # Ordered by entry_at for a chronologically meaningful drawdown --
    # resolved_with_candles is not otherwise guaranteed to be in that order
    # (episodes are already sorted by first_trigger_at, but entry_at is the
    # episode's own resolved extremum instant, which can differ).
    chronological = sorted(
        resolved_with_candles, key=lambda item: item[1].entry_at or item[0].first_trigger_at
    )
    drawdown = max_sequential_drawdown_pct(
        [
            result.net_return_pct
            for _episode, result, _candles in chronological
            if result.net_return_pct is not None
        ]
    )

    weeks_by_episode = [
        utc_week_key(result.entry_at)
        for _episode, result, _candles in resolved_with_candles
        if result.entry_at
    ]
    distinct_instrument_clusters = len(
        {result.native_market_id for _episode, result, _candles in resolved_with_candles}
    )
    distinct_utc_weeks = len(set(weeks_by_episode))
    instrument_counts = Counter(
        result.native_market_id for _episode, result, _candles in resolved_with_candles
    )
    week_counts = Counter(weeks_by_episode)
    resolved_count = len(resolved_with_candles)
    max_single_instrument_share = (
        max(instrument_counts.values()) / resolved_count if resolved_count else 0.0
    )
    max_single_week_share = max(week_counts.values()) / resolved_count if resolved_count else 0.0

    verdict = formal_verdict(
        resolved_episodes=resolved_count,
        distinct_asset_clusters=distinct_instrument_clusters,
        distinct_utc_weeks=distinct_utc_weeks,
        max_single_asset_share=max_single_instrument_share,
        max_single_week_share=max_single_week_share,
        ci_upper_bound_pct=primary_point.ci_upper_bound_pct,
    )

    instrument_returns: dict[str, list[float]] = {}
    for _episode, result, _candles in resolved_with_candles:
        assert result.net_return_pct is not None
        instrument_returns.setdefault(result.native_market_id, []).append(result.net_return_pct)
    instrument_breakdown = tuple(
        InstrumentBreakdownRow(
            native_market_id=native_market_id,
            resolved_episodes=len(returns),
            mean_net_return_pct=round(sum(returns) / len(returns), 6),
        )
        for native_market_id, returns in sorted(instrument_returns.items())
    )

    week_returns: dict[str, list[float]] = {}
    for _episode, result, _candles in resolved_with_candles:
        if not result.entry_at:
            continue
        assert result.net_return_pct is not None
        week_returns.setdefault(utc_week_key(result.entry_at), []).append(result.net_return_pct)
    week_breakdown = tuple(
        WeekBreakdownRow(
            utc_week=week_key,
            resolved_episodes=len(returns),
            mean_net_return_pct=round(sum(returns) / len(returns), 6),
        )
        for week_key, returns in sorted(week_returns.items())
    )

    fingerprint_rows = [
        {
            "episode_id": episode.episode_id,
            "exchange": episode.exchange,
            "native_market_id": episode.native_market_id,
            "position_side": episode.position_side,
            "coverage_kind": coverage_kind,
            "first_trigger_at": episode.first_trigger_at.isoformat(),
            "last_trigger_at": episode.last_trigger_at.isoformat(),
            "resolved": result.resolved,
            "unresolved_reason": result.unresolved_reason,
            "entry_at": result.entry_at.isoformat() if result.entry_at else None,
            "entry_price": result.entry_price,
            "net_return_pct": result.net_return_pct,
            "mfe_pct": result.mfe_pct,
            "mae_pct": result.mae_pct,
            "candle_path_sha256": _candle_path_sha256(candles),
        }
        for episode, result, candles in zip(
            matured, primary_results, candles_by_episode, strict=True
        )
    ]

    scope = ScopeReport(
        position_side=position_side,
        exchange=exchange,
        coverage_kind=coverage_kind,
        funnel=ScopeFunnel(
            trigger_minutes=len(scope_minutes),
            episodes_primary_threshold=len(episodes),
            matured_episodes=len(matured),
            resolved_episodes=resolved_count,
            unresolved_by_reason=unresolved_by_reason,
            sensitivity_family=tuple(sensitivity_counts),
        ),
        result=ScopeResult(
            resolved_episodes=resolved_count,
            distinct_instrument_clusters=distinct_instrument_clusters,
            distinct_utc_weeks=distinct_utc_weeks,
            max_single_instrument_share=round(max_single_instrument_share, 4),
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
            max_sequential_drawdown_pct=drawdown,
            ci_lower_bound_pct=primary_point.ci_lower_bound_pct,
            ci_upper_bound_pct=primary_point.ci_upper_bound_pct,
            verdict=verdict,
            slippage_sensitivity=tuple(sensitivity_points),
            instrument_breakdown=instrument_breakdown,
            week_breakdown=week_breakdown,
        ),
    )
    return scope, fingerprint_rows


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
    # Every (exchange, coverage_kind) pair actually present in the fetched
    # data -- not a fixed enumeration, so a future new coverage_kind (or an
    # exchange dropping one) is picked up automatically rather than
    # silently excluded.
    scope_keys = sorted({(row.exchange, row.coverage_kind) for row in raw_minutes})
    try:
        for client in clients.values():
            await client.load_markets()

        scope_reports: list[ScopeReport] = []
        all_fingerprint_rows: list[dict[str, Any]] = []
        for position_side in ("long", "short"):
            for exchange, coverage_kind in scope_keys:
                scope, rows = await _resolve_scope(
                    position_side=position_side,
                    exchange=exchange,
                    coverage_kind=coverage_kind,
                    raw_minutes=raw_minutes,
                    clients=clients,
                    symbol_cache=symbol_cache,
                    concurrency=args.candle_fetch_concurrency,
                    database_now=database_now,
                )
                scope_reports.append(scope)
                all_fingerprint_rows.extend(rows)
    finally:
        await asyncio.gather(*(client.close() for client in clients.values()))

    # A synthetic leading row pins the frozen contract's own cost/slippage/
    # bootstrap versions into the fingerprint -- colleague review,
    # 2026-09-03: two runs with byte-identical episode rows but a silently
    # different cost model, slippage assumption, or bootstrap seed
    # previously produced the SAME fingerprint despite not being the same
    # result.
    contract_row = {
        "kind": "contract_versions",
        "contract_version": CONTRACT_VERSION,
        "exit_slippage_bps_assumed": EXIT_SLIPPAGE_BPS_ASSUMED,
        "exit_slippage_sensitivity_bps": list(EXIT_SLIPPAGE_SENSITIVITY_BPS),
        "cascade_cooldown_minutes": CASCADE_COOLDOWN_MINUTES,
        "primary_cascade_notional_usd": PRIMARY_CASCADE_NOTIONAL_USD,
        "bootstrap_version": BOOTSTRAP_VERSION,
        "bootstrap_iterations": BOOTSTRAP_ITERATIONS,
        "bootstrap_seed": BOOTSTRAP_SEED,
        "confidence_level": CONFIDENCE_LEVEL,
    }
    fingerprint = canonical_json_array_fingerprint([contract_row, *all_fingerprint_rows])

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
        scopes=tuple(scope_reports),
        caveats=(
            "Entry is a post-hoc optimistic upper bound (the exact extremum the episode "
            "itself touched), not an executable order -- see the pure module's own "
            "docstring.",
            "A negative (reject) result rejects THIS specific candidate -- extremum entry "
            "plus a fixed 60-minute taker-style exit -- not the entire maker-reversion "
            "idea space; a causal variant with an earlier or dynamic exit is a different, "
            "unaddressed candidate. A positive result only warrants building a causal "
            "BBO/L2 shadow test, never paper or live trading on its own.",
            "Touching a price is not proof of a maker fill: queue position, available "
            "depth, and this strategy's own place in the book are all unknown here.",
            "order_expiry (how long an unfilled resting order could wait) is not modeled "
            "-- only MAX_POSITION_HOLD_MINUTES (60), the hold after the assumed fill.",
            "Each scope is one (direction, exchange, coverage_kind) combination -- Bybit's "
            "complete_stream and Binance's latest_per_symbol_1000ms liquidation capture "
            "are genuinely different measurement processes and are never pooled into one "
            "denominator or CI.",
            "Instrument clusters are native_market_id on ONE exchange, not verified "
            "cross-venue canonical asset identity -- see the pure module's own docstring.",
            f"Sensitivity family {SENSITIVITY_CASCADE_NOTIONAL_USD_FAMILY} is pre-"
            f"registered context (episode counts only, no full economics) -- the primary "
            f"verdict gates on {PRIMARY_CASCADE_NOTIONAL_USD:.0f} alone, never the "
            "best-looking threshold after seeing results.",
            f"Evidence floor: {EVIDENCE_FLOOR['min_resolved_episodes']} resolved episodes, "
            f"{EVIDENCE_FLOOR['min_distinct_asset_clusters']} distinct instrument "
            f"clusters, {EVIDENCE_FLOOR['min_distinct_utc_weeks']} distinct UTC weeks, "
            f"concentration caps {MAX_SINGLE_ASSET_EPISODE_SHARE:.0%} instrument / "
            f"{MAX_SINGLE_WEEK_EPISODE_SHARE:.0%} week, applied per (direction, exchange, "
            "coverage_kind) scope -- a smaller population per scope than a pooled "
            "Binance+Bybit read would have given, making the floor genuinely harder to "
            "reach; accepted rather than lowering it to compensate.",
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
        "order -- see caveats. A `reject` verdict closes THIS candidate (fixed 60-minute "
        "exit); `positive_warrants_shadow_test` only justifies the next causal BBO/L2 "
        "test, never paper or live trading.",
        "",
        f"Window: `{manifest.since.isoformat()}` -> `{manifest.until.isoformat()}`",
        f"Contract: `{manifest.contract_version}`",
        f"Primary cascade threshold: ${manifest.primary_cascade_notional_usd:,.0f}",
        f"Generated at: `{manifest.generated_at.isoformat()}`",
        f"Input fingerprint: `{manifest.input_fingerprint}`",
        "",
    ]
    for scope in report.scopes:
        funnel = scope.funnel
        result = scope.result
        interval = (
            "n/a"
            if result.ci_lower_bound_pct is None
            else f"[{result.ci_lower_bound_pct:.4f}%, {result.ci_upper_bound_pct:.4f}%]"
        )
        lines += [
            f"## {scope.position_side}-liquidation "
            f"({'buy' if scope.position_side == 'long' else 'sell'} reversion) "
            f"-- {scope.exchange} / {scope.coverage_kind}",
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
                    "Instrument clusters",
                    "Weeks",
                    "Median net",
                    "Mean net",
                    "Profit factor",
                    "Win rate",
                    "Median MFE",
                    "Median MAE",
                    "Max drawdown",
                    "95% cluster CI",
                    "Verdict",
                ),
                [
                    (
                        result.resolved_episodes,
                        result.distinct_instrument_clusters,
                        result.distinct_utc_weeks,
                        _fmt_pct(result.median_net_return_pct),
                        _fmt_pct(result.mean_net_return_pct),
                        _fmt_ratio(result.profit_factor),
                        "n/a" if result.win_rate is None else f"{result.win_rate:.2%}",
                        _fmt_pct(result.median_mfe_pct),
                        _fmt_pct(result.median_mae_pct),
                        _fmt_pct(result.max_sequential_drawdown_pct),
                        interval,
                        result.verdict,
                    )
                ],
            ),
            "",
            "### Exit-slippage sensitivity (REQUIRE_EXIT_SLIPPAGE_SENSITIVITY)",
            "",
            *markdown_table(
                ("Exit slippage (bps)", "Primary?", "Resolved", "Mean net", "95% cluster CI"),
                [
                    (
                        f"{point.exit_slippage_bps:.0f}",
                        "yes" if point.is_primary else "",
                        point.resolved_episodes,
                        _fmt_pct(point.mean_net_return_pct),
                        (
                            "n/a"
                            if point.ci_lower_bound_pct is None
                            else f"[{point.ci_lower_bound_pct:.4f}%, "
                            f"{point.ci_upper_bound_pct:.4f}%]"
                        ),
                    )
                    for point in result.slippage_sensitivity
                ],
            ),
            "",
            "### Per-instrument breakdown",
            "",
            *(
                markdown_table(
                    ("native_market_id", "Resolved", "Mean net return"),
                    [
                        (
                            row.native_market_id,
                            row.resolved_episodes,
                            _fmt_pct(row.mean_net_return_pct),
                        )
                        for row in result.instrument_breakdown
                    ],
                )
                if result.instrument_breakdown
                else ["_No resolved episodes yet._"]
            ),
            "",
            "### Per-week breakdown",
            "",
            *(
                markdown_table(
                    ("UTC week", "Resolved", "Mean net return"),
                    [
                        (row.utc_week, row.resolved_episodes, _fmt_pct(row.mean_net_return_pct))
                        for row in result.week_breakdown
                    ],
                )
                if result.week_breakdown
                else ["_No resolved episodes yet._"]
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
