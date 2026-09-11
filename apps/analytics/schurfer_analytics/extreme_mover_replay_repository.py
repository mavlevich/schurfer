"""Repeatable-read adapter for the extreme-mover endpoint replay."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from schurfer_journal.models import TradeDecision
from sqlalchemy import func, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .outcome_repository import async_database_url
from .replay_repository import map_replay_row_stream, replay_inputs_statement

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.sql import Select

    from .replay import ReplayDecision, ReplayFilters


def extreme_mover_inputs_statement(filters: ReplayFilters) -> Select[Any]:
    """Select only decisions that can become one of the two frozen anchors.

    The shared replay query intentionally loads every strategy row in candidate
    episodes so its generic dataset builder can diagnose mixed-strategy paths. This
    report supports both relevant strategy versions and needs only the first decision
    plus the first point-in-time quality-approved decision per pump event. Pushing
    that deterministic selection into PostgreSQL bounds application memory without
    changing either anchor.
    """
    if filters.since is None:
        raise ValueError("extreme-mover replay requires a bounded start")
    decisions = TradeDecision.__table__
    scoped = (
        decisions.c.ts >= filters.since,
        decisions.c.ts < filters.until,
        decisions.c.strategy_version.in_(filters.strategy_versions),
    )
    assigned = (
        decisions.c.pump_event_id.is_not(None),
        decisions.c.pump_event_id > 0,
    )
    first_decisions = (
        select(decisions.c.id)
        .where(*scoped, *assigned)
        .distinct(decisions.c.pump_event_id)
        .order_by(decisions.c.pump_event_id, decisions.c.ts, decisions.c.id)
        .cte("extreme_mover_first_decisions")
    )
    first_quality_decisions = (
        select(decisions.c.id)
        .where(
            *scoped,
            *assigned,
            decisions.c.liquidity["quality"]["allowed"].as_boolean().is_(True),
        )
        .distinct(decisions.c.pump_event_id)
        .order_by(decisions.c.pump_event_id, decisions.c.ts, decisions.c.id)
        .cte("extreme_mover_first_quality_decisions")
    )
    unassigned_decisions = (
        select(decisions.c.id)
        .where(
            *scoped,
            or_(decisions.c.pump_event_id.is_(None), decisions.c.pump_event_id <= 0),
        )
        .cte("extreme_mover_unassigned_decisions")
    )
    selected_ids = union_all(
        select(first_decisions.c.id),
        select(first_quality_decisions.c.id),
        select(unassigned_decisions.c.id),
    ).cte("extreme_mover_selected_decisions")
    return replay_inputs_statement(filters).where(decisions.c.id.in_(select(selected_ids.c.id)))


class ExtremeMoverReplayRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, db_url: str) -> ExtremeMoverReplayRepository:
        return cls(
            create_async_engine(
                async_database_url(db_url),
                pool_pre_ping=True,
                pool_size=1,
                max_overflow=0,
            )
        )

    async def load(
        self,
        filters: ReplayFilters,
    ) -> tuple[datetime, tuple[ReplayDecision, ...]]:
        async with self._engine.connect() as raw_connection:
            connection = await raw_connection.execution_options(
                isolation_level="REPEATABLE READ",
                postgresql_readonly=True,
            )
            async with connection.begin():
                database_snapshot_at = (await connection.execute(select(func.now()))).scalar_one()
                result = await connection.stream(
                    extreme_mover_inputs_statement(filters),
                    execution_options={"yield_per": 500},
                )
                decisions = await map_replay_row_stream(result.mappings())
        return database_snapshot_at, tuple(decisions)

    async def close(self) -> None:
        await self._engine.dispose()


__all__ = ["ExtremeMoverReplayRepository", "extreme_mover_inputs_statement"]
