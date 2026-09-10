"""Repeatable-read adapter for the extreme-mover endpoint replay."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .outcome_repository import async_database_url
from .replay_repository import map_replay_row_stream, replay_inputs_statement

if TYPE_CHECKING:
    from datetime import datetime

    from .replay import ReplayDecision, ReplayFilters


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
                    replay_inputs_statement(filters),
                    execution_options={"yield_per": 500},
                )
                decisions = await map_replay_row_stream(result.mappings())
        return database_snapshot_at, tuple(decisions)

    async def close(self) -> None:
        await self._engine.dispose()


__all__ = ["ExtremeMoverReplayRepository"]
