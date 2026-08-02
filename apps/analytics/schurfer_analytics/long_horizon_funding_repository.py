"""Read-only adapter for durable long-horizon funding observations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from schurfer_journal.models import (
    PumpDerivativesContextRun,
    PumpDerivativesContextSample,
)
from sqlalchemy import and_, column, select, values
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.types import BigInteger, String

from .derivatives_context_resolver import LONG_HORIZON_FUNDING_RESOLVER_VERSION
from .outcome_repository import async_database_url

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from sqlalchemy.sql import Select


@dataclass(frozen=True)
class FundingSample:
    source_at: datetime
    sample_key: str
    payload: dict[str, Any] | list[Any]


@dataclass(frozen=True)
class FundingSeries:
    event_id: int
    exchange: str
    status: str
    requested_since: datetime
    requested_until: datetime
    error: str | None
    samples: tuple[FundingSample, ...]


@dataclass
class _FundingSeriesBuilder:
    event_id: int
    exchange: str
    status: str
    requested_since: datetime
    requested_until: datetime
    error: str | None
    samples: list[FundingSample] = field(default_factory=list)

    def freeze(self) -> FundingSeries:
        return FundingSeries(
            event_id=self.event_id,
            exchange=self.exchange,
            status=self.status,
            requested_since=self.requested_since,
            requested_until=self.requested_until,
            error=self.error,
            samples=tuple(
                sorted(
                    self.samples,
                    key=lambda sample: (sample.source_at, sample.sample_key),
                )
            ),
        )


def funding_series_statement(
    keys: tuple[tuple[int, str], ...],
    *,
    resolver_version: str = LONG_HORIZON_FUNDING_RESOLVER_VERSION,
) -> Select[Any]:
    """Select exact event/venue funding runs and their persisted samples."""
    if not keys:
        raise ValueError("at least one funding-series key is required")
    runs = PumpDerivativesContextRun.__table__
    samples = PumpDerivativesContextSample.__table__
    key_values = values(
        column("event_id", BigInteger),
        column("exchange", String(32)),
        name="long_horizon_funding_keys",
    ).data(keys)
    return (
        select(
            runs.c.id.label("run_id"),
            runs.c.event_id,
            runs.c.exchange,
            runs.c.status,
            runs.c.requested_since,
            runs.c.requested_until,
            runs.c.error,
            samples.c.source_at,
            samples.c.sample_key,
            samples.c.payload,
        )
        .select_from(
            key_values.join(
                runs,
                and_(
                    runs.c.event_id == key_values.c.event_id,
                    runs.c.exchange == key_values.c.exchange,
                    runs.c.method == "funding_rate_history",
                    runs.c.resolver_version == resolver_version,
                ),
            ).outerjoin(samples, samples.c.run_id == runs.c.id)
        )
        .order_by(runs.c.event_id, runs.c.exchange, samples.c.source_at, samples.c.id)
    )


def map_funding_series(rows: Sequence[Any]) -> tuple[FundingSeries, ...]:
    builders: dict[int, _FundingSeriesBuilder] = {}
    for row in rows:
        run_id = int(row["run_id"])
        builder = builders.get(run_id)
        if builder is None:
            builder = _FundingSeriesBuilder(
                event_id=int(row["event_id"]),
                exchange=str(row["exchange"]),
                status=str(row["status"]),
                requested_since=row["requested_since"],
                requested_until=row["requested_until"],
                error=str(row["error"]) if row["error"] is not None else None,
            )
            builders[run_id] = builder
        if row["source_at"] is not None:
            builder.samples.append(
                FundingSample(
                    source_at=row["source_at"],
                    sample_key=str(row["sample_key"]),
                    payload=row["payload"],
                )
            )
    return tuple(
        sorted(
            (builder.freeze() for builder in builders.values()),
            key=lambda series: (series.event_id, series.exchange),
        )
    )


def funding_series_fingerprint(series: tuple[FundingSeries, ...]) -> str:
    payload = [asdict(item) for item in series]
    encoded = json.dumps(
        payload,
        default=str,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class LongHorizonFundingRepository:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        resolver_version: str = LONG_HORIZON_FUNDING_RESOLVER_VERSION,
    ) -> None:
        self._engine = engine
        self._resolver_version = resolver_version

    @classmethod
    def from_url(
        cls,
        db_url: str,
        *,
        resolver_version: str = LONG_HORIZON_FUNDING_RESOLVER_VERSION,
    ) -> LongHorizonFundingRepository:
        return cls(
            create_async_engine(
                async_database_url(db_url),
                pool_pre_ping=True,
                pool_size=1,
                max_overflow=0,
            ),
            resolver_version=resolver_version,
        )

    async def load(
        self,
        keys: tuple[tuple[int, str], ...],
    ) -> tuple[FundingSeries, ...]:
        normalized_keys = tuple(sorted(set(keys)))
        if not normalized_keys:
            return ()
        async with self._engine.connect() as raw_connection:
            connection = await raw_connection.execution_options(
                isolation_level="REPEATABLE READ",
                postgresql_readonly=True,
            )
            async with connection.begin():
                result = await connection.execute(
                    funding_series_statement(
                        normalized_keys,
                        resolver_version=self._resolver_version,
                    )
                )
                rows = result.mappings().all()
        return map_funding_series(rows)

    async def close(self) -> None:
        await self._engine.dispose()
