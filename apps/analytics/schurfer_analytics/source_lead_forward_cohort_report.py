"""DB-fetch, CLI, and Markdown/JSON rendering for source_lead_forward_cohort_v1
(research/source-lead-forward-cohort-plumbing-v1).

`source_lead_forward_cohort.py`'s own `resolve_episode`/`formal_verdict` are
pure functions, frozen and synthetic-fixture-tested well before any real
qualified capture exists (see that module's own docstring, "What is frozen
now vs. what waits"). This file is exactly the DB-fetching/CLI/rendering
plumbing that docstring defers to later -- built now, before the cohort's
own earliest possible checkpoint (~2026-10-01), against
`test_source_lead_forward_cohort_report.py`'s synthetic fixtures, so no one
has to design resolution mechanics after real outcomes are already visible.

## Colleague review (2026-09-03): five real gaps in the first version, fixed

1. **Wrong candle timeframe.** `fetch_symbol_candles` was called without a
   `timeframe`/`timeframe_ms` override, so it silently used its own 5m
   default while `expected_exit_boundary_ms` computes a 1m-aligned
   boundary (`EXIT_BAR_TIMEFRAME_MS = ONE_MINUTE_MS`). `_fetch_exit_bar`
   now passes `timeframe="1m", timeframe_ms=EXIT_BAR_TIMEFRAME_MS`
   explicitly.
2. **Maturity allowed reading a not-yet-closed exit bar.** A flat
   `entry_at + OUTCOME_HORIZON_MINUTES` cutoff can land exactly at the exit
   bar's own OPEN timestamp, not its close -- two runs against the SAME
   episode could then see different closes as the bar keeps accumulating
   trades. `episode_is_matured` (source_lead_forward_cohort.py) now checks
   `database_now >= exit_boundary + EXIT_BAR_TIMEFRAME_MS`, the bar's own
   full close.
3. **STOPPING_RULE was a comment, not code.** The frozen contract says
   evaluate exactly once, at the earliest point both floors are met, and
   never re-peek; the first version recomputed the verdict over whatever
   was matured on every run. `find_earliest_checkpoint_prefix_length`
   (source_lead_forward_cohort.py) makes this literal, and `generate_report`
   below persists the resulting prefix as an immutable artifact via
   `research_dataset_artifact.write_dataset_artifact` -- first successful
   write at a given content fingerprint wins; a later run with MORE matured
   episodes available reproduces the SAME prefix (proven by
   `find_earliest_checkpoint_prefix_length`'s own determinism: the boundary
   only depends on the ALREADY-matured episodes up to where it was first
   crossed, not on anything appended after), so the write is always either
   `CREATED` (first time) or `ALREADY_EXISTS` (every time after). This does
   NOT implement full cohort-level drift detection (a `cohort_key` +
   authoritative-fingerprint lock, as later built for
   research/cex-activity-discovery-completion-v1's own, much larger and
   more failure-prone extraction) -- if a late-arriving qualification were
   ever inserted with an EARLIER `source_first_observed_at` than an
   already-matured, already-checkpointed episode, this module would not
   detect that as drift. Accepted for this cohort's own small, upstream-
   deduplicated candidate set (14 canonical assets, `pump_events`-sequenced
   captures); revisit with a real `cohort_key` layer if that ever actually
   happens.
4. **Clustering by `base` (a bare ticker), not canonical identity.**
   `app.source_lead_qualifications.canonical_asset_id` is populated for
   every `status='qualified'` row (migration 0022's own CHECK constraint)
   but the repository never read it -- `distinct_asset_clusters`/
   concentration/bootstrap all clustered by ticker string instead, exactly
   the identity risk this codebase's own source-lead identity registry
   exists to close (two different assets sharing a ticker would silently
   merge; one multi-chain asset could silently split). The repository now
   selects `q.canonical_asset_id`, and `aggregate_cohort` clusters by it.
5. **`REQUIRE_EXIT_SLIPPAGE_SENSITIVITY` was frozen but never computed.**
   `resolve_episode_at_exit_slippage` (source_lead_forward_cohort.py) is
   the same resolution logic parameterized by slippage instead of hardcoded
   to `EXIT_SLIPPAGE_BPS_ASSUMED`; this report now resolves the checkpoint
   prefix at all three of `EXIT_SLIPPAGE_SENSITIVITY_BPS` (0bps / primary /
   2x primary) and reports all three, not just the primary number.

Also fixed: `asyncio.gather` over up to `--max-qualified-episodes` (default
20,000) exit-bar fetches with no concurrency bound and no isolation between
episodes (one exchange exception used to abort the whole batch) --
`_fetch_exit_bars_bounded` below caps concurrency via a semaphore, classifies
failures (a corrupted immutable market-path cache fails the whole run loudly
via `MarketPathCacheCorruptError`; an ordinary per-episode fetch failure is
logged and treated as a missing exit bar, never silently retried into a
different answer), and the whole fetch phase runs under an explicit
wall-clock budget (`--exchange-fetch-wall-seconds`).

## Honest scope note

The frozen contract's own "Required output" section also calls for a
secondary diagnostic (`SECONDARY_DIAGNOSTIC_VERSION`, the paired early-
minus-confirmed-entry delta HYP-012 originally used, kept here only as
non-gating context). No reusable Python computation for that delta exists
anywhere in this codebase yet -- `confirmed_within_hour` currently only
exists as a Go dashboard signal (`apps/api-gateway/internal/research/
handler.go`'s `SourceLeadProgress`), reading `app.pump_event_sources`
through a query this module would otherwise have to reimplement from
scratch, unreviewed, under this same PR. Since the secondary diagnostic
never gates `formal_verdict` (frozen contract, explicit), shipping the
PRIMARY verdict pipeline correctly now and the secondary diagnostic as a
clearly-labeled follow-up is a better trade than rushing a first, unreviewed
implementation of it into a report whose PRIMARY number is what any future
promotion decision actually depends on. `CohortResult.secondary_diagnostic`
is `None` throughout this version; `render_markdown` says so explicitly
rather than omitting the section silently.

Per-episode results are NOT separately dumped in the rendered Markdown/JSON
report: they live in the checkpoint artifact itself (every row the
checkpoint prefix contains, written via `write_dataset_artifact`), which is
the durable, reproducible record a reviewer or a later run can always read
back by fingerprint -- duplicating that into the rendered report would be
redundant with, and could drift from, the artifact that actually IS the
frozen result. Per-asset and per-week breakdowns ARE included in the
rendered report (`CohortResult.asset_breakdown`/`week_breakdown`) since
those are small, genuinely useful summary views a reviewer would otherwise
have to reconstruct from the artifact by hand.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from .clustered_inference import ClusterObservation, cluster_bootstrap_mean
from .exchange_registry import EXCHANGE_FACTORIES
from .market_path_cache import MarketPathCacheCorruptError
from .momentum_flow_bidirectional_burst_study import utc_week_key
from .ohlcv import fetch_symbol_candles
from .reporting import (
    json_ready,
    markdown_table,
    normalize_code_revision,
    parse_utc_datetime,
)
from .research_dataset_artifact import ArtifactWriteOutcome, write_dataset_artifact
from .source_lead_forward_cohort import (
    BOOTSTRAP_ITERATIONS,
    BOOTSTRAP_SEED,
    CHECKPOINT_DATASET_NAME,
    CHECKPOINT_DATASET_VERSION,
    CHECKPOINT_SCHEMA_VERSION,
    CONFIDENCE_LEVEL,
    CONTRACT_VERSION,
    ESTIMAND_VERSION,
    EVIDENCE_FLOOR,
    EXIT_BAR_TIMEFRAME_MS,
    EXIT_SLIPPAGE_BPS_ASSUMED,
    EXIT_SLIPPAGE_SENSITIVITY_BPS,
    HYPOTHESIS_ORIGIN,
    MAX_SINGLE_ASSET_EPISODE_SHARE,
    MAX_SINGLE_WEEK_EPISODE_SHARE,
    QUALIFICATION_VERSION,
    SECONDARY_DIAGNOSTIC_VERSION,
    SMALL_UNIVERSE_PROMOTION_NOTE,
    SOURCE_LEAD_FORWARD_COHORT_START,
    STOPPING_RULE,
    EpisodeInputs,
    EpisodeResult,
    episode_is_matured,
    expected_exit_boundary_ms,
    find_earliest_checkpoint_prefix_length,
    formal_verdict,
    resolve_episode_at_exit_slippage,
)
from .source_lead_forward_cohort_repository import (
    QUALIFIED_EPISODE_QUERY_VERSION,
    RawQualifiedEpisode,
    SourceLeadForwardCohortRepository,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from .ohlcv import Candle

logger = logging.getLogger("source-lead-forward-cohort-report")

REPORT_VERSION = "source_lead_forward_cohort_report_v2"
INTERPRETATION = "prospective_small_universe_no_trading_authorization"

# Generous headroom guard, this codebase's usual fail-loud-not-silently-
# large convention (see e.g. cex_activity_discovery_report.check_candidate_
# count) -- the entire identity- and route-verified universe is 14
# canonical assets (EVIDENCE_FLOOR's own "unreachable 30-cluster floor"
# rationale), so a cohort anywhere near this size would itself be the
# anomaly worth investigating, not a number to silently evaluate.
DEFAULT_MAX_QUALIFIED_EPISODES = 20_000

# Exit-bar fetch window padding around the expected exact boundary: wide
# enough to always contain MAX_EXIT_BAR_GAP_MINUTES worth of slack on
# either side, small enough to stay a cheap, single-page CCXT fetch per
# episode.
_FETCH_WINDOW_PAD_MS = 10 * 60_000

# Bounded concurrency for exit-bar fetches: colleague review, 2026-09-03 --
# up to --max-qualified-episodes (20,000 by default) coroutines used to
# launch in one asyncio.gather with no limit at all. A semaphore this size
# still lets a real (currently 14-asset) cohort's fetches finish quickly
# while keeping the exchange-facing concurrency at a sane, fixed ceiling
# regardless of how large the qualified-episode count ever grows.
DEFAULT_MAX_CONCURRENT_EXCHANGE_FETCHES = 10

# Wall-clock budget for the WHOLE exit-bar fetch phase, not any single
# fetch -- colleague review, 2026-09-03: no budget at all existed before,
# only per-request CCXT-level timeouts. 20 minutes is generous even for a
# few thousand episodes at the bounded concurrency above; a real run
# against the current ~14-asset universe finishes in seconds.
DEFAULT_EXCHANGE_FETCH_WALL_SECONDS = 20 * 60

_CHECKPOINT_ROW_ID_FIELD = "capture_id"
_CHECKPOINT_ROW_ORDER = "observed_at ascending, then capture_id ascending"


def check_qualified_episode_count(count: int, max_qualified_episodes: int) -> None:
    if count > max_qualified_episodes:
        raise ValueError(
            f"cohort produced {count} qualified episodes, over "
            f"--max-qualified-episodes={max_qualified_episodes}; investigate before "
            "raising the bound and silently evaluating an unexpectedly large result"
        )


@dataclass(frozen=True)
class CohortManifest:
    report_version: str
    interpretation: str
    contract_version: str
    estimand_version: str
    hypothesis_origin: str
    qualification_version: str
    qualified_episode_query_version: str
    code_revision: str
    working_tree_dirty: bool
    generated_at: datetime
    since: datetime
    checkpoint_reached: bool
    checkpoint_fingerprint: str | None
    checkpoint_artifact_outcome: str | None


@dataclass(frozen=True)
class CohortFunnel:
    qualified_episodes_total: int
    qualified_episodes_matured: int
    checkpoint_prefix_length: int | None
    resolved_episodes: int
    unresolved_by_reason: dict[str, int]


@dataclass(frozen=True)
class SlippageSensitivityPoint:
    exit_slippage_bps: float
    is_primary: bool
    resolved_episodes: int
    mean_net_return_pct: float | None
    ci_lower_bound_pct: float | None
    ci_upper_bound_pct: float | None


@dataclass(frozen=True)
class AssetBreakdownRow:
    canonical_asset_id: str
    resolved_episodes: int
    mean_net_return_pct: float | None


@dataclass(frozen=True)
class WeekBreakdownRow:
    utc_week: str
    resolved_episodes: int
    mean_net_return_pct: float | None


@dataclass(frozen=True)
class CohortResult:
    resolved_episodes: int
    distinct_asset_clusters: int
    distinct_utc_weeks: int
    max_single_asset_share: float
    max_single_week_share: float
    mean_net_return_pct: float | None
    ci_lower_bound_pct: float | None
    ci_upper_bound_pct: float | None
    verdict: str
    secondary_diagnostic: str | None
    slippage_sensitivity: tuple[SlippageSensitivityPoint, ...]
    asset_breakdown: tuple[AssetBreakdownRow, ...]
    week_breakdown: tuple[WeekBreakdownRow, ...]


@dataclass(frozen=True)
class SourceLeadForwardCohortReport:
    manifest: CohortManifest
    funnel: CohortFunnel
    result: CohortResult
    caveats: tuple[str, ...]


def _resolve_one(
    episode: RawQualifiedEpisode, exit_bar: Candle | None, *, exit_slippage_bps: float
) -> EpisodeResult:
    liquidity = episode.liquidity if isinstance(episode.liquidity, dict) else {}
    ask_vwap = liquidity.get("ask_vwap")
    if not isinstance(ask_vwap, int | float) or not (ask_vwap > 0):
        return EpisodeResult(episode.base, False, "invalid_market_data", None)
    return resolve_episode_at_exit_slippage(
        EpisodeInputs(
            base=episode.base,
            entry_at=episode.observed_at,
            entry_price=float(ask_vwap),
            entry_notional_usd=episode.requested_notional_usd,
            exit_bar=exit_bar,
        ),
        exit_slippage_bps=exit_slippage_bps,
    )


async def _fetch_exit_bar(
    clients: dict[str, object],
    episode: RawQualifiedEpisode,
) -> Candle | None:
    client = clients.get(episode.target_exchange)
    unified_symbol = episode.instrument.get("unified_symbol")
    if client is None or not isinstance(unified_symbol, str) or not unified_symbol:
        return None
    boundary_ms = expected_exit_boundary_ms(episode.observed_at)
    candles = await fetch_symbol_candles(
        client,
        unified_symbol,
        boundary_ms - _FETCH_WINDOW_PAD_MS,
        boundary_ms + _FETCH_WINDOW_PAD_MS,
        timeframe="1m",
        timeframe_ms=EXIT_BAR_TIMEFRAME_MS,
        use_cache=True,
    )
    candidates = [candle for candle in candles if candle.ts_ms >= boundary_ms]
    if not candidates:
        return None
    return min(candidates, key=lambda candle: candle.ts_ms)


async def _fetch_exit_bar_guarded(
    clients: dict[str, object],
    episode: RawQualifiedEpisode,
    semaphore: asyncio.Semaphore,
) -> Candle | None:
    """Bounded (via `semaphore`) and isolated: an ordinary per-episode fetch
    failure (network error, exchange-side error, an incomplete page) is
    logged and treated as a missing exit bar -- `resolve_episode` already
    has a real `missing_exit_bar` outcome for that, so this does not need a
    new one. A `MarketPathCacheCorruptError` is NOT caught here: a
    corrupted immutable cache is a systemic problem across potentially many
    episodes, not a per-episode data gap, and must fail this whole run
    loudly rather than quietly degrade every episode that happens to hit
    the same corrupt cache entry into a false `missing_exit_bar`."""
    async with semaphore:
        try:
            return await _fetch_exit_bar(clients, episode)
        except MarketPathCacheCorruptError:
            raise
        except Exception:
            logger.warning(
                "exit-bar fetch failed for capture_id=%s (%s, %s) -- treating as missing_exit_bar",
                episode.capture_id,
                episode.target_exchange,
                episode.base,
                exc_info=True,
            )
            return None


async def _fetch_exit_bars_bounded(
    clients: dict[str, object],
    episodes: Sequence[RawQualifiedEpisode],
    *,
    max_concurrency: int,
    wall_seconds: float,
) -> list[Candle | None]:
    semaphore = asyncio.Semaphore(max_concurrency)
    try:
        return await asyncio.wait_for(
            asyncio.gather(
                *(_fetch_exit_bar_guarded(clients, episode, semaphore) for episode in episodes)
            ),
            timeout=wall_seconds,
        )
    except TimeoutError as exc:
        raise TimeoutError(
            f"exit-bar fetch phase exceeded its own wall_seconds={wall_seconds} budget "
            f"fetching {len(episodes)} episodes at max_concurrency={max_concurrency} -- "
            "narrow the cohort, raise the bound explicitly, or investigate why exchange "
            "fetches are taking far longer than normal rather than letting this run "
            "indefinitely"
        ) from exc


def _slippage_sensitivity_point(
    *,
    exit_slippage_bps: float,
    matured: Sequence[RawQualifiedEpisode],
    exit_bars: Sequence[Candle | None],
) -> tuple[SlippageSensitivityPoint, tuple[EpisodeResult, ...]]:
    results = tuple(
        _resolve_one(episode, exit_bar, exit_slippage_bps=exit_slippage_bps)
        for episode, exit_bar in zip(matured, exit_bars, strict=True)
    )
    # Same length as matured/results throughout -- filtering happens once,
    # via the `if result.resolved` guard below, not by first building a
    # shorter "resolved-only" list and then re-zipping it against the
    # full-length matured/results (that mismatch is exactly what an earlier
    # version of this function did, caught by
    # test_aggregate_cohort_tracks_unresolved_reasons_within_the_checkpoint
    # once a fixture actually had an unresolved episode inside the
    # checkpoint prefix).
    resolved_pairs = [
        (episode, result.net_return_pct)
        for episode, result in zip(matured, results, strict=True)
        if result.resolved
    ]
    # mypy narrowing: resolve_episode_at_exit_slippage's own contract
    # guarantees resolved=True always pairs with a non-None net_return_pct.
    resolved_values = [value for _episode, value in resolved_pairs if value is not None]
    mean_net_return_pct = (
        round(sum(resolved_values) / len(resolved_values), 6) if resolved_values else None
    )
    ci_lower: float | None = None
    ci_upper: float | None = None
    if resolved_values:
        observations = tuple(
            ClusterObservation(cluster_key=episode.canonical_asset_id, value=value)
            for episode, value in resolved_pairs
            if value is not None
        )
        computation = cluster_bootstrap_mean(
            observations,
            iterations=BOOTSTRAP_ITERATIONS,
            seed=BOOTSTRAP_SEED,
            confidence_level=CONFIDENCE_LEVEL,
        )
        ci_lower = round(computation.estimate.lower_bound, 6)
        ci_upper = round(computation.estimate.upper_bound, 6)
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


@dataclass(frozen=True)
class CohortAggregate:
    funnel: CohortFunnel
    result: CohortResult
    checkpoint_rows: tuple[dict[str, Any], ...]


def aggregate_cohort(
    *,
    raw_episodes_count: int,
    matured: Sequence[RawQualifiedEpisode],
    exit_bars: Sequence[Candle | None],
) -> CohortAggregate:
    """Pure aggregation from already-matured episodes (with their own
    already-fetched exit bars) to the report's own funnel/result -- no I/O.
    Kept separate from `generate_report` specifically so it can be
    exercised directly against synthetic `RawQualifiedEpisode`/`Candle`
    fixtures before any real qualified capture exists (see this module's
    own docstring and `test_source_lead_forward_cohort_report.py`), the
    same discipline `source_lead_forward_cohort.py`'s own pure
    `resolve_episode`/`formal_verdict` were built under.

    Resolves at ALL of `EXIT_SLIPPAGE_SENSITIVITY_BPS` internally (resolved
    status does not depend on slippage, only `net_return_pct` does, so the
    checkpoint prefix boundary found via the PRIMARY slippage's own
    resolved/week outcomes applies identically to every sensitivity point)
    -- clusters and concentration are computed from `canonical_asset_id`,
    never `base`."""
    if len(matured) != len(exit_bars):
        raise ValueError("matured and exit_bars must be the same length and order")

    primary_results = tuple(
        _resolve_one(episode, exit_bar, exit_slippage_bps=EXIT_SLIPPAGE_BPS_ASSUMED)
        for episode, exit_bar in zip(matured, exit_bars, strict=True)
    )
    week_keys = [utc_week_key(episode.observed_at) for episode in matured]
    outcomes = [
        (week_keys[i] if primary_results[i].resolved else None, primary_results[i].resolved)
        for i in range(len(matured))
    ]
    checkpoint_prefix_length = find_earliest_checkpoint_prefix_length(outcomes)

    if checkpoint_prefix_length is None:
        checkpoint_matured: Sequence[RawQualifiedEpisode] = ()
        checkpoint_exit_bars: Sequence[Candle | None] = ()
        checkpoint_primary_results: Sequence[EpisodeResult] = ()
    else:
        checkpoint_matured = matured[:checkpoint_prefix_length]
        checkpoint_exit_bars = exit_bars[:checkpoint_prefix_length]
        checkpoint_primary_results = primary_results[:checkpoint_prefix_length]

    unresolved_reasons: list[str] = []
    for result in checkpoint_primary_results:
        if result.resolved:
            continue
        assert result.unresolved_reason is not None
        unresolved_reasons.append(result.unresolved_reason)
    unresolved_by_reason = dict(Counter(unresolved_reasons))

    resolved_episodes = [
        (episode, result)
        for episode, result in zip(checkpoint_matured, checkpoint_primary_results, strict=True)
        if result.resolved
    ]
    weeks_by_episode = [utc_week_key(episode.observed_at) for episode, _result in resolved_episodes]
    distinct_asset_clusters = len(
        {episode.canonical_asset_id for episode, _result in resolved_episodes}
    )
    distinct_utc_weeks = len(set(weeks_by_episode))
    asset_counts = Counter(episode.canonical_asset_id for episode, _result in resolved_episodes)
    week_counts = Counter(weeks_by_episode)
    resolved_count = len(resolved_episodes)
    max_single_asset_share = max(asset_counts.values()) / resolved_count if resolved_count else 0.0
    max_single_week_share = max(week_counts.values()) / resolved_count if resolved_count else 0.0

    sensitivity_points: list[SlippageSensitivityPoint] = []
    primary_point: SlippageSensitivityPoint | None = None
    for exit_slippage_bps in EXIT_SLIPPAGE_SENSITIVITY_BPS:
        point, _results = _slippage_sensitivity_point(
            exit_slippage_bps=exit_slippage_bps,
            matured=checkpoint_matured,
            exit_bars=checkpoint_exit_bars,
        )
        sensitivity_points.append(point)
        if point.is_primary:
            primary_point = point
    assert primary_point is not None  # EXIT_SLIPPAGE_SENSITIVITY_BPS always includes the primary

    verdict = formal_verdict(
        resolved_episodes=resolved_count,
        distinct_asset_clusters=distinct_asset_clusters,
        distinct_utc_weeks=distinct_utc_weeks,
        max_single_asset_share=max_single_asset_share,
        max_single_week_share=max_single_week_share,
        ci_lower_bound_pct=primary_point.ci_lower_bound_pct,
    )

    asset_returns: dict[str, list[float]] = {}
    for episode, result in resolved_episodes:
        assert result.net_return_pct is not None
        asset_returns.setdefault(episode.canonical_asset_id, []).append(result.net_return_pct)
    asset_breakdown = tuple(
        AssetBreakdownRow(
            canonical_asset_id=canonical_asset_id,
            resolved_episodes=len(returns),
            mean_net_return_pct=round(sum(returns) / len(returns), 6),
        )
        for canonical_asset_id, returns in sorted(asset_returns.items())
    )

    week_returns: dict[str, list[float]] = {}
    for (_episode, result), week_key in zip(resolved_episodes, weeks_by_episode, strict=True):
        assert result.net_return_pct is not None
        week_returns.setdefault(week_key, []).append(result.net_return_pct)
    week_breakdown = tuple(
        WeekBreakdownRow(
            utc_week=week_key,
            resolved_episodes=len(returns),
            mean_net_return_pct=round(sum(returns) / len(returns), 6),
        )
        for week_key, returns in sorted(week_returns.items())
    )

    checkpoint_rows = tuple(
        {
            _CHECKPOINT_ROW_ID_FIELD: episode.capture_id,
            "base": episode.base,
            "canonical_asset_id": episode.canonical_asset_id,
            "target_exchange": episode.target_exchange,
            "observed_at": episode.observed_at.isoformat(),
            "utc_week_key": utc_week_key(episode.observed_at),
            "resolved": result.resolved,
            "unresolved_reason": result.unresolved_reason,
            "net_return_pct_at_primary_slippage": result.net_return_pct,
        }
        for episode, result in zip(checkpoint_matured, checkpoint_primary_results, strict=True)
    )

    return CohortAggregate(
        funnel=CohortFunnel(
            qualified_episodes_total=raw_episodes_count,
            qualified_episodes_matured=len(matured),
            checkpoint_prefix_length=checkpoint_prefix_length,
            resolved_episodes=resolved_count,
            unresolved_by_reason=unresolved_by_reason,
        ),
        result=CohortResult(
            resolved_episodes=resolved_count,
            distinct_asset_clusters=distinct_asset_clusters,
            distinct_utc_weeks=distinct_utc_weeks,
            max_single_asset_share=round(max_single_asset_share, 4),
            max_single_week_share=round(max_single_week_share, 4),
            mean_net_return_pct=primary_point.mean_net_return_pct,
            ci_lower_bound_pct=primary_point.ci_lower_bound_pct,
            ci_upper_bound_pct=primary_point.ci_upper_bound_pct,
            verdict=verdict,
            secondary_diagnostic=None,
            slippage_sensitivity=tuple(sensitivity_points),
            asset_breakdown=asset_breakdown,
            week_breakdown=week_breakdown,
        ),
        checkpoint_rows=checkpoint_rows,
    )


async def generate_report(args: argparse.Namespace) -> SourceLeadForwardCohortReport:
    if args.since != SOURCE_LEAD_FORWARD_COHORT_START:
        raise ValueError(
            f"--since must equal the frozen SOURCE_LEAD_FORWARD_COHORT_START "
            f"({SOURCE_LEAD_FORWARD_COHORT_START.isoformat()}); this contract registers "
            "exactly one cohort boundary, not an arbitrary window"
        )
    code_revision = normalize_code_revision(args.code_revision)
    repository = SourceLeadForwardCohortRepository.from_url(os.environ["DATABASE_URL"])
    database_now = await repository.database_now()
    raw_episodes = await repository.fetch_qualified_episodes(
        qualification_version=QUALIFICATION_VERSION,
        since=args.since,
        limit=args.max_qualified_episodes + 1,
    )
    check_qualified_episode_count(len(raw_episodes), args.max_qualified_episodes)

    matured = [
        episode for episode in raw_episodes if episode_is_matured(episode.observed_at, database_now)
    ]

    clients = {exchange: factory() for exchange, factory in EXCHANGE_FACTORIES.items()}
    try:
        exit_bars = await _fetch_exit_bars_bounded(
            clients,
            matured,
            max_concurrency=args.max_concurrent_exchange_fetches,
            wall_seconds=args.exchange_fetch_wall_seconds,
        )
    finally:
        await asyncio.gather(*(client.close() for client in clients.values()))

    aggregate = aggregate_cohort(
        raw_episodes_count=len(raw_episodes), matured=matured, exit_bars=exit_bars
    )

    checkpoint_fingerprint: str | None = None
    checkpoint_artifact_outcome: str | None = None
    if aggregate.funnel.checkpoint_prefix_length is not None:
        outcome, manifest = write_dataset_artifact(
            dataset_name=CHECKPOINT_DATASET_NAME,
            dataset_version=CHECKPOINT_DATASET_VERSION,
            schema_version=CHECKPOINT_SCHEMA_VERSION,
            rows=list(aggregate.checkpoint_rows),
            row_id_field=_CHECKPOINT_ROW_ID_FIELD,
            row_order=_CHECKPOINT_ROW_ORDER,
            cohort={
                "since": args.since.isoformat(),
                "qualification_version": QUALIFICATION_VERSION,
                "evidence_floor": EVIDENCE_FLOOR,
            },
            code_revision=code_revision,
            working_tree_dirty=args.working_tree_dirty,
        )
        checkpoint_artifact_outcome = outcome.value
        if outcome in (ArtifactWriteOutcome.CREATED, ArtifactWriteOutcome.ALREADY_EXISTS):
            assert manifest is not None
            checkpoint_fingerprint = manifest.fingerprint
        else:
            raise ValueError(
                f"failed to persist the source-lead forward cohort checkpoint: {outcome.value} "
                "-- STOPPING_RULE requires this checkpoint to be durably recorded before its "
                "verdict can be trusted as 'evaluated exactly once'; investigate the artifact "
                "directory rather than silently returning an unpersisted verdict"
            )

    return SourceLeadForwardCohortReport(
        manifest=CohortManifest(
            report_version=REPORT_VERSION,
            interpretation=INTERPRETATION,
            contract_version=CONTRACT_VERSION,
            estimand_version=ESTIMAND_VERSION,
            hypothesis_origin=HYPOTHESIS_ORIGIN,
            qualification_version=QUALIFICATION_VERSION,
            qualified_episode_query_version=QUALIFIED_EPISODE_QUERY_VERSION,
            code_revision=code_revision,
            working_tree_dirty=args.working_tree_dirty,
            generated_at=database_now,
            since=args.since,
            checkpoint_reached=aggregate.funnel.checkpoint_prefix_length is not None,
            checkpoint_fingerprint=checkpoint_fingerprint,
            checkpoint_artifact_outcome=checkpoint_artifact_outcome,
        ),
        funnel=aggregate.funnel,
        result=aggregate.result,
        caveats=(
            "Prospective, small-universe estimand (gate -> binance only, 14 canonical "
            "assets) -- not a same-methodology confirmation of HYP-012's original "
            "4-route paired family; see the module docstring.",
            f"Secondary diagnostic ({SECONDARY_DIAGNOSTIC_VERSION}) is not yet computed "
            "by this report -- see this file's own 'Honest scope note'. It never gates "
            "the verdict above even once implemented.",
            "Exit price is an OHLCV-close proxy -- see the slippage-sensitivity table "
            "for how the result moves across 0bps / primary / 2x primary, not a "
            "guarantee a real fill could never be worse than any of these.",
            SMALL_UNIVERSE_PROMOTION_NOTE,
            STOPPING_RULE,
        ),
    )


def render_json(report: SourceLeadForwardCohortReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True)


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}%"


def render_markdown(report: SourceLeadForwardCohortReport) -> str:
    manifest = report.manifest
    funnel = report.funnel
    result = report.result
    interval = (
        "n/a"
        if result.ci_lower_bound_pct is None
        else f"[{result.ci_lower_bound_pct:.4f}%, {result.ci_upper_bound_pct:.4f}%]"
    )
    lines = [
        "# Source-Lead Forward Cohort",
        "",
        "Prospective, small-universe measurement. A `candidate` verdict does not by "
        "itself authorize paper or live execution -- see the small-universe promotion "
        "note below.",
        "",
        f"Cohort start: `{manifest.since.isoformat()}`",
        f"Contract: `{manifest.contract_version}` / estimand `{manifest.estimand_version}` "
        f"(motivated by `{manifest.hypothesis_origin}`, not a claimed replication of it)",
        f"Generated at: `{manifest.generated_at.isoformat()}`",
        f"Checkpoint reached: `{manifest.checkpoint_reached}`"
        + (
            f" (fingerprint `{manifest.checkpoint_fingerprint}`, "
            f"artifact `{manifest.checkpoint_artifact_outcome}`)"
            if manifest.checkpoint_fingerprint
            else ""
        ),
        "",
        "## Funnel",
        "",
        *markdown_table(
            ("Stage", "Count"),
            [
                ("Qualified episodes (total)", funnel.qualified_episodes_total),
                ("Matured (exit bar fully closed)", funnel.qualified_episodes_matured),
                (
                    "Checkpoint prefix length",
                    funnel.checkpoint_prefix_length
                    if funnel.checkpoint_prefix_length is not None
                    else "n/a (floor not yet reached)",
                ),
                ("Resolved (within checkpoint)", funnel.resolved_episodes),
                *[
                    (f"Unresolved: {reason}", count)
                    for reason, count in sorted(funnel.unresolved_by_reason.items())
                ],
            ],
        ),
        "",
        "## Frozen primary result",
        "",
        *markdown_table(
            (
                "Resolved",
                "Asset clusters",
                "UTC weeks",
                "Max asset share",
                "Max week share",
                "Mean net return",
                "95% cluster CI",
                "Verdict",
            ),
            [
                (
                    result.resolved_episodes,
                    result.distinct_asset_clusters,
                    result.distinct_utc_weeks,
                    f"{result.max_single_asset_share:.2%}",
                    f"{result.max_single_week_share:.2%}",
                    _fmt_pct(result.mean_net_return_pct),
                    interval,
                    result.verdict,
                )
            ],
        ),
        "",
        f"Evidence floor: {EVIDENCE_FLOOR['min_resolved_episodes']} resolved episodes, "
        f"{EVIDENCE_FLOOR['min_distinct_asset_clusters']} distinct asset clusters, "
        f"{EVIDENCE_FLOOR['min_distinct_utc_weeks']} distinct UTC weeks, concentration caps "
        f"{MAX_SINGLE_ASSET_EPISODE_SHARE:.0%} asset / {MAX_SINGLE_WEEK_EPISODE_SHARE:.0%} week.",
        "",
        "## Exit-slippage sensitivity (REQUIRE_EXIT_SLIPPAGE_SENSITIVITY)",
        "",
        *markdown_table(
            ("Exit slippage (bps)", "Primary?", "Resolved", "Mean net return", "95% cluster CI"),
            [
                (
                    f"{point.exit_slippage_bps:.0f}",
                    "yes" if point.is_primary else "",
                    point.resolved_episodes,
                    _fmt_pct(point.mean_net_return_pct),
                    (
                        "n/a"
                        if point.ci_lower_bound_pct is None
                        else f"[{point.ci_lower_bound_pct:.4f}%, {point.ci_upper_bound_pct:.4f}%]"
                    ),
                )
                for point in result.slippage_sensitivity
            ],
        ),
        "",
        "## Per-asset breakdown",
        "",
        *(
            markdown_table(
                ("Canonical asset", "Resolved", "Mean net return"),
                [
                    (
                        row.canonical_asset_id,
                        row.resolved_episodes,
                        _fmt_pct(row.mean_net_return_pct),
                    )
                    for row in result.asset_breakdown
                ],
            )
            if result.asset_breakdown
            else ["_No resolved episodes yet._"]
        ),
        "",
        "## Per-week breakdown",
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
        "## Caveats",
        "",
        *(f"- {item}" for item in report.caveats),
    ]
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="source_lead_forward_cohort_v1 -- prospective forward cohort report"
    )
    parser.add_argument(
        "--since", type=parse_utc_datetime, default=SOURCE_LEAD_FORWARD_COHORT_START
    )
    parser.add_argument(
        "--max-qualified-episodes",
        type=int,
        default=DEFAULT_MAX_QUALIFIED_EPISODES,
        help=(
            f"default {DEFAULT_MAX_QUALIFIED_EPISODES}; fails loudly rather than silently "
            "evaluating an unexpectedly large result"
        ),
    )
    parser.add_argument(
        "--max-concurrent-exchange-fetches",
        type=int,
        default=DEFAULT_MAX_CONCURRENT_EXCHANGE_FETCHES,
        help=f"default {DEFAULT_MAX_CONCURRENT_EXCHANGE_FETCHES}",
    )
    parser.add_argument(
        "--exchange-fetch-wall-seconds",
        type=float,
        default=DEFAULT_EXCHANGE_FETCH_WALL_SECONDS,
        help=f"default {DEFAULT_EXCHANGE_FETCH_WALL_SECONDS}",
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
