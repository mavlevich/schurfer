"""Read-only, measurement-only pump-event cohort for `analysis/bybit-
early-momentum-event-study-v0`.

Deliberately independent of `replay.py`/`replay_repository.py`'s
eligibility contract (`decision_exclusion_reasons`): that contract exists
for outcome-based challenger reports and requires a resolved N-hour
outcome (`missing_outcome:{horizon}`), a CLOSED pump event
(`right_censored_episode` -- `event_closed_at is None or event_closed_at
>= until`), and liquidity/features presence on the selected decision.
None of that is relevant to a report that only describes price/OI/flow
SHAPE around a trigger and never computes a trade outcome -- and reusing
it anyway would actively bias the cohort (a colleague review, 2026-08-12,
caught this): requiring `event_closed_at < until` systematically excludes
long-lived pumps, i.e. exactly the event's own character determining
whether it enters the sample. This module's own cohort needs only
point-in-time identity (which pump event, which base, which venue, which
EXACT instrument) and a trigger instant -- maturity against the
calibration window is checked separately by `momentum_flow_protocol.
event_is_mature`.

Venue-per-event selection (amended 2026-08-12, second colleague review,
before any real run): the first version picked a source alphabetically by
exchange name and treated `identity_conflict=False` as sufficient
identity proof. Both were wrong. Alphabetical selection can pick a venue
that confirmed the pump LATER than another venue did, silently leaking a
later, unrelated confirmation into what is supposed to be the earliest
point-in-time trigger. `identity_conflict` defaults to False and is only
set True when identity resolution actively found a CONFLICT -- a row
where identity resolution never ran, or produced nothing, also reads as
"False", which is an absence of a negative signal, not a positive one
(same fail-closed principle this project applies everywhere else). The
fix: establish the earliest source timestamp before applying identity
readiness (so a later confirmation can never replace the real first
source), then require the same exact same-venue derivative identity
contract used by token-history research: identity key, market id, unified
symbol, swap market type, matching base/USDT quote+settle, onboarding
strictly before the trigger, and no conflict. The confirmed unified symbol
is used for the OHLCV fetch rather than reconstructing a guessed
`{BASE}/USDT:USDT` pattern.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from schurfer_journal.models import PumpEvent, PumpEventSource
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .outcome_repository import async_database_url

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from sqlalchemy.sql import Select

MOMENTUM_FLOW_EVENT_COHORT_VERSION = "momentum_flow_event_cohort_v2"

EXCLUSION_NO_IDENTITY_READY_EARLIEST_SOURCE = "no_identity_ready_earliest_source"


@dataclass(frozen=True)
class MeasurementEvent:
    pump_event_id: int
    base: str
    exchange: str
    identity_key: str
    market_id: str
    unified_symbol: str
    market_type: str
    onboarded_at: datetime
    trigger_at: datetime


@dataclass(frozen=True)
class _SourceCandidate:
    pump_event_id: int
    base: str
    exchange: str
    identity_key: str | None
    market_id: str | None
    unified_symbol: str | None
    market_type: str | None
    base_asset: str | None
    quote_asset: str | None
    settle_asset: str | None
    onboarded_at: datetime | None
    identity_conflict: bool
    source_first_seen_at: datetime


@dataclass(frozen=True)
class MeasurementCohort:
    events: tuple[MeasurementEvent, ...]
    exclusion_reasons: tuple[tuple[str, int], ...]


def measurement_events_statement(*, since: datetime | None, until: datetime) -> Select[Any]:
    """Every (event, venue) candidate source in [since, until), by the
    EVENT's own first_seen_at. Deliberately does NOT filter on
    `identity_conflict`/`unified_symbol` here -- that selection happens in
    `_select_events` (pure, DB-free, unit-testable) so a rejected
    candidate can still be counted in the exclusion funnel instead of
    silently vanishing from a WHERE clause. `sources.c.first_seen_at <
    until` additionally bounds each SOURCE row's own observation time, not
    just the event's: a venue confirming the same event long after
    `until` must not be pulled in just because the parent event itself
    started before the cutoff."""
    events = PumpEvent.__table__
    sources = PumpEventSource.__table__
    clauses = [events.c.first_seen_at < until]
    if since is not None:
        clauses.append(events.c.first_seen_at >= since)
    return (
        select(
            events.c.id,
            events.c.base,
            events.c.first_seen_at,
            sources.c.exchange,
            sources.c.identity_key,
            sources.c.market_id,
            sources.c.unified_symbol,
            sources.c.market_type,
            sources.c.base_asset,
            sources.c.quote_asset,
            sources.c.settle_asset,
            sources.c.onboarded_at,
            sources.c.identity_conflict,
            sources.c.first_seen_at,
        )
        .select_from(
            events.outerjoin(
                sources,
                and_(sources.c.event_id == events.c.id, sources.c.first_seen_at < until),
            )
        )
        .where(and_(*clauses))
        .order_by(events.c.id, sources.c.first_seen_at, sources.c.exchange)
    )


def _select_events(
    rows: Sequence[
        tuple[
            int,
            str,
            datetime,
            str | None,
            str | None,
            str | None,
            str | None,
            str | None,
            str | None,
            str | None,
            str | None,
            datetime | None,
            bool | None,
            datetime | None,
        ]
    ],
) -> MeasurementCohort:
    """Pure selection logic, factored out of `MomentumFlowEventRepository.
    load` so it is unit-testable against hand-built rows without a live
    database (a colleague review, 2026-08-12, noted the original
    alphabetical-selection bug survived 47 green tests specifically
    because this logic only ever ran against a real query result).

    One `MeasurementEvent` per pump event. Source time is resolved before
    identity readiness: only candidates at the event's earliest observed
    source timestamp may qualify. A later venue can therefore never become
    the price venue merely because it acquired better metadata or confirmed
    the move later. Among exact earliest-time ties, exchange is the stable
    tie-break. An event without an identity-ready earliest source is
    excluded, never silently replaced by a future confirmation."""
    candidates_by_event: dict[int, list[_SourceCandidate]] = defaultdict(list)
    event_base: dict[int, str] = {}
    event_trigger_at: dict[int, datetime] = {}
    for row in rows:
        (
            event_id,
            base,
            event_first_seen_at,
            exchange,
            identity_key,
            market_id,
            unified_symbol,
            market_type,
            base_asset,
            quote_asset,
            settle_asset,
            onboarded_at,
            identity_conflict,
            source_first_seen_at,
        ) = row
        event_base[event_id] = base
        event_trigger_at[event_id] = event_first_seen_at
        # The OUTER JOIN deliberately yields one all-NULL source row for
        # source-less events so the funnel can count them. It is metadata
        # for the event, not a source candidate.
        if exchange is None or source_first_seen_at is None:
            candidates_by_event[event_id]
            continue
        candidates_by_event[event_id].append(
            _SourceCandidate(
                pump_event_id=event_id,
                base=base,
                exchange=exchange,
                identity_key=identity_key,
                market_id=market_id,
                unified_symbol=unified_symbol,
                market_type=market_type,
                base_asset=base_asset,
                quote_asset=quote_asset,
                settle_asset=settle_asset,
                onboarded_at=onboarded_at,
                identity_conflict=bool(identity_conflict),
                source_first_seen_at=source_first_seen_at,
            )
        )

    events: list[MeasurementEvent] = []
    excluded = 0
    for event_id in sorted(candidates_by_event):
        candidates = candidates_by_event[event_id]
        if not candidates:
            excluded += 1
            continue
        earliest_source_at = min(c.source_first_seen_at for c in candidates)
        earliest = [c for c in candidates if c.source_first_seen_at == earliest_source_at]
        trigger_at = event_trigger_at[event_id]
        qualifying = [
            c
            for c in earliest
            if not c.identity_conflict
            and c.identity_key
            and c.market_id
            and c.unified_symbol
            and c.market_type == "swap"
            and (c.base_asset or "").casefold() == event_base[event_id].casefold()
            and (c.quote_asset or "").upper() == "USDT"
            and (c.settle_asset or "").upper() == "USDT"
            and c.onboarded_at is not None
            and c.onboarded_at.utcoffset() is not None
            and c.onboarded_at < trigger_at
        ]
        if not qualifying:
            excluded += 1
            continue
        best = min(qualifying, key=lambda c: c.exchange)
        assert best.identity_key is not None
        assert best.market_id is not None
        assert best.unified_symbol is not None
        assert best.market_type is not None
        assert best.onboarded_at is not None
        events.append(
            MeasurementEvent(
                pump_event_id=event_id,
                base=event_base[event_id],
                exchange=best.exchange,
                identity_key=best.identity_key,
                market_id=best.market_id,
                unified_symbol=best.unified_symbol,
                market_type=best.market_type,
                onboarded_at=best.onboarded_at,
                trigger_at=trigger_at,
            )
        )

    exclusion_reasons: tuple[tuple[str, int], ...] = (
        ((EXCLUSION_NO_IDENTITY_READY_EARLIEST_SOURCE, excluded),) if excluded else ()
    )
    return MeasurementCohort(events=tuple(events), exclusion_reasons=exclusion_reasons)


def bybit_source_instants_statement(*, since: datetime, until: datetime) -> Select[Any]:
    """Every Bybit `PumpEventSource` observation timestamp in range, joined
    to its event's canonical `base` -- deliberately NOT filtered by identity
    readiness, "first source" status, or primary-cohort membership.

    A matched-control search needs to know about every real pump this
    instrument had on Bybit, not only the subset that happens to also be a
    Bybit-first, identity-clean, in-scope primary event (amended after
    colleague review, before any real run): a pump first observed on another
    exchange but that also touched Bybit, an identity-excluded row, or an
    event just outside the primary cohort's own `[dataset_since, until)`
    window are all still real Bybit-instrument activity that a control point
    must stay clear of. This is therefore a separate, wider query, not a
    byproduct of `measurement_events_statement`'s own primary-cohort
    selection.
    """
    events = PumpEvent.__table__
    sources = PumpEventSource.__table__
    return (
        select(events.c.base, sources.c.first_seen_at)
        .select_from(sources.join(events, events.c.id == sources.c.event_id))
        .where(
            sources.c.exchange == "bybit",
            sources.c.first_seen_at >= since,
            sources.c.first_seen_at < until,
        )
        .order_by(events.c.base, sources.c.first_seen_at)
    )


def group_bybit_source_instants(
    rows: Sequence[tuple[str, datetime]],
) -> dict[str, tuple[datetime, ...]]:
    """Pure grouping, unit-testable without a live database."""
    grouped: dict[str, list[datetime]] = defaultdict(list)
    for base, first_seen_at in rows:
        grouped[base].append(first_seen_at)
    return {base: tuple(sorted(instants)) for base, instants in grouped.items()}


class MomentumFlowEventRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, db_url: str) -> MomentumFlowEventRepository:
        engine = create_async_engine(
            async_database_url(db_url),
            pool_pre_ping=True,
            pool_size=1,
            max_overflow=0,
        )
        return cls(engine)

    async def load(self, *, since: datetime | None, until: datetime) -> MeasurementCohort:
        statement = measurement_events_statement(since=since, until=until)
        async with self._engine.connect() as connection:
            result = await connection.execute(statement)
            rows = result.all()
        return _select_events(rows)  # type: ignore[arg-type]

    async def load_bybit_source_instants(
        self, *, since: datetime, until: datetime
    ) -> dict[str, tuple[datetime, ...]]:
        statement = bybit_source_instants_statement(since=since, until=until)
        async with self._engine.connect() as connection:
            result = await connection.execute(statement)
            rows = result.all()
        return group_bybit_source_instants(rows)  # type: ignore[arg-type]

    async def close(self) -> None:
        await self._engine.dispose()
