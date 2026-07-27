"""Read-only target selection for derivatives context probes."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from schurfer_journal.models import PumpEvent, PumpEventSource
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .derivatives_context import DerivativesContextTarget
from .outcome_repository import async_database_url

if TYPE_CHECKING:
    from sqlalchemy.sql import Select


def latest_targets_statement(
    exchanges: tuple[str, ...],
    *,
    since: datetime,
    until: datetime,
    after_minutes: int,
) -> Select[Any]:
    anchor_at = func.coalesce(
        PumpEvent.entry_qualified_at,
        PumpEvent.first_seen_at,
    )
    complete_before = until - timedelta(minutes=after_minutes)
    ranked = (
        select(
            PumpEventSource.event_id.label("event_id"),
            PumpEventSource.exchange.label("exchange"),
            PumpEvent.base.label("base"),
            PumpEventSource.unified_symbol.label("unified_symbol"),
            PumpEventSource.market_id.label("market_id"),
            PumpEventSource.identity_key.label("identity_key"),
            anchor_at.label("anchor_at"),
            func.row_number()
            .over(
                partition_by=PumpEventSource.exchange,
                order_by=(
                    anchor_at.desc(),
                    PumpEventSource.last_seen_at.desc(),
                    PumpEventSource.id.desc(),
                ),
            )
            .label("target_rank"),
        )
        .join(PumpEvent, PumpEvent.id == PumpEventSource.event_id)
        .where(
            PumpEventSource.exchange.in_(exchanges),
            PumpEventSource.unified_symbol.is_not(None),
            PumpEventSource.identity_conflict.is_(False),
            anchor_at >= since,
            anchor_at < complete_before,
        )
        .subquery("ranked_derivatives_context_targets")
    )
    return (
        select(
            ranked.c.event_id,
            ranked.c.exchange,
            ranked.c.base,
            ranked.c.unified_symbol,
            ranked.c.market_id,
            ranked.c.identity_key,
            ranked.c.anchor_at,
        )
        .where(ranked.c.target_rank == 1)
        .order_by(ranked.c.exchange)
    )


class DerivativesContextRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, db_url: str) -> DerivativesContextRepository:
        return cls(
            create_async_engine(
                async_database_url(db_url),
                pool_pre_ping=True,
                pool_size=1,
                max_overflow=0,
            )
        )

    async def load_latest_targets(
        self,
        exchanges: tuple[str, ...],
        *,
        since: datetime,
        until: datetime,
        after_minutes: int,
    ) -> tuple[DerivativesContextTarget, ...]:
        statement = latest_targets_statement(
            exchanges,
            since=since,
            until=until,
            after_minutes=after_minutes,
        )
        async with self._engine.connect() as raw_connection:
            connection = await raw_connection.execution_options(
                isolation_level="REPEATABLE READ",
                postgresql_readonly=True,
            )
            async with connection.begin():
                result = await connection.execute(statement)
        return tuple(
            DerivativesContextTarget(
                event_id=int(row["event_id"]),
                exchange=str(row["exchange"]),
                base=str(row["base"]),
                unified_symbol=str(row["unified_symbol"]),
                market_id=(str(row["market_id"]) if row["market_id"] is not None else None),
                identity_key=(
                    str(row["identity_key"]) if row["identity_key"] is not None else None
                ),
                anchor_at=row["anchor_at"],
            )
            for row in result.mappings().all()
        )

    async def close(self) -> None:
        await self._engine.dispose()
