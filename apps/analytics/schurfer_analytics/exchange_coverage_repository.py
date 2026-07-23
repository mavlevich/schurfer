"""SQLAlchemy query layer for exchange discovery coverage."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from schurfer_journal.models import PumpEvent, PumpEventSource
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .exchange_coverage_report import (
    CoverageFilters,
    ExchangeCoverageReport,
    SourceObservation,
    build_report,
)
from .outcome_repository import async_database_url

if TYPE_CHECKING:
    from sqlalchemy.sql import ColumnElement, Select


def _filters(filters: CoverageFilters) -> list[ColumnElement[bool]]:
    clauses: list[ColumnElement[bool]] = []
    if filters.since is not None:
        clauses.append(PumpEvent.first_seen_at >= filters.since)
    if filters.until is not None:
        clauses.append(PumpEvent.first_seen_at < filters.until)
    return clauses


def total_episodes_statement(filters: CoverageFilters) -> Select[Any]:
    return select(func.count(PumpEvent.id)).where(*_filters(filters))


def source_observations_statement(filters: CoverageFilters) -> Select[Any]:
    return (
        select(
            PumpEventSource.event_id,
            PumpEventSource.exchange,
            PumpEventSource.first_seen_at,
        )
        .join(PumpEvent, PumpEvent.id == PumpEventSource.event_id)
        .where(*_filters(filters))
        .order_by(PumpEventSource.event_id, PumpEventSource.exchange)
    )


class ExchangeCoverageRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, db_url: str) -> ExchangeCoverageRepository:
        return cls(
            create_async_engine(
                async_database_url(db_url),
                pool_pre_ping=True,
                pool_size=1,
                max_overflow=0,
            )
        )

    async def generate(self, filters: CoverageFilters) -> ExchangeCoverageReport:
        async with self._engine.connect() as raw_connection:
            connection = await raw_connection.execution_options(
                isolation_level="REPEATABLE READ",
                postgresql_readonly=True,
            )
            async with connection.begin():
                total_result = await connection.execute(total_episodes_statement(filters))
                source_result = await connection.execute(source_observations_statement(filters))

        observations = [
            SourceObservation(
                event_id=int(row["event_id"]),
                exchange=str(row["exchange"]),
                first_seen_at=row["first_seen_at"],
            )
            for row in source_result.mappings().all()
        ]
        return build_report(filters, int(total_result.scalar_one()), observations)

    async def close(self) -> None:
        await self._engine.dispose()
