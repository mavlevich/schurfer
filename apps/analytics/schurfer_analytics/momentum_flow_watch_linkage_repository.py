"""Read-only WATCH linkage for the momentum-flow episode study.

Joins `momentum_flow_watch_evaluations_1m` against the event cohort's exact
Bybit instruments to answer three point-in-time-safe questions per event:

- was the WATCH worker actually producing decisions for this exact
  instrument with full coverage over `[trigger - PRE, trigger]` at all
  (observability -- see below), and only if so:
- did an eligible `watch` decision for this exact instrument exist AT OR
  BEFORE the pump trigger (recall/lead time), and
- did the FIRST `watch` decision for this instrument in the surrounding
  window arrive only AFTER the trigger (a false-lead-time signal that a
  live strategy could never have acted on early)?

This module only reads `bucket_start`/`decision_at`/`decision_status`/
`quality_ready`/instrument identity and the run's own `cohort_started_at`
-- it never re-evaluates the WATCH state machine and never computes
precision, false-WATCH rate, or economics (those require the fuller
confirmation-track apparatus HYP-014's `confirmation_requirement` still
needs; see the episode-study report's own manifest `interpretation`
field).

Observability gate (amended after second and third colleague review,
before any real run): `momentum_flow_watch_evaluations_1m` is a per-
instrument, per-UTC-minute table -- one row per evaluated minute,
`decision_status` recording every outcome including negative ones (e.g.
`"rejected_signal"`), not only `"watch"`. An event with zero -- or partial
-- evaluation rows over its own `[trigger - PRE, trigger]` span could mean
either "WATCH ran and genuinely never fired" (a true negative) or "the
WATCH worker was not running yet, or had a data gap, over that span"
(unobservable, not a negative at all). Silently treating the second case
as the first would understate real recall. `WatchLinkage.watch_observable`
requires the SAME 100%-coverage standard `momentum_flow_protocol.
FLOW_FULL_COVERAGE_FRACTION` already enforces for flow-bar cumulative
sums, applied here to the pre-trigger evaluation-minute count instead --
an event failing this gate belongs in the report's `unresolved_events`
count, not counted as a WATCH miss.

Coverage is computed against `bucket_start` (the market minute the row
covers), NOT `decision_at` (when the row became readable by a live
strategy) -- amended after THIRD colleague review, before any real run:
`bucket_start` is the table's own dedup key and the correct "which minute
was processed" axis; on production `decision_at` trails `bucket_start` by
roughly 90-100 seconds of evaluator latency, and after a worker restart or
backlog that lag can grow much further. Using `decision_at` to count
"which minutes were covered" would silently miscount coverage under any
catch-up/backlog condition even though the underlying minutes were, in
fact, evaluated. A bucket only counts toward pre-trigger coverage when
BOTH: its own `bucket_start` falls inside `[trigger - PRE, trigger]`, AND
its `decision_at` is at or before the trigger -- a decision that only
became available to a live strategy AFTER the pump already happened could
not have informed anything before it, coverage or not. Recall and lead
time (`earliest_watch_before_trigger_at`, `lead_minutes`) remain computed
on `decision_at`, unchanged: those ask when a decision was ACTIONABLE, a
genuinely different question from which minutes were processed at all.

Quality gate (amended after third colleague review, before any real run):
a bucket with `quality_ready=False` (`decision_status="rejected_quality"`
-- the evaluator's own cross-sectional/data-quality gate, see
`momentum_flow_watch_evaluator.py`) was PROCESSED but never reached a real
watch/no-watch call. The registered validation plan's own recall
denominator is `pumps_with_complete_pre_window`, i.e. only windows whose
pre-trigger span was quality-ready throughout -- a `rejected_quality`
bucket must not count toward coverage even though a row for it exists,
or an event with a quality gap would be silently treated as "no watch
decision" instead of "not evaluable" (a WATCH miss vs. an unresolved
event are two different claims).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, Column, DateTime, MetaData, String, Table, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .momentum_flow_protocol import FLOW_FULL_COVERAGE_FRACTION, LOOKBACK_OFFSETS_MINUTES
from .momentum_flow_watch_contract import WATCH_VERSION
from .outcome_repository import async_database_url

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from sqlalchemy.sql import Select

WATCH_LINKAGE_VERSION = "momentum_flow_watch_linkage_v1"

# How far before/after a trigger to look for a WATCH decision on the exact
# instrument. Amended after colleague review, before any real run: the
# WATCH evaluator's OWN internal feature lookback (momentum_flow_watch_
# contract.LOOKBACK_MINUTES, 60) bounds how far back its FEATURES look, not
# how long a WATCH state can stay active before a pump materializes --
# WATCH_COOLDOWN_MINUTES (360) alone shows a WATCH episode can span hours.
# Capping the search at 60 minutes silently truncated real recall and
# under-measured lead time past that point. Use the frozen post-trigger
# offset grid's own furthest point (240 minutes) instead, imported rather
# than restated, so this cannot silently drift from momentum_flow_
# protocol.py's own frozen lookback span.
PRE_TRIGGER_WINDOW_MINUTES = abs(LOOKBACK_OFFSETS_MINUTES[-1])
# Post-trigger stays short and separate from the pre-trigger bound: a
# "watch" arriving long after the pump already started is not usefully
# described as "for" this event -- it is closer to noise than a
# late-but-related signal, and conflating it with pre-trigger lead time
# would answer a different question (was WATCH watching at all vs. did it
# lead the move).
POST_TRIGGER_WINDOW_MINUTES = 15

_EVALUATION_BUCKET_MS = 60_000


def _expected_evaluation_minutes(start: datetime, end: datetime) -> int:
    """Count of UTC-minute-aligned instants in [start, end] -- an
    independent copy of momentum_flow_timeline's own bucket-counting logic
    for the flow-bar coverage gate (`_expected_minute_buckets`), kept
    separate here rather than reaching into that module's private helper:
    WATCH evaluations are a different table with their own minute grid, and
    this module should not depend on another module's internals for it."""
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    if end_ms < start_ms:
        return 0
    first_bucket = -(-start_ms // _EVALUATION_BUCKET_MS) * _EVALUATION_BUCKET_MS  # ceil
    last_bucket = (end_ms // _EVALUATION_BUCKET_MS) * _EVALUATION_BUCKET_MS  # floor
    if last_bucket < first_bucket:
        return 0
    return (last_bucket - first_bucket) // _EVALUATION_BUCKET_MS + 1


_metadata = MetaData()

_watch_runs = Table(
    "momentum_flow_watch_runs",
    _metadata,
    Column("watch_version", String),
    Column("cohort_started_at", DateTime(timezone=True)),
    Column("status", String),
    schema="app",
)

_evaluations = Table(
    "momentum_flow_watch_evaluations_1m",
    _metadata,
    Column("exchange", String),
    Column("market_type", String),
    Column("symbol", String),
    Column("watch_version", String),
    Column("bucket_start", DateTime(timezone=True)),
    Column("quality_ready", Boolean),
    Column("decision_status", String),
    Column("decision_at", DateTime(timezone=True)),
    schema="timeseries",
)


@dataclass(frozen=True)
class InstrumentWindow:
    pump_event_id: int
    exchange: str
    market_type: str
    symbol: str
    trigger_at: datetime


@dataclass(frozen=True)
class WatchLinkage:
    pump_event_id: int
    watch_evaluations_in_window: int
    # Fraction of expected one-minute evaluation buckets over [trigger -
    # PRE, trigger] actually present -- the observability diagnostic behind
    # `watch_observable` (see module docstring). Kept alongside the boolean
    # gate the same way `TimelinePoint.flow_coverage_pct` sits alongside
    # `flow_availability`.
    pre_trigger_evaluation_coverage_pct: float
    watch_observable: bool
    earliest_watch_before_trigger_at: datetime | None
    lead_minutes: float | None
    first_watch_at: datetime | None
    watch_arrived_only_after_trigger: bool


def watch_cohort_started_at_statement() -> Select[Any]:
    """No `status` filter: a historical episode-study report over a past
    window must still see the cohort a since-stopped WATCH run actually
    covered. `status` describes whether the worker is running NOW, not
    whether its recorded cohort is valid to report against."""
    return select(_watch_runs.c.cohort_started_at).where(
        _watch_runs.c.watch_version == WATCH_VERSION,
    )


def watch_evaluations_statement(windows: Sequence[InstrumentWindow]) -> Select[Any] | None:
    """One statement covering every requested instrument window via an OR of
    per-event bounded ranges, rather than one query per event -- a report
    over hundreds of events must not issue hundreds of round trips. Returns
    None for an empty input rather than a statement that would scan
    unbounded, since a plain `WHERE FALSE` client-side check is clearer than
    relying on SQLAlchemy's own empty-`or_` behavior at every call site."""
    if not windows:
        return None
    clauses = [
        and_(
            _evaluations.c.exchange == window.exchange,
            _evaluations.c.market_type == window.market_type,
            _evaluations.c.symbol == window.symbol,
            _evaluations.c.decision_at
            >= window.trigger_at - timedelta(minutes=PRE_TRIGGER_WINDOW_MINUTES),
            _evaluations.c.decision_at
            <= window.trigger_at + timedelta(minutes=POST_TRIGGER_WINDOW_MINUTES),
        )
        for window in windows
    ]
    return select(
        _evaluations.c.exchange,
        _evaluations.c.market_type,
        _evaluations.c.symbol,
        _evaluations.c.bucket_start,
        _evaluations.c.quality_ready,
        _evaluations.c.decision_status,
        _evaluations.c.decision_at,
    ).where(
        _evaluations.c.watch_version == WATCH_VERSION,
        or_(*clauses),
    )


@dataclass(frozen=True)
class _EvaluationRow:
    bucket_start: datetime
    quality_ready: bool
    decision_status: str
    decision_at: datetime


def build_watch_linkage(
    windows: Sequence[InstrumentWindow],
    rows: Sequence[tuple[str, str, str, datetime, bool, str, datetime]],
) -> dict[int, WatchLinkage]:
    """Pure join of already-fetched evaluation rows against the requested
    windows -- unit-testable without a live database, same split as every
    other repository module in this project.

    `rows` is the UNION of every window's own SQL range for a shared
    instrument (see `watch_evaluations_statement`'s own docstring on why one
    OR'd query replaces one query per event). For a repeat token -- the same
    instrument pumping more than once -- that union can include a decision
    that belongs to a DIFFERENT event's own window on the same instrument.
    Each window here re-filters its own candidates to its own
    `[trigger - PRE, trigger + POST]` bound before use (amended after
    colleague review, before any real run): grouping by instrument alone,
    without this second filter, could silently attribute one pump's WATCH
    decision to another pump of the same token.
    """
    by_instrument: dict[tuple[str, str, str], list[_EvaluationRow]] = defaultdict(list)
    for row in rows:
        exchange, market_type, symbol, bucket_start, quality_ready, decision_status, decision_at = (
            row
        )
        by_instrument[(exchange, market_type, symbol)].append(
            _EvaluationRow(
                bucket_start=bucket_start,
                quality_ready=bool(quality_ready),
                decision_status=decision_status,
                decision_at=decision_at,
            )
        )

    linkage: dict[int, WatchLinkage] = {}
    for window in windows:
        pre = window.trigger_at - timedelta(minutes=PRE_TRIGGER_WINDOW_MINUTES)
        post = window.trigger_at + timedelta(minutes=POST_TRIGGER_WINDOW_MINUTES)
        # Bounded on decision_at (when a row became visible at all), same as
        # before -- this candidate pool feeds BOTH the coverage gate below
        # AND the recall/lead-time computation, which both legitimately
        # need decision_at in this range.
        candidates = [
            row
            for row in by_instrument.get((window.exchange, window.market_type, window.symbol), ())
            if pre <= row.decision_at <= post
        ]

        # Observability/coverage: axis is `bucket_start` (which MARKET
        # MINUTE was processed), not `decision_at` (when the row became
        # readable) -- see module docstring on why these differ under
        # real evaluator latency, catch-up, and restarts. A bucket counts
        # only when its own decision was actually available before the
        # trigger (decision_at <= trigger_at -- a decision arriving after
        # the pump could not have informed anything before it) AND it was
        # quality-ready (quality_ready -- a rejected_quality bucket was
        # processed but never reached a real watch/no-watch call, so it
        # cannot stand in for `pumps_with_complete_pre_window`'s own
        # quality-ready requirement). Floored to the minute defensively,
        # rather than assuming exact on-grid alignment.
        observed_minutes = {
            row.bucket_start.replace(second=0, microsecond=0)
            for row in candidates
            if pre <= row.bucket_start <= window.trigger_at
            and row.decision_at <= window.trigger_at
            and row.quality_ready
        }
        expected_minutes = _expected_evaluation_minutes(pre, window.trigger_at)
        coverage_pct = (
            min(len(observed_minutes) / expected_minutes, 1.0) if expected_minutes > 0 else 0.0
        )
        watch_observable = coverage_pct >= FLOW_FULL_COVERAGE_FRACTION

        watch_decisions = sorted(
            row.decision_at for row in candidates if row.decision_status == "watch"
        )
        before_trigger = [at for at in watch_decisions if at <= window.trigger_at]
        earliest_before = min(before_trigger) if before_trigger else None
        first_watch_at = watch_decisions[0] if watch_decisions else None
        lead_minutes = (
            (window.trigger_at - earliest_before).total_seconds() / 60
            if earliest_before is not None
            else None
        )
        linkage[window.pump_event_id] = WatchLinkage(
            pump_event_id=window.pump_event_id,
            watch_evaluations_in_window=len(candidates),
            pre_trigger_evaluation_coverage_pct=coverage_pct,
            watch_observable=watch_observable,
            earliest_watch_before_trigger_at=earliest_before,
            lead_minutes=lead_minutes,
            first_watch_at=first_watch_at,
            watch_arrived_only_after_trigger=(
                earliest_before is None and first_watch_at is not None
            ),
        )
    return linkage


class MomentumFlowWatchLinkageRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, database_url: str) -> MomentumFlowWatchLinkageRepository:
        return cls(
            create_async_engine(
                async_database_url(database_url),
                pool_pre_ping=True,
                pool_size=1,
                max_overflow=0,
            )
        )

    async def watch_cohort_started_at(self) -> datetime | None:
        async with self._engine.connect() as connection:
            result = await connection.execute(watch_cohort_started_at_statement())
            return result.scalar_one_or_none()

    async def load_linkage(self, windows: Sequence[InstrumentWindow]) -> dict[int, WatchLinkage]:
        statement = watch_evaluations_statement(windows)
        if statement is None:
            return {}
        async with self._engine.connect() as connection:
            result = await connection.execute(statement)
            rows = result.all()
        return build_watch_linkage(windows, rows)  # type: ignore[arg-type]

    async def close(self) -> None:
        await self._engine.dispose()
