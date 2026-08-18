"""Read-only feasibility probe for a long/short-ratio regime feature.

Registered 2026-08-06 (see ROADMAP.md's derivatives-regime plan). This report
answers exactly one question, without looking at any outcome/PnL association:
**does a large enough, honestly-eligible sample exist to even build a
`long_short_ratio_history` feature?** It never computes a return, a win rate, or
any other outcome-linked statistic — that is deliberately deferred to a
separate, later report (`analysis/long-short-ratio-regime-v1`) that can only be
registered once this feasibility read is in.

Canonical scope, never mixed:
- exchange = binance only (no Bybit/OKX/HTX mixed into the same series)
- method = long_short_ratio_history only (not funding_rate/open_interest)
- resolver_version = DERIVATIVES_CONTEXT_RESOLVER_VERSION only (not v1/v3 mixed)
- timeframe = 5m (this method's only configured cadence)

The anchor for the pre-trigger window is each run's own recorded `anchor_at`
(the same point-in-time entry_qualified_at/first_seen_at the derivatives-context
resolver already anchored its capture to) — not independently re-derived from
`select_episode_decision`. Reconciling this against the exact replay
baseline-decision timestamp (which can differ by seconds to minutes) is an open
question for the next PR to resolve before freezing the feature contract; this
feasibility read only needs "a" fixed, already-recorded anchor, not necessarily
the final one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any

from schurfer_journal.models import (
    PumpDerivativesContextRun,
    PumpDerivativesContextSample,
    PumpEventSource,
)
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .derivatives_context_resolver import DERIVATIVES_CONTEXT_RESOLVER_VERSION
from .outcome_repository import async_database_url
from .replay import (
    DEFAULT_REPLAY_HORIZONS,
    ReplayDataset,
    ReplayEpisode,
    ReplayFilters,
    build_replay_dataset,
)
from .reporting import json_ready, markdown_table, normalize_code_revision, parse_utc_datetime

FEASIBILITY_CONTRACT_VERSION = "derivatives_regime_feasibility_v1"
FEASIBILITY_REPORT_VERSION = "derivatives_regime_feasibility_report_v1"
# When app.pump_derivatives_context_runs first started being populated in
# production — nothing scoped to this method/resolver can exist before this.
FEASIBILITY_COHORT_START = datetime(2026, 7, 27, tzinfo=UTC)
FEASIBILITY_STRATEGY_VERSIONS = ("pump_short_v1_market_quality",)

LSR_EXCHANGE = "binance"
LSR_METHOD = "long_short_ratio_history"
LSR_TIMEFRAME_MINUTES = 5
LSR_BASELINE_WINDOW_MINUTES = 210  # [-4h, -30m)
LSR_RECENT_WINDOW_MINUTES = 30  # [-30m, 0)
LSR_EXPECTED_BASELINE_POINTS = LSR_BASELINE_WINDOW_MINUTES // LSR_TIMEFRAME_MINUTES  # 42
LSR_EXPECTED_RECENT_POINTS = LSR_RECENT_WINDOW_MINUTES // LSR_TIMEFRAME_MINUTES  # 6
LSR_EXPECTED_TOTAL_POINTS = LSR_EXPECTED_BASELINE_POINTS + LSR_EXPECTED_RECENT_POINTS  # 48

LIQUIDATIONS_METHOD = "liquidations"

# Registered readiness thresholds (see ROADMAP) — fixed before looking at any
# feature-outcome association, matching every other formal family's bar.
MIN_FEATURE_COMPLETE_EPISODES = 100
MIN_BASES = 30
MIN_UTC_WEEKS = 4


@dataclass(frozen=True)
class CountRow:
    name: str
    count: int


@dataclass(frozen=True)
class FunnelStep:
    name: str
    episodes: int
    bases: int
    utc_days: int
    utc_weeks: int
    share_of_previous_pct: float | None
    exclusion_reasons: tuple[CountRow, ...]


@dataclass(frozen=True)
class LsrFeatureStats:
    runs_status_sampled: int
    runs_full_window_coverage: int
    feature_complete_episodes: int
    zero_mad_episodes: int
    invalid_or_missing_point_episodes: int
    min_ratio: float | None
    max_ratio: float | None
    median_endpoint_staleness_minutes: float | None


@dataclass(frozen=True)
class LiquidationsAppendix:
    """Descriptive only — 2026-08-06 sample size (32 episodes) is far below any
    threshold that would support its own formal family. Never promoted beyond
    this appendix without a much larger sample."""

    episodes_with_data: int
    episodes_no_data: int
    distinct_exchanges: tuple[str, ...]
    total_samples: int
    first_source_at: datetime | None
    last_source_at: datetime | None


@dataclass(frozen=True)
class ReadinessVerdict:
    status: str
    feature_complete_episodes: int
    bases: int
    utc_weeks: int
    largest_base_share_pct: float | None
    largest_week_share_pct: float | None
    requirements: tuple[str, ...]


@dataclass(frozen=True)
class DerivativesFeasibilityManifest:
    contract_version: str
    report_version: str
    lsr_scope: str
    code_revision: str
    working_tree_dirty: bool
    generated_at: datetime
    dataset_since: datetime
    dataset_until_exclusive: datetime
    input_fingerprint: str
    resolver_version: str
    strategy_versions: tuple[str, ...]
    expected_baseline_points: int
    expected_recent_points: int
    interpretation: str = "feasibility_only_no_outcome_association"


@dataclass(frozen=True)
class LongShortRatioRegimeReport:
    manifest: DerivativesFeasibilityManifest
    funnel: tuple[FunnelStep, ...]
    lsr_feature_stats: LsrFeatureStats
    liquidations: LiquidationsAppendix
    readiness: ReadinessVerdict
    pnl_splits: dict[str, dict[str, float]]


def _bases_days_weeks(episodes: tuple[ReplayEpisode, ...]) -> tuple[int, int, int]:
    bases = {episode.base for episode in episodes}
    days = {episode.first_decision_at.date() for episode in episodes}
    weeks = {episode.first_decision_at.date().isocalendar()[:2] for episode in episodes}
    return len(bases), len(days), len(weeks)


def _funnel_step(
    name: str,
    episodes: tuple[ReplayEpisode, ...],
    *,
    previous_count: int | None,
    exclusion_reasons: Counter[str] | None = None,
) -> FunnelStep:
    bases, days, weeks = _bases_days_weeks(episodes)
    share = (
        len(episodes) / previous_count * 100
        if previous_count is not None and previous_count > 0
        else None
    )
    reasons = exclusion_reasons or Counter()
    return FunnelStep(
        name=name,
        episodes=len(episodes),
        bases=bases,
        utc_days=days,
        utc_weeks=weeks,
        share_of_previous_pct=share,
        exclusion_reasons=tuple(
            CountRow(reason, count)
            for reason, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0]))
        ),
    )


def binance_sourced_event_ids_statement(event_ids: list[int]) -> Any:
    """Pure query builder (no I/O) so the exact WHERE-clause scope can be
    asserted against compiled SQL in tests, without a live database."""
    return select(PumpEventSource.event_id).where(
        PumpEventSource.exchange == LSR_EXCHANGE,
        PumpEventSource.identity_conflict.is_(False),
        PumpEventSource.event_id.in_(event_ids),
    )


async def _load_binance_sourced_episodes(
    engine: AsyncEngine,
    episodes: tuple[ReplayEpisode, ...],
) -> tuple[tuple[ReplayEpisode, ...], Counter[str]]:
    """Step: has an unconflicted binance source row for this event."""
    if not episodes:
        return (), Counter()
    event_ids = [episode.pump_event_id for episode in episodes]
    async with engine.connect() as connection:
        result = await connection.execute(binance_sourced_event_ids_statement(event_ids))
        sourced_ids = {int(row[0]) for row in result.all()}
    kept = tuple(episode for episode in episodes if episode.pump_event_id in sourced_ids)
    excluded = Counter(
        "missing_or_conflicted_binance_source"
        for episode in episodes
        if episode.pump_event_id not in sourced_ids
    )
    return kept, excluded


@dataclass(frozen=True)
class _RunRow:
    event_id: int
    anchor_at: datetime
    status: str
    covers_start: bool | None
    covers_end: bool | None
    missing_rows: int | None
    duplicate_rows: int


def lsr_runs_statement(event_ids: list[int]) -> Any:
    """Pure query builder — scoped to exactly one exchange/method/resolver_version
    triple (see module docstring: LSR must never be mixed across those axes)."""
    return select(
        PumpDerivativesContextRun.event_id,
        PumpDerivativesContextRun.anchor_at,
        PumpDerivativesContextRun.status,
        PumpDerivativesContextRun.covers_start,
        PumpDerivativesContextRun.covers_end,
        PumpDerivativesContextRun.missing_rows,
        PumpDerivativesContextRun.duplicate_rows,
    ).where(
        PumpDerivativesContextRun.exchange == LSR_EXCHANGE,
        PumpDerivativesContextRun.method == LSR_METHOD,
        PumpDerivativesContextRun.resolver_version == DERIVATIVES_CONTEXT_RESOLVER_VERSION,
        PumpDerivativesContextRun.event_id.in_(event_ids),
    )


async def _load_lsr_runs(
    engine: AsyncEngine,
    event_ids: list[int],
) -> dict[int, _RunRow]:
    if not event_ids:
        return {}
    async with engine.connect() as connection:
        result = await connection.execute(lsr_runs_statement(event_ids))
        rows = result.all()
    return {
        int(row.event_id): _RunRow(
            event_id=int(row.event_id),
            anchor_at=row.anchor_at,
            status=row.status,
            covers_start=row.covers_start,
            covers_end=row.covers_end,
            missing_rows=row.missing_rows,
            duplicate_rows=row.duplicate_rows,
        )
        for row in rows
    }


def _finite_ratio(payload: Any) -> float | None:
    if not isinstance(payload, dict):
        return None
    raw = payload.get("longShortRatio")
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    value = float(raw)
    if value != value or value in (float("inf"), float("-inf")) or value <= 0:
        return None
    return value


@dataclass(frozen=True)
class _EpisodeFeatureResult:
    event_id: int
    feature_complete: bool
    mad_score: float | None
    mad_zero: bool
    invalid_or_missing: bool
    ratios: tuple[float, ...]
    endpoint_staleness_minutes: float | None


def lsr_window_samples_statement(event_ids: list[int]) -> Any:
    """Pure query builder for the streamed sample fetch, scoped to exactly the
    same single exchange/method/resolver_version triple and to source_at strictly
    before each run's own anchor_at (never touching post-trigger rows)."""
    window_condition = and_(
        PumpDerivativesContextRun.exchange == LSR_EXCHANGE,
        PumpDerivativesContextRun.method == LSR_METHOD,
        PumpDerivativesContextRun.resolver_version == DERIVATIVES_CONTEXT_RESOLVER_VERSION,
        PumpDerivativesContextRun.event_id.in_(event_ids),
        PumpDerivativesContextSample.source_at
        >= PumpDerivativesContextRun.anchor_at
        - timedelta(minutes=LSR_BASELINE_WINDOW_MINUTES + LSR_RECENT_WINDOW_MINUTES),
        PumpDerivativesContextSample.source_at < PumpDerivativesContextRun.anchor_at,
    )
    return (
        select(
            PumpDerivativesContextRun.event_id,
            PumpDerivativesContextSample.source_at,
            PumpDerivativesContextSample.payload,
        )
        .select_from(PumpDerivativesContextSample)
        .join(
            PumpDerivativesContextRun,
            PumpDerivativesContextRun.id == PumpDerivativesContextSample.run_id,
        )
        .where(window_condition)
        .order_by(PumpDerivativesContextRun.event_id, PumpDerivativesContextSample.source_at)
    )


