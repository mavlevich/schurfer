"""Read-only point-in-time inputs for source-lead event studies."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

from schurfer_journal.models import PumpEvent, PumpEventSource
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .outcome_repository import async_database_url
from .source_lead import SourceLeadEvent, SourceLeadObservation

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.sql import Select


def source_lead_statement(since: datetime, until: datetime) -> Select[Any]:
    return (
        select(
            PumpEvent.id.label("event_id"),
            PumpEvent.base,
            PumpEvent.episode,
            PumpEvent.first_seen_at.label("event_first_seen_at"),
            PumpEvent.closed_at,
            PumpEventSource.exchange,
            PumpEventSource.symbol,
            PumpEventSource.identity_key,
            PumpEventSource.unified_symbol,
            PumpEventSource.market_type,
            PumpEventSource.base_asset,
            PumpEventSource.quote_asset,
            PumpEventSource.settle_asset,
            PumpEventSource.onboarded_at,
            PumpEventSource.identity_conflict,
            PumpEventSource.first_seen_at.label("source_first_seen_at"),
            PumpEventSource.first_change_pct,
            PumpEventSource.first_price,
            PumpEventSource.first_volume_24h_usd,
        )
        .outerjoin(
            PumpEventSource,
            and_(
                PumpEventSource.event_id == PumpEvent.id,
                PumpEventSource.first_seen_at < until,
            ),
        )
        .where(
            PumpEvent.first_seen_at >= since,
            PumpEvent.first_seen_at < until,
        )
        .order_by(PumpEvent.first_seen_at, PumpEvent.id, PumpEventSource.first_seen_at)
    )


def map_source_lead_rows(rows: list[dict[str, Any]]) -> tuple[SourceLeadEvent, ...]:
    events: dict[int, dict[str, Any]] = {}
    observations: dict[int, list[SourceLeadObservation]] = defaultdict(list)
    for row in rows:
        event_id = int(row["event_id"])
        identity = {
            "base": str(row["base"]),
            "episode": int(row["episode"]),
            "first_seen_at": row["event_first_seen_at"],
            "closed_at": row["closed_at"],
        }
        previous = events.setdefault(event_id, identity)
        if previous != identity:
            raise ValueError(f"inconsistent source-lead event rows: {event_id}")
        if row["exchange"] is None:
            continue
        observations[event_id].append(
            SourceLeadObservation(
                exchange=str(row["exchange"]),
                symbol=str(row["symbol"]),
                identity_key=(str(row["identity_key"]) if row["identity_key"] else None),
                unified_symbol=(str(row["unified_symbol"]) if row["unified_symbol"] else None),
                market_type=(str(row["market_type"]) if row["market_type"] else None),
                base_asset=(str(row["base_asset"]) if row["base_asset"] else None),
                quote_asset=(str(row["quote_asset"]) if row["quote_asset"] else None),
                settle_asset=(str(row["settle_asset"]) if row["settle_asset"] else None),
                onboarded_at=row["onboarded_at"],
                identity_conflict=bool(row["identity_conflict"]),
                first_seen_at=row["source_first_seen_at"],
                first_change_pct=float(row["first_change_pct"]),
                first_price=(float(row["first_price"]) if row["first_price"] is not None else None),
                first_volume_24h_usd=(
                    float(row["first_volume_24h_usd"])
                    if row["first_volume_24h_usd"] is not None
                    else None
                ),
            )
        )
    return tuple(
        SourceLeadEvent(
            event_id=event_id,
            base=identity["base"],
            episode=identity["episode"],
            first_seen_at=identity["first_seen_at"],
            closed_at=identity["closed_at"],
            observations=tuple(
                sorted(
                    observations[event_id],
                    key=lambda row: (row.first_seen_at, row.exchange),
                )
            ),
        )
        for event_id, identity in events.items()
    )


class SourceLeadRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, db_url: str) -> SourceLeadRepository:
        return cls(
            create_async_engine(
                async_database_url(db_url),
                pool_pre_ping=True,
                pool_size=1,
                max_overflow=0,
            )
        )

    async def load(self, since: datetime, until: datetime) -> tuple[SourceLeadEvent, ...]:
        async with self._engine.connect() as raw_connection:
            connection = await raw_connection.execution_options(
                isolation_level="REPEATABLE READ",
                postgresql_readonly=True,
            )
            async with connection.begin():
                result = await connection.execute(source_lead_statement(since, until))
                rows = [dict(row) for row in result.mappings().all()]
        return map_source_lead_rows(rows)

    async def close(self) -> None:
        await self._engine.dispose()
