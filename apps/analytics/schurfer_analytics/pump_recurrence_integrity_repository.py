"""SQLAlchemy query layer for the pump-recurrence-integrity audit.

Full-population reads: unlike a case-study lookup, this deliberately does
NOT filter to `CASE_STUDY_BASES` -- the whole point of this report is a
population-level denominator, not just the tickers that motivated writing
it. `--since`/`--until` still bound the read (matching every other report in
this package), but within that window every base is included.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from schurfer_journal.models import PumpEvent, PumpEventSource
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .outcome_repository import async_database_url
from .pump_recurrence_integrity_report import (
    Episode,
    PumpRecurrenceIntegrityFilters,
    SourceIdentityObservation,
)

if TYPE_CHECKING:
    from sqlalchemy.sql import ColumnElement, Select


def _event_filters(filters: PumpRecurrenceIntegrityFilters) -> list[ColumnElement[bool]]:
    clauses: list[ColumnElement[bool]] = []
    if filters.since is not None:
        clauses.append(PumpEvent.first_seen_at >= filters.since)
    if filters.until is not None:
        clauses.append(PumpEvent.first_seen_at < filters.until)
    return clauses


def episodes_statement(filters: PumpRecurrenceIntegrityFilters) -> Select[Any]:
    return (
        select(
            PumpEvent.id,
            PumpEvent.base,
            PumpEvent.episode,
            PumpEvent.first_seen_at,
            PumpEvent.last_seen_at,
            PumpEvent.peak_pct,
            PumpEvent.closed_at,
        )
        .where(*_event_filters(filters))
        .order_by(PumpEvent.base, PumpEvent.first_seen_at)
    )


def identity_observations_statement(filters: PumpRecurrenceIntegrityFilters) -> Select[Any]:
    """Inner join, deliberately: an event with zero `pump_event_sources` rows
    produces no row here at all, rather than a row with NULL exchange/identity
    columns. `build_report` (pump_recurrence_integrity_report.py) is the
    place that turns that absence into a counted, reported gap -- it takes
    the independently-fetched, join-free `episodes_statement` result as the
    full set of event ids and diffs it against the event ids seen here, so
    `population.events_without_source_observations` is correct regardless of
    this query's join type. Keeping this an inner join (vs. outdoing it with
    a LEFT JOIN + NULL-filtering in Python) keeps this query's row shape
    simple: every row here is a real, populated observation.
    """
    clauses = _event_filters(filters)
    return (
        select(
            PumpEventSource.event_id,
            PumpEvent.base,
            PumpEventSource.exchange,
            PumpEventSource.identity_key,
            PumpEventSource.unified_symbol,
            PumpEventSource.base_asset,
            PumpEventSource.identity_conflict,
        )
        .join(PumpEvent, PumpEvent.id == PumpEventSource.event_id)
        .where(*clauses)
        .order_by(PumpEventSource.event_id, PumpEventSource.exchange)
    )


class PumpRecurrenceIntegrityRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, db_url: str) -> PumpRecurrenceIntegrityRepository:
        return cls(
            create_async_engine(
                async_database_url(db_url),
                pool_pre_ping=True,
                pool_size=1,
                max_overflow=0,
            )
        )

    async def load(
        self, filters: PumpRecurrenceIntegrityFilters
    ) -> tuple[tuple[Episode, ...], tuple[SourceIdentityObservation, ...]]:
        """Load a repeatable-read point-in-time snapshot of episodes and
        per-venue identity observations. Both queries run in the same
        transaction so the identity picture matches the exact episode set
        this report's fragmentation numbers were computed from."""
        async with self._engine.connect() as raw_connection:
            connection = await raw_connection.execution_options(
                isolation_level="REPEATABLE READ",
                postgresql_readonly=True,
            )
            async with connection.begin():
                episode_result = await connection.execute(episodes_statement(filters))
                identity_result = await connection.execute(identity_observations_statement(filters))

        episodes = tuple(
            Episode(
                event_id=int(row["id"]),
                base=str(row["base"]),
                episode=int(row["episode"]),
                first_seen_at=row["first_seen_at"],
                last_seen_at=row["last_seen_at"],
                peak_pct=float(row["peak_pct"]),
                closed_at=row["closed_at"],
            )
            for row in episode_result.mappings().all()
        )
        identity_observations = tuple(
            SourceIdentityObservation(
                event_id=int(row["event_id"]),
                base=str(row["base"]),
                exchange=str(row["exchange"]),
                identity_key=row["identity_key"],
                unified_symbol=row["unified_symbol"],
                base_asset=row["base_asset"],
                identity_conflict=bool(row["identity_conflict"]),
            )
            for row in identity_result.mappings().all()
        )
        return episodes, identity_observations

    async def close(self) -> None:
        await self._engine.dispose()