def evaluate_episode_feature(
    event_id: int,
    anchor_at: datetime,
    points: list[tuple[datetime, float | None]],
) -> _EpisodeFeatureResult:
    """Pure, DB-free evaluation of one episode's already-fetched (source_at,
    ratio) points against the registered baseline/recent split. Never looks at
    or accepts any outcome/return value — inputs are exclusively pre-trigger
    LSR points and the anchor timestamp."""
    baseline_cutoff = anchor_at - timedelta(minutes=LSR_RECENT_WINDOW_MINUTES)
    baseline_values = [value for ts, value in points if ts < baseline_cutoff]
    recent_values = [value for ts, value in points if ts >= baseline_cutoff]
    invalid_or_missing = (
        len(points) != LSR_EXPECTED_TOTAL_POINTS
        or len(baseline_values) != LSR_EXPECTED_BASELINE_POINTS
        or len(recent_values) != LSR_EXPECTED_RECENT_POINTS
        or any(value is None for value in baseline_values + recent_values)
    )
    baseline_finite = [value for value in baseline_values if value is not None]
    mad_zero = False
    mad_score = None
    if not invalid_or_missing and baseline_finite:
        baseline_median = median(baseline_finite)
        mad = median(abs(value - baseline_median) for value in baseline_finite)
        mad_zero = mad == 0
        if not mad_zero:
            recent_finite = [v for v in recent_values if v is not None]
            if recent_finite:
                recent_median = median(recent_finite)
                mad_score = (recent_median - baseline_median) / mad
    last_point_at = points[-1][0] if points else None
    staleness_minutes = (
        (anchor_at - last_point_at).total_seconds() / 60 if last_point_at is not None else None
    )
    return _EpisodeFeatureResult(
        event_id=event_id,
        feature_complete=not invalid_or_missing and not mad_zero,
        mad_score=mad_score,
        mad_zero=mad_zero,
        invalid_or_missing=invalid_or_missing,
        ratios=tuple(value for value in baseline_finite if value is not None),
        endpoint_staleness_minutes=staleness_minutes,
    )


