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


_PRE_QUALIFICATION = text(
    """
    SELECT c.status AS status, coalesce(c.eligibility_reason, 'none') AS reason, count(*) AS n
    FROM app.source_lead_captures c
    WHERE c.source_first_observed_at >= :since
      AND NOT EXISTS (
        SELECT 1 FROM app.source_lead_qualifications q
        WHERE q.capture_id = c.id AND q.qualification_version = :qv
      )
    GROUP BY c.status, coalesce(c.eligibility_reason, 'none')
    ORDER BY n DESC
    """
)

_QUALIFICATION_ROWS = text(
    """
    SELECT count(*) AS n
    FROM app.source_lead_qualifications q
    JOIN app.source_lead_captures c ON c.id = q.capture_id
    WHERE q.qualification_version = :qv AND c.source_first_observed_at >= :since
    """
)


_QUALIFIED_WITHOUT_EPISODE = text(
    """
    SELECT coalesce(t.status, 'missing') AS status, count(*) AS n
    FROM app.source_lead_qualifications q
    JOIN app.source_lead_captures c ON c.id = q.capture_id
    LEFT JOIN app.source_lead_target_observations t
      ON t.capture_id = q.capture_id AND t.target_exchange = q.selected_target_exchange
    WHERE q.status = 'qualified' AND q.qualification_version = :qv
      AND c.source_first_observed_at >= :since
      AND t.status IS DISTINCT FROM 'sampled'
    GROUP BY coalesce(t.status, 'missing')
    ORDER BY n DESC
    """
)


# v2: every target diagnostic the qualification recorded, per venue. A target
# with no reason and an impact passed every check ("executable"); one without
# either never got a sampled quote ("not_sampled:<observation status>").
_TARGET_REASONS = text(
    """
    SELECT coalesce(t ->> 'exchange', 'unknown') AS venue,
           coalesce(
               t ->> 'reason',
               CASE
                   WHEN t ? 'round_trip_impact_bps' THEN 'executable'
                   WHEN coalesce(t ->> 'observation_status', '') <> 'sampled'
                       THEN 'not_sampled:' || coalesce(t ->> 'observation_status', 'unknown')
                   -- Sampled but the route was refused on identity, which the
                   -- qualification records only through these flags.
                   WHEN (t ->> 'identity_approved')::boolean IS NOT TRUE
                       THEN 'identity_unapproved'
                   WHEN (t ->> 'registry_confirmed')::boolean IS NOT TRUE
                       THEN 'identity_unconfirmed'
                   WHEN (t ->> 'canonical_match')::boolean IS NOT TRUE
                       THEN 'canonical_mismatch'
                   ELSE 'unclassified'
               END
           ) AS reason,
           count(*) AS n
    FROM app.source_lead_qualifications q
    JOIN app.source_lead_captures c ON c.id = q.capture_id
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(q.details -> 'targets') = 'array'
             THEN q.details -> 'targets' ELSE '[]'::jsonb END
    ) AS t
    WHERE q.qualification_version = :qv AND c.source_first_observed_at >= :since
    GROUP BY 1, 2
    """
)

# v2 exit-book coverage: statuses and delays only, never prices.
_EXIT_COVERAGE = text(
    """
    SELECT e.outcome AS outcome, coalesce(e.timeliness, 'none') AS timeliness,
           count(*) AS n, array_agg(e.lateness_ms) FILTER (WHERE e.lateness_ms IS NOT NULL)
             AS lateness
    FROM app.source_lead_exit_observations e
    JOIN app.source_lead_captures c ON c.id = e.capture_id
    WHERE e.qualification_version = :qv AND c.source_first_observed_at >= :since
    GROUP BY 1, 2
    """
)


# The exit window closes by entry + 30 min, ceiled to the minute, + 1 min bar
# + 2 min lateness: at most 34 min. 35 min is a safe "exit is due" cutoff.
_EXIT_DUE = text(
    """
    SELECT count(*) AS due,
           count(*) FILTER (WHERE NOT EXISTS (
               SELECT 1 FROM app.source_lead_exit_observations e
               WHERE e.capture_id = q.capture_id
                 AND e.qualification_version = q.qualification_version
           )) AS missing
    FROM app.source_lead_qualifications q
    JOIN app.source_lead_captures c ON c.id = q.capture_id
    JOIN app.source_lead_target_observations t
      ON t.capture_id = q.capture_id
     AND t.target_exchange = q.selected_target_exchange
     AND t.status = 'sampled'
    WHERE q.qualification_version = :qv AND q.status = 'qualified'
      AND c.source_first_observed_at >= :since
      AND t.observed_at < now() - interval '35 minutes'
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
                pre_rows = (
                    (
                        await connection.execute(
                            _PRE_QUALIFICATION,
                            {"qv": qualification_version, "since": cohort_start},
                        )
                    )
                    .mappings()
                    .all()
                )
                qualification_rows = (
                    await connection.execute(
                        _QUALIFICATION_ROWS,
                        {"qv": qualification_version, "since": cohort_start},
                    )
                ).scalar_one()
                without_episode_rows = (
                    (
                        await connection.execute(
                            _QUALIFIED_WITHOUT_EPISODE,
                            {"qv": qualification_version, "since": cohort_start},
                        )
                    )
                    .mappings()
                    .all()
                )
                params = {"qv": qualification_version, "since": cohort_start}
                target_rows = (await connection.execute(_TARGET_REASONS, params)).mappings().all()
                exit_rows = (await connection.execute(_EXIT_COVERAGE, params)).mappings().all()
                exit_due_row = (await connection.execute(_EXIT_DUE, params)).mappings().one()
    finally:
        await engine.dispose()

    return ReadinessInputs(
        cohort_start=cohort_start,
        database_now=database_now,
        episodes=episodes,
        captured_in_cohort=int(captured),
        excluded_by_reason={str(r["reason"]): int(r["n"]) for r in reason_rows},
        pre_qualification_by_reason={f"{r['status']}:{r['reason']}": int(r["n"]) for r in pre_rows},
        qualification_rows=int(qualification_rows),
        qualified_without_episode_by_status={
            str(r["status"]): int(r["n"]) for r in without_episode_rows
        },
        target_reasons_by_venue={f"{r['venue']}:{r['reason']}": int(r["n"]) for r in target_rows},
        exit_coverage={f"{r['outcome']}:{r['timeliness']}": int(r["n"]) for r in exit_rows},
        exit_due=int(exit_due_row["due"]),
        exit_missing=int(exit_due_row["missing"]),
        exit_lateness_ms=tuple(
            int(value) for r in exit_rows for value in (r["lateness"] or ()) if value is not None
        ),
    )


__all__ = ["load_readiness_inputs"]
