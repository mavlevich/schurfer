"""Read-only aggregate queries for the source-lead readiness funnel.

Outcome-blind: it counts captures, qualifications by status/reason, distinct qualified
clusters/weeks, and how many qualified episodes have simply had enough wall-clock time
elapse for the outcome horizon. It never selects an outcome, price, or return. Runs
under a read-only REPEATABLE READ snapshot.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .outcome_repository import async_database_url
from .source_lead_forward_cohort import OUTCOME_HORIZON_MINUTES
from .source_lead_readiness import ReadinessFunnel

DEFAULT_QUALIFICATION_VERSION = "source_lead_qualified_capture_v3"

_CAPTURED = text("SELECT count(*) AS n FROM app.source_lead_captures")

# One pass over the qualifications of one version. `matured` is a pure timing fact
# (horizon elapsed), never the outcome. `make_interval` binds the frozen horizon.
_FUNNEL = text(
    """
    SELECT
        count(*) AS attempts,
        count(*) FILTER (WHERE status = 'qualified') AS qualified,
        count(*) FILTER (WHERE status = 'excluded') AS excluded,
        count(DISTINCT canonical_asset_id) FILTER (WHERE status = 'qualified') AS clusters,
        count(DISTINCT to_char(qualified_at AT TIME ZONE 'UTC', 'IYYY-IW'))
            FILTER (WHERE status = 'qualified') AS weeks,
        count(*) FILTER (
            WHERE status = 'qualified'
              AND qualified_at + make_interval(mins => :horizon) <= now()
        ) AS matured,
        COALESCE(
            EXTRACT(EPOCH FROM (max(qualified_at) - min(qualified_at))) / 86400.0, 0.0
        ) AS span_days
    FROM app.source_lead_qualifications
    WHERE qualification_version = :qv
    """
)

_REASONS = text(
    """
    SELECT reason, count(*) AS n
    FROM app.source_lead_qualifications
    WHERE qualification_version = :qv AND status = 'excluded'
    GROUP BY reason
    ORDER BY n DESC
    """
)


class SourceLeadReadinessRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, db_url: str) -> SourceLeadReadinessRepository:
        return cls(
            create_async_engine(
                async_database_url(db_url),
                pool_pre_ping=True,
                pool_size=1,
                max_overflow=0,
            )
        )

    async def load(
        self, qualification_version: str = DEFAULT_QUALIFICATION_VERSION
    ) -> ReadinessFunnel:
        binds: dict[str, Any] = {
            "qv": qualification_version,
            "horizon": OUTCOME_HORIZON_MINUTES,
        }
        async with self._engine.connect() as raw_connection:
            connection = await raw_connection.execution_options(
                isolation_level="REPEATABLE READ",
                postgresql_readonly=True,
            )
            async with connection.begin():
                captured = (await connection.execute(_CAPTURED)).scalar_one()
                funnel_row = (await connection.execute(_FUNNEL, binds)).mappings().one()
                reason_rows = (await connection.execute(_REASONS, binds)).mappings().all()
        return ReadinessFunnel(
            captured=int(captured),
            qualification_attempts=int(funnel_row["attempts"]),
            qualified=int(funnel_row["qualified"]),
            excluded=int(funnel_row["excluded"]),
            excluded_by_reason={str(r["reason"]): int(r["n"]) for r in reason_rows},
            qualified_clusters=int(funnel_row["clusters"]),
            qualified_weeks=int(funnel_row["weeks"]),
            matured=int(funnel_row["matured"]),
            span_days=float(funnel_row["span_days"]),
        )

    async def close(self) -> None:
        await self._engine.dispose()


__all__ = ["DEFAULT_QUALIFICATION_VERSION", "SourceLeadReadinessRepository"]