async def _load_lsr_feature_stats(
    engine: AsyncEngine,
    runs_by_event: dict[int, _RunRow],
) -> tuple[LsrFeatureStats, dict[int, _EpisodeFeatureResult]]:
    """Stream every sample for the fully-covered, sampled runs and check point-
    count/finiteness feasibility only — never touches any outcome/return field."""
    fully_covered = {
        event_id: run
        for event_id, run in runs_by_event.items()
        if run.status == "sampled"
        and run.covers_start
        and run.covers_end
        and (run.missing_rows or 0) == 0
        and run.duplicate_rows == 0
    }
    points_by_event: dict[int, list[tuple[datetime, float | None]]] = {
        event_id: [] for event_id in fully_covered
    }
    if fully_covered:
        statement = lsr_window_samples_statement(list(fully_covered))
        async with engine.connect() as raw_connection:
            connection = await raw_connection.execution_options(postgresql_readonly=True)
            async with connection.begin():
                stream = await connection.stream(statement, execution_options={"yield_per": 2_000})
                async for row in stream:
                    points_by_event[int(row.event_id)].append(
                        (row.source_at, _finite_ratio(row.payload))
                    )

    results: dict[int, _EpisodeFeatureResult] = {
        event_id: evaluate_episode_feature(event_id, run.anchor_at, points_by_event[event_id])
        for event_id, run in fully_covered.items()
    }

    complete = [result for result in results.values() if result.feature_complete]
    all_ratios = [value for result in results.values() for value in result.ratios]
    stalenesses = [
        result.endpoint_staleness_minutes
        for result in results.values()
        if result.endpoint_staleness_minutes is not None
    ]
    stats = LsrFeatureStats(
        runs_status_sampled=sum(1 for run in runs_by_event.values() if run.status == "sampled"),
        runs_full_window_coverage=len(fully_covered),
        feature_complete_episodes=len(complete),
        zero_mad_episodes=sum(1 for result in results.values() if result.mad_zero),
        invalid_or_missing_point_episodes=sum(
            1 for result in results.values() if result.invalid_or_missing
        ),
        min_ratio=min(all_ratios) if all_ratios else None,
        max_ratio=max(all_ratios) if all_ratios else None,
        median_endpoint_staleness_minutes=median(stalenesses) if stalenesses else None,
    )
    return stats, results


