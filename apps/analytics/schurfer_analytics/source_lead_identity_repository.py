"""Read-only inputs for the prospective source-lead identity review queue."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from schurfer_journal.models import SourceLeadCapture, SourceLeadTargetObservation
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .outcome_repository import async_database_url
from .source_lead_contract import CAPTURE_VERSION

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.sql import Select


@dataclass(frozen=True)
class SourceLeadIdentityObservation:
    capture_id: int
    event_id: int
    base: str
    source_first_observed_at: datetime
    capture_status: str
    eligibility_reason: str
    source_identity_key: str | None
    source_market_id: str | None
    source_payload: dict[str, Any]
    target_exchange: str | None
    target_status: str | None
    target_eligibility_reason: str | None
    target_observed_at: datetime | None
    requested_notional_usd: float | None
    target_instrument: dict[str, Any]
    target_liquidity: dict[str, Any]


def source_lead_identity_statement(
    since: datetime,
    until: datetime,
) -> Select[Any]:
    return (
        select(
            SourceLeadCapture.id.label("capture_id"),
            SourceLeadCapture.event_id,
            SourceLeadCapture.base,
            SourceLeadCapture.source_first_observed_at,
            SourceLeadCapture.status.label("capture_status"),
            SourceLeadCapture.eligibility_reason,
            SourceLeadCapture.source_identity_key,
            SourceLeadCapture.source_market_id,
            SourceLeadCapture.source_payload,
            SourceLeadTargetObservation.target_exchange,
            SourceLeadTargetObservation.status.label("target_status"),
            SourceLeadTargetObservation.eligibility_reason.label("target_eligibility_reason"),
            SourceLeadTargetObservation.observed_at.label("target_observed_at"),
            SourceLeadTargetObservation.requested_notional_usd,
            SourceLeadTargetObservation.instrument.label("target_instrument"),
            SourceLeadTargetObservation.liquidity.label("target_liquidity"),
        )
        .outerjoin(
            SourceLeadTargetObservation,
            SourceLeadTargetObservation.capture_id == SourceLeadCapture.id,
        )
        .where(
            SourceLeadCapture.capture_version == CAPTURE_VERSION,
            SourceLeadCapture.source_first_observed_at >= since,
            SourceLeadCapture.source_first_observed_at < until,
        )
        .order_by(
            SourceLeadCapture.source_first_observed_at,
            SourceLeadCapture.id,
            SourceLeadTargetObservation.target_exchange,
        )
    )


def map_source_lead_identity_row(row: dict[str, Any]) -> SourceLeadIdentityObservation:
    source_payload = row["source_payload"]
    target_instrument = row["target_instrument"]
    target_liquidity = row["target_liquidity"]
    return SourceLeadIdentityObservation(
        capture_id=int(row["capture_id"]),
        event_id=int(row["event_id"]),
        base=str(row["base"]).upper(),
        source_first_observed_at=row["source_first_observed_at"],
        capture_status=str(row["capture_status"]),
        eligibility_reason=str(row["eligibility_reason"]),
        source_identity_key=(
            str(row["source_identity_key"]) if row["source_identity_key"] else None
        ),
        source_market_id=(str(row["source_market_id"]) if row["source_market_id"] else None),
        source_payload=source_payload if isinstance(source_payload, dict) else {},
        target_exchange=(str(row["target_exchange"]).lower() if row["target_exchange"] else None),
        target_status=str(row["target_status"]) if row["target_status"] else None,
        target_eligibility_reason=(
            str(row["target_eligibility_reason"]) if row["target_eligibility_reason"] else None
        ),
        target_observed_at=row["target_observed_at"],
        requested_notional_usd=(
            float(row["requested_notional_usd"])
            if row["requested_notional_usd"] is not None
            else None
        ),
        target_instrument=(target_instrument if isinstance(target_instrument, dict) else {}),
        target_liquidity=(target_liquidity if isinstance(target_liquidity, dict) else {}),
    )


class SourceLeadIdentityRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, db_url: str) -> SourceLeadIdentityRepository:
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
        since: datetime,
        until: datetime,
    ) -> tuple[SourceLeadIdentityObservation, ...]:
        async with self._engine.connect() as raw_connection:
            connection = await raw_connection.execution_options(
                isolation_level="REPEATABLE READ",
                postgresql_readonly=True,
            )
            async with connection.begin():
                result = await connection.execute(source_lead_identity_statement(since, until))
                return tuple(
                    map_source_lead_identity_row(dict(row)) for row in result.mappings().all()
                )

    async def close(self) -> None:
        await self._engine.dispose()
