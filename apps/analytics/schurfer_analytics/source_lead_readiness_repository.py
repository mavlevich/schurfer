"""Read-only inputs for the source-lead readiness view.

The candidate set is NOT reimplemented here: it is fetched through the frozen
`SourceLeadForwardCohortRepository`, so the readiness funnel counts EXACTLY the same
qualified episodes (cohort-start-filtered, `QUALIFICATION_VERSION`, a `sampled` target
observation) the formal reader uses. Only the cohort-scoped diagnostic funnel (captures
and exclusion reasons since the cohort start) is queried directly, read-only.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 -- runtime default arg value

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from .outcome_repository import async_database_url
from .source_lead_forward_cohort import (
    QUALIFICATION_VERSION,
    SOURCE_LEAD_FORWARD_COHORT_START,
)
from .source_lead_forward_cohort_repository import SourceLeadForwardCohortRepository
from .source_lead_readiness import QualifiedEpisode, ReadinessInputs

# All qualified cohort episodes so far. Large enough to never truncate the young
# cohort; the formal reader applies its own checkpoint prefix separately.
_FETCH_LIMIT = 1_000_000

_CAPTURED_IN_COHORT = text(
    "SELECT count(*) AS n FROM app.source_lead_captures WHERE source_first_observed_at >= :since"
)

_EXCLUDED_REASONS = text(
    """
    SELECT q.reason AS reason, count(*) AS n
    FROM app.source_lead_qualifications q
    JOIN app.source_lead_captures c ON c.id = q.capture_id
    WHERE q.qualification_version = :qv
      AND q.status = 'excluded'
      AND c.source_first_observed_at >= :since
    GROUP BY q.reason
    ORDER BY n DESC
    """
)


async def load_readiness_inputs(
    db_url: str,
    *,
    qualification_version: str = QUALIFICATION_VERSION,
    cohort_start: datetime = SOURCE_LEAD_FORWARD_COHORT_START,
) -> ReadinessInputs:
    """Fetch the formal candidate episodes plus the cohort-scoped diagnostic funnel.
    Read-only. Entry time is each episode's target-observation `observed_at`. The version
    and cohort start default to the frozen contract values; the overrides exist only for
    isolated integration tests and are never wired to the CLI."""
    forward = SourceLeadForwardCohortRepository.from_url(db_url)
    try:
        database_now = await forward.database_now()
        raw_episodes = await forward.fetch_qualified_episodes(
            qualification_version=qualification_version,
            since=cohort_start,
            limit=_FETCH_LIMIT,
        )
    finally:
        await forward.close()

    episodes = tuple(
        QualifiedEpisode(entry_at=r.observed_at, canonical_asset_id=r.canonical_asset_id)
        for r in raw_episodes
    )

    engine = create_async_engine(
        async_database_url(db_url), pool_pre_ping=True, pool_size=1, max_overflow=0
    )
    try:
        async with engine.connect() as raw_connection:
            connection = await raw_connection.execution_options(
                isolation_level="REPEATABLE READ", postgresql_readonly=True
            )
            async with connection.begin():
                captured = (
                    await connection.execute(_CAPTURED_IN_COHORT, {"since": cohort_start})
                ).scalar_one()
                reason_rows = (
                    (
                        await connection.execute(
                            _EXCLUDED_REASONS,
                            {"qv": qualification_version, "since": cohort_start},
                        )
                    )
                    .mappings()
                    .all()
                )
    finally:
        await engine.dispose()

    return ReadinessInputs(
        cohort_start=cohort_start,
        database_now=database_now,
        episodes=episodes,
        captured_in_cohort=int(captured),
        excluded_by_reason={str(r["reason"]): int(r["n"]) for r in reason_rows},
    )


__all__ = ["load_readiness_inputs"]