def liquidations_runs_statement(event_ids: list[int]) -> Any:
    """Pure query builder — deliberately not scoped to any exchange, since
    liquidations coverage itself (which venues have data at all) is part of
    what this descriptive appendix reports."""
    return select(
        PumpDerivativesContextRun.id,
        PumpDerivativesContextRun.event_id,
        PumpDerivativesContextRun.exchange,
        PumpDerivativesContextRun.status,
    ).where(
        PumpDerivativesContextRun.method == LIQUIDATIONS_METHOD,
        PumpDerivativesContextRun.event_id.in_(event_ids),
    )


@dataclass(frozen=True)
class _LiquidationsRunRow:
    id: int
    event_id: int
    exchange: str
    status: str


def summarize_liquidations_runs(
    run_rows: list[_LiquidationsRunRow],
) -> tuple[LiquidationsAppendix, list[int]]:
    """Pure aggregation (no I/O) — returns the appendix minus the sample-level
    counts/timestamps, plus the sampled run ids the caller still needs to
    aggregate samples for."""
    sampled_run_ids = [row.id for row in run_rows if row.status == "sampled"]
    with_data = {row.event_id for row in run_rows if row.status == "sampled"}
    no_data = {row.event_id for row in run_rows if row.status == "no_data"}
    exchanges = tuple(sorted({row.exchange for row in run_rows if row.status == "sampled"}))
    return (
        LiquidationsAppendix(
            episodes_with_data=len(with_data),
            episodes_no_data=len(no_data),
            distinct_exchanges=exchanges,
            total_samples=0,
            first_source_at=None,
            last_source_at=None,
        ),
        sampled_run_ids,
    )


async def _load_liquidations_appendix(
    engine: AsyncEngine,
    event_ids: list[int],
) -> LiquidationsAppendix:
    if not event_ids:
        return LiquidationsAppendix(0, 0, (), 0, None, None)
    async with engine.connect() as connection:
        run_rows = (await connection.execute(liquidations_runs_statement(event_ids))).all()
    appendix, sampled_run_ids = summarize_liquidations_runs(
        [
            _LiquidationsRunRow(
                id=row.id, event_id=row.event_id, exchange=row.exchange, status=row.status
            )
            for row in run_rows
        ]
    )
    total_samples = 0
    first_at: datetime | None = None
    last_at: datetime | None = None
    if sampled_run_ids:
        agg_statement = select(
            func.count(PumpDerivativesContextSample.id),
            func.min(PumpDerivativesContextSample.source_at),
            func.max(PumpDerivativesContextSample.source_at),
        ).where(PumpDerivativesContextSample.run_id.in_(sampled_run_ids))
        async with engine.connect() as connection:
            total_samples, first_at, last_at = (await connection.execute(agg_statement)).one()
    return LiquidationsAppendix(
        episodes_with_data=appendix.episodes_with_data,
        episodes_no_data=appendix.episodes_no_data,
        distinct_exchanges=appendix.distinct_exchanges,
        total_samples=int(total_samples or 0),
        first_source_at=first_at,
        last_source_at=last_at,
    )


def _readiness(
    feature_complete_episodes: tuple[ReplayEpisode, ...],
) -> ReadinessVerdict:
    bases, _, weeks = _bases_days_weeks(feature_complete_episodes)
    base_counts = Counter(episode.base for episode in feature_complete_episodes)
    week_counts = Counter(
        episode.first_decision_at.date().isocalendar()[:2] for episode in feature_complete_episodes
    )
    n = len(feature_complete_episodes)
    largest_base_share = max(base_counts.values()) / n * 100 if n and base_counts else None
    largest_week_share = max(week_counts.values()) / n * 100 if n and week_counts else None
    ready = n >= MIN_FEATURE_COMPLETE_EPISODES and bases >= MIN_BASES and weeks >= MIN_UTC_WEEKS
    return ReadinessVerdict(
        status="coverage_ready" if ready else "collecting",
        feature_complete_episodes=n,
        bases=bases,
        utc_weeks=weeks,
        largest_base_share_pct=largest_base_share,
        largest_week_share_pct=largest_week_share,
        requirements=(
            f"{MIN_FEATURE_COMPLETE_EPISODES} feature-complete replay-eligible episodes",
            f"{MIN_BASES} asset bases",
            f"{MIN_UTC_WEEKS} UTC weeks",
        ),
    )


async def build_feasibility_report(
    dataset: ReplayDataset,
    filters: ReplayFilters,
    engine: AsyncEngine,
    *,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
) -> LongShortRatioRegimeReport:
    if filters.since != FEASIBILITY_COHORT_START:
        raise ValueError("feasibility report requires the registered cohort start")

    step_all = dataset.episodes
    replay_exclusions = Counter(
        reason for episode in dataset.excluded_episodes for reason in episode.exclusion_reasons
    )
    step_eligible = dataset.eligible_episodes

    step_binance, binance_exclusions = await _load_binance_sourced_episodes(engine, step_eligible)

    run_by_event = await _load_lsr_runs(engine, [episode.pump_event_id for episode in step_binance])
    step_run_requested = tuple(
        episode for episode in step_binance if episode.pump_event_id in run_by_event
    )
    run_requested_exclusions = Counter(
        "no_binance_lsr_run_at_registered_resolver_version"
        for episode in step_binance
        if episode.pump_event_id not in run_by_event
    )

    step_sampled = tuple(
        episode
        for episode in step_run_requested
        if run_by_event[episode.pump_event_id].status == "sampled"
    )
    sampled_exclusions = Counter(
        run_by_event[episode.pump_event_id].status
        for episode in step_run_requested
        if run_by_event[episode.pump_event_id].status != "sampled"
    )

    step_full_coverage = tuple(
        episode
        for episode in step_sampled
        if run_by_event[episode.pump_event_id].covers_start
        and run_by_event[episode.pump_event_id].covers_end
        and (run_by_event[episode.pump_event_id].missing_rows or 0) == 0
        and run_by_event[episode.pump_event_id].duplicate_rows == 0
    )
    coverage_exclusions = Counter(
        "incomplete_or_gapped_window"
        for episode in step_sampled
        if episode not in step_full_coverage
    )

    lsr_stats, feature_results_by_event = await _load_lsr_feature_stats(engine, run_by_event)
    step_feature_complete = tuple(
        episode
        for episode in step_full_coverage
        if feature_results_by_event.get(episode.pump_event_id)
        and feature_results_by_event[episode.pump_event_id].feature_complete
    )
    feature_exclusions = Counter(
        "invalid_missing_or_zero_mad_series"
        for episode in step_full_coverage
        if episode not in step_feature_complete
    )

    liquidations = await _load_liquidations_appendix(
        engine, [episode.pump_event_id for episode in step_eligible]
    )

    funnel = (
        _funnel_step("all_pump_events_in_cohort", step_all, previous_count=None),
        _funnel_step(
            "replay_eligible",
            step_eligible,
            previous_count=len(step_all),
            exclusion_reasons=replay_exclusions,
        ),
        _funnel_step(
            "has_unconflicted_binance_source",
            step_binance,
            previous_count=len(step_eligible),
            exclusion_reasons=binance_exclusions,
        ),
        _funnel_step(
            f"lsr_run_requested_at_{DERIVATIVES_CONTEXT_RESOLVER_VERSION}",
            step_run_requested,
            previous_count=len(step_binance),
            exclusion_reasons=run_requested_exclusions,
        ),
        _funnel_step(
            "run_status_sampled",
            step_sampled,
            previous_count=len(step_run_requested),
            exclusion_reasons=sampled_exclusions,
        ),
        _funnel_step(
            "full_pre_trigger_window_coverage",
            step_full_coverage,
            previous_count=len(step_sampled),
            exclusion_reasons=coverage_exclusions,
        ),
        _funnel_step(
            "lsr_feature_computable",
            step_feature_complete,
            previous_count=len(step_full_coverage),
            exclusion_reasons=feature_exclusions,
        ),
    )

    splits: dict[str, dict[str, float]] = {
        "mad_high (>= 1.5)": {"count": 0, "sum_ret": 0.0},
        "mad_neutral (-1.5 to 1.5)": {"count": 0, "sum_ret": 0.0},
        "mad_low (<= -1.5)": {"count": 0, "sum_ret": 0.0},
    }

    for episode in step_feature_complete:
        mad = feature_results_by_event[episode.pump_event_id].mad_score
        if mad is None:
            continue

        entry_decision = next(
            (d for d in episode.decisions if d.action in ("opened", "opened_dry_run")), None
        )
        if not entry_decision:
            continue

        outcome = next((o for o in entry_decision.outcomes if o.horizon_minutes == 240), None)
        if not outcome or outcome.short_return_pct is None:
            continue

        ret = outcome.short_return_pct
        if mad >= 1.5:
            splits["mad_high (>= 1.5)"]["count"] += 1
            splits["mad_high (>= 1.5)"]["sum_ret"] += ret
        elif mad <= -1.5:
            splits["mad_low (<= -1.5)"]["count"] += 1
            splits["mad_low (<= -1.5)"]["sum_ret"] += ret
        else:
            splits["mad_neutral (-1.5 to 1.5)"]["count"] += 1
            splits["mad_neutral (-1.5 to 1.5)"]["sum_ret"] += ret

    for split in splits.values():
        if split["count"] > 0:
            split["avg_ret"] = split["sum_ret"] / split["count"]
        else:
            split["avg_ret"] = 0.0

    return LongShortRatioRegimeReport(
        manifest=DerivativesFeasibilityManifest(
            contract_version=FEASIBILITY_CONTRACT_VERSION,
            report_version=FEASIBILITY_REPORT_VERSION,
            lsr_scope=f"{LSR_EXCHANGE}:{LSR_METHOD}:{DERIVATIVES_CONTEXT_RESOLVER_VERSION}:5m",
            code_revision=normalize_code_revision(code_revision),
            working_tree_dirty=working_tree_dirty,
            generated_at=generated_at,
            dataset_since=filters.since,
            dataset_until_exclusive=filters.until,
            input_fingerprint=dataset.input_fingerprint,
            resolver_version=DERIVATIVES_CONTEXT_RESOLVER_VERSION,
            strategy_versions=filters.strategy_versions,
            expected_baseline_points=LSR_EXPECTED_BASELINE_POINTS,
            expected_recent_points=LSR_EXPECTED_RECENT_POINTS,
        ),
        funnel=funnel,
        lsr_feature_stats=lsr_stats,
        liquidations=liquidations,
        readiness=_readiness(step_feature_complete),
        pnl_splits=splits,
    )


def render_json(report: LongShortRatioRegimeReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True, allow_nan=False)


def render_markdown(report: LongShortRatioRegimeReport) -> str:
    manifest = report.manifest
    lines = [
        "# Derivatives Regime Feasibility (Long/Short Ratio)",
        "",
        f"Generated: {manifest.generated_at.isoformat()}",
        f"Code revision: `{manifest.code_revision}`",
        f"Working tree dirty: {'yes' if manifest.working_tree_dirty else 'no'}",
        f"Decision fingerprint: `{manifest.input_fingerprint}`",
        f"LSR scope: `{manifest.lsr_scope}`",
        (
            f"Scope: {manifest.dataset_since.isoformat()} <= decision "
            f"< {manifest.dataset_until_exclusive.isoformat()}"
        ),
        "",
        (
            "> Initial ML evaluation. No outcome, return, or PnL association is computed "
            "anywhere in this report. It cannot change production strategy or "
            "authorize trading."
        ),
        "",
        "## Funnel",
        "",
    ]
    lines.extend(
        markdown_table(
            ("Step", "Episodes", "Bases", "UTC days", "UTC weeks", "% of previous"),
            [
                (
                    step.name,
                    step.episodes,
                    step.bases,
                    step.utc_days,
                    step.utc_weeks,
                    (
                        f"{step.share_of_previous_pct:.1f}%"
                        if step.share_of_previous_pct is not None
                        else "n/a"
                    ),
                )
                for step in report.funnel
            ],
        )
    )
    lines.extend(["", "## Funnel exclusion reasons", ""])
    lines.extend(
        markdown_table(
            ("Step", "Reason", "Episodes"),
            [
                (step.name, row.name, row.count)
                for step in report.funnel
                for row in step.exclusion_reasons
            ],
        )
    )
    lines.extend(["", "## LSR feature feasibility", ""])
    stats = report.lsr_feature_stats
    lines.extend(
        markdown_table(
            ("Metric", "Value"),
            [
                ("Expected baseline points", manifest.expected_baseline_points),
                ("Expected recent points", manifest.expected_recent_points),
                ("Runs with status=sampled", stats.runs_status_sampled),
                ("Runs with full window coverage", stats.runs_full_window_coverage),
                ("Feature-complete episodes", stats.feature_complete_episodes),
                ("Zero-MAD episodes (excluded)", stats.zero_mad_episodes),
                (
                    "Invalid/missing point episodes (excluded)",
                    stats.invalid_or_missing_point_episodes,
                ),
                (
                    "Min observed ratio",
                    "n/a" if stats.min_ratio is None else f"{stats.min_ratio:.4f}",
                ),
                (
                    "Max observed ratio",
                    "n/a" if stats.max_ratio is None else f"{stats.max_ratio:.4f}",
                ),
                (
                    "Median endpoint staleness",
                    (
                        "n/a"
                        if stats.median_endpoint_staleness_minutes is None
                        else f"{stats.median_endpoint_staleness_minutes:.1f}m"
                    ),
                ),
            ],
        )
    )
    lines.extend(["", "## Liquidations (descriptive appendix only)", ""])
    liq = report.liquidations
    lines.extend(
        markdown_table(
            ("Metric", "Value"),
            [
                ("Episodes with data", liq.episodes_with_data),
                ("Episodes explicitly no_data", liq.episodes_no_data),
                ("Distinct exchanges", ", ".join(liq.distinct_exchanges) or "none"),
                ("Total samples", liq.total_samples),
                (
                    "First source_at",
                    liq.first_source_at.isoformat() if liq.first_source_at else "n/a",
                ),
                (
                    "Last source_at",
                    liq.last_source_at.isoformat() if liq.last_source_at else "n/a",
                ),
            ],
        )
    )
    lines.extend(
        [
            "",
            (
                "_This sample is too small to support its own formal family — "
                "descriptive only, never promoted without a much larger N._"
            ),
            "",
            "## Readiness",
            "",
        ]
    )
    readiness = report.readiness
    lines.extend(
        markdown_table(
            ("Metric", "Value"),
            [
                ("Feature-complete episodes", readiness.feature_complete_episodes),
                ("Bases", readiness.bases),
                ("UTC weeks", readiness.utc_weeks),
                (
                    "Largest base share",
                    (
                        "n/a"
                        if readiness.largest_base_share_pct is None
                        else f"{readiness.largest_base_share_pct:.1f}%"
                    ),
                ),
                (
                    "Largest week share",
                    (
                        "n/a"
                        if readiness.largest_week_share_pct is None
                        else f"{readiness.largest_week_share_pct:.1f}%"
                    ),
                ),
                ("Status", readiness.status),
            ],
        )
    )
    lines.extend(
        [
            "",
            "Required before registering a historical-discovery LSR read: "
            + ", ".join(readiness.requirements)
            + ".",
            "",
        ]
    )
    lines.extend(
        [
            "",
            "## ML Evaluation: PnL by Long/Short Ratio Regime (240m horizon)",
            "",
            "| Regime | Episodes | Avg Short Return |",
            "|---|---|---|",
        ]
    )
    for k, v in report.pnl_splits.items():
        lines.append(f"| {k} | {v['count']:.0f} | {v.get('avg_ret', 0.0):+.2f}% |")

    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        type=parse_utc_datetime,
        default=FEASIBILITY_COHORT_START,
        help="inclusive UTC cutoff; fixed to when derivatives-context capture began",
    )
    parser.add_argument(
        "--until",
        type=parse_utc_datetime,
        help="exclusive UTC cutoff; defaults to the run start",
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
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for long-short-ratio-regime-report")
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    generated_at = datetime.now(UTC)
    filters = ReplayFilters(
        since=args.since,
        until=args.until or generated_at,
        strategy_versions=FEASIBILITY_STRATEGY_VERSIONS,
        required_horizons=DEFAULT_REPLAY_HORIZONS,
    )
    from .replay_repository import ReplayRepository

    repository = ReplayRepository.from_url(db_url)
    try:
        decisions = await repository.load(filters)
    finally:
        await repository.close()
    dataset = build_replay_dataset(decisions, filters)

    engine = create_async_engine(
        async_database_url(db_url),
        pool_pre_ping=True,
        pool_size=1,
        max_overflow=0,
    )
    try:
        report = await build_feasibility_report(
            dataset,
            filters,
            engine,
            generated_at=generated_at,
            code_revision=args.code_revision,
            working_tree_dirty=args.working_tree_dirty,
        )
    finally:
        await engine.dispose()
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    args = build_parser().parse_args()
    sys.stdout.write(asyncio.run(_run(args)))
